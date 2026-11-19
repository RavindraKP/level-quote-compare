"""Vendor D: a printed rate card (PDF, for printing) and a synthetic phone photo of it.

The photo is the attachment D actually sends: perspective warp, uneven light, blur toward one
edge, JPEG compression, and one smudged digit in the 7-ply rate.
"""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from scripts.dataset.common import dmy, email_date, html_to_pdf, line_of, size, write_email

CSS = """
body { font-family: serif; font-size: 11pt; }
.name { font-size: 22pt; font-weight: bold; text-align: center; }
.sub { font-size: 9pt; text-align: center; }
.title { font-size: 14pt; font-weight: bold; text-align: center; }
table { border-collapse: collapse; width: 100%; }
td { border: 1px solid #000; padding: 5px; font-size: 11pt; }
th { border: 1px solid #000; padding: 5px; background-color: #DDD; font-size: 11pt; }
.r { text-align: right; }
"""


def rate_card_html(truth: dict) -> str:
    doc = truth["vendors"]["D"]["doc"]
    rates = doc["rates_per_kg"]
    grade = {p: next(ln["spec"] for ln in truth["lines"] if ln["spec"].get("ply") == p
                     and ln["kind"].endswith("ply")) for p in (3, 5, 7)}
    board_rows = "".join(
        f"<tr><td>{p} PLY</td><td>{grade[p]['board_gsm']} GSM {grade[p]['bursting_factor_bf']}"
        f" BF</td><td class='r'>Rs. {rates[str(p)]} / kg</td></tr>" for p in (3, 5, 7))
    specials = ""
    for item in doc["special_items"]:
        line = line_of(truth, item["line_no"])
        sp = line["spec"]
        board = f" ({item['board']})" if item["board"] else ""
        pr = "" if sp["print"] == "plain" else f", {sp['print'][0]} col print"
        specials += (f"<tr><td>RSC {sp['ply']} Ply {size(sp, 'x')}{pr}{board}</td>"
                     f"<td class='r'>Rs. {item['price']} / box</td></tr>")
    specials += (f"<tr><td>Partition set (as per sample)</td>"
                 f"<td class='r'>Rs. {doc['partition_per_box']} / box</td></tr>"
                 f"<tr><td>Pads 3 ply</td><td class='r'>Rs. {doc['pad_per_100']} / 100</td></tr>")
    return f"""
<p class="name">BHARAT CORRUGATORS</p>
<p class="sub">Gat No. 211, Chakan MIDC Phase II, Pune 410501 &nbsp; • &nbsp; Mob. 98220 4xxxx</p>
<hr/>
<p class="title">RATE CARD &nbsp; w.e.f. {dmy(doc['card_date'])}</p>
<p><b>Board rates</b> (on finished box weight)</p>
<table><tr><th>Ply</th><th>Grade</th><th>Rate</th></tr>{board_rows}</table>
<p><b>Special items</b></p>
<table><tr><th>Item</th><th>Rate</th></tr>{specials}</table>
<p>Other sizes as per your specification at board rates above.<br/>
Delivery free within 150 km. Payment 30 days. GST 18% extra.</p>
<p style="text-align:right">For Bharat Corrugators</p>"""


def _smudge(img: Image.Image, box: tuple[float, float, float, float]) -> None:
    """Over one digit, add strokes that make a '1' look like it might be a '4', then blur."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    draw = ImageDraw.Draw(img)
    ink = (95, 95, 95)
    draw.line([(x0 + 0.1 * w, y0 + 0.68 * h), (x0 + 0.55 * w, y0 + 0.12 * h)], fill=ink,
              width=max(2, int(w * 0.12)))
    draw.line([(x0 + 0.05 * w, y0 + 0.68 * h), (x0 + 0.95 * w, y0 + 0.68 * h)], fill=ink,
              width=max(2, int(w * 0.1)))
    pad = int(w * 0.6)
    region = (int(x0) - pad, int(y0) - pad, int(x1) + pad, int(y1) + pad)
    img.paste(img.crop(region).filter(ImageFilter.GaussianBlur(w * 0.12)), region[:2])
    blot = Image.new("L", img.size, 0)
    ImageDraw.Draw(blot).ellipse(region, fill=70)
    img.paste(Image.new("RGB", img.size, (120, 115, 105)), (0, 0),
              blot.filter(ImageFilter.GaussianBlur(w * 0.5)))


def _perspective_coeffs(src: list[tuple], dst: list[tuple]) -> list[float]:
    """Coefficients for PIL's PERSPECTIVE transform mapping output points to input points."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src, strict=True):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs += [u, v]
    return list(np.linalg.solve(np.array(rows, float), np.array(rhs, float)))


