import asyncio
from datetime import timedelta

from db import Database, from_iso, utcnow


def run(coro):
    return asyncio.run(coro)


def test_trial_and_referral_rewards_are_atomic_and_capped(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        for referred_id in range(2, 6):
            await db.ensure_user(referred_id, f"u{referred_id}", "Friend")
            assert await db.set_referrer_once(referred_id, 1)
            assert not await db.set_referrer_once(referred_id, 999)
            assert await db.activate_trial(referred_id, 1440, 1)
            assert not await db.activate_trial(referred_id, 1440, 1)
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
            currency_amount=86,
        )
        assert await db.mark_payment_intent_paid("intent-1")
        assert not await db.mark_payment_intent_paid("intent-1")

    run(scenario())
