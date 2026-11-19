"""The image path (PLAN §5 "Vision and OCR strategy").

1. Route    - by content: a PDF page with almost no text is a scan; pictures embedded in PDF,
              Word or Excel files are pulled out. (Routing decisions are made in intake.py.)
2. Prepare  - EXIF rotation, HEIC/WEBP/TIFF -> PNG, scanned pages rendered at 200 DPI,
              downscaled only above the API's size limit. The original file is untouched.
              No deskew, crop or binarise: they can destroy faint digits.
3. First read and 4. second read - two independent extraction calls (extract.py).
5. Zoom     - values the reads disagree on, or the model found hard to read, are read again
              from overlapping full-resolution tiles.
6. Decide   - code compares the reads; any disagreement opens a review item showing every
              candidate (run.py). The model's legibility remarks can flag, never confirm.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageOps

from level import db, llm
from level.core.normalise import parse_number
from level.pipeline import prompts
from level.types import ExtractionOutput, ZoomReadOutput

try:  # HEIC support registers itself with Pillow
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # pragma: no cover - dependency is declared in pyproject
    pass

MAX_LONG_EDGE = 2576  # high-resolution vision limit (claude-api skill); above it we downscale
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # API limit per image
SCAN_DPI = 200
SCANNED_PAGE_MIN_CHARS = 25  # fewer extractable characters than this -> treat page as a scan
MIN_EMBEDDED_PIXELS = 150 * 150  # ignore logos and icons smaller than this
TILE_GRID = 2  # 2 x 2 tiles
TILE_OVERLAP = 0.15


def derived_dir() -> Path:
    path = db.upload_dir() / "derived"
    path.mkdir(parents=True, exist_ok=True)
    return path


def prepare_image(data: bytes, name_hint: str) -> tuple[Path, Path]:
    """Normalise an image for the model. Returns (prepared PNG path, full-resolution PNG path).

    The full-resolution copy (rotated, converted, never downscaled) is what zoom tiles are
    cut from. Raises ValueError if Pillow cannot open it.
    """
    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)  # phone photos store rotation in EXIF
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    digest = hashlib.sha256(data).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name_hint)[:40]
    full_path = derived_dir() / f"{digest}_{safe}_full.png"
    image.save(full_path, "PNG")
    prepared = image
    if max(image.size) > MAX_LONG_EDGE:
        prepared = image.copy()
        prepared.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
    path = derived_dir() / f"{digest}_{safe}.png"
    prepared.save(path, "PNG")
    if path.stat().st_size > MAX_IMAGE_BYTES:  # very detailed photo: JPEG keeps it under limit
        path = path.with_suffix(".jpg")
        prepared.convert("RGB").save(path, "JPEG", quality=90)
    return path, full_path


def image_block(path: Path) -> dict:
    """An image content block for the Messages API."""
    media = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}


def tiles(full_path: Path) -> list[Path]:
    """Overlapping tiles of the full-resolution image, each saved as PNG."""
    image = Image.open(full_path)
    w, h = image.size
    tw, th = w / TILE_GRID, h / TILE_GRID
    out = []
    for row in range(TILE_GRID):
        for col in range(TILE_GRID):
            left = max(0, int(col * tw - tw * TILE_OVERLAP))
            top = max(0, int(row * th - th * TILE_OVERLAP))
            right = min(w, int((col + 1) * tw + tw * TILE_OVERLAP))
            bottom = min(h, int((row + 1) * th + th * TILE_OVERLAP))
            tile = image.crop((left, top, right, bottom))
            if max(tile.size) > MAX_LONG_EDGE:
                tile.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE))
            path = full_path.with_name(f"{full_path.stem}_tile{row}{col}.png")
            tile.save(path, "PNG")
            out.append(path)
    return out


# ---------------------------------------------------------------------------
# Comparing two independent reads
# ---------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _price_key(text: str | None):
    return parse_number(text)


def same_price(a: str | None, b: str | None) -> bool:
    """Two reads give the same number ('254.01' and 'Rs 254.01/-')."""
    return _price_key(a) is not None and _price_key(a) == _price_key(b)


@dataclass
class ReadDiff:
    row_index: int  # index in the first read
    item_text: str
    first: str | None
    second: str | None  # None when the second read did not find the item at all


PAIR_MIN = 0.85  # item texts this alike are the same row read with slightly different words


def _row_key(row) -> str:
    return _key(row.item_text + (row.quantity_break.text if row.quantity_break else ""))


def compare_reads(first: ExtractionOutput, second: ExtractionOutput) -> list[ReadDiff]:
    """Value-by-value comparison. Rows are paired by item text (and slab), not position.

    Two reads of a photo often word the same item slightly differently ('5 ply, 300x200'
    vs '5-ply 300 x 200 mm'). A row with no exact partner is paired with the most similar
    unpaired row of the other read when their item texts are at least PAIR_MIN alike; the
    prices are still compared. The numbers in the item text (sizes, codes) must be the same,
    so two sizes of one item are never paired. Only a row with no partner counts as not
    found."""
    pool: dict[str, list] = {}
    for row in second.rows:
        pool.setdefault(_row_key(row), []).append(row)
    paired: dict[int, object] = {}
    for i, row in enumerate(first.rows):
        matches = pool.get(_row_key(row)) or []
        if matches:
            paired[i] = matches.pop(0)
    left = [r for rows in pool.values() for r in rows]  # second-read rows not yet paired
    for i, row in enumerate(first.rows):
        if i in paired or not left:
            continue
        sizes = re.findall(r"\d+", _row_key(row))  # sizes and codes must be the same
        alike = [r for r in left if re.findall(r"\d+", _row_key(r)) == sizes]
        if not alike:
            continue
        best = max(alike, key=lambda r: SequenceMatcher(None, _row_key(row), _row_key(r)).ratio())
        if SequenceMatcher(None, _row_key(row), _row_key(best)).ratio() >= PAIR_MIN:
            paired[i] = best
            left.remove(best)
    diffs = []
    for i, row in enumerate(first.rows):
        other = paired.get(i)
        if other is None:
            diffs.append(ReadDiff(i, row.item_text, row.price, None))
        elif _price_key(row.price) != _price_key(other.price):
            diffs.append(ReadDiff(i, row.item_text, row.price, other.price))
    return diffs


def hard_to_read_rows(first: ExtractionOutput) -> list[int]:
    """Rows the model itself said were hard to read (matched by item text or price)."""
    notes = " ".join(first.hard_to_read).lower()
    if not notes:
        return []
    out = []
    for i, row in enumerate(first.rows):
        words = [w for w in re.findall(r"[a-z0-9]{3,}", row.item_text.lower())]
        price = re.sub(r"[^\d.]", "", row.price or "")
        if (price and price in re.sub(r"[^\d. ]", "", notes)) or (
                words and all(w in notes for w in words[:2])):
            out.append(i)
    return out


def zoom_reread(full_path: Path, items: list[str]) -> tuple[ZoomReadOutput, llm.LLMResult]:
    """Read the listed items' prices again from full-resolution tiles."""
    content: list[dict] = []
    for n, tile in enumerate(tiles(full_path), start=1):
        content.append({"type": "text", "text": f"Tile {n}:"})
        content.append(image_block(tile))
    listing = "\n".join(f"- {item}" for item in items)
    content.append({"type": "text", "text": llm.untrusted(
        "items to re-read", listing) + "\nCopy each item's price exactly as written."})
    result = llm.structured(ZoomReadOutput, [{"role": "user", "content": content}],
                            system=prompts.ZOOM_SYSTEM, vision=True, effort="medium",
                            max_tokens=8000)
    return result.parsed, result
