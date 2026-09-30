import asyncio
from datetime import timedelta

import aiosqlite
import pytest

from db import Database, from_iso, to_iso, utcnow
from catalog import rub_to_stars


def run(coro):
    return asyncio.run(coro)


def test_legacy_naive_timestamps_are_normalized_to_utc():
    parsed = from_iso("2026-10-15T12:30:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(0)
    assert from_iso("broken") is None


def test_active_users_are_batched_for_vpn_sync(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        await db.ensure_user(1, "active", "Active")
        await db.ensure_user(2, "inactive", "Inactive")
        await db.extend_subscription(1, 30, "1 месяц", 1)
        users = await db.list_active_users_for_vpn_sync(limit=10)
        assert [user["telegram_id"] for user in users] == [1]
        assert users[0]["sub_token"]

    run(scenario())


def test_new_user_channel_verification_is_persisted(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "mgn.sqlite3"))
        await db.init()
        user = await db.ensure_user(9, "new_user", "New")
        assert user["channel_verified_at"] is None
        verified = await db.mark_channel_verified(9)
        assert verified["channel_verified_at"] is not None

    run(scenario())


def test_trial_is_granted_once_and_expiry_notice_is_idempotent(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "trial.sqlite3"))
        await db.init()
        await db.ensure_user(91, "trial_user", "Trial")

        granted = await db.grant_trial_once(91)
        assert granted is not None
        assert granted["trial_used"] == 1
        assert granted["plan_name"] == "Пробный доступ"
        until = from_iso(granted["subscription_until"])
        assert until is not None
        assert utcnow() + timedelta(hours=23, minutes=59) < until <= utcnow() + timedelta(days=1, minutes=1)
        assert await db.grant_trial_once(91) is None
        assert await db.list_pending_trial_grant_notifications() == [91]
        assert await db.claim_trial_grant_notification(91)
        assert not await db.claim_trial_grant_notification(91)
        assert await db.list_pending_trial_grant_notifications() == []

        # Trial users receive the dedicated expiry flow, not the ordinary
        # immediate "1 day remaining" paid-subscription reminder.
        assert await db.list_due_expiry_notifications() == []

        async with aiosqlite.connect(db.path) as connection:
            await connection.execute(
                "UPDATE users SET subscription_until=? WHERE telegram_id=?",
                (to_iso(utcnow() - timedelta(seconds=1)), 91),
            )
            await connection.commit()
        assert await db.list_expired_trial_notifications() == [91]
        assert await db.claim_trial_expiry_notification(91)
        assert not await db.claim_trial_expiry_notification(91)
        assert await db.list_expired_trial_notifications() == []

    run(scenario())


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
            original_amount_rub=99,
            discount_amount_rub=0,
            final_amount_rub=99,
            currency="XTR",
            currency_amount=62,
        )
        assert await db.mark_payment_intent_paid("intent-1")
        assert not await db.mark_payment_intent_paid("intent-1")

    run(scenario())


