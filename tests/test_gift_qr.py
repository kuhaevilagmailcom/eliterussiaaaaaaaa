import asyncio
from datetime import timedelta
from pathlib import Path

import aiosqlite

from db import Database, from_iso, utcnow
from gift_qr import gift_link, gift_qr_png, parse_gift_token


def run(coro):
    return asyncio.run(coro)


def test_qr_opens_telegram_bot_and_contains_compatible_deep_link():
    token = "AaBb0123456789_-AaBb0123456789_-AaBb0123456789"
    link = gift_link("mgnvpn_bot", token)
    assert link == "https://t.me/mgnvpn_bot?start=g_" + token
    assert parse_gift_token(link) == token
    assert parse_gift_token("g_" + token) == token
    assert parse_gift_token("MGN-GIFT:" + token) == token
    assert parse_gift_token("https://evil.example/?start=g_" + token) is None
    assert parse_gift_token("https://t.me/fake?start=wrong_" + token) is None
    assert gift_qr_png(link).startswith(bytes([137,80,78,71,13,10,26,10]))


def test_gift_redemption_is_atomic_even_under_race(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "gifts.db"))
        await db.init()
        old = await db.ensure_user(333, "receiver", "Receive")
        await db.ensure_user(444, "other", "Other")
        await db.set_device_limit(333, 4)
        voucher = await db.create_gift_qr(created_by=1, days=90)
        link = gift_link("mgnvpn_bot", voucher["token"])
        results = await asyncio.gather(
            db.redeem_gift_qr(token=link, telegram_id=333),
            db.redeem_gift_qr(token=link, telegram_id=444),
        )
        assert sum(r is not None for r in results) == 1
        winner_id = 333 if results[0] is not None else 444
        winner = await db.get_user(winner_id)
        assert from_iso(winner["subscription_until"]) > utcnow() + timedelta(days=89)
        if winner_id == 333:
            assert winner["max_devices"] == 4
        assert await db.redeem_gift_qr(token=link, telegram_id=winner_id) is None
        async with aiosqlite.connect(db.path) as raw:
            rows = await (await raw.execute("SELECT token_hash, redeemed_by FROM gift_qr_codes")).fetchall()
        assert len(rows) == 1
        assert voucher["token"] not in str(rows)
        assert rows[0][1] == winner_id

    run(scenario())


def test_gifts_extend_existing_subscription_and_preserve_identity(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "extend.db"))
        await db.init()
        await db.ensure_user(20, "gift", "Gift")
        await db.set_device_limit(20, 3)
        baseline = await db.adjust_subscription_days(20, 10)
        old_expiry = from_iso(baseline["subscription_until"])
        old_client = baseline["vpn_client_id"]

        voucher = await db.create_gift_qr(created_by=1, days=365)
        result = await db.redeem_gift_qr(token=voucher["token"], telegram_id=20)
        assert result and result["days"] == 365
        user = result["user"]
        assert user["max_devices"] == 3
        assert user["vpn_client_id"] == old_client
        assert abs((from_iso(user["subscription_until"]) - old_expiry).days - 365) <= 1
        assert await db.redeem_gift_qr(token=voucher["token"], telegram_id=20) is None

    run(scenario())


def test_gift_expiry_and_blocked_recipient_are_rejected(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "blocked.db"))
        await db.init()
        await db.ensure_user(25, "buyer", "Buyer")
        gift = await db.create_gift_qr(created_by=1, days=30)
        async with aiosqlite.connect(db.path) as raw:
            await raw.execute("UPDATE users SET is_blocked=1 WHERE telegram_id=25")
            await raw.commit()
        assert await db.redeem_gift_qr(token=gift["token"], telegram_id=25) is None
        async with aiosqlite.connect(db.path) as raw:
            await raw.execute("UPDATE users SET is_blocked=0 WHERE telegram_id=25")
            await raw.execute("UPDATE gift_qr_codes SET expires_at='2020-01-01T00:00:00+00:00'")
            await raw.commit()
        assert await db.redeem_gift_qr(token=gift["token"], telegram_id=25) is None

    run(scenario())


def test_only_supported_gift_periods():
    async def scenario():
        db = Database(":memory:")
        for period in (1, 7, 60, 180, 366):
            try:
                await db.create_gift_qr(created_by=1, days=period)
            except ValueError:
                pass
            else:
                raise AssertionError("unsupported gift period accepted")
    run(scenario())
