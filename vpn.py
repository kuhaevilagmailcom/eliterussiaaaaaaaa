from __future__ import annotations

import asyncio
import json as json_module
import base64
import ipaddress
import math
import re
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
import logging
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import aiohttp


GB = 1024 ** 3
logger = logging.getLogger(__name__)


BASE_MGN_SERVERS: tuple[tuple[str, str], ...] = (
    ("nl", "🇳🇱 Нидерланды"),
    ("de", "🇩🇪 Германия"),
    ("fi", "🇫🇮 Финляндия"),
    ("lt", "🇱🇹 Литва"),
    ("us", "🇺🇸 США"),
)

LOCATION_LABELS = {
    "MGN-US2": "🇺🇸 США 2",
    "MGN-USA2": "🇺🇸 США 2",
    "MGN-NL": "🇳🇱 Нидерланды",
    "MGN-DE": "🇩🇪 Германия",
    "MGN-FI": "🇫🇮 Финляндия",
    "MGN-LT": "🇱🇹 Литва",
    "MGN-US": "🇺🇸 США",
    "MGN-PL": "🇵🇱 Польша",
    "MGN-PK": "🇵🇰 Пакистан",
    "MGN-SE": "🇸🇪 Швеция",
    "MGN-CH": "🇨🇭 Швейцария",
    "MGN-ES": "🇪🇸 Испания",
    "MGN-EE": "🇪🇪 Эстония",
    "MGN-MD": "🇲🇩 Молдова",
    "MGN-TR": "🇹🇷 Турция",
    "MGN-AL": "🇦🇱 Албания",
    "MGN-GE": "🇬🇪 Грузия",
    "MGN-RU": "🇷🇺 Москва",
    "MGN-MSK": "🇷🇺 Москва",
    "MGN-IN": "🇮🇳 Индия",
    "MGN-BY": "🇧🇾 Беларусь",
    "MGN-IL": "🇮🇱 Израиль",
}


def _location_label(value: str) -> str:
    decoded = unquote(value or "").upper()
    for marker, label in LOCATION_LABELS.items():
        if marker in decoded:
            return label

    host_markers = (
        ("NL1.H1CLOUD.NET", "🇳🇱 Нидерланды"),
        ("GERMANY-D5.H1CLOUD.NET", "🇩🇪 Германия"),
        ("DE5.H1CLOUD.NET", "🇩🇪 Германия"),
        ("FI5.H1CLOUD.NET", "🇫🇮 Финляндия"),
        ("LT3.H1CLOUD.NET", "🇱🇹 Литва"),
        ("US2.H1CLOUD.NET", "🇺🇸 США 2"),
        ("USA2", "🇺🇸 США 2"),
        ("US3.H1CLOUD.NET", "🇺🇸 США"),
        ("PL-D1.H1CLOUD.NET", "🇵🇱 Польша"),
        ("PAKISTAN", "🇵🇰 Пакистан"),
        ("KARACHI", "🇵🇰 Пакистан"),
        ("ISLAMABAD", "🇵🇰 Пакистан"),
        ("LAHORE", "🇵🇰 Пакистан"),
    )
    for marker, label in host_markers:
        if marker in decoded:
            return label

    # H1 Pakistan hosts are not consistent between panel versions. Keep the
    # client-facing name stable even when the raw node has a generated name.
    try:
        host = (urlsplit(value).hostname or "").upper()
    except ValueError:
        host = ""
    if host.endswith(".H1CLOUD.NET") and (
        host.startswith("PK")
        or host.startswith("PAK")
        or ".PK" in host
    ):
        return "🇵🇰 Пакистан"
    return ""


def _flagged_h1_node_name(value: str) -> str:
    """Return a readable H1 location with a country flag when it can be inferred."""
    raw = unquote(str(value or "")).strip()
    if not raw:
        return ""

    upper = raw.upper()
    countries: tuple[tuple[tuple[str, ...], str, str], ...] = (
        (("FINLAND", "ФИНЛЯНД", "MGN-FI"), "🇫🇮", "Финляндия"),
        (("POLAND", "ПОЛЬШ", "MGN-PL"), "🇵🇱", "Польша"),
        (("NETHERLAND", "НИДЕРЛ", "MGN-NL"), "🇳🇱", "Нидерланды"),
        (("GERMANY", "ГЕРМАН", "MGN-DE"), "🇩🇪", "Германия"),
        (("SWEDEN", "ШВЕЦ", "MGN-SE"), "🇸🇪", "Швеция"),
        (("SWITZERLAND", "ШВЕЙЦ", "MGN-CH"), "🇨🇭", "Швейцария"),
        (("SPAIN", "ИСПАН", "MGN-ES"), "🇪🇸", "Испания"),
        (("LITHUANIA", "ЛИТВ", "MGN-LT"), "🇱🇹", "Литва"),
        (("ESTONIA", "ЭСТОН", "MGN-EE"), "🇪🇪", "Эстония"),
        (("MOLDOVA", "МОЛДОВ", "MGN-MD"), "🇲🇩", "Молдова"),
        (("TURKEY", "TURKIYE", "ТУРЦ", "MGN-TR"), "🇹🇷", "Турция"),
        (("ALBANIA", "АЛБАН", "MGN-AL"), "🇦🇱", "Албания"),
        (("GEORGIA", "ГРУЗ", "MGN-GE"), "🇬🇪", "Грузия"),
        (("MOSCOW", "МОСКВ", "MGN-MSK", "MGN-RU"), "🇷🇺", "Москва"),
        (("INDIA", "ИНДИ", "MGN-IN"), "🇮🇳", "Индия"),
        (("BELARUS", "БЕЛАР", "MGN-BY"), "🇧🇾", "Беларусь"),
        (("ISRAEL", "ИЗРАИЛ", "MGN-IL"), "🇮🇱", "Израиль"),
        (("PAKISTAN", "ПАКИСТ", "KARACHI", "ISLAMABAD", "LAHORE", "MGN-PK"), "🇵🇰", "Пакистан"),
        (("UNITED STATES", "USA", "США", "MGN-US"), "🇺🇸", "США"),
    )

    for markers, flag, russian_name in countries:
        matched = next((token for token in markers if token in upper), "")
        if not matched:
            continue

        suffix = ""
        # Preserve useful H1 variants such as "-1", "-Премиум" or "-боты-2".
        match = re.search(
            r"(?:-|_|\s)(\d+|PREMIUM|ПРЕМИУМ|BOTS?(?:[-_\s]?\d+)?|БОТЫ?(?:[-_\s]?\d+)?)",
            upper,
        )
        if match:
            suffix_raw = match.group(1).replace("_", "-").replace(" ", "-")
            translations = {
                "PREMIUM": "Премиум",
                "BOTS": "боты",
                "BOT": "боты",
            }
            suffix = translations.get(suffix_raw, suffix_raw)
            if suffix.startswith("BOTS-") or suffix.startswith("BOT-"):
                suffix = "боты-" + suffix.split("-", 1)[1]
            elif suffix.startswith("БОТ"):
                suffix = suffix_raw.lower()
            elif suffix == "ПРЕМИУМ":
                suffix = "Премиум"
            suffix = f"-{suffix}"

        return f"{flag} {russian_name}{suffix}"

    # Short H1 host/node codes, e.g. fi5.h1cloud.net or pl-2.
    code_map = {
        "FI": ("🇫🇮", "Финляндия"),
        "PL": ("🇵🇱", "Польша"),
        "NL": ("🇳🇱", "Нидерланды"),
        "DE": ("🇩🇪", "Германия"),
        "SE": ("🇸🇪", "Швеция"),
        "CH": ("🇨🇭", "Швейцария"),
        "ES": ("🇪🇸", "Испания"),
        "LT": ("🇱🇹", "Литва"),
        "EE": ("🇪🇪", "Эстония"),
        "MD": ("🇲🇩", "Молдова"),
        "TR": ("🇹🇷", "Турция"),
        "AL": ("🇦🇱", "Албания"),
        "GE": ("🇬🇪", "Грузия"),
        "IN": ("🇮🇳", "Индия"),
        "BY": ("🇧🇾", "Беларусь"),
        "IL": ("🇮🇱", "Израиль"),
        "PK": ("🇵🇰", "Пакистан"),
        "US": ("🇺🇸", "США"),
    }
    code_match = re.search(r"(?:^|[^A-Z0-9])(FI|PL|NL|DE|SE|CH|ES|LT|EE|MD|TR|AL|GE|IN|BY|IL|PK|US)[-_]?(\d+)?(?:[^A-Z0-9]|$)", upper)
    if code_match:
        flag, russian_name = code_map[code_match.group(1)]
        number = code_match.group(2)
        return f"{flag} {russian_name}{'-' + number if number else ''}"

    return ""


