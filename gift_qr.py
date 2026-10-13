"""MGN VPN gift QR payload and scannable PNG.

The QR holds a one-time bearer-token link, not the recipient's identity.
Never log its contents; send the PNG only to an authorized administrator.
"""
from __future__ import annotations

import base64
import io
import re
from urllib.parse import parse_qs, urlsplit

import qrcode
from qrcode.constants import ERROR_CORRECT_Q

GIFT_PREFIX = "g_"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")
_BOT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


def parse_gift_token(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.startswith(GIFT_PREFIX):
        value = value[len(GIFT_PREFIX):]
    elif value.upper().startswith("MGN-GIFT:"):
        value = value[len("MGN-GIFT:"):]
    elif value.startswith("https://"):
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if host in {"t.me", "telegram.me", "www.t.me"}:
            args = parse_qs(parsed.query)
            value = args.get("start", [""])[0]
            if not value.startswith(GIFT_PREFIX):
                return None
            value = value[len(GIFT_PREFIX):]
        elif host == "mgnvpn.ru" and parsed.path.startswith("/gift/"):
            value = parsed.path.removeprefix("/gift/")
        else:
            return None
    return value if _TOKEN_RE.fullmatch(value) else None


def gift_link(bot_username: str, token: str) -> str:
    username = str(bot_username or "").lstrip("@")
    if not _BOT_RE.fullmatch(username) or parse_gift_token(token) != token:
        raise ValueError("Invalid gift bot username or token")
    return f"https://t.me/{username}?start={GIFT_PREFIX}{token}"


def gift_qr_png(link: str) -> bytes:
    if parse_gift_token(link) is None or not link.startswith("https://t.me/"):
        raise ValueError("Invalid gift deep link")
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_Q,
        box_size=10,
        border=4,
    )
    qr.add_data(link)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#101014", back_color="#FFFFFF")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def gift_qr_data_uri(link: str) -> str:
    return "data:image/png;base64," + base64.b64encode(gift_qr_png(link)).decode("ascii")
