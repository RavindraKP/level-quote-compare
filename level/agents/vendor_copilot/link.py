"""The per-vendor reply link: a signed token that names one RFx and one vendor.

The token is `<rfx_id>-<vendor_id>-<signature>`. The signature is an HMAC of the two ids
with a server secret, so a vendor cannot change the ids to open another vendor's page.
No database column is needed: the token can always be recomputed from the ids.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass

from level import db

DEFAULT_PUBLIC_URL = "http://localhost:8501"


@dataclass(frozen=True)
class LinkScope:
    """Everything a reply-link page is allowed to know about: one RFx, one vendor."""

    rfx_id: int
    vendor_id: int


def _secret() -> bytes:
    """LEVEL_LINK_SECRET, or a random secret kept next to the database (made once)."""
    if os.environ.get("LEVEL_LINK_SECRET"):
        return os.environ["LEVEL_LINK_SECRET"].encode()
    path = db.db_path().parent / ".link_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip().encode()


def _sign(rfx_id: int, vendor_id: int) -> str:
    digest = hmac.new(_secret(), f"{rfx_id}:{vendor_id}".encode(), hashlib.sha256)
    return digest.hexdigest()[:24]


def make_token(rfx_id: int, vendor_id: int) -> str:
    return f"{rfx_id}-{vendor_id}-{_sign(rfx_id, vendor_id)}"


def verify_token(token: str | None) -> LinkScope | None:
    """The scope a token grants, or None if it is malformed or forged."""
    try:
        rfx_part, vendor_part, sig = (token or "").split("-")
        rfx_id, vendor_id = int(rfx_part), int(vendor_part)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(rfx_id, vendor_id)):
        return None
    return LinkScope(rfx_id, vendor_id)


def link_url(rfx_id: int, vendor_id: int) -> str:
    """The full link put in the vendor's RFx email. LEVEL_PUBLIC_URL is the app's address."""
    base = os.environ.get("LEVEL_PUBLIC_URL", DEFAULT_PUBLIC_URL).rstrip("/")
    return f"{base}/?t={make_token(rfx_id, vendor_id)}"
