"""WP1: the dataset generator. Generates into a temp dir; never reads the committed fixtures."""

from __future__ import annotations

import json
from decimal import Decimal

import docx
import openpyxl
import pymupdf
import pytest
import sqlalchemy as sa

from level import db
from scripts import seed as seed_script
from scripts.dataset import outcomes, past_event, truth
from scripts.generate_dataset import generate

ALL_TRAPS = {f"X{n:02d}" for n in range(1, 27)}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    gt = generate("A", data, with_heldout=True)
    return gt, data


@pytest.mark.parametrize("seed", ["A", "1", "2", "3", "4"])
def test_designed_outcomes_hold(seed):
    gt, _ = truth.build(seed)
    failures = [r for r in outcomes.check_all(gt) if not r[1]]
    assert not failures, failures


def test_two_seeds_differ_in_prices_and_gaps():
    a, _ = truth.build("A")
    b, _ = truth.build("7")

    def gaps(gt):
        return {(k, c["line_no"]) for k, v in gt["vendors"].items() for c in v["cells"]
                if not c["quoted"]}

    def prices(gt):
        return [c["unit_price_inr"] for v in gt["vendors"].values() for c in v["cells"]]

    assert gaps(a) != gaps(b)
    assert prices(a) != prices(b)


def test_same_seed_is_reproducible():
    assert truth.build("A") == truth.build("A")


def test_registry_has_every_trap(generated):
    gt, data = generated
    ids = {t["id"] for t in gt["traps"]}
    assert ids == ALL_TRAPS
    held = json.loads((data / "ground_truth" / "heldout.json").read_text(encoding="utf-8"))
    assert held["traps"][0]["id"] == "X26"


def test_not_quoted_is_never_zero(generated):
    gt, _ = generated
    for v in gt["vendors"].values():
        for c in v["cells"]:
            assert c["quoted"] == (c["unit_price_inr"] is not None)
            if c["quoted"]:
                assert Decimal(c["unit_price_inr"]) > 0


def test_traps_exist_in_vendor_a_documents(generated):
    gt, data = generated
    folder = data / "fixtures" / "seed-A" / "A"
    wb = openpyxl.load_workbook(folder / "KP-Quotation-SundariFoods.xlsx")
    assert wb.sheetnames[:2] == ["Pune & Hosur", "Baddi"]  # X04
    ws = wb["Pune & Hosur"]
    assert ws["F8"].value == "Rate / 1000 Nos (Rs.)"  # X02, merged header
    assert "Above 1,00,000" in ws["G9"].value  # X03
    codes = [r[1] for r in ws.iter_rows(min_row=10, values_only=True) if r[1]]
    assert all(c.startswith("KP-") for c in codes)  # X01: own codes
    revision = (folder / "02-email.txt").read_text(encoding="utf-8")
    for r in gt["vendors"]["A"]["doc"]["revisions"]:  # X23
        assert r["code"] in revision and f"{r['new_rate']:,}" in revision


def test_traps_exist_in_vendor_b_pdf(generated):
    gt, data = generated
    pdf = next((data / "fixtures" / "seed-A" / "B").glob("Shreeji-*.pdf"))
    text = "\n".join(p.get_text() for p in pymupdf.open(pdf))
    doc = gt["vendors"]["B"]["doc"]
    assert text.count("Regret") == 3  # X06
    assert "incl. GST" in text  # X08
    assert "rebate" in text and "3.5 Cr" in text  # X05
    assert "Printing plates" in text  # X25
    assert Decimal(doc["stated_total"]) != Decimal(doc["sum_of_lines"])  # X07


def test_traps_exist_in_vendor_c_docx(generated):
    _, data = generated
    path = next((data / "fixtures" / "seed-A" / "C").glob("*.docx"))
    text = "\n".join(p.text for p in docx.Document(path).paragraphs)
    for phrase in ["USD", "ex-works", "valid for 90", "USD/INR", "Baddi on actuals",
                   "uniform USD"]:  # X09-X12, rule claim
        assert phrase in text


def test_traps_exist_in_vendor_d_rate_card(generated):
    gt, data = generated
    folder = data / "fixtures" / "seed-A" / "D"
    text = pymupdf.open(folder / "print" / "rate-card.pdf")[0].get_text()
    for phrase in ["/ kg", "/ box", "/ 100", "120 GSM 16 BF"]:  # X14-X16
        assert phrase in text
    assert (folder / "IMG_20250222_174512.jpg").stat().st_size > 50_000  # X13 photo
    cert = pymupdf.open(folder / "iso certificate.pdf")[0].get_text()
    assert "31 January 2025" in cert  # X17: expired before the award date
    assert gt["vendors"]["D"]["gating"]["by_question"]["1"] == "fail"


def test_traps_exist_in_vendor_e_email(generated):
    gt, data = generated
    body = (data / "fixtures" / "seed-A" / "E" / "01-email.txt").read_text(encoding="utf-8")
    assert "rest same as last year" in body and "freight extra" in body  # X19, X20
    assert "/kg for the 5-ply" in body  # X21
    assert gt["vendors"]["E"]["gating"]["overall"] == "unclear"  # X22


