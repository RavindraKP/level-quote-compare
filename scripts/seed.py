"""Load one seed into SQLite: RFx, lines, questionnaire, vendors, history, assumptions, inbox,
and last year's awarded event (WP13), so the buyer can start a repeat from it.

Documents are stored unprocessed (original file under data/uploads/<sha256>); the pipeline
reads them later.

    uv run python scripts/seed.py                 # seed A into the configured database
    uv run python scripts/seed.py --reset         # drop and recreate the schema first
    uv run python scripts/seed.py --heldout       # also load the held-out vendor (QA only)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import shutil
import sys
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from level import db  # noqa: E402
from scripts.dataset import past_event  # noqa: E402


def parse_email(path: Path) -> dict:
    """Headers until the first blank line, then the body."""
    head, _, body = path.read_text(encoding="utf-8").partition("\n\n")
    headers = dict(line.split(": ", 1) for line in head.splitlines())
    received = parsedate_to_datetime(headers["Date"]).astimezone(UTC)
    return {"sender": headers["From"], "recipient": headers["To"], "subject": headers["Subject"],
            "body": body.strip(), "received_at": received.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}


def store_file(path: Path) -> tuple[str, str, int]:
    """Copy the original into the upload dir, named by its SHA-256. Returns (path, sha, size)."""
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    target = db.upload_dir() / sha
    if not target.exists():
        shutil.copyfile(path, target)
    return str(target), sha, len(data)


def _vendor(conn, v: dict, incumbent: bool) -> int:
    return conn.execute(db.vendor.insert().values(
        name=v["name"], email=v["email"], is_incumbent=incumbent)).inserted_primary_key[0]


def _past_event(conn, past: dict, vendor_ids: dict[str, int]) -> int:
    """Insert last year's awarded event (lines, questionnaire, invitations, award per line)."""
    rfx_id = conn.execute(db.rfx.insert().values(
        title=past["title"], category=past["category"], base_currency="INR",
        status=past["status"], issued_at=past["issued"] + "T00:00:00.000000Z",
        created_at=past["issued"] + "T00:00:00.000000Z", terms=past["terms"],
    )).inserted_primary_key[0]
    line_ids = {}
    for ln in past["lines"]:
        line_ids[ln["line_no"]] = conn.execute(db.rfx_line.insert().values(
            rfx_id=rfx_id, line_no=ln["line_no"], description=ln["description"],
            spec={**ln["spec"], "sku": ln["sku"]}, canonical_uom=ln["canonical_uom"],
            annual_qty=str(ln["annual_qty"]), conversion_factors=ln["conversion_factors"],
            plant=ln["plant"])).inserted_primary_key[0]
    for qn in past["questionnaire"]:
        conn.execute(db.question.insert().values(rfx_id=rfx_id, **qn))
    for key in past["invited"]:
        conn.execute(db.invitation.insert().values(rfx_id=rfx_id, vendor_id=vendor_ids[key],
                                                   status="replied"))
    for a in past["awards"]:
        conn.execute(db.award_line.insert().values(
            rfx_id=rfx_id, rfx_line_id=line_ids[a["line_no"]], vendor_id=vendor_ids[a["vendor"]],
            unit_price=a["unit_price"]))
    return rfx_id


def _inbox(conn, fixtures: Path, rfx_id: int, vendor_ids: dict[str, int]) -> int:
    manifest = json.loads((fixtures / "manifest.json").read_text(encoding="utf-8"))
    count = 0
    for m in manifest["messages"]:
        mail = parse_email(fixtures / m["email"])
        vendor_id = vendor_ids[m["vendor"]]
        msg_id = conn.execute(db.message.insert().values(
            rfx_id=rfx_id, vendor_id=vendor_id, direction="in", channel="email",
            status="received", **mail)).inserted_primary_key[0]
        for a in m["attachments"]:
            src = fixtures / a["file"]
            path, sha, size_bytes = store_file(src)
            conn.execute(db.attachment.insert().values(
                message_id=msg_id, vendor_id=vendor_id, filename=src.name,
                mime=mimetypes.guess_type(src.name)[0] or "application/octet-stream",
                kind=a["kind"], path=path, sha256=sha, size_bytes=size_bytes))
        count += 1
    return count


