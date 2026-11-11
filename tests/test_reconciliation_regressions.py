import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite

from config import Config
from db import Database, from_iso
from miniapp import MiniAppServer
from vpn import H1CloudVpnProvider


def run(coro):
    return asyncio.run(coro)


def test_sbp_reconciliation_prioritizes_pending_over_old_paid(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "rotation.sqlite3"))
        await db.init()
        await db.ensure_user(1, "user", "User")

        async with aiosqlite.connect(db.path) as connection:
            for index in range(80):
                await connection.execute(
                    """
                    INSERT INTO sbp_payments (
                        payment_id, order_id, telegram_id, target_telegram_id,
                        plan_code, amount_rub, status, created_at, paid_at
                    ) VALUES (?, ?, 1, 1, '30', 99, 'paid', ?, ?)
                    """,
                    (
                        f"paid-{index}",
                        f"order-{index}",
                        f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                        f"2026-01-{(index % 28) + 1:02d}T00:00:00+00:00",
                    ),
                )
            await connection.execute(
                """
                INSERT INTO sbp_payments (
                    payment_id, order_id, telegram_id, target_telegram_id,
                    plan_code, amount_rub, status, created_at
                ) VALUES ('new-pending', 'new-order', 1, 1, '30', 99,
                          'awaiting_payment', '2026-09-30T00:00:00+00:00')
                """
            )
            await connection.commit()

        batch = await db.list_sbp_for_reconciliation(limit=50)
        ids = [item["payment_id"] for item in batch]
        assert "new-pending" in ids

        for item in batch:
            await db.mark_sbp_reconciled(item["payment_id"])
        next_batch = await db.list_sbp_for_reconciliation(limit=50)
        assert any(item["payment_id"].startswith("paid-") for item in next_batch)

    run(scenario())


def test_refund_reverses_subscription_once(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "refund.sqlite3"))
        await db.init()
        await db.ensure_user(42, "buyer", "Buyer")
        await db.create_sbp_payment(
            "payment-1", "order-1", 42, "30", 70,
            target_telegram_id=42,
            original_amount_rub=70,
        )
        assert await db.settle_sbp_payment("payment-1")
        paid_until = from_iso((await db.get_user(42))["subscription_until"])
        assert paid_until is not None

        first = await db.reverse_sbp_access("payment-1", "refunded")
        assert first and first["changed"] is True
        after_first = await db.get_user(42)
        assert after_first["subscription_until"] is None

        second = await db.reverse_sbp_access("payment-1", "chargeback")
        assert second and second["changed"] is False
        after_second = await db.get_user(42)
        assert after_second["subscription_until"] is None

        payment = await db.get_sbp_payment("payment-1")
        assert payment["access_reversed_at"]
        assert payment["status"] == "chargeback"

    run(scenario())


def test_happ_headers_always_use_current_db_expiry(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_ONLY")
    config = Config.from_env()
    server = object.__new__(MiniAppServer)
    server.config = config
    server._bot_username = "mgnvpn_bot"

    async def username():
        return "mgnvpn_bot"

    server._username = username
    user = {
        "subscription_until": "2030-01-02T03:04:05+00:00",
        "traffic_limit_gb": 10,
    }
    headers = run(
        server._subscription_profile_headers(
            user,
            {
                "Subscription-Userinfo":
                    "upload=100; download=200; total=1; expire=123",
            },
        )
    )
    value = headers["Subscription-Userinfo"]
    assert "upload=100" in value
    assert "download=200" in value
    assert "expire=123" not in value
    assert "total=10737418240" in value


def test_h1_federation_merges_link_registry_and_lagg():
    provider = object.__new__(H1CloudVpnProvider)

    async def request(_method, path, **_kwargs):
        if path == "/fed/link":
            return {"links": ["link-node"]}
        if path == "/fed/registry":
            return {"nodes": [{"id": "registry-node"}]}
        if path == "/fed/lagg":
            return {"nodes": [{"id": "lagg-node"}, {"id": "link-node"}]}
        raise AssertionError(path)

    provider._request = request
    nodes = run(provider._federated_nodes())
    pairs = {(item["proxy_kind"], provider._node_id(item)) for item in nodes}
    assert ("lproxy", "link-node") in pairs
    assert ("proxy", "registry-node") in pairs
    assert ("lproxy", "lagg-node") in pairs
    assert len([pair for pair in pairs if pair == ("lproxy", "link-node")]) == 1



def test_h1_federation_accepts_nested_and_object_response_shapes():
    provider = object.__new__(H1CloudVpnProvider)

    async def request(_method, path, **_kwargs):
        if path == "/fed/link":
            return {
                "data": {
                    "links": [
                        {"sid": "fi-1", "name": "MGN-FI"},
                        {"service_id": "de-1", "name": "MGN-DE"},
                    ]
                }
            }
        if path == "/fed/registry":
            return {"result": {"items": [{"node_id": "pl-1", "name": "MGN-PL"}]}}
        if path == "/fed/lagg":
            return {"payload": {"servers": [{"billingId": "us-1", "name": "MGN-US"}]}}
        raise AssertionError(path)

    provider._request = request
    nodes = run(provider._federated_nodes())
    pairs = {(item["proxy_kind"], provider._node_id(item)) for item in nodes}
    assert ("lproxy", "fi-1") in pairs
    assert ("lproxy", "de-1") in pairs
    assert ("proxy", "pl-1") in pairs
    assert ("lproxy", "us-1") in pairs

def test_revoke_subscription_clears_only_access_fields_and_is_idempotent(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "revoke.sqlite3"))
        await db.init()
        await db.ensure_user(42, "buyer", "Buyer")
        async with aiosqlite.connect(db.path) as connection:
            await connection.execute(
                "UPDATE users SET subscription_until=?, plan_name='30 дней' WHERE telegram_id=?",
                ("2030-01-01T00:00:00+00:00", 42),
            )
            await connection.commit()

        assert await db.revoke_subscription(42) is True
        revoked = await db.get_user(42)
        assert revoked["subscription_until"] is None
        assert revoked["plan_name"] == ""
        assert revoked["max_devices"] == 2

        # Repeating the operation for an existing user is safe.
        assert await db.revoke_subscription(42) is True
        assert await db.revoke_subscription(404) is False

    run(scenario())


