from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp


@dataclass
class VpnState:
    subscription_url: str
    server: str
    traffic_used_gb: float
    traffic_limit_gb: float
    devices: list[dict[str, Any]]


class VpnProvider:
    async def provision(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def get_state(self, user: dict[str, Any]) -> VpnState:
        raise NotImplementedError

    async def delete_device(self, user: dict[str, Any], device_id: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class DemoVpnProvider(VpnProvider):
    def __init__(self, base_url: str, server_name: str):
        self.base_url = base_url
        self.server_name = server_name

    def _state(self, user: dict[str, Any]) -> VpnState:
        return VpnState(
            subscription_url=f'{self.base_url}/{quote(user["sub_token"])}',
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
