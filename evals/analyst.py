"""Analyst suite: questions whose expected answers are computed from ground truth by code.

Sets: base (~20), paraphrase (3 wordings each of 5 base questions), held-out (never used while
writing analyst prompts), unanswerable (must be declined). Numbers are graded in code; a model
grader is used only to judge "declined appropriately".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal

from pydantic import BaseModel, Field

from evals import expected as X
from evals.truth import line, name

TOLERANCE = Decimal("0.005")
SCALE = {"crore": Decimal(10**7), "cr": Decimal(10**7), "lakh": Decimal(10**5),
         "lakhs": Decimal(10**5), "lac": Decimal(10**5), "l": Decimal(10**5),
         "k": Decimal(1000), "million": Decimal(10**6), "mn": Decimal(10**6)}
NUMBER = re.compile(r"(-?\d[\d,]*(?:\.\d+)?)\s*(crore|cr|lakhs?|lac|million|mn|k|l)?\b",
                    re.IGNORECASE)


def numbers_in(text: str) -> list[Decimal]:
    """Every number in the text, with lakh/crore/million scaling applied."""
    out = []
    for raw, unit in NUMBER.findall(text):
        value = Decimal(raw.replace(",", ""))
        out.append(value * SCALE[unit.lower()] if unit else value)
    return out


# A count of zero is often said in words: "is not missing a price on any line", "none".
ZERO_WORDS = re.compile(r"\b(zero|none|no lines?|missing no (lines?|prices?)|"
                        r"not missing (a |any )?prices? (on|for) any|"
                        r"nothing (is )?missing|has a price on (every|all))\b", re.IGNORECASE)


def has_number(text: str, want: Decimal) -> bool:
    tol = max(abs(want) * TOLERANCE, Decimal("0.01"))
    if want == 0 and ZERO_WORDS.search(text):
        return True
    return any(abs(n - want) <= tol for n in numbers_in(text))


def _money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("1"))


def questions(truth: dict) -> list[dict]:
    """Build the question set for this ground truth. Expected values are never typed."""
    a, b, c, d, e = (name(truth, k) for k in "ABCDE")
    elig = X.eligible(truth)
    gated = X.split(truth, elig, "landed")
    cap2 = X.capped(truth, elig, 2, "landed")
    all_split = X.split(truth, list(truth["vendors"]), "ex_works")
    single_a = X.single(truth, "A", "ex_works")
    ly_gated = X.last_year_total(truth, list(gated["allocation"]))
    fx90 = X.single(truth, "C", "ex_works", fx=Decimal("90"))
    carrying = X.split(truth, elig, "landed", carrying=True)
    light = truth["vendors"]["D"]["doc"]["light_line"]
    partition = next(ln for ln in truth["lines"] if ln["kind"] == "partition")["line_no"]
    contracts = [h for h in truth["history"] if h["source"] == "contract"]  # what we paid
    a_history = next(h for h in contracts if h["vendor"] == "A")
    biggest = max(truth["lines"], key=lambda ln: ln["annual_qty"])["line_no"]
    some_line = truth["lines"][len(truth["lines"]) // 2]["line_no"]
    a_five = [X.price(truth, "A", ln["line_no"]) for ln in truth["lines"] if ln["kind"] == "5ply"]
    ly_spend = {ln["line_no"]: Decimal(ln["last_price"]) * ln["annual_qty"]
                for ln in truth["lines"]}

    q = []

    def add(qid, set_, text, kind, want, group=None):
        q.append({"id": qid, "set": set_, "text": text, "kind": kind, "expected": want,
                  "group": group or qid})

    num, ven, has, dec = "number", "vendor", "contains", "decline"
    any_of = "contains_any"
    refuse = "refuse_mixed_units"
    add("Q01", "base", f"How many of the 30 lines did {b} quote?", num,
        sum(c_["quoted"] for c_ in truth["vendors"]["B"]["cells"]))
    add("Q02", "base", f"Which vendor has the lowest per-piece price on line {some_line}, "
        "before freight?", ven, name(truth, X.cheapest_vendor(truth, some_line)))
    add("Q03", "base", f"What would a full year cost if every line went to {a}, at per-piece "
        "prices ex-GST before freight?", num, _money(single_a["total"]))
    add("Q04", "base", "Cheapest vendor per line across all vendors, per-piece price before "
        "freight: what is the annual total?", num, _money(all_split["total"]))
    add("Q05", "base", "Split cheapest per line, landed cost (price plus freight), only among "
        "vendors who passed every gating question. What is the annual total?", num,
        _money(gated["total"]))
    add("Q06", "base", "Same split, landed cost, gating applied, but capped at two vendors. "
        "What is the annual total?", num, _money(cap2["total"]))
    add("Q07", "base", "Against last year's prices for the same lines, how much does the "
        "gated landed cheapest-per-line split save per year?", num,
        _money(ly_gated - gated["total"]))
    add("Q08", "base", "Which vendors fail a gating question?", has,
        [name(truth, k) for k, v in truth["vendors"].items() if v["gating"]["overall"] ==
         "fail"])
    add("Q09", "base", f"Why is {d} so cheap on line {light}?", any_of, ["16 BF", "120 GSM"])
    add("Q10", "base", f"If USD/INR moves to 90, what is {c}'s annual total for the lines it "
        "quoted, per-piece before freight?", num, _money(fx90["total"]))
    add("Q11", "base", "Cheapest per line among gating-passed vendors on landed cost with "
        "payment terms factored in at our cost of capital: annual total?", num,
        _money(carrying["total"]))
    add("Q12", "base", f"What did we pay {a} per piece for line {a_history['line_no']} last "
        "year?", num, Decimal(a_history["unit_price"]))
    add("Q13", "base", f"How many lines is {e} still missing a price for?", num,
        sum(not c_["quoted"] for c_ in truth["vendors"]["E"]["cells"]))
    add("Q14", "base", f"What payment terms, in days, did {c} offer?", num,
        Decimal(truth["vendors"]["C"]["payment_days"]))
    # Q15: with lines in more than one unit (pieces and sets), the right answer refuses to
    # add them into one figure and says why; a per-unit breakdown is welcome but not needed.
    qty_by_unit: dict[str, Decimal] = {}
    for ln in truth["lines"]:
        unit = ln["canonical_uom"]
        qty_by_unit[unit] = qty_by_unit.get(unit, Decimal(0)) + Decimal(ln["annual_qty"])
    if len(qty_by_unit) == 1:
        add("Q15", "base", "What is the total annual quantity across all lines?", num,
            next(iter(qty_by_unit.values())))
    else:
        add("Q15", "base", "What is the total annual quantity across all lines?", refuse,
            {"mixed_total": sum(qty_by_unit.values()), "units": sorted(qty_by_unit)})
    add("Q16", "base", f"Who is cheapest on line {partition}, per set?", ven,
        name(truth, X.cheapest_vendor(truth, partition)))
    add("Q17", "base", f"What rebate percentage does {b} offer, and is it conditional?", num,
        Decimal(truth["vendors"]["B"]["doc"]["rebate_pct"]))
    add("Q18", "base", "How much more per year does capping at two vendors cost than the "
        "uncapped gated landed split?", num, _money(cap2["total"] - gated["total"]))
    add("Q19", "base", f"Who supplied line {biggest} last year?", ven,
        name(truth, line(truth, biggest)["last_vendor"]))
    add("Q20", "base", "Under the gated landed cheapest-per-line split, which vendors are "
        "used?", has, [name(truth, k) for k in gated["vendors_used"]])

    # Paraphrases: three more wordings of five base questions; all must agree.
    for i, text in enumerate([
        f"If we gave the whole contract to {a}, what's the yearly bill before freight?",
        f"Single-source everything with {a}: annual spend on per-piece prices?",
        f"Total cost of awarding all 30 lines to {a} (ex-works, ex-GST)?"], start=1):
        add(f"P03.{i}", "paraphrase", text, num, _money(single_a["total"]), "Q03")
    for i, text in enumerate([
        "Only vendors that cleared the quality questionnaire, cheapest per line, delivered "
        "cost: what's the total?",
        "Best landed price per line, excluding anyone who failed or didn't answer the gating "
        "questions. Annual total?",
        "What does the cheapest delivered split cost if we exclude vendors that did not pass "
        "gating?"], start=1):
        add(f"P05.{i}", "paraphrase", text, num, _money(gated["total"]), "Q05")
    for i, text in enumerate([
        "And with at most two suppliers, delivered cost, gating applied?",
        "What if we limit the award to two vendors (landed, gated)? Total?",
        "Two-vendor cap on the gated landed split: what's the annual cost?"], start=1):
        add(f"P06.{i}", "paraphrase", text, num, _money(cap2["total"]), "Q06")
    for i, text in enumerate([
        "How much do we save versus last year with the gated landed split?",
        "Savings against FY25 prices for the cheapest delivered split among qualified "
        "vendors?",
        "Compared with last year's contract, what's the annual saving of the gated landed "
        "award?"], start=1):
        add(f"P07.{i}", "paraphrase", text, num, _money(ly_gated - gated["total"]), "Q07")
    for i, text in enumerate([
        f"What happens to {c}'s total if the dollar goes to 90?",
        f"Re-price {c}'s quote at 90 rupees to the dollar: annual total before freight?",
        f"At USD/INR 90, what would {c}'s quoted lines cost per year (ex-works)?"], start=1):
        add(f"P10.{i}", "paraphrase", text, num, _money(fx90["total"]), "Q10")

    # Held-out: not to be looked at while writing analyst prompts.
    add("H01", "heldout", f"What is {a}'s average per-piece price across the 5-ply lines?", num,
        sum(a_five) / len(a_five))
    add("H02", "heldout", f"How many lines does {c} quote?", num,
        sum(c_["quoted"] for c_ in truth["vendors"]["C"]["cells"]))
    add("H03", "heldout", f"Ignoring gating, what is {d}'s total if it got every line, "
        "per-piece before freight?", num, _money(X.single(truth, "D")["total"]))
    add("H04", "heldout", "Which line had the largest annual spend at last year's prices?",
        num, Decimal(max(ly_spend, key=ly_spend.get)))
    add("H05", "heldout", f"How many lines did {e} supply us last year?", num,
        sum(h["vendor"] == "E" for h in contracts))

    # Unanswerable: the data cannot answer these; a correct answer declines.
    for qid, text in [
        ("U01", f"What was {a}'s on-time delivery rate last year?"),
        ("U02", f"What is {b}'s credit rating?"),
        ("U03", "What will kraft paper prices be next quarter?"),
        ("U04", f"What did {c} quote to our competitors?")]:
        add(qid, "unanswerable", text, dec, None)
    return q


class DeclineVerdict(BaseModel):
    declined: bool = Field(description="True if the answer says the data cannot answer the "
                           "question and does not invent a figure.")
    reason: str


def llm_decline_grader(question: str, answer: str) -> bool:
    """Model grader, used only for 'declined appropriately'."""
    from level import llm

    result = llm.structured(
        DeclineVerdict,
        [{"role": "user", "content": (
            "You grade an analyst's answer. The question cannot be answered from the "
            "procurement data the analyst has. Did the analyst decline (say the data does not "
            "contain it) rather than give an invented answer?\n\n"
            f"Question: {question}\n\n" + llm.untrusted("analyst_answer", answer))}],
        system="You are a strict, literal grader. Reply only with the requested JSON.",
        effort="low", max_tokens=1000)
    return result.parsed.declined


# The reason for not adding different units together, in the words an analyst would use.
MIXED_UNIT_REASON = re.compile(
    r"different units?|not the same unit|units? differ|separate(?:ly)?|"
    r"(?:can(?:no|')t|could not|would not|won't|do not|don't|not) (?:be )?(?:add|sum|combin)|"
    r"(?:rather than|instead of|without) (?:add|sum|combin)|mix(?:ing|ed)? units?", re.I)


def refuses_mixed_units(answer: str, want: dict) -> bool:
    """Correct when the answer gives no single total across units and says why."""
    if has_number(answer, Decimal(want["mixed_total"])):
        return False
    return bool(MIXED_UNIT_REASON.search(answer))


def grade(item: dict, answer: str, grader: Callable[[str, str], bool]) -> bool:
    kind, want = item["kind"], item["expected"]
    if kind == "refuse_mixed_units":
        return refuses_mixed_units(answer, want)
    if kind == "number":
        return has_number(answer, Decimal(want))
    if kind == "vendor":
        return want.lower() in answer.lower()
    if kind == "contains":
        return all(w.lower() in answer.lower() for w in want)
    if kind == "contains_any":
        return any(w.lower() in answer.lower() for w in want)
    return grader(item["text"], answer)


def score(truth: dict, ask: Callable[[str], str], grader=llm_decline_grader) -> dict:
    results = []
    for item in questions(truth):
        try:
            answer = ask(item["text"])
            ok = grade(item, answer, grader)
        except Exception as exc:  # an error is a failed answer, shown with its reason
            answer, ok = f"ERROR: {exc}", False
        results.append({**item, "expected": _jsonable(item["expected"]), "answer": answer,
                        "pass": ok, "link": f"question:{item['id']}"})

    def rate(set_):
        rows = [r for r in results if r["set"] == set_]
        return round(sum(r["pass"] for r in rows) / len(rows), 4) if rows else None

    groups: dict[str, list[bool]] = {}  # a base question and its paraphrases
    for r in results:
        if r["set"] in ("base", "paraphrase"):
            groups.setdefault(r["group"], []).append(r["pass"])
    agree = [all(v) for v in groups.values() if len(v) > 1]
    return {"correctness": rate("base"), "paraphrase_agreement":
            round(sum(agree) / len(agree), 4) if agree else None,
            "heldout": rate("heldout"), "declined": rate("unanswerable"),
            "results": results}


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value