def test_admin_stats_separate_self_paid_from_admin_and_gift_access(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "admin-stats.sqlite3"))
        await db.init()
        for user_id in (201, 202, 203, 204):
            await db.ensure_user(user_id, f"u{user_id}", "User")

        await db.create_sbp_payment(
            payment_id="self-paid", order_id="self-order",
            telegram_id=201, target_telegram_id=201,
            plan_code="30", amount_rub=99, original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("self-paid")
        await db.grant_subscription_by_admin(
            202, 30, "30 дн.", granted_by=999,
        )
        await db.create_sbp_payment(
            payment_id="gift-paid", order_id="gift-order",
            telegram_id=204, target_telegram_id=203,
            plan_code="30", amount_rub=99, original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("gift-paid")

        stats = await db.admin_overview()
        assert stats["paid_total"] == 1
        assert stats["active_paid"] == 1
        assert stats["admin_granted_total"] == 1
        assert stats["active_admin_granted"] == 1

        await db.create_sbp_payment(
            payment_id="admin-user-buys", order_id="admin-user-order",
            telegram_id=202, target_telegram_id=202,
            plan_code="30", amount_rub=99, original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("admin-user-buys")
        stats = await db.admin_overview()
        assert stats["paid_total"] == 2
        assert stats["active_paid"] == 2
        assert stats["admin_granted_total"] == 0
        assert stats["active_admin_granted"] == 0

    run(scenario())


def test_expiry_reminders_are_unique_for_each_expiry_and_day(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "expiry-reminders.sqlite3"))
        await db.init()
        await db.ensure_user(301, "reminder", "Reminder")
        now = utcnow()
        expiry = now + timedelta(days=2, hours=2)

        async with aiosqlite.connect(db.path) as connection:
            await connection.execute(
                "UPDATE users SET subscription_until=? WHERE telegram_id=?",
                (to_iso(expiry), 301),
            )
            await connection.commit()

        due = await db.list_due_expiry_notifications(now=now)
        assert [(item["telegram_id"], item["days_before"]) for item in due] == [(301, 3)]
        assert await db.claim_expiry_notification(301, to_iso(expiry), 3)
        assert not await db.claim_expiry_notification(301, to_iso(expiry), 3)
        assert await db.list_due_expiry_notifications(now=now) == []

        two_day_pass = now + timedelta(hours=3)
        due = await db.list_due_expiry_notifications(now=two_day_pass)
        assert [(item["telegram_id"], item["days_before"]) for item in due] == [(301, 2)]

        await db.release_expiry_notification(301, to_iso(expiry), 3)
        due = await db.list_due_expiry_notifications(now=now)
        assert [(item["telegram_id"], item["days_before"]) for item in due] == [(301, 3)]

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


def test_paid_tariff_switch_stacks_days_instead_of_replacing_them(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "stacked-tariffs.sqlite3"))
        await db.init()
        await db.ensure_user(200, "stack", "Stack")

        await db.create_sbp_payment(
            payment_id="week",
            order_id="order-week",
            telegram_id=200,
            target_telegram_id=200,
            plan_code="30",
            amount_rub=99,
            original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("week")
        after_week = from_iso((await db.get_user(200))["subscription_until"])

        await db.create_sbp_payment(
            payment_id="month",
            order_id="order-month",
            telegram_id=200,
            target_telegram_id=200,
            plan_code="30",
            amount_rub=99,
            original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("month")
        user = await db.get_user(200)
        after_month = from_iso(user["subscription_until"])

        assert after_week is not None and after_month is not None
        assert after_month >= after_week + timedelta(days=30)
        assert user["plan_name"] == "1 месяц"

    run(scenario())


def test_attribution_source_is_first_touch_and_counts_paid_buyers(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "attribution.sqlite3"))
        await db.init()
        await db.ensure_user(101, "from_chat", "From Chat")
        await db.ensure_user(102, "no_payment", "No Payment")
        await db.ensure_user(103, "from_utm", "From UTM")

        assert await db.set_attribution_source_once(101, "anonchat_mgn")
        assert not await db.set_attribution_source_once(101, "other_campaign")
        assert await db.set_attribution_source_once(102, "anonchat_mgn")
        assert await db.set_attribution_source_once(103, "utm_telegram_ads")

        user = await db.get_user(101)
        assert user["attribution_source"] == "anonchat_mgn"
        assert user["attribution_at"]

        before = await db.attribution_stats("anonchat_mgn")
        assert before["arrived"] == 2
        assert before["buyers"] == 0
        assert before["conversion"] == 0.0

        await db.create_sbp_payment(
            payment_id="src-paid",
            order_id="src-order",
            telegram_id=101,
            target_telegram_id=101,
            plan_code="30",
            amount_rub=99,
            original_amount_rub=99,
        )
        assert await db.settle_sbp_payment("src-paid")

        after = await db.attribution_stats("anonchat_mgn")
        assert after["arrived"] == 2
        assert after["buyers"] == 1
        assert after["conversion"] == 50.0
        utm = await db.list_attribution_stats()
        assert utm == [{
            "source": "utm_telegram_ads",
            "arrived": 1,
            "buyers": 0,
            "conversion": 0.0,
        }]

    asyncio.run(scenario())


def test_star_conversion_uses_single_50_to_80_rate():
    assert {
        amount: rub_to_stars(amount)
        for amount in (80, 100, 150, 200, 300, 400, 500, 600, 1000)
    } == {80: 50, 100: 63, 150: 94, 200: 125, 300: 188, 400: 250, 500: 313, 600: 375, 1000: 625}


def test_paid_promo_is_revalidated_atomically_at_settlement(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "promo-settlement.sqlite3"))
        await db.init()
        for user_id in (101, 102):
            await db.ensure_user(user_id, f"u{user_id}", "Buyer")
        promo = await db.create_service_promo(
            code="ONLYONE",
            promo_type="discount",
            value=50,
            max_uses=1,
            created_by=1,
            applicable_plans="30",
        )
        for index, user_id in enumerate((101, 102), 1):
            await db.create_sbp_payment(
                payment_id=f"pay-{index}",
                order_id=f"order-{index}",
                telegram_id=user_id,
                target_telegram_id=user_id,
                plan_code="30",
                amount_rub=50,
                original_amount_rub=99,
                discount_amount_rub=49,
                promo_id=int(promo["id"]),
                promo_code="ONLYONE",
            )

        results = await asyncio.gather(
            db.settle_sbp_payment("pay-1"),
            db.settle_sbp_payment("pay-2"),
            return_exceptions=True,
        )
        assert sum(result is True for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 1
        promos = await db.list_service_promos()
        assert promos[0]["used_count"] == 1

    run(scenario())


@pytest.mark.parametrize("change", ["expired", "disabled"])
def test_paid_promo_rejects_expiry_or_disable_after_invoice(tmp_path, change):
    async def scenario():
        db = Database(str(tmp_path / f"promo-{change}.sqlite3"))
        await db.init()
        await db.ensure_user(110, "buyer", "Buyer")
        promo = await db.create_service_promo(
            code="LATE",
            promo_type="discount",
            value=20,
            created_by=1,
            applicable_plans="30",
        )
        await db.create_payment_intent(
            intent_id="intent-late",
            buyer_id=110,
            target_id=110,
            product_code="30",
            original_amount_rub=99,
            discount_amount_rub=19,
            final_amount_rub=80,
            currency="XTR",
            currency_amount=50,
            promo_id=int(promo["id"]),
            promo_code="LATE",
        )
        async with aiosqlite.connect(db.path) as connection:
            if change == "expired":
                await connection.execute(
                    "UPDATE service_promo_codes SET expires_at=? WHERE id=?",
                    ("2000-01-01T00:00:00+00:00", promo["id"]),
                )
            else:
                await connection.execute(
                    "UPDATE service_promo_codes SET active=0 WHERE id=?",
                    (promo["id"],),
                )
            await connection.commit()
        with pytest.raises(ValueError):
            await db.settle_star_payment(
                "charge-late", 110, 110, "30", 50, intent_id="intent-late"
            )
        assert (await db.get_payment_intent("intent-late"))["status"] == "created"

    run(scenario())


def test_stars_intent_expires_and_replay_is_idempotent(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "intent-expiry.sqlite3"))
        await db.init()
        await db.ensure_user(120, "buyer", "Buyer")
        await db.create_payment_intent(
            intent_id="fresh",
            buyer_id=120,
            target_id=120,
            product_code="30",
            original_amount_rub=99,
            discount_amount_rub=0,
            final_amount_rub=99,
            currency="XTR",
            currency_amount=62,
        )
        assert await db.settle_star_payment("charge-fresh", 120, 120, "30", 62, intent_id="fresh")
        assert not await db.settle_star_payment("charge-fresh", 120, 120, "30", 62, intent_id="fresh")

        await db.create_payment_intent(
            intent_id="expired",
            buyer_id=120,
            target_id=120,
            product_code="30",
            original_amount_rub=99,
            discount_amount_rub=0,
            final_amount_rub=99,
            currency="XTR",
            currency_amount=62,
        )
        async with aiosqlite.connect(db.path) as connection:
            await connection.execute(
                "UPDATE payment_intents SET expires_at=? WHERE intent_id='expired'",
                ("2000-01-01T00:00:00+00:00",),
            )
            await connection.commit()
        with pytest.raises(ValueError, match="expired"):
            await db.settle_star_payment("charge-expired", 120, 120, "30", 62, intent_id="expired")

    run(scenario())


def test_full_discount_is_atomic_and_does_not_create_payment(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "free-promo.sqlite3"))
        await db.init()
        await db.ensure_user(130, "free", "Free")
        promo = await db.create_service_promo(
            code="FREE100", promo_type="discount", value=100,
            max_uses=1, created_by=1, applicable_plans="30",
        )
        granted = await db.redeem_full_discount(
            promo_id=int(promo["id"]), buyer_id=130,
            target_id=130, product_code="30",
        )
        assert from_iso(granted["subscription_until"]) > utcnow() + timedelta(days=29)
        with pytest.raises(ValueError):
            await db.redeem_full_discount(
                promo_id=int(promo["id"]), buyer_id=130,
                target_id=130, product_code="30",
            )
        async with aiosqlite.connect(db.path) as connection:
            assert (await (await connection.execute("SELECT COUNT(*) FROM star_payments")).fetchone())[0] == 0
            assert (await (await connection.execute("SELECT COUNT(*) FROM sbp_payments")).fetchone())[0] == 0

    run(scenario())


