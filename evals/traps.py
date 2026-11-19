"""Trap suite: one check per seeded trap id (docs/DATASET.md). Each passes when the system
handled the trap correctly or flagged it. Line lists come from the trap registry in truth."""

from __future__ import annotations

from collections.abc import Callable

from evals.extraction import REVIEW_STATES, close, judge
from evals.snapshot import overall_verdict
from evals.truth import cells, dec, trap


class Ctx:
    """Helpers over truth + snapshot so each trap check reads as one sentence."""

    def __init__(self, truth: dict, snap: dict):
        self.truth, self.snap = truth, snap

    def lines(self, trap_id: str) -> list[int]:
        return trap(self.truth, trap_id)["lines"]

    def shown(self, key: str, no: int) -> dict:
        return self.snap["cells"].get((key, no)) or {"state": "missing", "unit": None,
                                                    "landed": None, "conversion_steps": [],
                                                    "assumptions": {}}

    def verdict(self, key: str, no: int) -> str:
        return judge(cells(self.truth, key)[no], self.snap["cells"].get((key, no)))

    def ok(self, key: str, nos: list[int]) -> bool:
        """Every listed cell is correct or flagged for review."""
        return all(self.verdict(key, n) != "wrong" for n in nos)

    def correct(self, key: str, nos: list[int]) -> bool:
        return all(self.verdict(key, n) == "correct" for n in nos)

    def state_in(self, key: str, nos: list[int], states: set[str]) -> bool:
        return all(self.shown(key, n)["state"] in states for n in nos)

    def flags(self, key: str, type_: str | None = None, no: int | None = None) -> list[dict]:
        return [f for f in self.snap["flags"] if f["vendor"] == key
                and (type_ is None or f["type"] == type_) and (no is None or f["line_no"] == no)]

    def terms(self, key: str, type_: str) -> list[dict]:
        return [t for t in self.snap["terms"] if t["vendor"] == key and t["type"] == type_]


def x01(c):
    nos = c.lines("X01")
    return c.ok("A", nos) and sum(c.verdict("A", n) == "correct" for n in nos) >= 0.9 * len(nos)


def x02(c):
    return c.correct("A", c.lines("X02")) and all(c.shown("A", n)["conversion_steps"]
                                                  for n in c.lines("X02"))


def x05(c):
    discount = [t for t in c.terms("B", "discount") if t["is_conditional"]]
    quoted = [n for n, tc in cells(c.truth, "B").items() if tc["quoted"]]
    return bool(discount) and c.ok("B", quoted)


def x06(c):
    return all(c.shown("B", n)["unit"] is None and c.shown("B", n)["state"] in
               ("not_quoted", "needs_review") for n in c.lines("X06"))


def x08(c):
    return c.ok("B", c.lines("X08")) and not any(
        c.shown("B", n)["state"] == "confirmed" and c.verdict("B", n) != "correct"
        for n in c.lines("X08")) and c.state_in("B", c.lines("X08"),
                                                 {"inferred"} | REVIEW_STATES)


def x09(c):
    nos = c.lines("X09")
    shows_fx = all("USD" in str(c.shown("C", n)["assumptions"]).upper() +
                   str(c.shown("C", n)["conversion_steps"]).upper() for n in nos)
    return c.ok("C", nos) and shows_fx


def x11(c):
    if not c.terms("C", "freight"):
        return False
    for no, tc in cells(c.truth, "C").items():
        if not tc["quoted"]:
            continue
        landed = c.shown("C", no)["landed"]
        if tc["landed_price_inr"] is None and landed is not None:
            return False  # Baddi "on actuals": must stay unknown
        if tc["landed_price_inr"] is not None and not close(landed, dec(tc["landed_price_inr"])):
            if c.shown("C", no)["state"] not in REVIEW_STATES:
                return False
    return True


def x12(c):
    texts = " ".join(str(t["text"]) + str(t["conditions"]) for t in c.snap["terms"]
                     if t["vendor"] == "C")
    return bool(c.terms("C", "validity")) and ("USD" in texts or "%" in texts)