def prettify_subscription_payload(payload: bytes) -> tuple[bytes, int]:
    """Normalize H1 node names and return a standard base64 subscription."""

    raw_text = payload.decode("utf-8", errors="ignore").strip()
    if not raw_text:
        return payload, 0

    decoded_text = raw_text
    if "vless://" not in raw_text.lower():
        compact = "".join(raw_text.split())
        if compact:
            padded = compact + "=" * (-len(compact) % 4)
            for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                try:
                    candidate = decoder(padded.encode()).decode("utf-8")
                except Exception:
                    continue
                if "vless://" in candidate.lower():
                    decoded_text = candidate
                    break

    count = 0
    output: list[str] = []
    used_labels: dict[str, int] = {}

    for raw_line in decoded_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.lower().startswith("vless://"):
            smart_recommended = "MGN-SMART" in unquote(line or "").upper()
            label = _location_label(line)
            if label:
                base_label = label
                used_labels[base_label] = used_labels.get(base_label, 0) + 1
                suffix = used_labels[base_label]
                if suffix > 1:
                    if base_label == "🇺🇸 США" and suffix == 2:
                        label = "🇺🇸 США 2"
                    else:
                        label = f"{label} · {suffix}"
                if smart_recommended:
                    label = f"⚡ Рекомендуемый · {label}"
                line = line.split("#", 1)[0] + "#" + quote(label, safe="")
            count += 1
        output.append(line)

    rendered = "\n".join(output).encode("utf-8")
    # A base64-encoded newline list is the common subscription format shared
    # by Happ, Hiddify and v2rayNG. Always returning it avoids client-specific
    # behavior when H1 alternates between encoded and plain responses.
    return base64.b64encode(rendered), count


@dataclass
class VpnState:
    subscription_url: str
    server: str
    traffic_used_gb: float
    traffic_limit_gb: float
    devices: list[dict[str, Any]]


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_device_list: bool = False
    supports_device_removal: bool = False
    supports_device_reset: bool = False
    supports_federation: bool = False
    supports_subscription_proxy: bool = False