def load(seed: str = "A", data_dir: Path = ROOT / "data", heldout: bool = False) -> dict:
    """Insert everything for `seed`. Returns ids: rfx_id, vendors {key: id}, lines {no: id}."""
    truth_path = data_dir / "ground_truth" / f"seed-{seed}.json"
    if not truth_path.exists():
        raise SystemExit(f"{truth_path} missing: run scripts/generate_dataset.py --seed {seed}")
    gt = json.loads(truth_path.read_text(encoding="utf-8"))
    with db.begin() as conn:
        if conn.execute(db.rfx.select()).first():
            raise SystemExit("Database already has an RFx; use --reset to start again.")
        vendors = {k: _vendor(conn, v, incumbent=(k == "A")) for k, v in gt["vendors"].items()}
        # Last year's event goes in first, so this year's event stays the newest one.
        past_id = _past_event(conn, past_event.build(gt, gt["history"]), vendors)
        rfx_id = conn.execute(db.rfx.insert().values(
            title=gt["rfx"]["title"], category=gt["rfx"]["category"], base_currency="INR",
            status="collecting", issued_at=gt["rfx"]["issued"] + "T00:00:00.000000Z",
            terms={**gt["rfx"], "buyer": gt["buyer"]["name"],
                   "plants": gt["buyer"]["plants"]})).inserted_primary_key[0]

        lines = {}
        for ln in gt["lines"]:
            lines[ln["line_no"]] = conn.execute(db.rfx_line.insert().values(
                rfx_id=rfx_id, line_no=ln["line_no"], description=ln["description"],
                spec={**ln["spec"], "sku": ln["sku"]}, canonical_uom=ln["canonical_uom"],
                annual_qty=str(ln["annual_qty"]), conversion_factors=ln["conversion_factors"],
                plant=ln["plant"], last_price=ln["last_price"],
                incumbent_vendor_id=vendors[ln["last_vendor"]])).inserted_primary_key[0]
        for qn in gt["questionnaire"]:
            conn.execute(db.question.insert().values(rfx_id=rfx_id, **qn))
        for h in gt["history"]:
            conn.execute(db.price_history.insert().values(
                vendor_id=vendors[h["vendor"]], rfx_line_id=lines[h["line_no"]],
                item_text=h["description"], period=h["period"], unit_price=h["unit_price"],
                currency=h["currency"], uom=h["uom"], source=h["source"]))  # contract / quote
        for key, a in gt["assumptions"].items():
            conn.execute(db.assumption.insert().values(key=key, **a))
        for vid in vendors.values():
            conn.execute(db.invitation.insert().values(rfx_id=rfx_id, vendor_id=vid,
                                                       status="replied"))
        n = _inbox(conn, data_dir / "fixtures" / f"seed-{seed}", rfx_id, vendors)

        if heldout:
            heldout_path = data_dir / "ground_truth" / "heldout.json"
            f = json.loads(heldout_path.read_text(encoding="utf-8"))["vendor"]
            vendors["F"] = _vendor(conn, f, incumbent=False)
            conn.execute(db.invitation.insert().values(rfx_id=rfx_id, vendor_id=vendors["F"],
                                                       status="replied"))
            n += _inbox(conn, data_dir / "fixtures" / "heldout", rfx_id, vendors)
    print(f"seed {seed}: rfx {rfx_id}, {len(lines)} lines, {len(vendors)} vendors, {n} messages"
          f" (plus last year's event, rfx {past_id})")
    return {"rfx_id": rfx_id, "vendors": vendors, "lines": lines, "past_rfx_id": past_id}


def main() -> int:
    parser = argparse.ArgumentParser(description="Load a dataset seed into SQLite.")
    parser.add_argument("--seed", default="A")
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema")
    parser.add_argument("--heldout", action="store_true", help="also load the held-out vendor")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    db.reset() if args.reset else db.init()
    load(args.seed, args.data_dir, args.heldout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
