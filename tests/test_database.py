import asyncio
from datetime import timedelta

import pytest

from db import Database, from_iso, utcnow
from catalog import rub_to_stars


def run(coro):
    return asyncio.run(coro)


def test_referral_rewards_are_atomic_and_capped(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        for referred_id in range(2, 6):
            await db.ensure_user(referred_id, f"u{referred_id}", "Friend")
            assert await db.set_referrer_once(referred_id, 1)
            assert not await db.set_referrer_once(referred_id, 999)
        stats = await db.referral_stats(1)
        assert stats == {"invited": 4, "rewarded": 3}
        owner = await db.get_user(1)
        assert from_iso(owner["subscription_until"]) > utcnow() + timedelta(days=2, hours=23)

    run(scenario())


def test_device_slots_survive_renewal_and_stay_in_range(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(10, "device", "Device")
        for expected in range(2, 6):
            user = await db.grant_extra_device(10)
            assert user["max_devices"] == expected
        assert await db.grant_extra_device(10) is None
        renewed = await db.extend_subscription(10, 30, "1 месяц", 1)
        assert renewed["max_devices"] == 5
        for expected in range(4, 0, -1):
            user = await db.revoke_extra_device(10)
            assert user["max_devices"] == expected
        assert await db.revoke_extra_device(10) is None

    run(scenario())


def test_promos_are_case_insensitive_limited_and_preserve_slots(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(20, "promo", "Promo")
        await db.grant_extra_device(20)
        promo = await db.create_service_promo(
            code="Mgn20",
            promo_type="discount",
            value=20,
            max_uses=1,
            created_by=1,
            applicable_plans="90,180,365",
        )
        quote = await db.promo_quote(
            code="mGN20", telegram_id=20, plan_code="90", original_price=349
        )
        assert quote["discount"] == 69
        assert quote["final_price"] == 280
        assert await db.consume_promo(
            promo_id=promo["id"], telegram_id=20, payment_id="pay-1"
        )
        assert await db.promo_quote(
            code="MGN20", telegram_id=20, plan_code="90", original_price=349
        ) is None

        await db.create_service_promo(
            code="MGNFREE",
            promo_type="free_days",
            value=7,
            created_by=1,
        )
        user = await db.redeem_free_days_promo("mgnfree", 20)
        assert user["max_devices"] == 2
        assert await db.redeem_free_days_promo("MGNFREE", 20) is None

    run(scenario())


def test_payment_events_are_idempotent(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(30, "payer", "Payer")
        assert await db.record_star_payment("charge-1", 30, 30, "30", 86)
        assert not await db.record_star_payment("charge-1", 30, 30, "30", 86)
        await db.create_payment_intent(
            intent_id="intent-1",
            buyer_id=30,
            target_id=30,
            product_code="30",
            original_amount_rub=149,
            discount_amount_rub=0,
            final_amount_rub=149,
            currency="XTR",
            currency_amount=94,
        )
        assert await db.mark_payment_intent_paid("intent-1")
        assert not await db.mark_payment_intent_paid("intent-1")

    run(scenario())


def test_referrer_assignment_rejects_self_and_is_race_safe(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "referral-race.sqlite3"))
        await db.init()
        for telegram_id in (1, 2, 3):
            await db.ensure_user(telegram_id, f"u{telegram_id}", "User")
        assert not await db.set_referrer_once(1, 1)
        results = await asyncio.gather(
            db.set_referrer_once(3, 1),
            db.set_referrer_once(3, 2),
            db.set_referrer_once(3, 1),
        )
        assert results.count(True) == 1
        referred = await db.get_user(3)
        assert referred["referrer_id"] in {1, 2}
        total_rewards = (await db.referral_stats(1))["rewarded"] + (await db.referral_stats(2))["rewarded"]
        assert total_rewards == 1

    run(scenario())


def test_star_conversion_uses_single_50_to_80_rate():
    assert {
        amount: rub_to_stars(amount)
        for amount in (80, 100, 150, 200, 300, 400, 500, 600, 1000)
    } == {80: 50, 100: 63, 150: 94, 200: 125, 300: 188, 400: 250, 500: 313, 600: 375, 1000: 625}


def test_support_threads_persist_messages_permissions_and_soft_delete(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "support.sqlite3"))
        await db.init()
        await db.ensure_user(50, "owner", "Owner")
        await db.ensure_user(51, "other", "Other")
        ticket = await db.create_support_thread(
            telegram_id=50, username="owner", first_name="Owner",
            message_type="text", text="Первое сообщение",
        )
        ticket_id = int(ticket["id"])
        assert await db.add_support_message(
            ticket_id=ticket_id, sender_type="user", sender_telegram_id=50,
            message_type="photo", file_id="photo-id", file_unique_id="photo-unique",
            caption="Снимок ошибки", owner_id=50,
        )
        assert await db.add_support_message(
            ticket_id=ticket_id, sender_type="admin", sender_telegram_id=1,
            message_type="text", text="Проверяем", is_admin=True,
        )
        assert len(await db.list_support_messages(ticket_id, owner_id=50)) == 3
        assert await db.list_support_messages(ticket_id, owner_id=51) == []
        assert await db.set_support_status(ticket_id, "closed", 50, is_admin=False)
        with pytest.raises(ValueError):
            await db.add_support_message(
                ticket_id=ticket_id, sender_type="user", sender_telegram_id=50,
                message_type="text", text="После закрытия", owner_id=50,
            )
        assert await db.set_support_status(ticket_id, "open", 1, is_admin=True)
        assert await db.soft_delete_support_ticket(ticket_id, 1)
        assert await db.get_support_ticket(ticket_id, owner_id=50) is None
        assert await db.get_support_ticket(ticket_id, is_admin=True, include_deleted=True)

    run(scenario())
