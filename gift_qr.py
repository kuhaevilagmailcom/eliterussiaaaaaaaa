"""MGN VPN gift QR payload and scannable PNG.

The QR holds a one-time bearer token, not the recipient's identity.
Never log its contents; send the PNG only to an authorized administrator.
"""
from __future__ import annotations

import base64
import io
import re

import qrcode
from qrcode.constants import ERROR_CORRECT_Q

GIFT_PREFIX = "MGN-GIFT:"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,80}$")


def parse_gift_token(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if value.upper().startswith(GIFT_PREFIX):
        value = value[len(GIFT_PREFIX):]
    elif value.startswith("https://mgnvpn.ru/gift/"):
        value = value.split("/gift/", 1)[1]
    elif value.startswith("https://mgnvpn.ru/app?gift="):
        value = value.split("?gift=", 1)[1]
    return value if _TOKEN_RE.fullmatch(value) else None


def gift_qr_payload(token: str) -> str:
    if parse_gift_token(token) != token:
        raise ValueError("Invalid gift token")
    return GIFT_PREFIX + token


def gift_qr_png(token: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_Q,
        box_size=10,
        border=4,
    )
    qr.add_data(gift_qr_payload(token))
    qr.make(fit=True)
    image = qr.make_image(fill_color="#101014", back_color="#FFFFFF")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def gift_qr_data_uri(token: str) -> str:
    return "data:image/png;base64," + base64.b64encode(gift_qr_png(token)).decode("ascii")
