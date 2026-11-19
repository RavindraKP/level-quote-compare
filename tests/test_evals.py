"""WP7: the eval harness scores a fake system correctly, and expected answers follow truth."""

from __future__ import annotations

import copy
import sys
from decimal import Decimal

import pytest

from evals import analyst, extraction, report
from evals import run as runner
from scripts.generate_dataset import generate


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    generate("A", data)
    return data


@pytest.fixture
def fake_result(data_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(report, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(runner, "WORK", tmp_path / "work")
    monkeypatch.setenv("LEVEL_DB_PATH", str(tmp_path / "unused.db"))
    return runner.run("A", fake=True, data_dir=data_dir)


def test_fake_run_catches_exactly_the_planted_mistakes(fake_result):
    s = fake_result["suites"]
    assert s["extraction"]["silent_errors"] == 1
    silent = s["extraction"]["silent"][0]
    assert (silent["vendor"], silent["state"]) == ("A", "confirmed")
    # the wrong-but-flagged partition price is not counted as silent
    assert not any(f["vendor"] == "D" for f in s["extraction"]["failures"])
    failed = {t["id"] for t in s["traps"]["results"] if t["status"] == "fail"}
    assert failed == {"X01", "X02", "X03", "X07"}  # X01-X03 all touch the wrong A line
    assert [q["id"] for q in s["analyst"]["results"] if not q["pass"]] == ["Q04"]
    assert s["analyst"]["declined"] == 1.0
    assert s["injection"]["pass"] is True


def test_failures_link_to_cell_trap_or_question(fake_result):
    links = [f["link"] for f in report.failure_rows(fake_result)]
    assert any(link.startswith("cell:A:") for link in links)
    assert "trap:X07" in links
    assert "question:Q04" in links
    assert (report.RESULTS / (fake_result["file"].split("/")[-1])).exists()


def test_expected_answers_change_when_truth_changes(data_dir):
    truth = runner.T.load("A", data_dir)
    before = {q["id"]: q["expected"] for q in analyst.questions(truth)}
    changed = copy.deepcopy(truth)
    for c in changed["vendors"]["A"]["cells"]:
        c["unit_price_inr"] = str(Decimal(c["unit_price_inr"]) * Decimal("0.9"))
    after = {q["id"]: q["expected"] for q in analyst.questions(changed)}
    assert before["Q03"] != after["Q03"]  # single-vendor total for A
    assert before["H01"] != after["H01"]


def test_numbers_are_read_with_indian_units():
    assert Decimal("41200000") in analyst.numbers_in("about ₹4.12 crore a year")
    assert Decimal("1250000") in analyst.numbers_in("saves 12.5 lakh")
    assert Decimal("12345678") in analyst.numbers_in("Rs 1,23,45,678")
    assert analyst.has_number("total 4,61,35,102", Decimal("46135102"))
    assert not analyst.has_number("total 4.7 crore", Decimal("46135102"))


def test_zero_is_not_the_same_as_not_quoted():
    truth_cell = {"unit_price_inr": None}
    assert extraction.judge(truth_cell, {"state": "not_quoted", "unit": None}) == "correct"
    assert extraction.judge(truth_cell, {"state": "confirmed", "unit": Decimal("0")}) == "wrong"


def test_unseen_seed_is_refused_without_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["evals.run", "--seed", "B"])
    assert runner.main() == 2


def test_landed_price_is_scored_and_a_wrong_confirmed_landed_is_silent():
    truth = {"vendors": {"Q": {"cells": [
        {"line_no": 1, "unit_price_inr": "10.00", "landed_price_inr": None,
         "expected_state": "confirmed"},
        {"line_no": 2, "unit_price_inr": "10.00", "landed_price_inr": "11.00",
         "expected_state": "confirmed"},
        {"line_no": 3, "unit_price_inr": "10.00", "landed_price_inr": None,
         "expected_state": "inferred"},
    ]}}}
    cell = lambda state, unit, landed: {"state": state, "unit": Decimal(unit),  # noqa: E731
                                        "landed": None if landed is None else Decimal(landed)}
    snap = {"cells": {("Q", 1): cell("confirmed", "10", "10"),   # unknown shown as a value
                      ("Q", 2): cell("confirmed", "10", "11"),
                      ("Q", 3): cell("inferred", "10", None)},
            "flags": [], "terms": []}
    s = extraction.score(truth, snap)
    assert s["accuracy"] == 1.0  # unit prices all right
    assert s["landed"]["wrong"] == 1 and s["landed_accuracy"] == round(2 / 3, 4)
    assert s["silent_errors"] == 1 and s["landed_silent_errors"] == 1
    assert s["landed_failures"][0]["link"] == "cell:Q:1:landed"