def test_history_holds_contracts_and_quotes(generated):
    gt, data = generated
    on_disk = json.loads((data / "history" / "seed-A" / "fy25.json").read_text(encoding="utf-8"))
    assert on_disk == gt["history"]
    contracts = [h for h in gt["history"] if h["source"] == "contract"]
    assert sorted(h["line_no"] for h in contracts) == [ln["line_no"] for ln in gt["lines"]]
    assert {h["vendor"] for h in gt["history"]} == set("ABCDE")  # every vendor bid last year
    paid = {h["line_no"]: Decimal(h["unit_price"]) for h in contracts}
    for h in gt["history"]:
        if h["source"] == "quote":  # a losing bid was above the price that won
            assert Decimal(h["unit_price"]) > paid[h["line_no"]]
    kinds = {ln["line_no"]: ln["kind"] for ln in gt["lines"]}
    e_contract = {kinds[h["line_no"]] for h in contracts if h["vendor"] == "E"}
    assert e_contract == {"7ply", "partition", "pad"}
    e_quotes = {kinds[h["line_no"]] for h in gt["history"]
                if h["vendor"] == "E" and h["source"] == "quote"}
    assert e_quotes == {"3ply", "5ply"}
    for ln in gt["lines"]:  # the savings baseline is the contract price, never a quote
        assert ln["last_price"] == next(h["unit_price"] for h in contracts
                                        if h["line_no"] == ln["line_no"])


def test_x19_rest_lines_resolve_from_own_contract(generated):
    gt, _ = generated
    own = {h["line_no"]: h["unit_price"] for h in gt["history"]
           if h["vendor"] == "E" and h["source"] == "contract"}
    ref = [c for c in gt["vendors"]["E"]["cells"] if c["as_quoted"] == "same as last year"]
    assert ref and {c["line_no"] for c in ref} == set(own)
    trap = next(t for t in gt["traps"] if t["id"] == "X19")
    assert sorted(trap["lines"]) == sorted(own)
    for c in ref:
        assert c["quoted"] and c["expected_state"] == "inferred"
        assert Decimal(c["unit_price_inr"]) == Decimal(own[c["line_no"]])


def test_seed_loads_into_database(generated, tmp_db, tmp_path, monkeypatch):
    gt, data = generated
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    ids = seed_script.load("A", data)
    with db.connect() as conn:
        count = lambda t: conn.execute(sa.select(sa.func.count()).select_from(t)).scalar()  # noqa: E731
        this_year = lambda t: conn.execute(sa.select(sa.func.count()).select_from(t).where(  # noqa: E731
            t.c.rfx_id == ids["rfx_id"])).scalar()
        assert count(db.rfx) == 2  # this year's event and last year's
        assert this_year(db.rfx_line) == 30 and count(db.rfx_line) == 60
        assert count(db.vendor) == 5
        assert this_year(db.question) == 12
        assert count(db.message) == 6  # A sends two emails
        assert count(db.price_history) == len(gt["history"])
        sources = dict(conn.execute(sa.select(db.price_history.c.source, sa.func.count())
                                    .group_by(db.price_history.c.source)).all())
        assert sources["contract"] == 30 and sources["quote"] > 0  # one contract per line
        assert count(db.cell) == 0  # documents are loaded unprocessed
        gating = conn.execute(sa.select(db.question.c.q_no).where(
            db.question.c.is_gating, db.question.c.rfx_id == ids["rfx_id"]))
        assert sorted(r[0] for r in gating) == [1, 2, 8]
    assert len(ids["lines"]) == 30


# --- WP13: last year's event ------------------------------------------------------------


def test_past_event_agrees_with_fy25_contracts(generated):
    gt, _ = generated
    past = past_event.build(gt, gt["history"])
    assert past["status"] == "awarded" and past["period"] == "FY25"
    assert past["award_date"] < gt["rfx"]["issued"]  # awarded before this year's event
    awards = {a["line_no"]: a for a in past["awards"]}
    assert sorted(awards) == [ln["line_no"] for ln in sorted(gt["lines"],
                                                             key=lambda x: x["line_no"])]
    for ln in gt["lines"]:  # same supplier and price as this year's "last year" reference
        assert awards[ln["line_no"]]["vendor"] == ln["last_vendor"]
        assert Decimal(awards[ln["line_no"]]["unit_price"]) == Decimal(ln["last_price"])
    assert past["invited"] == sorted(gt["vendors"])  # every vendor bid last year
    assert past["questionnaire"] == gt["questionnaire"]
    for key in ("price_basis", "currency", "payment_days", "delivery"):
        assert past["terms"][key]


def test_past_event_is_loaded_as_an_awarded_record(generated, tmp_db, tmp_path, monkeypatch):
    gt, data = generated
    monkeypatch.setenv("LEVEL_UPLOAD_DIR", str(tmp_path / "uploads"))
    ids = seed_script.load("A", data)
    past_id = ids["past_rfx_id"]
    assert past_id < ids["rfx_id"]  # this year's event stays the newest
    with db.connect() as conn:
        rfx = conn.execute(sa.select(db.rfx).where(db.rfx.c.id == past_id)).mappings().one()
        n = lambda t: conn.execute(sa.select(sa.func.count()).select_from(t).where(  # noqa: E731
            t.c.rfx_id == past_id)).scalar()
        assert rfx["status"] == "awarded" and rfx["terms"]["ref"].endswith("FY25/009")
        assert (n(db.rfx_line), n(db.question), n(db.invitation), n(db.award_line)) == (
            30, 12, 5, 30)
        assert n(db.message) == 0  # a record: no replies to read
        paid = conn.execute(
            sa.select(db.rfx_line.c.line_no, db.award_line.c.unit_price, db.vendor.c.name)
            .join(db.award_line, db.award_line.c.rfx_line_id == db.rfx_line.c.id)
            .join(db.vendor, db.vendor.c.id == db.award_line.c.vendor_id)).all()
    truth_paid = {h["line_no"]: (h["unit_price"], gt["vendors"][h["vendor"]]["name"])
                  for h in gt["history"] if h["source"] == "contract"}
    assert {no: (price, name) for no, price, name in paid} == truth_paid
