"""Run the scorecard for one seed.

    uv run python -m evals.run --seed A            # the real system (needs WP2, WP3, WP6)
    uv run python -m evals.run --seed A --fake     # the fake system, to check the harness

Each run uses its own fresh database under evals/results/work/, seeds it, sends every inbox
message through the pipeline, rebuilds the grid, then scores extraction, traps, timing,
analyst and injection. Writes evals/results/<timestamp>-seed-<S>.json and a .md summary.

Seed B and the held-out vendor are refused unless --allow-unseen is given: they are run
once, at the end, when QA (WP9) says so.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals import adapters, analyst, extraction, injection, report, snapshot, timing, traps
from evals import fake as fake_system
from evals import truth as T

WORK = report.RESULTS / "work"


def _use_fresh_db(name: str) -> None:
    """Point level.db at a new database file for this run (never the app's own database)."""
    WORK.mkdir(parents=True, exist_ok=True)
    path = WORK / f"{name}.db"
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    os.environ["LEVEL_DB_PATH"] = str(path)
    os.environ["LEVEL_UPLOAD_DIR"] = str(WORK / "uploads")


def _inbox(rfx_id: int) -> list[int]:
    import sqlalchemy as sa

    from level import db

    with db.connect() as conn:
        return list(conn.execute(sa.select(db.message.c.id).where(
            db.message.c.rfx_id == rfx_id, db.message.c.direction == "in").order_by(
            db.message.c.received_at)).scalars())


def run(seed: str = "A", fake: bool = False, heldout: bool = False,
        data_dir: Path = T.DATA, skip_analyst: bool = False) -> dict:
    from level import db
    from scripts import seed as seeder

    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    truth = T.load(seed, data_dir)
    if heldout:
        truth = T.with_heldout(truth, T.load_heldout(data_dir))
    _use_fresh_db(f"seed-{seed}{'-fake' if fake else ''}")
    db.reset()
    ids = seeder.load(seed, data_dir, heldout=heldout)
    suites: dict = {}

    # 1. Read every reply and build the grid.
    try:
        if fake:
            fake_system.write_fake_results(truth, ids)
        else:
            for message_id in _inbox(ids["rfx_id"]):
                adapters.process_message(message_id)
            adapters.rebuild_cells(ids["rfx_id"])
        snap = snapshot.take(truth)
        suites["extraction"] = extraction.score(truth, snap)
        suites["traps"] = traps.score(truth, snap)
        suites["timing"] = timing.score(snap)
    except adapters.NotBuilt as exc:
        for name in ("extraction", "traps", "timing"):
            suites[name] = {"status": "not built", "detail": str(exc)}

    # 2. Ask the analyst.
    if skip_analyst:
        suites["analyst"] = {"status": "skipped"}
    elif fake:
        suites["analyst"] = analyst.score(truth, fake_system.fake_ask(truth),
                                          fake_system.fake_grader)
    else:
        try:
            adapters._resolve("level.agents.analyst", "ask")
            suites["analyst"] = analyst.score(
                truth, lambda q: adapters.ask_analyst(ids["rfx_id"], q))
        except adapters.NotBuilt as exc:
            suites["analyst"] = {"status": "not built", "detail": str(exc)}

    # 3. Injection last: it adds a vendor.
    try:
        process = fake_system.fake_process(truth) if fake else adapters.process_message
        suites["injection"] = injection.score(truth, ids["rfx_id"], process)
    except adapters.NotBuilt as exc:
        suites["injection"] = {"status": "not built", "detail": str(exc)}

    result = {"seed": seed, "heldout": heldout, "fake": fake, "started_at": started,
              "model": os.environ.get("LEVEL_MODEL", "claude-haiku-5-5"), "suites": suites}
    result["file"] = str(report.write(result))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Level eval scorecard.")
    parser.add_argument("--seed", default="A")
    parser.add_argument("--fake", action="store_true", help="score the fake system instead")
    parser.add_argument("--heldout", action="store_true", help="include the held-out vendor")
    parser.add_argument("--allow-unseen", action="store_true",
                        help="permit seed B / held-out (only when QA says so)")
    parser.add_argument("--skip-analyst", action="store_true")
    args = parser.parse_args()
    if (args.seed != "A" or args.heldout) and not args.allow_unseen:
        print("Seed B and the held-out vendor are run once, at the end, when QA says so. "
              "Pass --allow-unseen to run them.")
        return 2
    result = run(args.seed, args.fake, args.heldout, skip_analyst=args.skip_analyst)
    for metric, value, target in report.headline(result):
        print(f"  {metric:<38} {value:>12}   target {target}")
    print(f"wrote {result['file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
