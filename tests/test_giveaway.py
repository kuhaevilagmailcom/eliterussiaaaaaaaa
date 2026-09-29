from giveaway import render_giveaway_post


def test_giveaway_does_not_publish_private_first_name_without_username():
    giveaway = {
        "text_html": "Тестовый розыгрыш",
        "winners_count": 1,
        "prize_days": 30,
        "status": "finished",
    }
    text = render_giveaway_post(
        giveaway,
        participant_count=1,
        winners=[{"telegram_id": 42, "username": "", "first_name": "Private Name"}],
    )
    assert "Private Name" not in text
    assert "Победитель #1" in text


def test_cancelled_giveaway_has_no_winner_claim():
    giveaway = {
        "text_html": "Тестовый розыгрыш",
        "winners_count": 2,
        "prize_days": 90,
        "status": "cancelled",
    }
    text = render_giveaway_post(giveaway, participant_count=10)
    assert "Розыгрыш отменён" in text
    assert "Победители:" not in text

import asyncio
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db import Database, from_iso, utcnow
from giveaway import finish_giveaway, reroll_giveaway_winner, undo_giveaway_reroll


def test_finished_giveaway_notifies_admin_with_participants_and_reroll_buttons(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "admin-notify.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        await db.ensure_user(301, "winner", "Winner")

        giveaway = await db.create_giveaway(
            created_by=1,
            text_html="Test",
            text_plain="Test",
            photo_file_id=None,
            winners_count=1,
            prize_days=30,
            end_mode="participants",
            participant_limit=1,
        )
        giveaway_id = int(giveaway["id"])
        await db.add_giveaway_participant(
            giveaway_id=giveaway_id,
            telegram_id=301,
            username="winner",
            first_name="Winner",
        )

        bot = SimpleNamespace(send_message=AsyncMock())
        config = SimpleNamespace(admin_ids=(1,), display_tz=timezone.utc)
        provider = SimpleNamespace(service_ready=False)
        assert await finish_giveaway(
            bot,
            db,
            config,
            provider,
            giveaway_id,
            force=True,
        )

        admin_calls = [
            call for call in bot.send_message.await_args_list
            if call.args and int(call.args[0]) == 1
        ]
        assert len(admin_calls) == 1
        markup = admin_calls[0].kwargs["reply_markup"]
        callback_data = [
            button.callback_data
            for row in markup.inline_keyboard
            for button in row
        ]
        assert f"admin:giveaway:participants:{giveaway_id}:0" in callback_data
        assert f"admin:giveaway:reroll:{giveaway_id}" in callback_data
        assert (await db.get_giveaway(giveaway_id))["admin_notified_at"] is not None

    asyncio.run(scenario())


def test_reroll_moves_prize_to_another_participant(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "service-reroll.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        for uid, username in ((401, "old"), (402, "new")):
            await db.ensure_user(uid, username, username.title())

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
        for uid, username in ((401, "old"), (402, "new")):
            await db.add_giveaway_participant(
                giveaway_id=giveaway_id,
                telegram_id=uid,
                username=username,
                first_name=username.title(),
            )

        participants = await db.list_giveaway_participants(giveaway_id)
        old = next(item for item in participants if int(item["telegram_id"]) == 401)
        await db.save_giveaway_winners(giveaway_id, [old])
        await db.grant_giveaway_prizes(giveaway_id)
        await db.mark_giveaway_finished(giveaway_id)

        bot = SimpleNamespace(send_message=AsyncMock())
        config = SimpleNamespace(admin_ids=(1,), display_tz=timezone.utc)
        provider = SimpleNamespace(service_ready=False)

        result = await reroll_giveaway_winner(
            bot,
            db,
            config,
            provider,
            giveaway_id,
            401,
            rerolled_by=1,
        )
        assert result["new_telegram_id"] == 402
        winners = await db.get_giveaway_winners(giveaway_id)
        assert [int(item["telegram_id"]) for item in winners] == [402]
        assert from_iso((await db.get_user(401))["subscription_until"]) is None
        assert from_iso((await db.get_user(402))["subscription_until"]) > utcnow()

    asyncio.run(scenario())

def test_reroll_can_be_undone_within_safety_window(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "reroll-undo.sqlite3"))
        await db.init()
        await db.ensure_user(1, "owner", "Owner")
        for uid, username in ((501, "first"), (502, "second")):
            await db.ensure_user(uid, username, username.title())

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
        for uid, username in ((501, "first"), (502, "second")):
            await db.add_giveaway_participant(
                giveaway_id=giveaway_id,
                telegram_id=uid,
                username=username,
                first_name=username.title(),
            )
        participants = await db.list_giveaway_participants(giveaway_id)
        first = next(row for row in participants if int(row["telegram_id"]) == 501)
        await db.save_giveaway_winners(giveaway_id, [first])
        await db.grant_giveaway_prizes(giveaway_id)
        await db.mark_giveaway_finished(giveaway_id)

        bot = SimpleNamespace(send_message=AsyncMock())
        config = SimpleNamespace(admin_ids=(1,), display_tz=timezone.utc)
        provider = SimpleNamespace(service_ready=False)

        rerolled = await reroll_giveaway_winner(
            bot,
            db,
            config,
            provider,
            giveaway_id,
            501,
            rerolled_by=1,
        )
        assert rerolled["new_telegram_id"] == 502

        undone = await undo_giveaway_reroll(
            bot,
            db,
            config,
            provider,
            giveaway_id,
            undone_by=1,
            max_age_seconds=600,
        )
        assert undone["restored_telegram_id"] == 501
        winners = await db.get_giveaway_winners(giveaway_id)
        assert [int(row["telegram_id"]) for row in winners] == [501]
        rerolls = await db.list_giveaway_rerolls(giveaway_id)
        assert rerolls[-1]["undone_at"] is not None

    asyncio.run(scenario())
