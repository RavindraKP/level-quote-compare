"""Certificates (ISO 9001, FSC chain of custody) and board test reports as simple PDFs."""

from __future__ import annotations

from pathlib import Path

from scripts.dataset.common import html_to_pdf, long_date

CSS = """
body { font-family: serif; font-size: 11pt; text-align: center; }
.issuer { font-size: 10pt; color: #2E4A7D; font-weight: bold; }
.title { font-size: 22pt; font-weight: bold; color: #2E4A7D; }
.holder { font-size: 16pt; font-weight: bold; }
table { border-collapse: collapse; width: 90%; }
td { border: 1px solid #888; padding: 4px; font-size: 10pt; text-align: left; }
"""

ISSUER = "Meridian Assurance Registrar Pvt Ltd"


def certificate_html(vendor: dict, cert: dict) -> str:
    if cert["kind"] == "test_report":
        return test_report_html(vendor, cert)
    standard, title, statement = {
        "iso9001": ("ISO 9001:2015", "CERTIFICATE OF REGISTRATION",
                    "has been assessed and found to conform to the requirements of"),
        "fsc": ("FSC-STD-40-004 V3-1", "CHAIN OF CUSTODY CERTIFICATE",
                "has been audited and found to meet the chain-of-custody requirements of"),
    }[cert["kind"]]
    return f"""
<p class="issuer">{ISSUER}<br/>Accredited certification body</p>
<p>&nbsp;</p><p class="title">{title}</p><p>&nbsp;</p>
<p>This is to certify that the management system of</p>
<p class="holder">{vendor['name']}</p>
<p>{vendor['city']}, India</p>
<p>{statement}</p>
<p class="holder">{standard}</p>
<p>Scope: Manufacture and supply of corrugated boxes, sheets and allied packaging.</p>
<p>&nbsp;</p>
<table>
<tr><td>Certificate No.</td><td>{cert['number']}</td></tr>
<tr><td>Date of issue</td><td>{long_date(cert['issued'])}</td></tr>
<tr><td>Valid until</td><td>{long_date(cert['expires'])}</td></tr>
</table>
<p>&nbsp;</p>
<p style="font-size:8pt">Validity of this certificate is subject to satisfactory surveillance
audits. This certificate remains the property of {ISSUER}.</p>"""


def test_report_html(vendor: dict, cert: dict) -> str:
    rows = [("Grammage (GSM)", "IS 1060", "772"), ("Bursting strength (kg/cm²)", "IS 1060",
             "14.6"), ("Box compression (kgf)", "ASTM D642", "612"),
            ("Edge crush (kN/m)", "TAPPI T811", "7.9"), ("Moisture (%)", "IS 1060", "7.4")]
    body = "".join(f"<tr><td>{a}</td><td>{b}</td><td>{c}</td></tr>" for a, b, c in rows)
    return f"""
<p class="title">BOARD TEST REPORT</p>
<p>{vendor['name']} — Quality Control Laboratory</p>
<table>
<tr><td>Report No.</td><td>{cert['number']}</td></tr>
<tr><td>Date of test</td><td>{long_date(cert['issued'])}</td></tr>
<tr><td>Sample</td><td>5-ply board, 150/120/120/120/150, BC flute</td></tr>
</table><p>&nbsp;</p>
<table><tr><td><b>Parameter</b></td><td><b>Method</b></td><td><b>Result</b></td></tr>{body}
</table>
<p>&nbsp;</p><p style="font-size:9pt">Tested by: QC Chemist &nbsp;&nbsp; Approved by: QA Head</p>"""


def render(vendor: dict, names: list[str], out: Path) -> None:
    """Write each certificate under the attachment name the vendor's email uses.

    `names` are the email's non-quote attachments, in the same order as vendor['certificates'].
    """
    for cert, name in zip(vendor["certificates"], names, strict=True):
        html_to_pdf([certificate_html(vendor, cert)], out / name, CSS)
