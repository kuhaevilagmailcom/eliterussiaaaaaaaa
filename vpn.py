from __future__ import annotations

import asyncio
import json as json_module
import base64
import math
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
import logging
from typing import Any
from urllib.parse import quote, unquote

import aiohttp


GB = 1024 ** 3
logger = logging.getLogger(__name__)


LOCATION_LABELS = {
    "MGN-NL": "🇳🇱 Нидерланды",
    "MGN-DE": "🇩🇪 Германия",
    "MGN-FI": "🇫🇮 Финляндия",
    "MGN-LT": "🇱🇹 Литва",
    "MGN-US": "🇺🇸 США",
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
        ("US3.H1CLOUD.NET", "🇺🇸 США"),
    )
    for marker, label in host_markers:
        if marker in decoded:
            return label
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
            label = _location_label(line)
            if label:
                used_labels[label] = used_labels.get(label, 0) + 1
                suffix = used_labels[label]
                if suffix > 1:
                    label = f"{label} · {suffix}"
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
        verify_ssl: bool = False,
    ):
        if not api_url:
            raise RuntimeError("H1_API_URL is required for VPN_MODE=h1cloud")
        if not api_token:
            raise RuntimeError("H1_API_TOKEN is required for VPN_MODE=h1cloud")

        self.api_url = api_url.rstrip("/")
        self.api_token = api_token
        self.subscription_template = subscription_template.strip()
        self.server_name = server_name
        self.verify_ssl = verify_ssl
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

    @staticmethod
    def _name(user: dict[str, Any]) -> str:
        return f'mgn_{int(user["telegram_id"])}'

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
            ssl=self.verify_ssl,
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

    async def _federated_nodes(self) -> list[dict[str, Any]]:
        """Return every H1 remote node with the correct proxy transport.

        H1 has TWO federation stores:
        - /fed/link -> billing-linked server IDs, accessed through /fed/lproxy/<sid>
        - /fed/registry -> manual registry node IDs/tokens, accessed through /fed/proxy/<id>

        They are separate stores. Using only one store explains why a unified
        subscription could contain NL + US while FI/DE/LT were missing.
        """
        nodes: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        async def load_linked() -> None:
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/link"),
                    timeout=1.5,
                )
            except Exception as exc:
                logger.warning(
                    "H1Cloud /fed/link unavailable: %s",
                    str(exc).strip() or type(exc).__name__,
                )
                return

            if not isinstance(data, dict):
                return
            raw_links = data.get("links")
            if not isinstance(raw_links, list):
                return
            for value in raw_links:
                node_id = str(value or "").strip()
                key = ("lproxy", node_id)
                if node_id and key not in seen:
                    seen.add(key)
                    nodes.append(
                        {
                            "id": node_id,
                            "proxy_kind": "lproxy",
                        }
                    )

        async def load_registry() -> None:
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/registry"),
                    timeout=1.5,
                )
            except Exception as exc:
                logger.warning(
                    "H1Cloud /fed/registry unavailable: %s",
                    str(exc).strip() or type(exc).__name__,
                )
                return

            if not isinstance(data, dict):
                return
            raw_nodes = data.get("nodes")
            if not isinstance(raw_nodes, list):
                return
            for item in raw_nodes:
                if not isinstance(item, dict):
                    continue
                node_id = self._node_id(item)
                key = ("proxy", node_id)
                if node_id and key not in seen:
                    seen.add(key)
                    node = dict(item)
                    node["proxy_kind"] = "proxy"
                    nodes.append(node)

        await asyncio.gather(load_linked(), load_registry())

        if not nodes:
            # Compatibility fallback for older H1 builds.
            try:
                data = await asyncio.wait_for(
                    self._request("GET", "/fed/lagg"),
                    timeout=3.0,
                )
                raw_nodes = data.get("nodes") if isinstance(data, dict) else None
                if isinstance(raw_nodes, list):
                    for item in raw_nodes:
                        if not isinstance(item, dict):
                            continue
                        node_id = self._node_id(item)
                        key = ("lproxy", node_id)
                        if node_id and key not in seen:
                            seen.add(key)
                            node = dict(item)
                            node["proxy_kind"] = "lproxy"
                            nodes.append(node)
            except Exception:
                pass

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
        for key in ("node_id", "id", "server_id"):
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

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        client = await self._get_client(self._name(user))
        if client is None:
            raise RuntimeError("H1Cloud client does not exist yet")

        subscription_url = self._subscription_url(client)
        if not subscription_url:
            raise RuntimeError("H1Cloud client has no subscription URL")

        devices: list[dict[str, Any]] = []
        raw_devices = client.get("devices") or []
        if isinstance(raw_devices, list):
            for index, item in enumerate(raw_devices):
                if not isinstance(item, dict):
                    continue
                devices.append(
                    {
                        "id": str(item.get("id") or item.get("hwid") or index),
                        "name": str(
                            item.get("name")
                            or item.get("device_name")
                            or item.get("model")
                            or "Устройство"
                        ),
                        "platform": str(
                            item.get("platform")
                            or item.get("os")
                            or ""
                        ),
                        "fingerprint": str(
                            item.get("fingerprint")
                            or item.get("hwid")
                            or ""
                        ),
                        "last_seen": item.get("last_seen") or item.get("lastSeen"),
                    }
                )

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

    async def fetch_subscription(
        self,
        user: dict[str, Any],
    ) -> tuple[bytes, dict[str, str]]:
        """Build one MGN subscription from every reachable linked H1 node."""
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
                timeout=5.0,
            )

        main_links = self._client_vless_links(main)
        if not main_links:
            # Repair legacy clients that were created with channels=[].
            patched = await asyncio.wait_for(
                self._request(
                    "PATCH",
                    f"/clients/{quote(name, safe='')}",
                    json={"channels": standard_channels},
                ),
                timeout=2.5,
            )
            repaired = self._extract_client(patched)
            if repaired is not None:
                main = repaired
            else:
                main = await asyncio.wait_for(self._get_client(name), timeout=1.5)
            main_links = self._client_vless_links(main)

        links: list[str] = []
        seen: set[str] = set()

        def add_many(values: list[str]) -> None:
            for value in values:
                if value not in seen:
                    seen.add(value)
                    links.append(value)

        add_many(main_links)
        main_uuid = str((main or {}).get("uuid") or "").strip()

        # H1 already has its own unified subscription endpoint (sub_url). It
        # merges every server marked "Добавлена" in the H1 panel and does not
        # depend on lproxy authentication. Prefer it as the federation source.
        aggregate_url = str((main or {}).get("sub_url") or "").strip()

        async def load_h1_aggregate() -> tuple[list[str], str | None]:
            if not aggregate_url.startswith(("http://", "https://")):
                return [], "sub_url_missing"
            try:
                async with asyncio.timeout(6.0):
                    async with self.public_session.get(
                        aggregate_url,
                        allow_redirects=True,
                        ssl=self.verify_ssl,
                    ) as response:
                        body = await response.read()
                        if response.status >= 400:
                            return [], f"HTTP {response.status}"
                        return self._subscription_vless_links(body), None
            except Exception as exc:
                return [], str(exc).strip() or type(exc).__name__

        # In parallel, inspect linked billing nodes directly. This supplements
        # the H1 aggregate if one remote public /sub endpoint is slow.
        try:
            nodes = await asyncio.wait_for(self._federated_nodes(), timeout=1.8)
        except Exception as exc:
            logger.warning(
                "H1Cloud linked-node discovery failed for %s: %s",
                name,
                str(exc).strip() or type(exc).__name__,
            )
            nodes = []

        async def load_remote(
            node: dict[str, Any],
        ) -> tuple[str, list[str], str | None]:
            node_id = self._node_id(node)
            prefix = self._node_prefix(node)
            label = f"{node.get('proxy_kind')}:{node_id}"
            try:
                client = await asyncio.wait_for(
                    self._get_client(name, prefix=prefix),
                    timeout=2.5,
                )
                remote_links = self._client_vless_links(client)
                if remote_links:
                    return label, remote_links, None

                if not main_uuid:
                    return label, [], "main_uuid_missing"

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
                    timeout=6.0,
                )
                return label, self._client_vless_links(repaired), None
            except Exception as exc:
                return label, [], str(exc).strip() or type(exc).__name__

        aggregate_task = asyncio.create_task(load_h1_aggregate())
        remote_nodes = [
            node
            for node in nodes
            if self._node_id(node) and self._node_prefix(node)
        ]
        remote_tasks = [
            asyncio.create_task(load_remote(node))
            for node in remote_nodes
        ]

        # Keep the whole /sub response inside normal VPN-client timeouts.
        all_tasks = [aggregate_task, *remote_tasks]
        done, pending = await asyncio.wait(all_tasks, timeout=6.2)
        for task in pending:
            task.cancel()

        federation_errors: list[str] = []
        if aggregate_task in done:
            try:
                aggregate_links, aggregate_error = aggregate_task.result()
                add_many(aggregate_links)
                if aggregate_error:
                    federation_errors.append(f"aggregate: {aggregate_error}")
            except Exception as exc:
                federation_errors.append(
                    f"aggregate: {str(exc).strip() or type(exc).__name__}"
                )
        else:
            federation_errors.append("aggregate: timeout")

        for task in remote_tasks:
            if task not in done:
                federation_errors.append("remote: timeout")
                continue
            try:
                node_id, remote_links, error = task.result()
                add_many(remote_links)
                if error:
                    federation_errors.append(f"{node_id}: {error}")
            except Exception as exc:
                federation_errors.append(
                    str(exc).strip() or type(exc).__name__
                )

        if federation_errors:
            logger.warning(
                "H1Cloud subscription federation partial for %s: %s",
                name,
                "; ".join(federation_errors),
            )

        if not links:
            raise RuntimeError("H1Cloud returned no VLESS links")

        logger.info(
            "H1Cloud unified subscription built for %s with %s VLESS link(s) from %s linked node(s)",
            name,
            len(links),
            len(remote_nodes),
        )
        payload = ("\n".join(links) + "\n").encode("utf-8")
        return base64.b64encode(payload), {}

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