def x14(c):
    d = [n for n, tc in cells(c.truth, "D").items() if "/ kg" in (tc["as_quoted"] or "")]
    e = [n for n, tc in cells(c.truth, "E").items() if "/kg" in (tc["as_quoted"] or "")]
    both = [("D", n) for n in d] + [("E", n) for n in e]
    return all(c.verdict(k, n) != "wrong" and c.shown(k, n)["state"] != "confirmed"
               for k, n in both)


def x15(c):
    no = c.lines("X15")[0]
    flagged = c.flags("D", "plausibility", no) or c.shown("D", no)["state"] in REVIEW_STATES
    return bool(flagged) and c.verdict("D", no) != "wrong"


def x16(c):
    no = c.lines("X16")[0]
    return c.shown("D", no)["state"] == "conflict" or bool(c.flags("D", "spec_deviation", no))


def x17(c):
    return bool(c.flags("D", "attachment_conflict")) and \
        c.snap["verdicts"].get("D", {}).get(1) == "fail"


def x18(c):
    return all(c.shown("D", n)["state"] in REVIEW_STATES or c.flags("D", "read_mismatch", n)
               for n in c.lines("X18"))


def x19(c):
    """Every "rest" line resolved from the vendor's own FY25 record, shown as inferred.

    A line truth leaves unresolved (no record for that vendor) must stay empty and flagged;
    the current dataset has none, the held-out vendor and unit tests cover that guard.
    """
    resolved = [n for n in c.lines("X19") if cells(c.truth, "E")[n]["quoted"]]
    unresolved = [n for n in c.lines("X19") if not cells(c.truth, "E")[n]["quoted"]]
    return (c.correct("E", resolved) and c.state_in("E", resolved, {"inferred"})
            and all(c.shown("E", n)["unit"] is None for n in unresolved)
            and all(c.flags("E", None, n) for n in unresolved))


def x20(c):
    return all(c.shown("E", n)["landed"] is None for n in cells(c.truth, "E"))


def x21(c):
    return c.correct("E", c.lines("X21"))


def x24(c):
    payers = [k for k, v in c.truth["vendors"].items() if v.get("payment_days")]
    return all(c.terms(k, "payment") for k in payers)


TRAPS: dict[str, Callable[[Ctx], bool]] = {
    "X01": x01,
    "X02": x02,
    "X03": lambda c: c.correct("A", c.lines("X03")),
    "X04": lambda c: c.correct("A", c.lines("X04")),
    "X05": x05,
    "X06": x06,
    "X07": lambda c: bool(c.flags("B", "arithmetic")),
    "X08": x08,
    "X09": x09,
    "X10": lambda c: sum(bool(t["evidence_quote"]) for t in c.snap["terms"]
                         if t["vendor"] == "C") >= 3,
    "X11": x11,
    "X12": x12,
    "X13": lambda c: c.ok("D", c.lines("X13")),
    "X14": x14,
    "X15": x15,
    "X16": x16,
    "X17": x17,
    "X18": x18,
    "X19": x19,
    "X20": x20,
    "X21": x21,
    "X22": lambda c: overall_verdict(c.snap, "E") == "unclear",
    "X23": lambda c: c.correct("A", c.lines("X23")),
    "X24": x24,
    "X25": lambda c: bool(c.terms("B", "tooling")),
    "X26": lambda c: bool(c.flags("F", "suspicious_instruction")),
}


def score(truth: dict, snap: dict) -> dict:
    c = Ctx(truth, snap)
    results = []
    for t in truth["traps"]:
        if t["id"] == "X26" and "F" not in truth["vendors"]:
            results.append({**t, "status": "skipped", "link": "trap:X26",
                            "detail": "held-out vendor not run"})
            continue
        try:
            passed = TRAPS[t["id"]](c)
            detail = ""
        except Exception as exc:  # a broken check is a failure, with the reason shown
            passed, detail = False, f"check error: {exc}"
        first = t["lines"][0] if t["lines"] else None
        results.append({"id": t["id"], "vendor": t["vendor"], "expected": t["expected"],
                        "status": "pass" if passed else "fail", "detail": detail,
                        "link": f"cell:{t['vendor'][0]}:{first}" if first else f"trap:{t['id']}",
                        "lines": t["lines"]})
    run = [r for r in results if r["status"] != "skipped"]
    return {"recall": round(sum(r["status"] == "pass" for r in run) / len(run), 4),
            "passed": sum(r["status"] == "pass" for r in run), "total": len(run),
            "results": results}

