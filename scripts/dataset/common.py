"""Shared rendering helpers: Indian number format, dates, HTML -> PDF, email files."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pymupdf


def inr(amount, decimals: int = 2) -> str:
    """Indian digit grouping: 3712345.5 -> '37,12,345.50'."""
    value = Decimal(str(amount)).quantize(Decimal(1).scaleb(-decimals))
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.{decimals}f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    text = ",".join(groups + [tail]) if groups else tail
    return sign + text + (f".{frac}" if decimals else "")


def dmy(iso: str, sep: str = ".") -> str:
    """'2025-02-21' -> '21.02.2025'."""
    return date.fromisoformat(iso).strftime(f"%d{sep}%m{sep}%Y")


def long_date(iso: str) -> str:
    """'2025-02-21' -> '21 February 2025'."""
    d = date.fromisoformat(iso)
    return f"{d.day} {d:%B %Y}"


def email_date(iso: str, hhmm: str) -> str:
    """RFC 2822 date in IST, e.g. 'Fri, 21 Feb 2025 16:05:00 +0530'."""
    d = datetime.fromisoformat(f"{iso}T{hhmm}:00")
    return d.strftime("%a, %d %b %Y %H:%M:%S +0530")


def size(spec: dict, sep: str = " x ") -> str:
    return sep.join(str(spec[k]) for k in ("length_mm", "width_mm", "height_mm") if k in spec)


def html_to_pdf(pages: list[str], path: Path, css: str = "", landscape: bool = False) -> None:
    """Render each HTML string as one or more A4 pages with PyMuPDF's Story engine."""
    rect = pymupdf.paper_rect("a4-l" if landscape else "a4")
    writer = pymupdf.DocumentWriter(str(path))
    for html in pages:
        story = pymupdf.Story(html=html, user_css=css)
        more = True
        while more:
            device = writer.begin_page(rect)
            more, _ = story.place(rect + (40, 36, -40, -36))
            story.draw(device)
            writer.end_page()
    writer.close()


def write_email(path: Path, *, sender: str, to: str, date_: str, subject: str, body: str,
                attachments: list[str]) -> None:
    """A received email as a text file: headers, blank line, body (what seed.py parses)."""
    headers = [f"From: {sender}", f"To: {to}", f"Date: {date_}", f"Subject: {subject}"]
    if attachments:
        headers.append("Attachments: " + "; ".join(attachments))
    path.write_text("\n".join(headers) + "\n\n" + body.strip() + "\n", encoding="utf-8")


def line_of(truth: dict, line_no: int) -> dict:
    return next(ln for ln in truth["lines"] if ln["line_no"] == line_no)


def cell_of(truth: dict, key: str, line_no: int) -> dict:
    return next(c for c in truth["vendors"][key]["cells"] if c["line_no"] == line_no)
