"""Regression checks for the actual H1 server inventory in MGN admin UI."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import make_mocked_request

from miniapp import MiniAppServer


def test_admin_api_uses_current_h1_inventory_instead_of_sold_legacy_fleet(tmp_path):
    async def scenario():
        names = [
            "🇳🇱 Нидерланды", "🇵🇰 Пакистан", "🇹🇷 Турция",
            "🇩🇪 Германия", "🇺🇸 США 1", "🇫🇮 Финляндия",
        ]
        report = {
            "servers": [
                {
                    "name": name,
                    "configured": True,
                    "available": True,
                    "latency_ms": 40,
                    "check": "health",
                } for name in names
            ]
        }
        db = SimpleNamespace(
            list_active_users_for_vpn_sync=AsyncMock(return_value=[]),
            list_server_health=AsyncMock(return_value=[]),
            recent_smart_notifications=AsyncMock(return_value=[]),
        )
        provider = SimpleNamespace(server_diagnostics=AsyncMock(return_value=report))
        server = MiniAppServer(
            None, SimpleNamespace(db_path=str(tmp_path / "vpn.sqlite3")),
            db, provider,
        )
        server._admin_auth = AsyncMock(return_value=(1, {}, {}, "owner"))
        req = make_mocked_request("GET", "/api/miniapp/admin/servers")

        response = await server.admin_servers(req)
        actual = json.loads(response.body)
        assert response.status == 200
        assert [x["name"] for x in actual["servers"]] == names
        assert "Польша" not in str(actual) and "США 2" not in str(actual)
        assert all(x["available"] is True for x in actual["servers"])

        # H1 can return nothing during a transient discovery outage; do not
        # assert that the old six hard-coded countries are actually offline.
        provider.server_diagnostics = AsyncMock(return_value={"servers": []})
        empty = json.loads((await server.admin_servers(req)).body)
        assert empty["servers"] == []

    asyncio.run(scenario())
