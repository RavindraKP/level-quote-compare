"""Extraction suite: every vendor x line cell against ground truth.

A cell is
- correct  : priced within 0.5% of truth, or unpriced where truth is not quoted;
- flagged  : shown as needs_review or conflict (a human will look at it);
- wrong    : neither. Wrong AND shown as confirmed is a silent error (target: zero).

Landed prices (price + freight, ground truth `landed_price_inr`) are judged the same way on
their own: unknown must stay unknown, and a wrong landed value on a Confirmed cell is a silent
error too. A cell counts once in silent_errors even if both its prices are wrong.
"""

from __future__ import annotations

from decimal import Decimal

from evals.truth import dec

TOLERANCE = Decimal("0.005")
REVIEW_STATES = {"needs_review", "conflict"}

# Document-level flags that point at a real issue in the dataset (no line attached).
DOC_ISSUES = {"B": {"arithmetic"}, "C": {"missing_info"}, "D": {"attachment_conflict",
              "read_mismatch"}, "E": {"missing_info", "reference"},
              "F": {"suspicious_instruction"}}


def close(shown: Decimal | None, truth: Decimal | None) -> bool:
    if shown is None or truth is None:
        return shown is None and truth is None
    return abs(shown - truth) <= abs(truth) * TOLERANCE


def judge(truth_cell: dict, shown: dict | None) -> str:
    """'correct', 'flagged' or 'wrong' for one cell."""
    shown = shown or {"state": "missing", "unit": None}
    priced = shown["unit"] is not None and shown["state"] not in ("not_quoted", "awaiting")
    value = shown["unit"] if priced else None
    if close(value, dec(truth_cell["unit_price_inr"])):
        return "correct"
    return "flagged" if shown["state"] in REVIEW_STATES else "wrong"


def judge_landed(truth_cell: dict, shown: dict | None) -> str:
    """'correct', 'flagged' or 'wrong' for one cell's landed price."""
    shown = shown or {"state": "missing", "unit": None, "landed": None}
    priced = shown["unit"] is not None and shown["state"] not in ("not_quoted", "awaiting")
    value = shown.get("landed") if priced else None
    if close(value, dec(truth_cell.get("landed_price_inr"))):
        return "correct"
    return "flagged" if shown["state"] in REVIEW_STATES else "wrong"


def link(key: str, line_no: int) -> str:
    return f"cell:{key}:{line_no}"


def score(truth: dict, snap: dict) -> dict:
    counts = {"correct": 0, "flagged": 0, "wrong": 0}
    landed_counts = {"correct": 0, "flagged": 0, "wrong": 0}
    failures, landed_failures, silent, judged = [], [], [], {}
    for key, vendor in truth["vendors"].items():
        for tc in vendor["cells"]:
            shown = snap["cells"].get((key, tc["line_no"]))
            state = shown["state"] if shown else "missing"
            verdict = judge(tc, shown)
            judged[(key, tc["line_no"])] = verdict
            counts[verdict] += 1
            if verdict == "wrong":
                item = {"link": link(key, tc["line_no"]), "vendor": key,
                        "line_no": tc["line_no"], "state": state, "price": "unit",
                        "shown": _text(shown and shown["unit"]), "truth": tc["unit_price_inr"]}
                failures.append(item)
                if state == "confirmed":
                    silent.append(item)
            landed = judge_landed(tc, shown)
            landed_counts[landed] += 1
            if landed == "wrong":
                item = {"link": link(key, tc["line_no"]) + ":landed", "vendor": key,
                        "line_no": tc["line_no"], "state": state, "price": "landed",
                        "shown": _text(shown and shown.get("landed")),
                        "truth": tc.get("landed_price_inr")}
                landed_failures.append(item)
                if state == "confirmed" and verdict != "wrong":  # count each cell once
                    silent.append(item)
    total = sum(counts.values())
    landed_total = sum(landed_counts.values())
    precision = flag_precision(truth, snap, judged)
    recall = term_recall(truth, snap)
    return {
        "cells": total, **counts,
        "accuracy": round((counts["correct"] + counts["flagged"]) / total, 4) if total else 0,
        "silent_errors": len(silent),
        "landed": {**landed_counts, "cells": landed_total},
        "landed_accuracy": round((landed_counts["correct"] + landed_counts["flagged"])
                                 / landed_total, 4) if landed_total else 0,
        "landed_silent_errors": sum(1 for i in silent if i["price"] == "landed"),
        "landed_failures": landed_failures,
        "flag_precision": precision,
        "term_recall": recall,
        "silent": silent,
        "failures": failures,
    }


def _text(value) -> str | None:
    return None if value is None else str(value)


def flag_precision(truth: dict, snap: dict, judged: dict) -> float | None:
    """Share of flags that point at a real issue: an expected non-confirmed cell, a cell the
    system got wrong, or a known document-level issue for that vendor."""
    flags = snap["flags"]
    if not flags:
        return None
    states = {(k, c["line_no"]): c["expected_state"] for k, v in truth["vendors"].items()
              for c in v["cells"]}
    real = 0
    for f in flags:
        key, no = f["vendor"], f["line_no"]
        if no is not None:
            real += states.get((key, no)) not in (None, "confirmed") or \
                judged.get((key, no)) != "correct"
        elif f["type"] == "missing_info" and (f.get("reason") or "").startswith(
                "landed price unknown"):
            real += _landed_unknown_in_truth(truth, key)
        else:
            real += f["type"] in DOC_ISSUES.get(key, set())
    return round(real / len(flags), 4)


def _landed_unknown_in_truth(truth: dict, key: str) -> bool:
    """Truth has a priced cell for this vendor whose landed price is unknown."""
    cells = truth["vendors"].get(key, {}).get("cells", [])
    return any(c["unit_price_inr"] is not None and c.get("landed_price_inr") is None
               for c in cells)


def expected_terms(vendor: dict) -> set[str]:
    want = {"freight"}
    if vendor.get("payment_days"):
        want.add("payment")
    if vendor.get("validity_days"):
        want.add("validity")
    if "rebate_pct" in vendor.get("doc", {}):
        want |= {"discount", "tooling"}
    return want


def term_recall(truth: dict, snap: dict) -> float:
    want = {(k, t) for k, v in truth["vendors"].items() for t in expected_terms(v)}
    got = {(t["vendor"], t["type"]) for t in snap["terms"]}
    return round(len(want & got) / len(want), 4) if want else 1.0
