"""Vendor E: a short informal email. Per-kg rules, 'rest same as last year', freight extra."""

from __future__ import annotations

from pathlib import Path

from scripts.dataset.common import email_date, write_email


def render(truth: dict, out: Path) -> list[str]:
    v = truth["vendors"]["E"]
    rates = v["doc"]["rates_per_kg"]
    attachments = ["ISO cert.pdf"]
    write_email(out / "01-email.txt",
                sender=f"Suresh Patil <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date(v["doc"]["email_date"], "09:31"),
                subject="Re: RFQ SF/PKG/FY26/014",
                body=f"""Sir,
₹{rates['5']}/kg for the 5-ply, {rates['3']} for the 3-ply, rest same as last year, freight extra.
ISO cert attached. Capacity no issue.
Regards, Suresh
Mahalaxmi Packaging""",
                attachments=attachments)
    return attachments