def _char_box(page: pymupdf.Page, text: str, index: int) -> tuple | None:
    """Bounding box of character `index` inside the first span containing `text`."""
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                chars = span["chars"]
                pos = "".join(c["c"] for c in chars).find(text)
                if pos >= 0:
                    return chars[pos + index]["bbox"]
    return None


def photo_from_pdf(pdf_path: Path, out_path: Path, rng: random.Random, smudge: str) -> None:
    """`smudge` is the text whose second character gets smudged (e.g. the 7-ply rate)."""
    page = pymupdf.open(pdf_path)[0]
    zoom = 2.0
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    paper = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    box = _char_box(page, smudge, 1)
    if box:
        _smudge(paper, tuple(c * zoom for c in box))

    # put the sheet on a desk, then photograph it at an angle
    w, h = paper.size
    desk = Image.new("RGB", (int(w * 1.4), int(h * 1.25)), (122, 86, 58))
    grain = Image.effect_noise(desk.size, 28).convert("RGB")
    desk = ImageChops.multiply(desk, Image.blend(grain, Image.new("RGB", desk.size, "white"),
                                                 0.7))
    ox, oy = int(w * 0.2), int(h * 0.12)
    desk.paste(paper.rotate(rng.uniform(-2.5, 2.5), expand=False, fillcolor=(122, 86, 58)),
               (ox, oy))
    dw, dh = desk.size
    corners = [(0, 0), (dw, 0), (dw, dh), (0, dh)]
    skew = [(dw * 0.06, dh * 0.03), (dw * 0.93, 0), (dw, dh), (0, dh * 0.97)]
    photo = desk.transform(desk.size, Image.Transform.PERSPECTIVE,
                           _perspective_coeffs(skew, corners), Image.Resampling.BICUBIC)

    # uneven light: a bright spot toward the lower right, darker toward the top left corner
    glow = Image.radial_gradient("L").resize((dw * 2, dh * 2)).point(lambda p: 255 - int(p * 0.4))
    light = glow.crop((int(dw * 0.35), int(dh * 0.3), int(dw * 1.35), int(dh * 1.3)))
    photo = ImageChops.multiply(photo, Image.merge("RGB", [light] * 3))

    # slightly out of focus, more so at the left edge
    soft = photo.filter(ImageFilter.GaussianBlur(2.2))
    mask = Image.linear_gradient("L").rotate(90).resize(photo.size)
    photo = Image.composite(photo.filter(ImageFilter.GaussianBlur(0.6)), soft, mask)
    photo = photo.resize((int(dw * 0.8), int(dh * 0.8)), Image.Resampling.LANCZOS)
    photo.save(out_path, "JPEG", quality=68)


def render(truth: dict, out: Path, rng: random.Random) -> list[str]:
    v = truth["vendors"]["D"]
    doc = v["doc"]
    printable = out / "print"
    printable.mkdir(exist_ok=True)
    pdf = printable / "rate-card.pdf"
    html_to_pdf([rate_card_html(truth)], pdf, CSS)
    photo = "IMG_20250222_174512.jpg"
    photo_from_pdf(pdf, out / photo, rng, smudge=doc["smudged_7ply_reads"][0])

    attachments = [photo, "iso certificate.pdf", "test report.pdf"]
    a = v["answers"]
    write_email(out / "01-email.txt",
                sender=f"Bharat Corrugators <{v['email']}>", to="purchase@sundarifoods.example",
                date_=email_date("2025-02-22", "17:52"),
                subject="rates",
                body=f"""Dear Sir,

Pls find our rate card attached (photo). {a['1']['text']}. {a['2']['text']}.
{a['8']['text']}. {a['10']['text']}.

Regards
Santosh Jadhav
Bharat Corrugators, Chakan
Sent from my phone""",
                attachments=attachments)
    return attachments
