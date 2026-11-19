"""Vendor A: a polished Excel that ignores the buyer's template, plus a revision email.

Own item codes and order, two price sheets (Pune & Hosur / Baddi), merged headers,
rates per 1000 nos in two volume slabs, a questionnaire sheet.
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from scripts.dataset.common import dmy, email_date, line_of, write_email

THIN = Side(style="thin", color="808080")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")
HEAD_FONT = Font(bold=True, color="FFFFFF", size=10)
QUESTION_TEXT = {  # A paraphrases the buyer's questions in its own sheet
    1: "ISO 9001 certification (valid)", 2: "In-house burst / BCT testing",
    3: "Rejection % (last 12 months)", 4: "Board test report", 5: "FSC chain of custody",
    6: "Recycled content in paper", 7: "Monthly capacity", 8: "Standard lead time",
    9: "Safety stock – 2 weeks", 10: "Payment terms (60 days)", 11: "Price validity",
    12: "Freight to plants",
}


def a_description(line: dict) -> str:
    """Vendor A's own shorthand, e.g. 'RSC 5PLY 400X300X250 1C'."""
    sp = line["spec"]
    if line["kind"] == "partition":
        return f"PARTITION SET {sp['cells']} CELL 3PLY"
    if line["kind"] == "pad":
        return f"PAD 3PLY {sp['length_mm']}X{sp['width_mm']}"
    pr = {"plain": "PLAIN", "1-colour": "1C", "2-colour": "2C"}[sp["print"]]
    return f"RSC {sp['ply']}PLY {sp['length_mm']}X{sp['width_mm']}X{sp['height_mm']} {pr}"


def a_board(line: dict) -> str:
    """'150/120/120/120/150' -> '150K/120F/120M/120F/150K' (kraft liner, flute, medium)."""
    parts = line["spec"]["board_gsm"].split("/")
    tags = ["K"] + ["F" if i % 2 else "M" for i in range(1, len(parts) - 1)] + ["K"]
    return "/".join(p + t for p, t in zip(parts, tags, strict=True))


def _header(ws, doc: dict, plant_note: str) -> int:
    ws.merge_cells("A1:H1")
    ws["A1"] = "KAVERI PACKAGING PVT. LTD."
    ws["A1"].font = Font(bold=True, size=16, color="1F4E78")
    ws.merge_cells("A2:H2")
    ws["A2"] = ("Plot 27, SIPCOT Industrial Complex Phase I, Hosur 635126, Tamil Nadu  |  "
                "GSTIN 33AAKCK4821R1ZQ  |  sales@kaveripackaging.example")
    ws["A2"].font = Font(size=9, italic=True)
    ws["A4"], ws["D4"] = "QUOTATION", f"Ref: {doc['quote_no']}"
    ws["G4"] = f"Date: {dmy(doc['quote_date'], '-')}"
    ws["A4"].font = Font(bold=True, size=12)
    ws["A5"] = "To: Sundari Foods Pvt Ltd — Kind Attn: Ms Anita Kulkarni"
    ws["G5"] = "Your RFQ: SF/PKG/FY26/014"
    ws.merge_cells("A6:H6")
    ws["A6"] = plant_note
    ws["A6"].font = Font(bold=True, color="C00000")
    for col, text in zip("ABCDEH", ["S.No", "Item Code", "Description", "Board (GSM)",
                                    "Deliver to", "Remarks"], strict=True):
        ws.merge_cells(f"{col}8:{col}9")
        ws[f"{col}8"] = text
    ws.merge_cells("F8:G8")
    ws["F8"] = "Rate / 1000 Nos (Rs.)"
    ws["F9"], ws["G9"] = "Up to 1,00,000 nos/yr", "Above 1,00,000 nos/yr"
    for row in ws.iter_rows(min_row=8, max_row=9, min_col=1, max_col=8):
        for c in row:
            c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BOX
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    return 10


