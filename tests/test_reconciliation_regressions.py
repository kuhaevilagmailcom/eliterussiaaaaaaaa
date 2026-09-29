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
            "payment-1", "order-1", 42, "30", 99,
            target_telegram_id=42,
            original_amount_rub=99,
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
