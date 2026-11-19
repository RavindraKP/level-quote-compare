"""Vendor B: a PDF quotation on letterhead.

Table in the buyer's line order, per piece; 7-ply lines declined; page 2 rates include GST;
a grand total that does not equal the sum of lines; rebate and plate charges in footnotes.
"""

from __future__ import annotations

from pathlib import Path

from scripts.dataset.common import (
    dmy,
    email_date,
    html_to_pdf,
    inr,
    line_of,
    long_date,
    size,
    write_email,
)

CSS = """
body { font-family: sans-serif; font-size: 9pt; color: #222; }
.lh-name { font-size: 20pt; font-weight: bold; color: #8B1A1A; }
.lh-sub { font-size: 8pt; color: #555; }
table.q { border-collapse: collapse; width: 100%; }
table.q th { background-color: #EFE4D2; border: 1px solid #777; padding: 3px; font-size: 8pt; }
table.q td { border: 1px solid #999; padding: 3px; font-size: 8pt; }
.r { text-align: right; }
.foot { font-size: 6.5pt; color: #444; }
.small { font-size: 7.5pt; }
"""

LETTERHEAD = """
<p class="lh-name">SHREEJI CARTONS</p>
<p class="lh-sub">Manufacturers of Corrugated Boxes, Sheets &amp; Rolls<br/>
Gat No. 1187, Pune–Nashik Highway, Moshi, Pune 412105 &nbsp;|&nbsp; Tel 020-2712 xxxx
&nbsp;|&nbsp; quotes@shreejicartons.example<br/>GSTIN 27ABJFS5520K1Z3</p>
<hr/>
"""


def b_item(line: dict) -> str:
    sp = line["spec"]
    if line["kind"] == "partition":
        return f"Partition set {sp['cells']} cell (3 ply)"
    if line["kind"] == "pad":
        return f"3 Ply Pad {sp['length_mm']}x{sp['width_mm']}"
    pr = {"plain": "plain", "1-colour": "single colour", "2-colour": "two colour"}[sp["print"]]
    return f"{sp['ply']} Ply Box {size(sp, 'x')}, {pr}"


def _rows_html(truth: dict, rows: list[dict]) -> str:
    out = []
    for r in rows:
        line = line_of(truth, r["line_no"])
        if r["declined"]:
            out.append(f"<tr><td>{line['line_no']}</td><td>{b_item(line)}</td>"
                       f"<td class='r'>{inr(line['annual_qty'], 0)}</td>"
                       f"<td colspan='2'><i>Regret – not in our manufacturing range</i></td></tr>")
        else:
            out.append(f"<tr><td>{line['line_no']}</td><td>{b_item(line)}</td>"
                       f"<td class='r'>{inr(line['annual_qty'], 0)}</td>"
                       f"<td class='r'>{inr(r['rate_shown'])}</td>"
                       f"<td class='r'>{inr(r['amount'])}</td></tr>")
    return "\n".join(out)


def _table(head_rate: str, body: str, tail: str = "") -> str:
    return (f"<table class='q'><tr><th>Sr</th><th>Item</th><th>Qty/yr</th><th>{head_rate}</th>"
            f"<th>Amount (Rs)</th></tr>{body}{tail}</table>")


def render(truth: dict, out: Path) -> list[str]:
    v = truth["vendors"]["B"]
    doc = v["doc"]
    rows = doc["rows"]
    page1 = [r for r in rows if r["line_no"] <= doc["page_one_rows"]]
    page2 = [r for r in rows if r["line_no"] > doc["page_one_rows"]]
    answers = v["answers"]

    p1 = f"""{LETTERHEAD}
<p>Ref: {doc['quote_no']} &nbsp;&nbsp;&nbsp;&nbsp; Date: {dmy(doc['quote_date'])}</p>
<p>To,<br/>The Purchase Manager<br/>Sundari Foods Pvt Ltd<br/>Hinjewadi Phase 2, Pune</p>
<p><b>Sub: Quotation for corrugated boxes – your RFQ SF/PKG/FY26/014</b></p>
<p>Dear Madam,<br/>With reference to the above enquiry, we are pleased to quote our best rates
as under. Rates are per piece, FOR your plants.</p>
{_table("Rate (Rs/pc)", _rows_html(truth, page1))}
<p class="small"><i>contd. on next page</i></p>"""

    total_row = (f"<tr><td></td><td><b>Grand Total</b> <sup>1</sup></td><td></td><td></td>"
                 f"<td class='r'><b>{inr(doc['stated_total'])}</b></td></tr>")
    p2 = f"""{LETTERHEAD}
<p class="small">Ref: {doc['quote_no']} – page 2</p>
{_table("Rate incl. GST 18% (Rs/pc)", _rows_html(truth, page2), total_row)}
<p><b>Terms &amp; Conditions</b></p>
<p class="small">1. Delivery: FOR your Pune, Hosur and Baddi plants.<br/>
2. Payment: {answers['10']['text']}.<br/>
3. Validity: {answers['11']['text']}.<br/>
4. Lead time: {answers['8']['text']}.<br/>
5. GST @ 18% extra unless stated otherwise.<br/>
6. Printing plates: as per note 2 below.</p>
<p>Thanking you and awaiting your valued order.</p>
<p>For <b>Shreeji Cartons</b><br/><br/>Mitesh Shah<br/>Partner</p>
<hr/>
<p class="foot"><sup>1</sup> A further {doc['rebate_pct']}% rebate is offered on all items if
annual offtake exceeds Rs {doc['rebate_threshold_cr']} Cr.<br/>
<sup>2</sup> Printing plates charged one time at Rs {doc['plate_charge']} per colour per item.</p>
"""

    qrows = "".join(
        f"<tr><td>{q['q_no']}</td><td>{q['text']}</td>"
        f"<td>{answers[str(q['q_no'])]['text'] if str(q['q_no']) in answers else ''}</td></tr>"
        for q in truth["questionnaire"])
    p3 = f"""{LETTERHEAD}
<p><b>Annexure – Supplier Questionnaire</b></p>
<table class='q'><tr><th>No</th><th>Question</th><th>Shreeji Cartons response</th></tr>
{qrows}</table>
<p class="small">ISO 9001 certificate copy enclosed.</p>"""

    pdf = f"Shreeji-Quotation-{doc['quote_no'].replace('/', '-')}.pdf"
    html_to_pdf([p1, p2, p3], out / pdf, CSS)
    attachments = [pdf, "ISO-9001.pdf"]
    write_email(out / "01-email.txt",
                sender=f"{v['contact']} <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date(doc["quote_date"], "18:47"),
                subject="Quotation SC/2425/311 - Corrugated Boxes",
                body=f"""Respected Madam,

Please find attached our quotation dated {long_date(doc['quote_date'])} against your RFQ
along with the questionnaire and our ISO certificate.

Kindly consider us for 3 ply and 5 ply items. We regret for 7 ply.

Thanks & Regards,
Mitesh Shah
Shreeji Cartons, Moshi
98230 xxxxx""",
                attachments=attachments)
    return attachments
