"""Device model hints from VPN subscription refresh requests.

H1 remains authoritative for device counts and access enforcement. A client may
supply a model/OS hint, but that hint alone never creates an active device.
Raw HWIDs are processed in memory and are not stored by this module.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from typing import Any


_HWID = re.compile(r"^[A-Za-z0-9._:=-]{6,160}$")
_GENERIC_NAMES = {"", "устройство", "подключённое устройство", "unknown", "unknown device", "device", "desktop"}


def device_fingerprint_key(user_id: int, hwid: str) -> str:
    """Namespace a device fingerprint to its account without persisting HWID."""
    normalized = str(hwid or "").strip().casefold()
    return hashlib.sha256(f"{int(user_id)}:{normalized}".encode("utf-8")).hexdigest()


def _clean(value: object, max_len: int = 80) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(
        ch for ch in value.strip()
        if unicodedata.category(ch)[0] != "C" and ch not in "<>{}\\"
    )
    text = " ".join(text.split())
    return text[:max_len]


def _header(headers: Mapping[str, str], name: str) -> str:
    # aiohttp CIMultiDict is case insensitive; support plain dicts in tests.
    return str(next((v for k, v in headers.items() if k.lower() == name.lower()), "") or "")


def subscription_device_hint(headers: Mapping[str, str]) -> dict[str, str] | None:
    """Accept only HWID-bearing requests, never a guess from IP or User-Agent."""
    hwid = _header(headers, "X-HWID").strip()
    if not _HWID.fullmatch(hwid):
        return None

    model = _clean(_header(headers, "X-Device-Model"))
    os_name = _clean(_header(headers, "X-Device-OS"), 40)
    os_version = _clean(_header(headers, "X-Ver-OS"), 24)
    if not model and not os_name:
        return None
    return {
        "hwid": hwid,
        "name": model or _fallback_name(os_name),
        "platform": " ".join(x for x in (os_name, os_version) if x),
    }


def _fallback_name(os_name: str) -> str:
    os_low = os_name.lower()
    if "windows" in os_low:
        return "Windows PC"
    if "ios" in os_low or "iphone" in os_low:
        return "iPhone"
    if "ipad" in os_low:
        return "iPad"
    if "android" in os_low:
        return "Android"
    if "mac" in os_low:
        return "Mac"
    if "linux" in os_low:
        return "Linux PC"
    return "Устройство"


def enrich_h1_devices(
    user_id: int,
    devices: list[dict[str, Any]],
    hints: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Overlay descriptive hints on panel-confirmed device rows only."""
    by_key = {str(h["fingerprint_key"]): h for h in hints}
    rows = [dict(row) for row in devices]
    for row in rows:
        fingerprint = str(row.get("fingerprint") or "").strip()
        # H1 installations also sometimes return their HWID as the row ID.
        identifier = str(row.get("id") or "").strip()
        hint = by_key.get(device_fingerprint_key(user_id, fingerprint)) if _HWID.fullmatch(fingerprint) else None
        if hint is None and _HWID.fullmatch(identifier):
            hint = by_key.get(device_fingerprint_key(user_id, identifier))
        # Older H1 versions only report device count. With a single device and
        # a single recently refreshed subscription profile the hint is usable
        # for the label, but still doesn't represent VPN connect activity.
        if hint is None and len(rows) == 1 and len(hints) == 1 and identifier == "count-1":
            hint = hints[0]
        if hint is None:
            continue
        original_name = str(row.get("name") or "").strip().lower()
        if hint.get("name") and (original_name in _GENERIC_NAMES or hint["name"] != "Устройство"):
            row["name"] = hint["name"]
        if hint.get("platform"):
            row["platform"] = hint["platform"]
        row["profile_updated_at"] = hint.get("updated_at")
        row["identity_source"] = "client-reported"
    return rows