def test_miniapp_revoke_suspends_h1_before_clearing_database():
    async def scenario():
        row = {
            "telegram_id": 42,
            "sub_token": "old-token",
            "subscription_until": "2030-01-01T00:00:00+00:00",
            "plan_name": "30 дней",
        }
        updated = {
            **row,
            "subscription_until": None,
            "plan_name": "",
        }
        events = []

        async def suspend(user, value):
            events.append(("provider", value))

        async def revoke(user_id):
            events.append(("database", user_id))
            return True

        server = object.__new__(MiniAppServer)
        server._admin_auth = AsyncMock(return_value=(999, {}, {}, "owner"))
        server._json_body = AsyncMock(return_value={"action": "revoke_subscription"})
        server.db = SimpleNamespace(
            get_user=AsyncMock(side_effect=[row, updated]),
            revoke_subscription=AsyncMock(side_effect=revoke),
            add_admin_audit=AsyncMock(),
            admin_user_detail=AsyncMock(return_value={"user": updated}),
        )
        server.provider = SimpleNamespace(set_suspended=AsyncMock(side_effect=suspend))
        server.invalidate_subscription_cache = AsyncMock()
        server._sync_bot_subscription_menu = AsyncMock()

        response = await server.admin_user_action(
            SimpleNamespace(match_info={"user_id": "42"})
        )
        assert response.status == 200
        assert events == [("provider", True), ("database", 42)]
        server.invalidate_subscription_cache.assert_awaited_once_with("old-token")
        server.db.add_admin_audit.assert_awaited_once()
        server._sync_bot_subscription_menu.assert_awaited_once_with(updated)

    run(scenario())


def test_miniapp_revoke_does_not_clear_database_when_h1_suspension_fails():
    async def scenario():
        row = {
            "telegram_id": 42,
            "sub_token": "old-token",
            "subscription_until": "2030-01-01T00:00:00+00:00",
            "plan_name": "30 дней",
        }
        server = object.__new__(MiniAppServer)
        server._admin_auth = AsyncMock(return_value=(999, {}, {}, "owner"))
        server._json_body = AsyncMock(return_value={"action": "revoke_subscription"})
        server.db = SimpleNamespace(
            get_user=AsyncMock(return_value=row),
            revoke_subscription=AsyncMock(return_value=True),
        )
        server.provider = SimpleNamespace(
            set_suspended=AsyncMock(side_effect=RuntimeError("remote node offline"))
        )

        try:
            await server.admin_user_action(
                SimpleNamespace(match_info={"user_id": "42"})
            )
        except Exception as exc:
            assert getattr(exc, "status", None) == 503
        else:
            raise AssertionError("H1 suspension failure must return HTTP 503")

        server.db.revoke_subscription.assert_not_awaited()

    run(scenario())

def test_h1_federation_discovery_excludes_removed_netherlands_nodes():
    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)

        async def request(_method, path, **_kwargs):
            if path == "/fed/link":
                return {
                    "links": [
                        {"id": "nl-node", "name": "MGN-NL"},
                        {"id": "fi-node", "name": "MGN-FI"},
                    ]
                }
            if path == "/fed/registry":
                return {"nodes": []}
            if path == "/fed/lagg":
                return None
            raise AssertionError(path)

        provider._request = request
        nodes = await provider._discover_federated_nodes()
        assert [provider._node_id(node) for node in nodes] == ["fi-node"]
        assert provider._federation_discovery_ok is True

    run(scenario())

