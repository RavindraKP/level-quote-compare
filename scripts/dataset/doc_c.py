"""Vendor C: a Word letter. Commercials in prose, a partial table, USD ex-works prices."""

from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from scripts.dataset.common import email_date, line_of, long_date, size, write_email

PRINT_WORDS = {"plain": "plain", "1-colour": "one-colour print", "2-colour": "two-colour print"}


def _sentence_items(truth: dict, rows: list[tuple[int, dict]]) -> str:
    """'USD 0.248 per piece for the 400 × 300 × 250 mm shipper (one-colour print), ...'"""
    parts = []
    for no, row in rows:
        line = line_of(truth, no)
        parts.append(f"USD {row['usd']} per piece for the {size(line['spec'], ' × ')} mm "
                     f"carton ({PRINT_WORDS[line['spec']['print']]})")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def render(truth: dict, out: Path) -> list[str]:
    v = truth["vendors"]["C"]
    doc = v["doc"]
    usd = {int(k): r for k, r in doc["usd_rows"].items()}
    five = sorted((no, r) for no, r in usd.items() if line_of(truth, no)["kind"] == "5ply")
    seven = sorted((no, r) for no, r in usd.items() if line_of(truth, no)["kind"] == "7ply")
    prose_five, table_five = five[:4], five[4:]  # some in sentences, the rest in a table
    a = v["answers"]

    d = Document()
    style = d.styles["Normal"]
    style.font.name, style.font.size = "Calibri", Pt(10.5)
    head = d.add_paragraph()
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = head.add_run("PACIFIC BOARD INDUSTRIES")
    run.bold, run.font.size, run.font.color.rgb = True, Pt(18), RGBColor(0x0B, 0x3D, 0x5C)
    sub = d.add_paragraph("A 100% Export Oriented Unit  •  Plot 9, Sector 4, SEZ Mundra, "
                          "Kutch, Gujarat 370421  •  exports@pacificboard.example")
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.runs[0].font.size = Pt(8)

    d.add_paragraph(f"Ref: {doc['quote_no']}\nDate: {long_date(doc['quote_date'])}")
    d.add_paragraph("To\nMs Anita Kulkarni\nCategory Manager – Packaging\nSundari Foods Pvt Ltd"
                    "\nPune")
    d.add_paragraph().add_run("Subject: Offer for corrugated shipper cartons – "
                              "your enquiry SF/PKG/FY26/014").bold = True
    d.add_paragraph("Dear Ms Kulkarni,")
    d.add_paragraph(
        "Further to your enquiry dated 3 February 2025, we thank you for the opportunity and "
        "are pleased to submit our offer for corrugated shipper cartons as below.")
    if prose_five:
        d.add_paragraph(
            "We are pleased to offer our five-ply range at " +
            _sentence_items(truth, prose_five) + ", ex-works our Mundra unit.")
    if seven:
        d.add_paragraph(
            "For the heavy-duty seven-ply cartons we can offer " + _sentence_items(truth, seven)
            + ". These are produced on our new BCB-flute line.")
    d.add_paragraph(
        f"Three-ply items are offered at a uniform USD {doc['rule_usd_3ply']} per piece "
        "irrespective of size, up to 300 mm length. Three-ply cartons above 300 mm length, "
        "partitions and pads can be offered on request.")
    if table_five:
        d.add_paragraph("The balance five-ply items are priced as follows:")
        table = d.add_table(rows=1, cols=4)
        table.style = "Light Grid Accent 1"
        for cell, text in zip(table.rows[0].cells, ["Size (L × W × H, mm)", "Ply", "Print",
                                                     "Price (USD/pc, ex-works)"], strict=True):
            cell.text = text
        for no, row in table_five:
            sp = line_of(truth, no)["spec"]
            cells = table.add_row().cells
            cells[0].text, cells[1].text = size(sp, " × "), "5"
            cells[2].text, cells[3].text = sp["print"], row["usd"]
    d.add_paragraph()
    d.add_paragraph(
        "All prices are ex-works our Mundra unit and exclusive of GST. Prices are valid for 90 "
        f"days from the date of this offer and are subject to revision should the USD/INR rate "
        f"move by more than {doc['fx_clause_pct']}% from the date of this offer.")
    d.add_paragraph(
        f"Freight to your Pune and Hosur plants would be approximately INR "
        f"{doc['freight_per_piece']} per piece; Baddi on actuals. {a['10']['text']}")
    d.add_paragraph(
        f"On the points in your questionnaire: {a['1']['text']} {a['2']['text']} "
        f"{a['3']['text']} {a['5']['text']} {a['6']['text']} {a['7']['text']} "
        f"{a['8']['text']} We are unable to commit to holding safety stock at this stage.")
    d.add_paragraph("We look forward to your valued order.")
    d.add_paragraph("Yours faithfully,\nFor Pacific Board Industries\n\nFarhan Qureshi\n"
                    "Head – Export Sales")
    name = f"Pacific-Board-Offer-{doc['quote_no'].replace('/', '-')}.docx"
    d.save(out / name)

    attachments = [name, "ISO9001-PBI.pdf", "FSC-CoC-PBI.pdf"]
    write_email(out / "01-email.txt",
                sender=f"{v['contact']} <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date(doc["quote_date"], "10:12"),
                subject="Offer – corrugated cartons – PBI/EXP/0925",
                body="""Dear Ms Kulkarni,

Please find our offer attached, with copies of our ISO 9001 and FSC certificates.
Prices are in USD as per our export price list.

Best regards,
Farhan Qureshi
Pacific Board Industries""",
                attachments=attachments)
    return attachments