def _price_sheet(ws, truth: dict, rows: list[dict], plant_note: str) -> None:
    doc = truth["vendors"]["A"]["doc"]
    r = _header(ws, doc, plant_note)
    for i, row in enumerate(rows, start=1):
        line = line_of(truth, row["line_no"])
        values = [i, row["code"], a_description(line), a_board(line), line["plant"],
                  row["rate_upto"], row["rate_above"],
                  "Printing as per artwork" if line["spec"]["print"] != "plain" else ""]
        for col, value in enumerate(values, start=1):
            c = ws.cell(row=r, column=col, value=value)
            c.border = BOX
            c.font = Font(size=10)
            if col in (6, 7):
                c.number_format = "#,##0"
        r += 1
    notes = ["Notes:",
             "1. Rates are FOR destination (delivered to your plant).",
             "2. GST extra @ 18% as applicable.",
             "3. Payment: 60 days from date of invoice.",
             "4. Rates valid till 31.03.2026.",
             "5. Slab rate applies on the annual scheduled quantity per item.",
             "", "For Kaveri Packaging Pvt. Ltd.", "R. Venkatesh, Sr. Manager – Sales"]
    for note in notes:
        r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8)
        ws.cell(row=r, column=1, value=note).font = Font(size=9, bold=note.endswith(":"))
    for col, width in zip("ABCDEFGH", [6, 14, 34, 28, 10, 16, 16, 24], strict=True):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A10"


def render(truth: dict, out: Path) -> list[str]:
    """Write the quotation workbook and both emails. Returns the attachment file names."""
    v = truth["vendors"]["A"]
    doc = v["doc"]
    rows = sorted(doc["rows"], key=lambda r: r["code"])  # A's own order, not the RFx order
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Pune & Hosur"
    _price_sheet(ws1, truth, [r for r in rows if line_of(truth, r["line_no"])["plant"] !=
                              "Baddi"], "WEST & SOUTH PLANTS — PUNE / HOSUR")
    ws2 = wb.create_sheet("Baddi")
    _price_sheet(ws2, truth, [r for r in rows if line_of(truth, r["line_no"])["plant"] ==
                              "Baddi"], "NORTH PLANT — BADDI (H.P.)")
    ws3 = wb.create_sheet("Questionnaire")
    ws3.append(["Q.No", "Point", "Our response"])
    for c in ws3[1]:
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BOX
    for n in range(1, 13):
        answer = v["answers"].get(str(n))
        ws3.append([n, QUESTION_TEXT[n], answer["text"] if answer else ""])
    for col, width in zip("ABC", [6, 36, 48], strict=True):
        ws3.column_dimensions[col].width = width
    xlsx = "KP-Quotation-SundariFoods.xlsx"
    wb.save(out / xlsx)

    attachments = [xlsx, "ISO-9001-Certificate.pdf", "FSC-CoC-Certificate.pdf",
                   "Board-Test-Report-Jan25.pdf"]
    write_email(out / "01-email.txt",
                sender=f"{v['contact']} <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date(doc["quote_date"], "16:05"),
                subject="Quotation – Corrugated boxes FY26 – your RFQ SF/PKG/FY26/014",
                body=f"""Dear Ms Kulkarni,

Thank you for your enquiry. Please find attached our quotation ref. {doc['quote_no']} for
your Pune, Hosur and Baddi plants, along with our responses to your questionnaire (sheet 3)
and copies of our ISO 9001 and FSC certificates and a recent board test report.

We have quoted in our standard item codes. Kindly note the slab rates.

Warm regards,
R. Venkatesh
Sr. Manager – Sales, Kaveri Packaging Pvt. Ltd.
+91 4344 27xxxx""",
                attachments=attachments)

    revised = "\n".join(
        f"  {r['code']}  {a_description(line_of(truth, r['line_no']))}  –  "
        f"Rs. {r['new_rate']:,} per 1000 nos (all quantities)" for r in doc["revisions"])
    write_email(out / "02-email.txt",
                sender=f"{v['contact']} <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date(doc["revision_date"], "11:20"),
                subject="RE: Quotation – Corrugated boxes FY26 – revised rates",
                body=f"""Dear Ms Kulkarni,

Further to our quotation ref. {doc['quote_no']} dated {dmy(doc['quote_date'], '-')}, we have
reworked our costing on two items. Please note the revised rates:

{revised}

All other rates and terms remain as per our quotation.

Regards,
R. Venkatesh""",
                attachments=[])
    return attachments