class VpnProvider:
    service_ready: bool = True
    mode_name: str = "vpn"
    capabilities = ProviderCapabilities()

    async def provision(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        raise NotImplementedError

    async def reset_devices(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def fetch_subscription(
        self,
        user: dict[str, Any],
    ) -> tuple[bytes, dict[str, str]]:
        raise RuntimeError("Provider does not expose subscription payloads")

    async def health(self) -> dict[str, Any]:
        """Provider readiness signal. Concrete providers may perform a real upstream check."""
        return {"ok": bool(self.service_ready), "mode": self.mode_name}

    async def server_diagnostics(
        self,
        sample_user: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Read-only server inventory for the admin panel."""
        started = asyncio.get_running_loop().time()
        try:
            health = await asyncio.wait_for(self.health(), timeout=3.0)
            available = bool(health.get("ok", True)) if isinstance(health, dict) else True
            error = ""
        except Exception as exc:
            available = False
            error = type(exc).__name__
        latency_ms = int(max(0.0, (asyncio.get_running_loop().time() - started) * 1000))
        return {
            "provider": self.mode_name,
            "discovery_ok": True,
            "discovery_error": "",
            "sources": {},
            "servers": [
                {
                    "kind": "main",
                    "name": str(getattr(self, "server_name", "MGN VPN") or "MGN VPN"),
                    "id": "main",
                    "available": available,
                    "latency_ms": latency_ms,
                    "check": "health",
                    "error": error,
                }
            ],
        }

    async def close(self) -> None:
        return None


class DemoVpnProvider(VpnProvider):
    service_ready = False
    mode_name = "demo"

    def __init__(self, base_url: str, server_name: str):
        self.base_url = base_url
        self.server_name = server_name

    def _state(self, user: dict[str, Any]) -> VpnState:
        return VpnState(
            # Demo mode intentionally does not expose a fake connection URL.
            # As soon as a real provider is configured, handlers use the
            # provider's actual subscription URL automatically.
            subscription_url="",
            server=self.server_name,
            traffic_used_gb=0.0,
            traffic_limit_gb=float(user["traffic_limit_gb"] or 0),
            devices=[],
        )

    async def provision(self, user: dict[str, Any]) -> VpnState:
        return self._state(user)

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        return self._state(user)

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        return None


class WebhookVpnProvider(VpnProvider):
    capabilities = ProviderCapabilities(
        supports_device_list=True,
        supports_device_removal=True,
    )
    service_ready = True
    mode_name = "webhook"

    def __init__(self, api_url: str, api_token: str):
        if not api_url:
            raise RuntimeError("VPN_API_URL is required for VPN_MODE=webhook")
        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self.session.request(
            method,
            f"{self.api_url}{path}",
            json=json,
        ) as response:
            response.raise_for_status()
            if response.status == 204:
                return {}
            return await response.json()

    @staticmethod
    def _parse(data: dict[str, Any]) -> VpnState:
        return VpnState(
            subscription_url=str(data.get("subscription_url", "")),
            server=str(data.get("server", "MGN VPN")),
            traffic_used_gb=float(data.get("traffic_used_gb", 0)),
            traffic_limit_gb=float(data.get("traffic_limit_gb", 0)),
            devices=list(data.get("devices") or []),
        )

    async def provision(self, user: dict[str, Any]) -> VpnState:
        data = await self._request(
            "POST",
            "/subscriptions",
            json={
                "telegram_id": user["telegram_id"],
                "token": user["sub_token"],
                "expires_at": user["subscription_until"],
                "traffic_limit_gb": user["traffic_limit_gb"],
                "max_devices": user["max_devices"],
            },
        )
        return self._parse(data)

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        data = await self._request(
            "GET",
            f'/subscriptions/{user["telegram_id"]}',
        )
        return self._parse(data)

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        await self._request(
            "DELETE",
            f'/subscriptions/{user["telegram_id"]}/devices/{quote(device_id, safe="")}',
        )

    async def close(self) -> None:
        await self.session.close()


class H1CloudVpnProvider(VpnProvider):
    capabilities = ProviderCapabilities(
        supports_device_list=True,
        supports_device_removal=False,
        supports_device_reset=True,
        supports_federation=True,
        supports_subscription_proxy=True,
    )
    """Native provider for H1/VLESS Panel, including federated locations."""

    service_ready = True
    mode_name = "h1cloud"

    def __init__(
        self,
        api_url: str,
        api_token: str,
        subscription_template: str,
        server_name: str,
        verify_ssl: bool = True,
        ca_file: str = "",
        subscription_hosts: tuple[str, ...] = (".h1cloud.net",),
        allow_insecure: bool = False,
    ):
        if not api_url:
            raise RuntimeError("H1_API_URL is required for VPN_MODE=h1cloud")
        if not api_token:
            raise RuntimeError("H1_API_TOKEN is required for VPN_MODE=h1cloud")

        parsed_api = urlsplit(api_url)
        api_host = (parsed_api.hostname or "").lower().rstrip(".")
        legacy_h1_http = (
            parsed_api.scheme == "http"
            and (api_host == "h1cloud.net" or api_host.endswith(".h1cloud.net"))
        )
        if parsed_api.scheme not in {"http", "https"}:
            raise RuntimeError("H1_API_URL must use HTTP or HTTPS")
        if parsed_api.scheme == "http" and not (allow_insecure or legacy_h1_http):
            raise RuntimeError(
                "H1_API_URL must use HTTPS unless it is a legacy *.h1cloud.net node"
            )
        if parsed_api.scheme == "https" and not verify_ssl and not allow_insecure:
            raise RuntimeError("H1 TLS verification cannot be disabled")

        # Existing H1Cloud nodes used by MGN expose their panel API over plain
        # HTTP on a dedicated port. Keep those legacy *.h1cloud.net endpoints
        # working without forcing an extra hosting environment flag, while
        # still rejecting arbitrary third-party HTTP endpoints.
        effective_verify_ssl = bool(verify_ssl and parsed_api.scheme == "https")
        if legacy_h1_http:
            logger.warning(
                "Legacy H1Cloud HTTP API endpoint is in use; migrate this node to HTTPS when available"
            )

        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.subscription_template = subscription_template.strip()
        self.server_name = server_name
        self.allow_insecure = bool(allow_insecure)
        self.verify_ssl = effective_verify_ssl
        self.ssl_context: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=ca_file or None)
            if effective_verify_ssl
            else False
        )
        configured_hosts = {
            str(host).strip().lower().rstrip(".")
            for host in subscription_hosts
            if str(host).strip()
        }
        if parsed_api.hostname:
            configured_hosts.add(parsed_api.hostname.lower().rstrip("."))
        template_host = urlsplit(subscription_template).hostname
        if template_host:
            configured_hosts.add(template_host.lower().rstrip("."))
        self.subscription_hosts = tuple(sorted(configured_hosts))
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )
        # Never reuse the API session for public subscription URLs:
        # its Authorization header must not leak to another host.
        self.public_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={
                "Accept": "text/plain,*/*",
                "User-Agent": "MGN-VPN/1.0",
            },
        )
        # Endpoint health is deliberately conservative: one or two failed
        # probes must never remove a user's working country from an existing
        # subscription. Only repeated fresh failures are eligible for removal.
        self._endpoint_health_cache: dict[tuple[str, int], tuple[float, float | None]] = {}
        self._endpoint_failure_streak: dict[tuple[str, int], int] = {}

    @staticmethod
    def _name(user: dict[str, Any]) -> str:
        private_id = str(user.get("vpn_client_id") or "").strip()
        return f"mgn_{private_id}" if private_id else f'mgn_{int(user["telegram_id"])}'

    @staticmethod
    def _desired_expiry(user: dict[str, Any]) -> int:
        value = user.get("subscription_until")
        if not value:
            return 0
        try:
            return int(datetime.fromisoformat(str(value)).timestamp())
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _expiry_timestamp(client: dict[str, Any] | None) -> int:
        if not isinstance(client, dict):
            return 0
        value = (
            client.get("expires_at")
            or client.get("expiry_time")
            or client.get("expiryTime")
            or client.get("expire")
        )
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            try:
                return int(float(raw))
            except ValueError:
                try:
                    return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    return 0
        return 0

    @staticmethod
    def _days_until(expires_at: int, *, since: int | None = None) -> int:
        start = int(datetime.now().timestamp()) if since is None else int(since)
        return max(1, math.ceil((int(expires_at) - start) / 86400))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> dict[str, Any] | None:
        async with self.session.request(
            method,
            f"{self.api_url}{path}",
            json=json,
            ssl=self.ssl_context,
        ) as response:
            raw = await response.text()
            data: dict[str, Any] = {}
            if raw.strip():
                try:
                    parsed = json_module.loads(raw)
                except (ValueError, TypeError):
                    parsed = None
                if isinstance(parsed, dict):
                    data = parsed

            if allow_missing and (
                response.status == 404
                or (
                    data.get("ok") is False
                    and str(data.get("error") or "") == "user_not_found"
                )
            ):
                return None

            if response.status >= 400:
                message = str(data.get("error") or data.get("message") or "")
                if not message and raw.strip():
                    message = raw.strip()[:300]
                raise RuntimeError(
                    f"H1Cloud API HTTP {response.status}: {message or path}"
                )

            # DELETE/reset endpoints may legitimately return 204 or an empty body.
            if not raw.strip():
                return {}

            if not data:
                raise RuntimeError("H1Cloud returned invalid JSON")
            if data.get("ok") is False:
                raise RuntimeError(
                    f'H1Cloud API error: {data.get("error") or "unknown"}'
                )
            return data

    @staticmethod
    def _extract_client(data: dict[str, Any] | None) -> dict[str, Any] | None:
        """Unwrap the response variants used by H1Cloud installations."""

        current: Any = data
        for _depth in range(5):
            if not isinstance(current, dict) or not current:
                return None
            if current.get("name") or current.get("uuid") or current.get("links"):
                return dict(current)
            nested = next(
                (
                    current.get(key)
                    for key in ("client", "data", "result", "item")
                    if isinstance(current.get(key), dict)
                ),
                None,
            )
            if nested is None:
                return None
            current = nested
        return None

    async def _get_client(
        self,
        name: str,
        *,
        prefix: str = "",
    ) -> dict[str, Any] | None:
        data = await self._request(
            "GET",
            f"{prefix}/clients/{quote(name, safe='')}",
            allow_missing=True,
        )
        return self._extract_client(data)

    async def _inbound_ids(self, *, prefix: str = "") -> list[str]:
        data = await self._request("GET", f"{prefix}/inbounds")

        raw = data.get("inbounds") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict):
            for key in ("data", "result"):
                nested = data.get(key)
                if isinstance(nested, dict) and nested.get("inbounds") is not None:
                    raw = nested.get("inbounds")
                    break

        candidates: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(value: Any, label: str = "") -> None:
            inbound_id = str(value or "").strip()
            if inbound_id and inbound_id not in seen:
                seen.add(inbound_id)
                candidates.append((inbound_id, str(label or "")))

        if isinstance(raw, dict):
            for map_key, item in raw.items():
                if isinstance(item, dict):
                    inbound_id = (
                        item.get("id")
                        or item.get("inbound_id")
                        or item.get("inboundId")
                        or map_key
                    )
                    label = str(
                        item.get("remark")
                        or item.get("name")
                        or item.get("tag")
                        or ""
                    )
                    add(inbound_id, label)
                elif isinstance(item, (list, tuple)) and item:
                    label = " ".join(str(value) for value in item[1:] if value is not None)
                    add(item[0], label)
                elif isinstance(item, (str, int)):
                    add(item if str(item).strip() else map_key)
                else:
                    add(map_key)

        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    inbound_id = (
                        item.get("id")
                        or item.get("inbound_id")
                        or item.get("inboundId")
                    )
                    label = str(
                        item.get("remark")
                        or item.get("name")
                        or item.get("tag")
                        or ""
                    )
                    if inbound_id is not None:
                        add(inbound_id, label)
                elif isinstance(item, (list, tuple)) and item:
                    label = " ".join(str(value) for value in item[1:] if value is not None)
                    add(item[0], label)
                elif isinstance(item, (str, int)):
                    add(item)

        if not candidates:
            item_shape = "none"
            if isinstance(raw, list):
                item_shape = "list(empty)" if not raw else type(raw[0]).__name__
            elif isinstance(raw, dict):
                item_shape = f"map(keys={list(raw.keys())[:12]})"
            else:
                item_shape = type(raw).__name__
            logger.warning(
                "H1Cloud /inbounds unsupported response for %s; item_shape=%s",
                prefix or "main",
                item_shape,
            )
            raise RuntimeError(
                f"H1Cloud panel returned no usable inbounds for {prefix or 'main'}"
            )

        preferred = [
            inbound_id
            for inbound_id, label in candidates
            if "MGN-" in label.upper()
        ]
        selected = preferred or [inbound_id for inbound_id, _label in candidates]

        if len(selected) > 1 and preferred:
            logger.warning(
                "H1Cloud location %s has %s MGN inbounds; using all selected IDs=%s",
                prefix or "main",
                len(selected),
                selected,
            )
        return selected

    @staticmethod
    def _federation_collection(
        data: dict[str, Any] | None,
        *keys: str,
    ) -> list[Any]:
        """Read H1 federation lists across old/new response envelopes."""
        if not isinstance(data, dict):
            return []

        queue: list[dict[str, Any]] = [data]
        visited: set[int] = set()
        while queue:
            current = queue.pop(0)
            marker = id(current)
            if marker in visited:
                continue
            visited.add(marker)

            for key in keys:
                value = current.get(key)
                if isinstance(value, list):
                    return list(value)

            for key in ("data", "result", "payload", "federation"):
                nested = current.get(key)
                if isinstance(nested, dict):
                    queue.append(nested)
                elif isinstance(nested, list):
                    return list(nested)
        return []

    async def _federated_nodes(self) -> list[dict[str, Any]]:
        """Merge every H1 federation store and tolerate response-shape changes."""
        nodes: list[dict[str, Any]] = []
        index_by_key: dict[tuple[str, str], int] = {}

        def add_value(value: Any, proxy_kind: str) -> None:
            if isinstance(value, dict):
                node = dict(value)
                node_id = self._node_id(node)
            else:
                node_id = str(value or "").strip()
                node = {"id": node_id}

            key = (proxy_kind, node_id)
            if not node_id:
                return

            node["proxy_kind"] = proxy_kind
            existing_index = index_by_key.get(key)
            if existing_index is not None:
                # /fed/link often gives only an ID while /fed/lagg later gives
                # country/name/host. Keep the richer metadata for diagnostics.
                current = dict(nodes[existing_index])
                for field, field_value in node.items():
                    if field_value not in (None, "", [], {}):
                        current[field] = field_value
                nodes[existing_index] = current
                return

            index_by_key[key] = len(nodes)
            nodes.append(node)

        async def load_linked() -> None:
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/link"),
                    timeout=2.5,
                )
                raw = self._federation_collection(
                    data,
                    "links",
                    "nodes",
                    "items",
                    "servers",
                    "services",
                )
                for value in raw:
                    add_value(value, "lproxy")
            except Exception as exc:
                logger.warning(
                    "H1Cloud /fed/link unavailable: %s",
                    str(exc).strip() or type(exc).__name__,
                )

        async def load_registry() -> None:
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/registry"),
                    timeout=2.5,
                )
                raw = self._federation_collection(
                    data,
                    "nodes",
                    "items",
                    "servers",
                    "links",
                )
                for value in raw:
                    add_value(value, "proxy")
            except Exception as exc:
                logger.warning(
                    "H1Cloud /fed/registry unavailable: %s",
                    str(exc).strip() or type(exc).__name__,
                )

        async def load_lagg() -> None:
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/lagg", allow_missing=True),
                    timeout=4.0,
                )
                raw = self._federation_collection(
                    data,
                    "nodes",
                    "items",
                    "servers",
                    "links",
                )
                for value in raw:
                    add_value(value, "lproxy")
            except Exception as exc:
                logger.warning(
                    "H1Cloud /fed/lagg unavailable: %s",
                    str(exc).strip() or type(exc).__name__,
                )

        await asyncio.gather(load_linked(), load_registry(), load_lagg())

        logger.info(
            "H1Cloud federation discovery: %s remote node(s): %s",
            len(nodes),
            [
                f"{node.get('proxy_kind')}:{self._node_id(node)}"
                for node in nodes
            ],
        )
        return nodes

    @staticmethod
    def _node_prefix(node: dict[str, Any]) -> str:
        node_id = H1CloudVpnProvider._node_id(node)
        if not node_id:
            return ""
        kind = str(node.get("proxy_kind") or "lproxy").strip()
        route = "proxy" if kind == "proxy" else "lproxy"
        return f"/fed/{route}/{quote(node_id, safe='')}"

    @staticmethod
    def _node_id(node: dict[str, Any]) -> str:
        for key in (
            "node_id",
            "id",
            "server_id",
            "sid",
            "service_id",
            "serviceId",
            "billing_id",
            "billingId",
        ):
            value = node.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _first_vless(client: dict[str, Any]) -> str:
        links = client.get("links")
        if isinstance(links, dict):
            for value in links.values():
                if isinstance(value, str) and value.startswith("vless://"):
                    return value
        if isinstance(links, list):
            for value in links:
                if isinstance(value, str) and value.startswith("vless://"):
                    return value
        link = client.get("link")
        if isinstance(link, str) and link.startswith("vless://"):
            return link
        return ""

    @staticmethod
    def _client_vless_links(client: dict[str, Any] | None) -> list[str]:
        if not isinstance(client, dict):
            return []

        found: list[str] = []
        seen: set[str] = set()

        def add(value: Any) -> None:
            if isinstance(value, str):
                normalized = value.strip()
                if normalized.lower().startswith("vless://") and normalized not in seen:
                    seen.add(normalized)
                    found.append(normalized)
                return
            if isinstance(value, dict):
                for nested in value.values():
                    add(nested)
                return
            if isinstance(value, (list, tuple)):
                for nested in value:
                    add(nested)

        # H1Cloud versions expose links as strings, maps, lists, or nested
        # inbound objects. Traverse only the link-bearing response fields.
        for key in ("links", "link", "inbound_links", "inboundLinks"):
            add(client.get(key))

        return found

    @staticmethod
    def _subscription_vless_links(payload: bytes) -> list[str]:
        text = payload.decode("utf-8", errors="ignore").strip()
        if not text:
            return []

        decoded = text
        if "vless://" not in text.lower():
            compact = "".join(text.split())
            if compact:
                padded = compact + "=" * (-len(compact) % 4)
                for decoder in (base64.b64decode, base64.urlsafe_b64decode):
                    try:
                        candidate = decoder(padded.encode()).decode(
                            "utf-8",
                            errors="ignore",
                        )
                    except Exception:
                        continue
                    if "vless://" in candidate.lower():
                        decoded = candidate
                        break

        result: list[str] = []
        seen: set[str] = set()
        for raw in decoded.splitlines():
            line = raw.strip()
            if line.lower().startswith("vless://") and line not in seen:
                seen.add(line)
                result.append(line)
        return result

    def _subscription_host_allowed(self, hostname: str) -> bool:
        host = hostname.lower().rstrip(".")
        return any(
            host == allowed or (allowed.startswith(".") and host.endswith(allowed))
            for allowed in self.subscription_hosts
        )

    async def _validate_subscription_url(self, value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        legacy_h1_http = (
            parsed.scheme == "http"
            and (host == "h1cloud.net" or host.endswith(".h1cloud.net"))
        )
        scheme_allowed = (
            parsed.scheme == "https"
            or legacy_h1_http
            or bool(getattr(self, "allow_insecure", False))
        )
        if (
            not scheme_allowed
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or not self._subscription_host_allowed(parsed.hostname)
        ):
            raise RuntimeError("H1 subscription URL is not allowed")
        try:
            addresses = await asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except OSError as exc:
            raise RuntimeError("H1 subscription host cannot be resolved") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0].split("%", 1)[0])
            if not ip.is_global:
                raise RuntimeError("H1 subscription host resolved to a private address")
        return value

    async def _fetch_public_subscription(self, value: str) -> bytes:
        current = await self._validate_subscription_url(value)
        for _redirect in range(4):
            async with self.public_session.get(
                current,
                allow_redirects=False,
                ssl=self.ssl_context if urlsplit(current).scheme == "https" else False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        raise RuntimeError("H1 subscription redirect is missing a location")
                    current = await self._validate_subscription_url(urljoin(current, location))
                    continue
                body = await response.read()
                if response.status >= 400:
                    raise RuntimeError(f"H1 subscription HTTP {response.status}")
                return body
        raise RuntimeError("H1 subscription has too many redirects")

    def _subscription_url(self, client: dict[str, Any]) -> str:
        for key in ("subscription_url", "sub_url", "subscription"):
            value = client.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value

        client_uuid = str(client.get("uuid") or "").strip()
        if (
            self.subscription_template.startswith(("http://", "https://"))
            and "{uuid}" in self.subscription_template
            and client_uuid
        ):
            return self.subscription_template.replace(
                "{uuid}",
                quote(client_uuid, safe=""),
            )

        return self._first_vless(client)

    async def _upsert_location(
        self,
        *,
        name: str,
        client_uuid: str,
        expires_at: int,
        traffic_limit: int,
        device_limit: int,
        prefix: str = "",
    ) -> dict[str, Any]:
        inbound_ids = await self._inbound_ids(prefix=prefix)
        logger.info(
            "H1Cloud location %s: %s usable inbound(s)",
            prefix or "main",
            len(inbound_ids),
        )
        existing = await self._get_client(name, prefix=prefix)

        if existing is None:
            # H1Cloud's public API accepts a duration, not an absolute expiry.
            # Sending expires_at makes the panel reject creation as bad_days.
            payload: dict[str, Any] = {
                "name": name,
                "uuid": client_uuid,
                "days": self._days_until(expires_at),
                "traffic_limit_gb": traffic_limit,
                "device_limit": device_limit,
                "manual": True,
                # H1 treats [] as "no standard channels", which produces no
                # VLESS links. Keep all standard channels selected; H1 itself
                # omits transports that are not configured on this node.
                "channels": ["main", "reality", "bs", "wscdn"],
                "inbound_ids": inbound_ids,
            }
            data = await self._request(
                "POST",
                f"{prefix}/create",
                json=payload,
            )
        else:
            existing_uuid = str(existing.get("uuid") or "").strip()
            if existing_uuid and existing_uuid != client_uuid:
                raise RuntimeError(
                    f"H1Cloud UUID mismatch for {name} at {prefix or 'main'}"
                )
            payload = {
                # Absolute expiry makes retries idempotent. PATCH with `days`
                # would add the same duration again after an uncertain reply.
                "expires_at": expires_at,
                "traffic_limit_gb": traffic_limit,
                "device_limit": device_limit,
                # Repair users created by older builds with channels=[].
                "channels": ["main", "reality", "bs", "wscdn"],
                "inbound_ids": inbound_ids,
            }
            data = await self._request(
                "PATCH",
                f"{prefix}/clients/{quote(name, safe='')}",
                json=payload,
            )

        client = self._extract_client(data)
        if client is None:
            client = await self._get_client(name, prefix=prefix)
        if client is None:
            raise RuntimeError(
                f"H1Cloud client {name} missing after upsert at {prefix or 'main'}"
            )
        return client

    async def provision(self, user: dict[str, Any]) -> VpnState:
        name = self._name(user)
        desired_expiry = self._desired_expiry(user)
        if desired_expiry <= int(datetime.now().timestamp()):
            desired_expiry = int(datetime.now().timestamp()) + 86400

        traffic_limit = max(0, int(user.get("traffic_limit_gb") or 0))
        device_limit = max(1, int(user.get("max_devices") or 1))

        main_existing = await self._get_client(name)
        client_uuid = (
            str(main_existing.get("uuid") or "").strip()
            if main_existing
            else ""
        ) or str(uuid4())

        # The main panel must succeed: it owns the canonical client/sub_url.
        await self._upsert_location(
            name=name,
            client_uuid=client_uuid,
            expires_at=desired_expiry,
            traffic_limit=traffic_limit,
            device_limit=device_limit,
        )

        nodes = await self._federated_nodes()
        remote_nodes = [
            node
            for node in nodes
            if self._node_id(node) and self._node_prefix(node)
        ]
        logger.info(
            "H1Cloud federation: %s connected remote node(s) for %s; nodes=%s",
            len(remote_nodes),
            name,
            [
                f"{node.get('proxy_kind')}:{self._node_id(node)}"
                for node in remote_nodes
            ],
        )

        async def sync_node(node: dict[str, Any]) -> tuple[str, str | None]:
            node_id = self._node_id(node)
            prefix = self._node_prefix(node)
            label = f"{node.get('proxy_kind')}:{node_id}"
            try:
                await asyncio.wait_for(
                    self._upsert_location(
                        name=name,
                        client_uuid=client_uuid,
                        expires_at=desired_expiry,
                        traffic_limit=traffic_limit,
                        device_limit=device_limit,
                        prefix=prefix,
                    ),
                    timeout=20.0,
                )
                logger.info(
                    "H1Cloud federation node %s synced for %s",
                    label,
                    name,
                )
                return label, None
            except asyncio.TimeoutError:
                return label, "timeout"
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                return label, message

        # Remote panels are independent. Sync them concurrently so a slow or
        # broken country cannot hold the whole purchase/Mini App for 20+ sec.
        results = await asyncio.gather(
            *(sync_node(node) for node in remote_nodes),
            return_exceptions=False,
        )
        errors = [
            f"{node_id}: {error}"
            for node_id, error in results
            if error
        ]
        if errors:
            logger.warning(
                "H1Cloud federation partial for %s: %s",
                name,
                "; ".join(errors),
            )

        return await self.get_state(user)

    @staticmethod
    def _devices_from_client(client: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Normalize the device shapes returned by different H1Cloud nodes."""
        if not isinstance(client, dict):
            return []

        raw: Any = None
        for key in ("devices", "hwids", "device_list", "deviceList"):
            value = client.get(key)
            if value:
                raw = value
                break

        if raw is None:
            for key in ("stats", "limits", "usage"):
                nested = client.get(key)
                if not isinstance(nested, dict):
                    continue
                for nested_key in ("devices", "hwids", "device_list", "deviceList"):
                    value = nested.get(nested_key)
                    if value:
                        raw = value
                        break
                if raw is not None:
                    break

        items: list[Any] = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = [
                (dict(value, id=value.get("id") or key) if isinstance(value, dict) else {"id": key, "name": value})
                for key, value in raw.items()
            ]

        devices: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                continue
            devices.append(
                {
                    "id": str(
                        item.get("id")
                        or item.get("device_id")
                        or item.get("deviceId")
                        or item.get("hwid")
                        or item.get("fingerprint")
                        or index
                    ),
                    "name": str(
                        item.get("name")
                        or item.get("device_name")
                        or item.get("deviceName")
                        or item.get("model")
                        or item.get("deviceModel")
                        or "Устройство"
                    ),
                    "platform": str(
                        item.get("platform")
                        or item.get("os")
                        or item.get("deviceOs")
                        or ""
                    ),
                    "fingerprint": str(
                        item.get("fingerprint")
                        or item.get("hwid")
                        or item.get("device_id")
                        or item.get("deviceId")
                        or ""
                    ),
                    "last_seen": (
                        item.get("last_seen")
                        or item.get("lastSeen")
                        or item.get("updated_at")
                    ),
                }
            )

        if devices:
            return devices

        # Some H1Cloud builds expose only a remembered-device count on the
        # client object. Preserve that count so the UI does not incorrectly
        # show 0/N after a device is already registered.
        count = 0
        for key in (
            "device_count",
            "devices_count",
            "current_devices",
            "current_count",
            "deviceCount",
        ):
            try:
                count = max(count, int(client.get(key) or 0))
            except (TypeError, ValueError):
                continue
        return [
            {
                "id": f"count-{index + 1}",
                "name": "Подключённое устройство",
                "platform": "",
                "fingerprint": "",
                "last_seen": None,
            }
            for index in range(max(0, count))
        ]

    @staticmethod
    def _merge_devices(
        groups: list[list[dict[str, Any]]],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                fingerprint = str(item.get("fingerprint") or "").strip().lower()
                device_id = str(item.get("id") or "").strip().lower()
                name = str(item.get("name") or "").strip().lower()
                platform = str(item.get("platform") or "").strip().lower()
                if fingerprint:
                    key = f"fp:{fingerprint}"
                elif device_id and not device_id.startswith("count-"):
                    key = f"id:{device_id}"
                else:
                    key = f"name:{name}|platform:{platform}"
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= max(1, int(limit)):
                    return merged
        return merged

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        name = self._name(user)
        client = await self._get_client(name)
        if client is None:
            raise RuntimeError("H1Cloud client does not exist yet")

        subscription_url = self._subscription_url(client)
        if not subscription_url:
            raise RuntimeError("H1Cloud client has no subscription URL")

        max_devices = max(1, int(user.get("max_devices") or 1))
        device_groups: list[list[dict[str, Any]]] = [
            self._devices_from_client(client)
        ]

        # A connected device may be remembered by the country node it actually
        # uses rather than by the main NL panel. When the main panel reports
        # fewer devices than the account allows, inspect federation nodes in
        # parallel and merge the same user's device records.
        if len(device_groups[0]) < max_devices:
            try:
                nodes = await asyncio.wait_for(self._federated_nodes(), timeout=2.0)
            except Exception:
                nodes = []

            async def load_remote_devices(node: dict[str, Any]) -> list[dict[str, Any]]:
                prefix = self._node_prefix(node)
                if not prefix:
                    return []
                try:
                    remote = await asyncio.wait_for(
                        self._get_client(name, prefix=prefix),
                        timeout=1.5,
                    )
                except Exception:
                    return []
                return self._devices_from_client(remote)

            if nodes:
                device_groups.extend(
                    await asyncio.gather(
                        *(load_remote_devices(node) for node in nodes),
                        return_exceptions=False,
                    )
                )

        devices = self._merge_devices(device_groups, limit=max_devices)

        used_gb = float(client.get("traffic_used_gb") or 0)
        limit_gb = float(
            client.get("traffic_limit_gb")
            or user.get("traffic_limit_gb")
            or 0
        )

        return VpnState(
            subscription_url=subscription_url,
            server=self.server_name,
            traffic_used_gb=used_gb,
            traffic_limit_gb=limit_gb,
            devices=devices,
        )

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        raise RuntimeError(
            "H1Cloud does not expose a documented per-device removal endpoint"
        )

    async def reset_devices(self, user: dict[str, Any]) -> VpnState:
        """Clear H1's remembered devices without changing the VPN UUID."""
        name = self._name(user)

        # H1/VLESS exposes an explicit reset-devices client action. This is
        # safer than deleting/recreating the whole client and keeps the same
        # subscription/UUID.
        await self._request(
            "PATCH",
            f"/clients/{quote(name, safe='')}/reset-devices",
        )

        nodes = await self._federated_nodes()
        remote_nodes = [
            node
            for node in nodes
            if self._node_id(node) and self._node_prefix(node)
        ]

        async def reset_remote(node: dict[str, Any]) -> tuple[str, str | None]:
            node_id = self._node_id(node)
            prefix = self._node_prefix(node)
            label = f"{node.get('proxy_kind')}:{node_id}"
            try:
                await asyncio.wait_for(
                    self._request(
                        "PATCH",
                        f"{prefix}/clients/{quote(name, safe='')}/reset-devices",
                        allow_missing=True,
                    ),
                    timeout=7.0,
                )
                return label, None
            except Exception as exc:
                return label, str(exc).strip() or type(exc).__name__

        results = await asyncio.gather(
            *(reset_remote(node) for node in remote_nodes),
            return_exceptions=False,
        )
        errors = [f"{node_id}: {error}" for node_id, error in results if error]
        if errors:
            logger.warning(
                "H1Cloud device reset partial for %s: %s",
                name,
                "; ".join(errors),
            )

        logger.info("H1Cloud remembered devices reset for %s", name)
        return await self.get_state(user)

    async def health(self) -> dict[str, Any]:
        data = await self._request("GET", "/health")
        return dict(data or {})

    @staticmethod
    def _diagnostic_node_label(
        node: dict[str, Any],
        *responses: dict[str, Any] | None,
    ) -> str:
        """Best-effort human label without exposing federation tokens."""
        values: list[str] = []
        for source in (node, *responses):
            if not isinstance(source, dict):
                continue
            for key in (
                "name",
                "label",
                "remark",
                "country",
                "country_code",
                "location",
                "region",
                "host",
                "hostname",
                "domain",
                "server",
                "node_id",
                "id",
                "server_id",
            ):
                value = source.get(key)
                if value is not None and str(value).strip():
                    values.append(str(value).strip())

        identity = " ".join(values)
        flagged = _flagged_h1_node_name(identity)
        if flagged:
            return flagged

        location = _location_label(identity)
        if location:
            return location

        node_id = H1CloudVpnProvider._node_id(node)
        for value in values:
            cleaned = str(value).strip()
            if cleaned and cleaned != node_id and len(cleaned) <= 64:
                inferred = _flagged_h1_node_name(cleaned)
                return inferred or f"🌐 {cleaned}"
        return f"🌐 Узел {node_id}" if node_id else "🌐 Удалённый узел"

    async def server_diagnostics(
        self,
        sample_user: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inspect H1 inventory without changing VPN clients."""
        loop = asyncio.get_running_loop()

        async def timed_request(
            path: str,
            *,
            timeout: float = 2.5,
            allow_missing: bool = False,
        ) -> tuple[dict[str, Any] | None, int, str]:
            started = loop.time()
            try:
                value = await asyncio.wait_for(
                    self._request("GET", path, allow_missing=allow_missing),
                    timeout=timeout,
                )
                elapsed = int(max(0.0, (loop.time() - started) * 1000))
                return (dict(value or {}) if value is not None else None), elapsed, ""
            except Exception as exc:
                elapsed = int(max(0.0, (loop.time() - started) * 1000))
                return None, elapsed, type(exc).__name__

        main_health, main_ms, main_error = await timed_request("/health")
        parsed_main_api = urlsplit(self.api_url)
        main_host = (parsed_main_api.hostname or "").strip()
        main_port = int(parsed_main_api.port or (443 if parsed_main_api.scheme == "https" else 80))
        main_label = _location_label(self.api_url) or str(self.server_name or "").strip()
        if not main_label:
            main_label = main_host or "Основной сервер"

        main_available = main_health is not None and not (
            isinstance(main_health, dict) and main_health.get("ok") is False
        )
        servers: list[dict[str, Any]] = [
            {
                "kind": "main",
                "name": main_label,
                "id": "main",
                "host": main_host,
                "port": main_port,
                "proxy_kind": "direct",
                "available": bool(main_available),
                "latency_ms": main_ms,
                "check": "health",
                "error": main_error,
            }
        ]

        async def source(
            path: str,
            *keys: str,
        ) -> tuple[str, dict[str, Any] | None, list[Any], str]:
            data, _elapsed, error = await timed_request(path, timeout=4.2)
            raw = self._federation_collection(data, *keys)
            return path, data, raw, error

        linked_result, registry_result, lagg_result = await asyncio.gather(
            source("/fed/link", "links", "nodes", "items", "servers", "services"),
            source("/fed/registry", "nodes", "items", "servers", "links"),
            source("/fed/lagg", "nodes", "items", "servers", "links"),
        )

        source_rows: dict[str, dict[str, Any]] = {}
        nodes: list[dict[str, Any]] = []
        node_index: dict[tuple[str, str], int] = {}

        def add_diag_node(value: Any, proxy_kind: str) -> None:
            if isinstance(value, dict):
                node = dict(value)
                node_id = self._node_id(node)
            else:
                node_id = str(value or "").strip()
                node = {"id": node_id}
            key = (proxy_kind, node_id)
            if not node_id:
                return
            node["proxy_kind"] = proxy_kind

            existing_index = node_index.get(key)
            if existing_index is not None:
                current = dict(nodes[existing_index])
                for field, field_value in node.items():
                    if field_value not in (None, "", [], {}):
                        current[field] = field_value
                nodes[existing_index] = current
                return

            node_index[key] = len(nodes)
            nodes.append(node)

        for result, kind in (
            (linked_result, "lproxy"),
            (registry_result, "proxy"),
            (lagg_result, "lproxy"),
        ):
            path, data, raw, error = result
            source_rows[path] = {
                "available": data is not None,
                "count": len(raw),
                "error": error,
            }
            for value in raw:
                add_diag_node(value, kind)

        async def inspect_remote(node: dict[str, Any]) -> dict[str, Any]:
            node_id = self._node_id(node)
            prefix = self._node_prefix(node)
            kind = str(node.get("proxy_kind") or "lproxy")

            health_data, health_ms, health_error = await timed_request(
                f"{prefix}/health",
                timeout=2.2,
                allow_missing=True,
            )
            if health_data is not None and health_data.get("ok") is not False:
                return {
                    "kind": "federation",
                    "name": self._diagnostic_node_label(node, health_data),
                    "id": node_id,
                    "proxy_kind": kind,
                    "available": True,
                    "configured": True,
                    "latency_ms": health_ms,
                    "check": "health",
                    "error": "",
                }

            # Some H1 nodes do not proxy /health but do proxy the panel. A
            # successful read-only /inbounds call is enough to mark the panel
            # path as reachable for diagnostics.
            started = loop.time()
            try:
                await asyncio.wait_for(
                    self._inbound_ids(prefix=prefix),
                    timeout=2.8,
                )
                inbound_ms = int(max(0.0, (loop.time() - started) * 1000))
                return {
                    "kind": "federation",
                    "name": self._diagnostic_node_label(node, health_data),
                    "id": node_id,
                    "proxy_kind": kind,
                    "available": True,
                    "configured": True,
                    "latency_ms": inbound_ms,
                    "check": "inbounds",
                    "error": "",
                }
            except Exception as exc:
                inbound_ms = int(max(0.0, (loop.time() - started) * 1000))
                error = type(exc).__name__ or health_error
                return {
                    "kind": "federation",
                    "name": self._diagnostic_node_label(node, health_data),
                    "id": node_id,
                    "proxy_kind": kind,
                    "available": False,
                    "configured": True,
                    "latency_ms": max(health_ms, inbound_ms),
                    "check": "health+inbounds",
                    "error": error or health_error or "unavailable",
                }

        if nodes:
            servers.extend(
                await asyncio.gather(
                    *(inspect_remote(node) for node in nodes),
                    return_exceptions=False,
                )
            )

        modern_sources_ok = bool(
            source_rows.get("/fed/link", {}).get("available")
            or source_rows.get("/fed/registry", {}).get("available")
        )
        fallback_ok = bool(source_rows.get("/fed/lagg", {}).get("available"))
        discovery_ok = modern_sources_ok or fallback_ok
        discovery_error = ""
        if not discovery_ok:
            errors = [
                str(item.get("error") or "")
                for item in source_rows.values()
                if item.get("error")
            ]
            discovery_error = ", ".join(dict.fromkeys(errors)) or "federation unavailable"

        # Federation API у H1 иногда пустой, хотя aggregate subscription реально
        # содержит рабочие страны. Если есть активный пользователь, читаем его
        # текущий main client + aggregate sub_url без provisioning/patch/create.
        subscription_links: list[str] = []
        if isinstance(sample_user, dict):
            try:
                sample_name = self._name(sample_user)
                sample_client = await asyncio.wait_for(
                    self._get_client(sample_name),
                    timeout=2.0,
                )
                if sample_client is not None:
                    subscription_links.extend(
                        self._client_vless_links(sample_client)
                    )
                    aggregate_url = str(
                        sample_client.get("sub_url")
                        or sample_client.get("subscription_url")
                        or sample_client.get("subscription")
                        or ""
                    ).strip()
                    if aggregate_url.startswith(("http://", "https://")):
                        try:
                            aggregate_body = await asyncio.wait_for(
                                self._fetch_public_subscription(aggregate_url),
                                timeout=4.5,
                            )
                            subscription_links.extend(
                                self._subscription_vless_links(aggregate_body)
                            )
                        except Exception as exc:
                            source_rows["subscription"] = {
                                "available": False,
                                "count": len(subscription_links),
                                "error": type(exc).__name__,
                            }
                    else:
                        source_rows["subscription"] = {
                            "available": bool(subscription_links),
                            "count": len(subscription_links),
                            "error": "sub_url_missing" if not subscription_links else "",
                        }
            except Exception as exc:
                source_rows["subscription"] = {
                    "available": False,
                    "count": 0,
                    "error": type(exc).__name__,
                }

        # Дедуплицируем реальные VLESS, но не отбрасываем медленные/неотвечающие.
        subscription_links = list(dict.fromkeys(subscription_links))
        if subscription_links:
            source_rows["subscription"] = {
                "available": True,
                "count": len(subscription_links),
                "error": "",
            }

        # Keep the real H1 inventory. The old fixed 7-country catalog silently
        # dropped every newer H1 location, making the admin panel disagree with
        # the subscription and with H1's own availability checks.
        for item in servers:
            item.setdefault("configured", bool(item.get("available")))

        existing_endpoints: dict[tuple[str, int], int] = {}
        for index, item in enumerate(servers):
            host = str(item.get("host") or "").lower().rstrip(".")
            port = int(item.get("port") or 0)
            if host and port:
                existing_endpoints[(host, port)] = index

        for link_index, link in enumerate(subscription_links, start=1):
            host = ""
            port = 0
            fragment = ""
            try:
                parsed = urlsplit(link)
                host = str(parsed.hostname or "").lower().rstrip(".")
                port = int(parsed.port or 0)
                fragment = unquote(str(parsed.fragment or "")).split("|", 1)[0].strip()
            except (TypeError, ValueError):
                pass

            label = _location_label(link)
            if not label:
                label = fragment or host or f"VLESS {link_index}"

            latency: float | None = None
            if host and port:
                try:
                    latency = await self._probe_vless_endpoint(host, port)
                except Exception:
                    latency = None

            endpoint = (host, port)
            existing_index = existing_endpoints.get(endpoint) if host and port else None
            payload = {
                "kind": "main" if host == main_host.lower().rstrip(".") else "federation",
                "name": label,
                "id": f"sub:{link_index}",
                "host": host,
                "port": port,
                "proxy_kind": "subscription",
                "available": latency is not None,
                "configured": True,
                "latency_ms": int(latency) if latency is not None else None,
                "check": "vless_tcp" if latency is not None else "subscription",
                "error": "" if latency is not None else "probe_unverified",
            }

            if existing_index is not None:
                current = dict(servers[existing_index])
                current.update(
                    {
                        "host": host,
                        "port": port,
                        "configured": True,
                        "available": bool(current.get("available")) or latency is not None,
                        "latency_ms": (
                            current.get("latency_ms")
                            if current.get("available")
                            else payload["latency_ms"]
                        ),
                        "check": (
                            current.get("check")
                            if current.get("available")
                            else payload["check"]
                        ),
                        "error": (
                            ""
                            if current.get("available") or latency is not None
                            else "probe_unverified"
                        ),
                    }
                )
                if not str(current.get("name") or "").strip():
                    current["name"] = label
                servers[existing_index] = current
            else:
                servers.append(payload)
                if host and port:
                    existing_endpoints[endpoint] = len(servers) - 1

        # Federation stores can expose the same physical node through both
        # proxy and lproxy. Collapse only exact endpoint/id duplicates; never
        # collapse different VLESS endpoints from the same country.
        deduped: list[dict[str, Any]] = []
        seen_inventory: set[tuple[str, str, int]] = set()
        for item in servers:
            host = str(item.get("host") or "").lower().rstrip(".")
            port = int(item.get("port") or 0)
            node_id = str(item.get("id") or "")
            if host and port:
                key = ("endpoint", host, port)
            else:
                key = ("node", node_id, 0)
            if key in seen_inventory:
                continue
            seen_inventory.add(key)
            deduped.append(item)

        name_counts: dict[str, int] = {}
        for item in deduped:
            base_name = str(item.get("name") or "").strip() or "H1Cloud"
            name_counts[base_name] = name_counts.get(base_name, 0) + 1
            occurrence = name_counts[base_name]
            if occurrence > 1:
                item["name"] = f"{base_name} · {occurrence}"

        def base_code(item: dict[str, Any]) -> str:
            identity = " ".join(
                str(item.get(key) or "")
                for key in ("name", "host", "id")
            )
            label = _location_label(identity) or _flagged_h1_node_name(identity)
            if "Нидерланды" in label:
                return "nl"
            if "Германия" in label:
                return "de"
            if "Финляндия" in label:
                return "fi"
            if "Литва" in label:
                return "lt"
            if "США" in label:
                return "us"
            return ""

        present_base = {
            code
            for item in deduped
            if (code := base_code(item))
        }
        for code, label in BASE_MGN_SERVERS:
            if code in present_base:
                continue
            deduped.append(
                {
                    "kind": "main" if code == "nl" else "federation",
                    "name": label,
                    "id": f"base:{code}",
                    "host": "",
                    "port": 0,
                    "proxy_kind": "direct" if code == "nl" else "federation",
                    "available": False,
                    "configured": True,
                    "latency_ms": None,
                    "check": "catalog_fallback",
                    "error": "temporarily_not_reported",
                }
            )

        # Keep the stable MGN base locations first, then any extra H1 nodes.
        base_order = {code: index for index, (code, _label) in enumerate(BASE_MGN_SERVERS)}
        deduped.sort(
            key=lambda item: (
                0 if base_code(item) in base_order else 1,
                base_order.get(base_code(item), 999),
                str(item.get("name") or ""),
            )
        )

        servers = deduped

        return {
            "provider": self.mode_name,
            "discovery_ok": discovery_ok,
            "discovery_error": discovery_error,
            "sources": source_rows,
            "servers": servers,
        }

    async def _probe_vless_endpoint(self, host: str, port: int) -> float | None:
        """Return TCP connect latency in ms, or None when the endpoint is unavailable."""
        endpoint = (str(host).lower().rstrip("."), int(port))
        loop = asyncio.get_running_loop()
        cache = getattr(self, "_endpoint_health_cache", None)
        if not isinstance(cache, dict):
            cache = {}
            self._endpoint_health_cache = cache

        now = loop.time()
        cached = cache.get(endpoint)
        if cached and now - float(cached[0]) <= 20.0:
            return cached[1]

        latency: float | None = None
        for timeout in (0.9, 1.4):
            writer = None
            try:
                started = loop.time()
                _reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(endpoint[0], endpoint[1]),
                    timeout=timeout,
                )
                latency = max(0.1, (loop.time() - started) * 1000.0)
                break
            except (OSError, asyncio.TimeoutError):
                continue
            finally:
                if writer is not None:
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

        cache[endpoint] = (loop.time(), latency)
        streaks = getattr(self, "_endpoint_failure_streak", None)
        if not isinstance(streaks, dict):
            streaks = {}
            self._endpoint_failure_streak = streaks
        if latency is None:
            streaks[endpoint] = min(20, int(streaks.get(endpoint, 0)) + 1)
        else:
            streaks.pop(endpoint, None)
        if len(cache) > 128:
            cutoff = loop.time() - 120.0
            self._endpoint_health_cache = {
                key: value
                for key, value in cache.items()
                if float(value[0]) >= cutoff
            }
        return latency

    async def _rank_live_vless_links(self, links: list[str]) -> list[str]:
        """Put verified endpoints first without ever deleting a user's configured country."""
        parsed_links: list[tuple[int, str, tuple[str, int] | None]] = []
        endpoints: set[tuple[str, int]] = set()
        for index, link in enumerate(links):
            endpoint: tuple[str, int] | None = None
            try:
                parsed = urlsplit(link)
                host = parsed.hostname
                port = parsed.port
                if host and port:
                    endpoint = (host.lower().rstrip("."), int(port))
                    endpoints.add(endpoint)
            except (TypeError, ValueError):
                endpoint = None
            parsed_links.append((index, link, endpoint))

        if not endpoints:
            return links

        endpoint_list = list(endpoints)
        latencies = await asyncio.gather(
            *(self._probe_vless_endpoint(host, port) for host, port in endpoint_list),
            return_exceptions=False,
        )
        latency_by_endpoint = dict(zip(endpoint_list, latencies))

        verified: list[tuple[float, int, str]] = []
        uncertain: list[tuple[int, int, str]] = []
        for index, link, endpoint in parsed_links:
            if endpoint is None:
                uncertain.append((0, index, link))
                continue
            latency = latency_by_endpoint.get(endpoint)
            if latency is not None:
                verified.append((float(latency), index, link))
                continue

            # BotHost cannot prove that a route is unusable from the subscriber's
            # phone/network. Never delete it. Repeated server-side failures only
            # demote it behind routes we have just verified.
            failure_streak = int(self._endpoint_failure_streak.get(endpoint, 0))
            uncertain.append((failure_streak, index, link))

        if not verified:
            logger.warning(
                "H1Cloud endpoint health probes found no verified endpoint; preserving original order for %s link(s)",
                len(links),
            )
            return links

        verified.sort(key=lambda item: (item[0], item[1]))
        uncertain.sort(key=lambda item: (item[0], item[1]))
        ranked = [item[2] for item in verified] + [item[2] for item in uncertain]
        logger.info(
            "H1Cloud smart selection: %s verified, %s preserved, best %.0f ms",
            len(verified),
            len(uncertain),
            verified[0][0],
        )

        # Mark only the verified fastest link. Happ can additionally measure
        # delay on the user's own network when Provider ID is configured.
        best = ranked[0]
        if "#" in best:
            base, fragment = best.split("#", 1)
            ranked[0] = f"{base}#{fragment}|MGN-SMART"
        else:
            ranked[0] = best + "#MGN-SMART"
        return ranked

    async def fetch_subscription(
        self,
        user: dict[str, Any],
    ) -> tuple[bytes, dict[str, str]]:
        """Build a fast, resilient MGN subscription for one active user."""
        name = self._name(user)
        standard_channels = ["main", "reality", "bs", "wscdn"]

        try:
            main = await asyncio.wait_for(self._get_client(name), timeout=2.0)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("H1Cloud main client lookup timed out") from exc

        if main is None:
            desired_expiry = self._desired_expiry(user)
            if desired_expiry <= int(datetime.now().timestamp()):
                desired_expiry = int(datetime.now().timestamp()) + 86400
            main = await asyncio.wait_for(
                self._upsert_location(
                    name=name,
                    client_uuid=str(uuid4()),
                    expires_at=desired_expiry,
                    traffic_limit=max(0, int(user.get("traffic_limit_gb") or 0)),
                    device_limit=max(1, int(user.get("max_devices") or 1)),
                ),
                timeout=5.5,
            )

        links: list[str] = []
        seen: set[str] = set()

        def add_many(values: list[str]) -> None:
            for value in values:
                if value and value not in seen:
                    seen.add(value)
                    links.append(value)

        # Direct links are the fastest fallback and make a first import usable
        # even when federation metadata is temporarily slow.
        add_many(self._client_vless_links(main))
        main_uuid = str((main or {}).get("uuid") or "").strip()

        # H1's own sub_url normally contains every linked country. Prefer it
        # before billing-node discovery: this removes the old 503-prone race
        # where /sub waited for several panel calls before fetching the actual
        # subscription.
        aggregate_url = str((main or {}).get("sub_url") or "").strip()
        aggregate_error: str | None = None
        if aggregate_url.startswith(("http://", "https://")):
            try:
                body = await asyncio.wait_for(
                    self._fetch_public_subscription(aggregate_url),
                    timeout=5.5,
                )
                aggregate_links = self._subscription_vless_links(body)
                add_many(aggregate_links)
                if aggregate_links:
                    logger.info(
                        "H1Cloud aggregate subscription loaded for %s with %s VLESS link(s)",
                        name,
                        len(links),
                    )
            except Exception as exc:
                aggregate_error = type(exc).__name__
        else:
            aggregate_error = "sub_url_missing"

        # Repair old clients created with channels=[] only when neither the
        # main client nor H1's aggregate subscription returned a usable link.
        if not links:
            try:
                patched = await asyncio.wait_for(
                    self._request(
                        "PATCH",
                        f"/clients/{quote(name, safe='')}",
                        json={"channels": standard_channels},
                    ),
                    timeout=2.5,
                )
                repaired = self._extract_client(patched)
                if repaired is None:
                    repaired = await asyncio.wait_for(
                        self._get_client(name),
                        timeout=1.5,
                    )
                if repaired is not None:
                    main = repaired
                    main_uuid = str(main.get("uuid") or main_uuid).strip()
                    add_many(self._client_vless_links(main))
            except Exception as exc:
                logger.warning(
                    "H1Cloud main-link repair failed for %s (%s)",
                    name,
                    type(exc).__name__,
                )

        # Always inspect linked nodes directly and merge their links. H1's
        # aggregate sub_url can lag behind federation changes, which used to
        # make a newly added country (for example Germany) invisible in Happ.
        # Failures of one country never invalidate links returned by another.
        try:
            nodes = await asyncio.wait_for(self._federated_nodes(), timeout=4.5)
        except Exception as exc:
            logger.warning(
                "H1Cloud linked-node discovery failed for %s (%s)",
                name,
                type(exc).__name__,
            )
            nodes = []

        remote_nodes = [
            node
            for node in nodes
            if self._node_id(node) and self._node_prefix(node)
        ]

        async def load_remote(node: dict[str, Any]) -> list[str]:
            prefix = self._node_prefix(node)
            try:
                client = await asyncio.wait_for(
                    self._get_client(name, prefix=prefix),
                    timeout=2.5,
                )
                remote_links = self._client_vless_links(client)
                if remote_links:
                    return remote_links
                if not main_uuid:
                    return []

                desired_expiry = self._desired_expiry(user)
                if desired_expiry <= int(datetime.now().timestamp()):
                    desired_expiry = int(datetime.now().timestamp()) + 86400
                repaired = await asyncio.wait_for(
                    self._upsert_location(
                        name=name,
                        client_uuid=main_uuid,
                        expires_at=desired_expiry,
                        traffic_limit=max(0, int(user.get("traffic_limit_gb") or 0)),
                        device_limit=max(1, int(user.get("max_devices") or 1)),
                        prefix=prefix,
                    ),
                    timeout=5.0,
                )
                return self._client_vless_links(repaired)
            except Exception:
                return []

        if remote_nodes:
            tasks = [asyncio.create_task(load_remote(node)) for node in remote_nodes]
            done, pending = await asyncio.wait(tasks, timeout=6.2)
            for task in pending:
                task.cancel()
            for task in done:
                try:
                    add_many(task.result())
                except Exception:
                    pass

        if links:
            try:
                links = await asyncio.wait_for(
                    self._rank_live_vless_links(links),
                    timeout=1.6,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "H1Cloud endpoint ranking timed out for %s; preserving all %s link(s)",
                    name,
                    len(links),
                )

        if not links:
            raise RuntimeError("H1Cloud returned no VLESS links")

        logger.info(
            "H1Cloud merged subscription built for %s with %s VLESS link(s)",
            name,
            len(links),
        )
        payload = ("\n".join(links) + "\n").encode("utf-8")

        # Happ reads traffic/expiry from subscription-userinfo. H1Cloud's
        # client object already contains the canonical traffic counter, so
        # expose it here without an extra state/devices request.
        try:
            used_gb = max(0.0, float((main or {}).get("traffic_used_gb") or 0))
        except (TypeError, ValueError):
            used_gb = 0.0
        try:
            limit_gb = max(
                0.0,
                float(
                    (main or {}).get("traffic_limit_gb")
                    or user.get("traffic_limit_gb")
                    or 0
                ),
            )
        except (TypeError, ValueError):
            limit_gb = 0.0

        used_bytes = int(used_gb * GB)
        total_bytes = int(limit_gb * GB)
        expire = max(0, self._desired_expiry(user))
        metadata_headers = {
            "subscription-userinfo": (
                f"upload=0; download={used_bytes}; "
                f"total={total_bytes}; expire={expire}"
            ),
        }
        return base64.b64encode(payload), metadata_headers

    async def close(self) -> None:
        await self.session.close()
        await self.public_session.close()


class XuiVpnProvider(VpnProvider):
    capabilities = ProviderCapabilities(
        supports_device_list=True,
        supports_device_removal=True,
    )
    """Native provider for the configured 3x-ui client API."""

    service_ready = True
    mode_name = "3xui"

    def __init__(
        self,
        panel_url: str,
        api_token: str,
        inbound_ids: tuple[int, ...],
        subscription_template: str,
        server_name: str,
        verify_ssl: bool = True,
    ):
        if not panel_url:
            raise RuntimeError("XUI_URL is required for VPN_MODE=3xui")
        if not api_token:
            raise RuntimeError("XUI_TOKEN is required for VPN_MODE=3xui")
        if not inbound_ids:
            raise RuntimeError("XUI_INBOUND_IDS is required for VPN_MODE=3xui")
        if "{sub_id}" not in subscription_template:
            raise RuntimeError(
                "XUI_SUBSCRIPTION_TEMPLATE must contain {sub_id}"
            )

        self.panel_url = panel_url.rstrip("/")
        self.inbound_ids = inbound_ids
        self.subscription_template = subscription_template
        self.server_name = server_name
        self.verify_ssl = verify_ssl
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20),
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
        )

    @staticmethod
    def _email(user: dict[str, Any]) -> str:
        return f'mgn_{int(user["telegram_id"])}'

    @staticmethod
    def _expiry_ms(user: dict[str, Any]) -> int:
        value = user.get("subscription_until")
        if not value:
            return 0
        return int(datetime.fromisoformat(value).timestamp() * 1000)

    def _subscription_url(self, sub_id: str) -> str:
        return self.subscription_template.replace(
            "{sub_id}",
            quote(sub_id, safe=""),
        )

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        allow_missing: bool = False,
    ) -> Any:
        async with self.session.request(
            method,
            f"{self.panel_url}{path}",
            json=json,
            ssl=self.verify_ssl,
        ) as response:
            if allow_missing and response.status == 404:
                return None

            response.raise_for_status()
            if response.status == 204:
                return None

            data = await response.json(content_type=None)
            if isinstance(data, dict) and data.get("success") is False:
                message = str(data.get("msg") or "3x-ui API error")
                if allow_missing and "not found" in message.lower():
                    return None
                raise RuntimeError(message)

            if isinstance(data, dict) and "obj" in data:
                return data.get("obj")
            return data

    async def _get_client(self, email: str) -> dict[str, Any] | None:
        data = await self._api(
            "GET",
            f"/panel/api/clients/get/{quote(email, safe='')}",
            allow_missing=True,
        )
        if not data:
            return None
        return dict(data)

    async def _ensure_attached(
        self,
        email: str,
        current_inbound_ids: list[int],
    ) -> None:
        missing = [
            inbound_id
            for inbound_id in self.inbound_ids
            if inbound_id not in current_inbound_ids
        ]
        if not missing:
            return
        await self._api(
            "POST",
            f"/panel/api/clients/{quote(email, safe='')}/attach",
            json={"inboundIds": missing},
        )

    async def provision(self, user: dict[str, Any]) -> VpnState:
        email = self._email(user)
        existing = await self._get_client(email)

        total_bytes = max(0, int(user.get("traffic_limit_gb") or 0)) * GB
        expiry_ms = self._expiry_ms(user)
        hwid_limit = max(1, int(user.get("max_devices") or 1))

        if existing is None:
            client = {
                "email": email,
                "subId": user["sub_token"],
                "totalGB": total_bytes,
                "expiryTime": expiry_ms,
                "tgId": int(user["telegram_id"]),
                "limitIp": 0,
                "limitHwid": hwid_limit,
                "enable": True,
                "comment": "MGN VPN",
            }
            await self._api(
                "POST",
                "/panel/api/clients/add",
                json={
                    "client": client,
                    "inboundIds": list(self.inbound_ids),
                },
            )
        else:
            client = dict(existing.get("client") or {})
            current_ids = [
                int(value)
                for value in (existing.get("inboundIds") or [])
            ]
            client.update(
                {
                    "email": email,
                    "subId": client.get("subId") or user["sub_token"],
                    "totalGB": total_bytes,
                    "expiryTime": expiry_ms,
                    "tgId": int(user["telegram_id"]),
                    "limitHwid": hwid_limit,
                    "enable": True,
                    "comment": client.get("comment") or "MGN VPN",
                }
            )
            await self._api(
                "POST",
                f"/panel/api/clients/update/{quote(email, safe='')}",
                json=client,
            )
            await self._ensure_attached(email, current_ids)

        return await self.get_state(user)

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        email = self._email(user)
        existing = await self._get_client(email)
        if existing is None:
            raise RuntimeError("3x-ui client does not exist yet")

        client = dict(existing.get("client") or {})
        sub_id = str(client.get("subId") or user["sub_token"])

        traffic = await self._api(
            "GET",
            f"/panel/api/clients/traffic/{quote(email, safe='')}",
            allow_missing=True,
        )
        traffic = dict(traffic or {})
        used_bytes = int(traffic.get("up") or 0) + int(traffic.get("down") or 0)
        total_bytes = int(
            traffic.get("total")
            or client.get("totalGB")
            or max(0, int(user.get("traffic_limit_gb") or 0)) * GB
        )

        raw_devices = await self._api(
            "POST",
            f"/panel/api/clients/hwids/{quote(email, safe='')}",
            allow_missing=True,
        )
        devices: list[dict[str, Any]] = []
        for item in raw_devices or []:
            if not isinstance(item, dict):
                continue
            os_name = str(item.get("deviceOs") or "")
            version = str(item.get("osVersion") or "")
            platform = " ".join(x for x in (os_name, version) if x).strip()
            model = str(item.get("deviceModel") or "").strip()
            user_agent = str(item.get("userAgent") or "").strip()
            name = model or user_agent or "Устройство"
            devices.append(
                {
                    "id": str(item.get("id") or ""),
                    "name": name,
                    "platform": platform,
                    "fingerprint": str(item.get("fingerprint") or ""),
                    "last_seen": item.get("lastSeen"),
                }
            )

        return VpnState(
            subscription_url=self._subscription_url(sub_id),
            server=self.server_name,
            traffic_used_gb=used_bytes / GB,
            traffic_limit_gb=total_bytes / GB,
            devices=devices,
        )

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        if not device_id.isdigit():
            raise RuntimeError("Invalid device id")

        email = self._email(user)
        await self._api(
            "DELETE",
            (
                f"/panel/api/clients/hwids/"
                f"{quote(email, safe='')}/{device_id}"
            ),
        )

    async def close(self) -> None:
        await self.session.close()
