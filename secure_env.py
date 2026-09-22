from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


SECURE_ENV_FILE = Path(__file__).with_name("rollypay_secrets.enc")


def _bot_token() -> str:
    return (
        os.getenv("BOT_TOKEN", "").strip()
        or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        or os.getenv("API_TOKEN", "").strip()
        or os.getenv("TOKEN", "").strip()
    )


def load_secure_env() -> None:
    if not SECURE_ENV_FILE.exists():
        return

    token = _bot_token()
    if not token:
        return

    digest = hashlib.sha256(
        ("MGN_VPN_SECURE_ENV_V1:" + token).encode("utf-8")
    ).digest()
    key = base64.urlsafe_b64encode(digest)

    try:
        payload = Fernet(key).decrypt(
            SECURE_ENV_FILE.read_bytes().strip()
        )
        data = json.loads(payload.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError):
        return

    if not isinstance(data, dict):
        return

    for name, value in data.items():
        if (
            isinstance(name, str)
            and isinstance(value, str)
            and name.startswith("ROLLYPAY_")
            and value
        ):
            os.environ.setdefault(name, value)
