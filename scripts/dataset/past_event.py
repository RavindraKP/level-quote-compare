"""Last year's (FY25) event, rebuilt from the FY25 ground truth and history (WP13).

The buyer ran the same rate contract a year earlier. Its record is derived, not invented:
- lines: the same SKUs, specs, units, quantities and plants as this year's lines;
- questionnaire: the same twelve questions and pass rules;
- invited vendors: everyone with an FY25 record (every vendor bid last year);
- award: one supplier per line, at the FY25 contract price in the history file.

So the past event always agrees with `last_price` / `last_vendor` on this year's lines.
No random numbers are drawn here, so adding it changes nothing else in the dataset.
"""

from __future__ import annotations

# Dates one year before this year's event (truth.RFX: issued 2025-02-03, award 2025-03-14).
PAST_RFX = {
    "ref": "SF/PKG/FY25/009",
    "title": "Annual rate contract — corrugated shipper cartons, FY25",
    "period": "FY25",
    "issued": "2024-02-05",
    "due": "2024-02-26",
    "award_date": "2024-03-15",
}

# Terms in the keys the RFx co-pilot uses, so a draft started from this event is complete.
PAST_TERMS = {
    "price_basis": "Price per piece, ex-GST, delivered to plant",
    "currency": "INR",
    "payment_days": "60",
    "delivery": "Delivered to plant (Pune, Hosur, Baddi)",
}


def build(gt: dict, history: list[dict]) -> dict:
    """The FY25 event as plain data, keyed by vendor key and line number like ground truth."""
    contracts = [h for h in history if h["source"] == "contract" and h["period"] == "FY25"]
    invited = sorted({h["vendor"] for h in history if h["period"] == "FY25"})
    lines = [{k: ln[k] for k in ("line_no", "sku", "description", "spec", "canonical_uom",
                                 "annual_qty", "conversion_factors", "plant")}
             for ln in gt["lines"]]
    awards = [{"line_no": h["line_no"], "vendor": h["vendor"], "unit_price": h["unit_price"]}
              for h in sorted(contracts, key=lambda h: h["line_no"])]
    return {
        **PAST_RFX,
        "category": gt["rfx"]["category"],
        "status": "awarded",
        "terms": {**PAST_RFX, **PAST_TERMS, "buyer": gt["buyer"]["name"],
                  "plants": gt["buyer"]["plants"]},
        "lines": lines,
        "questionnaire": gt["questionnaire"],
        "invited": invited,
        "awards": awards,
    }