def test_private_vpn_id_and_interaction_session_survive_restart(tmp_path):
    async def scenario():
        path = str(tmp_path / "session.sqlite3")
        db = Database(path)
        await db.init()
        created = await db.ensure_user(140, "gift", "Gift")
        assert created["vpn_client_id"]
        assert str(created["telegram_id"]) not in created["vpn_client_id"]
        await db.set_support_session(140, "gift", payload="90")

        reopened = Database(path)
        await reopened.init()
        session = await reopened.get_support_session(140)
        assert session and session["mode"] == "gift" and session["payload"] == "90"

    run(scenario())


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


def test_support_close_is_compatible_with_legacy_status_constraint(tmp_path):
    async def scenario():
        path = tmp_path / "legacy-support.sqlite3"
        async with aiosqlite.connect(path) as connection:
            await connection.execute(
                """
                CREATE TABLE support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open', 'answered')),
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    answered_by INTEGER,
                    answer_text TEXT
                )
                """
            )
            await connection.commit()

        db = Database(str(path))
        await db.init()
        await db.ensure_user(50, "owner", "Owner")
        ticket = await db.create_support_thread(
            telegram_id=50,
            username="owner",
            first_name="Owner",
            message_type="text",
            text="Не работает подключение",
        )
        ticket_id = int(ticket["id"])

        closed = await db.set_support_status(ticket_id, "closed", 1, is_admin=True)
        assert closed and closed["status"] == "closed"
        closed_tickets, total = await db.list_support_tickets(status="closed")
        assert total == 1
        assert closed_tickets[0]["id"] == ticket_id
        with pytest.raises(ValueError):
            await db.add_support_message(
                ticket_id=ticket_id,
                sender_type="admin",
                sender_telegram_id=1,
                message_type="text",
                text="Ответ после закрытия",
                is_admin=True,
            )

        reopened = await db.set_support_status(ticket_id, "open", 1, is_admin=True)
        assert reopened and reopened["status"] == "open"
        answered = await db.answer_support_ticket(
            ticket_id=ticket_id,
            answered_by=1,
            answer_text="Проверили и исправили",
        )
        assert answered and answered["status"] == "answered"

    run(scenario())


