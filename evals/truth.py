"""Load ground truth and answer simple questions about it. Evals only; never imported by level/."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(seed: str, data_dir: Path = DATA) -> dict:
    return json.loads((data_dir / "ground_truth" / f"seed-{seed}.json").read_text(encoding="utf-8"))


def load_heldout(data_dir: Path = DATA) -> dict:
    return json.loads((data_dir / "ground_truth" / "heldout.json").read_text(encoding="utf-8"))


def with_heldout(truth: dict, heldout: dict) -> dict:
    """A copy of truth with vendor F added (for the held-out run)."""
    out = {**truth, "vendors": {**truth["vendors"], "F": heldout["vendor"]}}
    out["traps"] = truth["traps"]
    return out


def dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def cells(truth: dict, key: str) -> dict[int, dict]:
    return {c["line_no"]: c for c in truth["vendors"][key]["cells"]}


def line(truth: dict, line_no: int) -> dict:
    return next(ln for ln in truth["lines"] if ln["line_no"] == line_no)


def name(truth: dict, key: str) -> str:
    return truth["vendors"][key]["name"]


def trap(truth: dict, trap_id: str) -> dict:
    return next(t for t in truth["traps"] if t["id"] == trap_id)
