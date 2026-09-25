from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4
import logging
from typing import Any
from urllib.parse import quote

import aiohttp


GB = 1024 ** 3
logger = logging.getLogger(__name__)


@dataclass
class VpnState:
    subscription_url: str
    server: str
    traffic_used_gb: float
    traffic_limit_gb: float
    devices: list[dict[str, Any]]


class VpnProvider:
    service_ready: bool = True
    mode_name: str = "vpn"

    async def provision(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        raise NotImplementedError

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
            data = await response.json(content_type=None)
            if allow_missing and (
                response.status == 404
                or (
                    isinstance(data, dict)
                    and data.get("ok") is False
                    and str(data.get("error") or "") == "user_not_found"
                )
            ):
                return None

            if response.status >= 400:
                message = (
                    str(data.get("error") or data.get("message") or "")
                    if isinstance(data, dict)
                    else ""
                )
                raise RuntimeError(
                    f"H1Cloud API HTTP {response.status}: {message or path}"
                )

            if not isinstance(data, dict):
                raise RuntimeError("H1Cloud returned invalid JSON")
            if data.get("ok") is False:
                raise RuntimeError(
                    f'H1Cloud API error: {data.get("error") or "unknown"}'
                )
            return data

    @staticmethod
    def _extract_client(data: dict[str, Any] | None) -> dict[str, Any] | None:
        if not data:
            return None
        client = data.get("client")
        if isinstance(client, dict):
            return dict(client)
        if data.get("name") or data.get("uuid"):
            return dict(data)
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

        containers: list[Any] = []
        if isinstance(data, dict):
            for key in ("inbounds", "items"):
                if data.get(key) is not None:
                    containers.append(data[key])
            for key in ("data", "result"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    for nested_key in ("inbounds", "items"):
                        if nested.get(nested_key) is not None:
                            containers.append(nested[nested_key])
                elif isinstance(nested, list):
                    containers.append(nested)

        result: list[str] = []
        seen: set[str] = set()

        for container in containers:
            if isinstance(container, list):
                entries = [(None, item) for item in container]
            elif isinstance(container, dict):
                entries = [(str(key), item) for key, item in container.items()]
            else:
                entries = []

            for map_key, item in entries:
                inbound_id = ""
                protocol = ""
                label = ""

                if isinstance(item, (str, int)):
                    inbound_id = str(item).strip()
                elif isinstance(item, dict):
                    for key in ("id", "inbound_id", "inboundId"):
                        value = item.get(key)
                        if value is not None and str(value).strip():
                            inbound_id = str(value).strip()
                            break
                    if not inbound_id and map_key:
                        inbound_id = map_key.strip()

                    protocol = str(item.get("protocol") or "").lower()
                    label = str(
                        item.get("remark")
                        or item.get("name")
                        or item.get("tag")
                        or ""
                    ).lower()

                if not inbound_id:
                    continue
                if protocol in {"api", "dokodemo-door"} or label == "api":
                    continue
                if inbound_id not in seen:
                    seen.add(inbound_id)
                    result.append(inbound_id)

        if not result:
            top_keys = list(data.keys())[:12] if isinstance(data, dict) else []
            logger.warning(
                "H1Cloud /inbounds unsupported response for %s; keys=%s",
                prefix or "main",
                top_keys,
            )
            raise RuntimeError(
                f"H1Cloud panel returned no usable inbounds for {prefix or 'main'}"
            )
        return result

    async def _federated_nodes(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/fed/lagg")
        if not isinstance(data, dict):
            return []
        raw = data.get("nodes")
        if not isinstance(raw, list):
            raw = data.get("items")
        if not isinstance(raw, list):
            for key in ("data", "result"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    candidate = nested.get("nodes")
                    if isinstance(candidate, list):
                        raw = candidate
                        break
        if not isinstance(raw, list):
            return []
        return [dict(item) for item in raw if isinstance(item, dict)]

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

        payload: dict[str, Any] = {
            "expires_at": expires_at,
            "traffic_limit_gb": traffic_limit,
            "device_limit": device_limit,
            "channels": [],
            "inbound_ids": inbound_ids,
        }

        if existing is None:
            payload.update(
                {
                    "name": name,
                    "uuid": client_uuid,
                    "manual": True,
                }
            )
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

        # Main NL panel.
        await self._upsert_location(
            name=name,
            client_uuid=client_uuid,
            expires_at=desired_expiry,
            traffic_limit=traffic_limit,
            device_limit=device_limit,
        )

        # Every panel connected in H1 "Servers" gets the same UUID.
        nodes = await self._federated_nodes()
        logger.info(
            "H1Cloud federation: %s connected remote node(s) for %s",
            len(nodes),
            name,
        )
        errors: list[str] = []
        for node in nodes:
            node_id = self._node_id(node)
            if not node_id:
                continue
            try:
                await self._upsert_location(
                    name=name,
                    client_uuid=client_uuid,
                    expires_at=desired_expiry,
                    traffic_limit=traffic_limit,
                    device_limit=device_limit,
                    prefix=f"/fed/lproxy/{quote(node_id, safe='')}",
                )
                logger.info(
                    "H1Cloud federation node %s synced for %s",
                    node_id,
                    name,
                )
            except Exception as exc:
                errors.append(f"{node_id}: {exc}")

        if errors:
            raise RuntimeError(
                "H1Cloud federation incomplete: " + "; ".join(errors)
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

    async def health(self) -> dict[str, Any]:
        data = await self._request("GET", "/health")
        return dict(data or {})

    async def close(self) -> None:
        await self.session.close()


class XuiVpnProvider(VpnProvider):
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