def test_first_bot_start_is_distinct_from_other_user_creation(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "first-start.sqlite3"))
        await db.init()
        await db.ensure_user(501, "giveaway_user", "User")
        assert await db.claim_first_bot_start(501)
        assert not await db.claim_first_bot_start(501)

    run(scenario())


def test_giveaway_can_be_staged_before_channel_publication(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "giveaway-stage.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        item = await db.create_giveaway(
            created_by=1,
            text_html="Test",
            text_plain="Test",
            photo_file_id=None,
            winners_count=1,
            prize_days=30,
            end_mode="participants",
            participant_limit=5,
            activate=False,
        )
        assert item["status"] == "cancelled"
        assert not await db.activate_giveaway(int(item["id"]))
        await db.add_giveaway_post(int(item["id"]), "@test_channel", 99)
        assert await db.activate_giveaway(int(item["id"]))
        assert (await db.get_giveaway(int(item["id"])))["status"] == "active"

    run(scenario())

def test_finished_giveaway_winner_can_be_replaced_atomically(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "reroll.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        await db.ensure_user(101, "oldwinner", "Old")
        await db.ensure_user(102, "newwinner", "New")
        await db.ensure_user(103, "third", "Third")

        paid = await db.extend_subscription(101, 60, "2 месяца", 1)
        paid_until = from_iso(paid["subscription_until"])

        giveaway = await db.create_giveaway(
            created_by=1,
            text_html="Test",
            text_plain="Test",
            photo_file_id=None,
            winners_count=1,
            prize_days=30,
            end_mode="participants",
            participant_limit=3,
        )
        giveaway_id = int(giveaway["id"])
        for uid, username, name in (
            (101, "oldwinner", "Old"),
            (102, "newwinner", "New"),
            (103, "third", "Third"),
        ):
            result = await db.add_giveaway_participant(
                giveaway_id=giveaway_id,
                telegram_id=uid,
                username=username,
                first_name=name,
            )
            assert result["state"] == "joined"

        participants = await db.list_giveaway_participants(giveaway_id)
        old = next(item for item in participants if int(item["telegram_id"]) == 101)
        await db.save_giveaway_winners(giveaway_id, [old])
        assert await db.grant_giveaway_prizes(giveaway_id) == [101]
        await db.mark_giveaway_finished(giveaway_id)

        after_prize = from_iso((await db.get_user(101))["subscription_until"])
        assert after_prize >= paid_until + timedelta(days=30) - timedelta(seconds=2)

        result = await db.replace_giveaway_winner(
            giveaway_id,
            old_telegram_id=101,
            new_telegram_id=102,
            rerolled_by=1,
        )
        assert result["old_telegram_id"] == 101
        assert result["new_telegram_id"] == 102

        restored = from_iso((await db.get_user(101))["subscription_until"])
        assert abs((restored - paid_until).total_seconds()) < 3

        winners = await db.get_giveaway_winners(giveaway_id)
        assert [int(item["telegram_id"]) for item in winners] == [102]
        assert winners[0]["granted_at"] is None
        rerolls = await db.list_giveaway_rerolls(giveaway_id)
        assert len(rerolls) == 1
        assert int(rerolls[0]["old_telegram_id"]) == 101
        assert int(rerolls[0]["new_telegram_id"]) == 102

        assert await db.grant_giveaway_prizes(giveaway_id) == [102]
        replacement = await db.get_user(102)
        assert from_iso(replacement["subscription_until"]) > utcnow() + timedelta(days=29)

    run(scenario())


def test_giveaway_participants_page_marks_current_winner(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "participants.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        giveaway = await db.create_giveaway(
            created_by=1,
            text_html="Test",
            text_plain="Test",
            photo_file_id=None,
            winners_count=1,
            prize_days=30,
            end_mode="participants",
            participant_limit=2,
        )
        giveaway_id = int(giveaway["id"])
        for uid in (201, 202):
            await db.ensure_user(uid, f"user{uid}", f"User {uid}")
            await db.add_giveaway_participant(
                giveaway_id=giveaway_id,
                telegram_id=uid,
                username=f"user{uid}",
                first_name=f"User {uid}",
            )
        participants = await db.list_giveaway_participants(giveaway_id)
        await db.save_giveaway_winners(giveaway_id, [participants[0]])

        rows, total = await db.list_giveaway_participants_page(
            giveaway_id,
            page=0,
            page_size=20,
        )
        assert total == 2
        assert [int(item["is_winner"]) for item in rows] == [1, 0]

    run(scenario())
