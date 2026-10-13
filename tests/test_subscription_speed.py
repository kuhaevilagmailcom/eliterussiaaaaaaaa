"""Performance regressions for concurrent VPN subscription refreshes."""
import asyncio
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp.test_utils import make_mocked_request

from db import to_iso, utcnow
from miniapp import MiniAppServer
from vpn import H1CloudVpnProvider


def test_cold_subscription_requests_share_single_h1_fetch(tmp_path):
    async def scenario():
        user = {
            "telegram_id": 1002,
            "subscription_until": to_iso(utcnow() + timedelta(days=30)),
            "is_blocked": False,
            "max_devices": 2,
        }
        calls = 0

        async def fetch_subscription(_user):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.09)
            return (
                b"vless://uuid@nl1.h1cloud.net:443?security=reality#MGN-NL\n",
                {},
            )

        db = SimpleNamespace(get_user_by_sub_token=AsyncMock(return_value=user))
        provider = SimpleNamespace(
            mode_name="demo",
            fetch_subscription=AsyncMock(side_effect=fetch_subscription),
        )
        server = MiniAppServer(
            None,
            SimpleNamespace(
                db_path=str(tmp_path / "vpn.db"),
                display_tz=timezone.utc,
                trusted_proxy_ips=(),
            ),
            db,
            provider,
        )
        server._read_persistent_subscription_cache = Mock(return_value=None)
        server._write_persistent_subscription_cache = Mock()
        server._subscription_profile_headers = AsyncMock(return_value={})
        try:
            requests = [
                make_mocked_request(
                    "GET", "/sub/safe-token",
                    match_info={"token": "safe-token"},
                    headers={"User-Agent": "Happ/test"},
                )
                for _ in range(12)
            ]
            responses = await asyncio.gather(
                *(server.subscription(request) for request in requests)
            )
            assert all(response.status == 200 for response in responses)
            assert all(response.body == responses[0].body for response in responses)
            assert calls == 1
            assert provider.fetch_subscription.await_count == 1
        finally:
            await server.close()

    asyncio.run(scenario())


def test_h1_federation_discovery_is_singleflight_and_short_lived():
    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)
        discovery = AsyncMock(side_effect=_discover)
        provider._discover_federated_nodes = discovery

        results = await asyncio.gather(
            *(provider._federated_nodes() for _ in range(10))
        )
        assert discovery.await_count == 1
        assert all(row == [{"id": "de", "proxy_kind": "lproxy"}] for row in results)
        results[0][0]["id"] = "modified"
        assert (await provider._federated_nodes())[0]["id"] == "de"
        assert discovery.await_count == 1

        # Expiring cache never holds back newly added countries indefinitely.
        _time, nodes = provider._federation_cache
        provider._federation_cache = (-10_000.0, nodes)
        await provider._federated_nodes()
        assert discovery.await_count == 2

    async def _discover():
        await asyncio.sleep(0.05)
        return [{"id": "de", "proxy_kind": "lproxy"}]

    asyncio.run(scenario())
