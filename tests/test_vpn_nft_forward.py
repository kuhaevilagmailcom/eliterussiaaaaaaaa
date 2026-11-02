"""The VPN NFT rebroadcast is opt-in, deduplicated and restart-safe."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from db import Database
from vpn_nft_forward import (
    _claim_recipient,
    audience_size,
    cancel_job,
    channel_origin_key,
    current_job,
    enqueue,
    job_by_id,
    raffle_keyboard,
    raffle_url,
    run_worker,
)


def test_link_and_button_point_to_nft_bot(monkeypatch):
    monkeypatch.delenv("NFT_RAFFLE_BOT_USERNAME", raising=False)
    assert raffle_url() == "https://t.me/AnonChatMgn_Bot?start=nft"
    button = raffle_keyboard().inline_keyboard[0][0]
    assert button.url == raffle_url()
    assert "Участвовать" in button.text


def test_accept_only_forward_from_channel():
    forwarded = SimpleNamespace(
        forward_origin=None,
        forward_from_chat=SimpleNamespace(id=-100987654321, type="channel"),
        forward_from_message_id=77,
    )
    assert channel_origin_key(forwarded) == "channel:-100987654321:77"
    forwarded.forward_from_chat = SimpleNamespace(id=54321, type="private")
    assert channel_origin_key(forwarded) is None


def test_job_sends_copied_post_with_link_and_prevents_repeat(tmp_path):
    async def scenario():
        path = str(tmp_path / "vpn.sqlite3")
        db = Database(path)
        await db.init()
        for uid in [101, 202, 303]:
            await db.ensure_user(uid, None, f"User {uid}")
        assert await audience_size(path) == 3
        status, job_id, count = await enqueue(
            path, actor_id=101, origin_key="channel:-10012:77",
            source_chat_id=101, source_message_id=42,
        )
        assert (status, count) == ("queued", 3)
        again = await enqueue(
            path, actor_id=101, origin_key="channel:-10012:77",
            source_chat_id=101, source_message_id=99,
        )
        assert again == ("duplicate", job_id, 3)
        busy = await enqueue(
            path, actor_id=101, origin_key="channel:-10012:78",
            source_chat_id=101, source_message_id=43,
        )
        assert busy[0] == "busy" and busy[1] == job_id
        bot = SimpleNamespace(copy_message=AsyncMock(), send_message=AsyncMock())
        task = asyncio.create_task(run_worker(bot, path, interval_seconds=0.01))
        try:
            for _ in range(300):
                row = await job_by_id(path, job_id)
                if row["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("Worker did not finish")
            assert row["sent"] == 3 and row["failed"] == 0
            assert row["total"] == 3
            assert bot.copy_message.await_count == 3
            assert {x.kwargs["chat_id"] for x in bot.copy_message.await_args_list} == {
                101, 202, 303,
            }
            for call in bot.copy_message.await_args_list:
                assert call.kwargs["from_chat_id"] == 101
                assert call.kwargs["message_id"] == 42
                markup = call.kwargs["reply_markup"]
                assert markup.inline_keyboard[0][0].url == (
                    "https://t.me/AnonChatMgn_Bot?start=nft"
                )
            # Worker reports progress to initiating admin.
            for _ in range(100):
                if bot.send_message.await_count:
                    break
                await asyncio.sleep(0.01)
            assert bot.send_message.await_count == 1
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    asyncio.run(scenario())


def test_cursor_is_committed_before_external_send(tmp_path):
    async def scenario():
        path = str(tmp_path / "vpn.sqlite3")
        db = Database(path)
        await db.init()
        for uid in [123, 456]:
            await db.ensure_user(uid, None, str(uid))
        outcome, job_id, _ = await enqueue(
            path, actor_id=123, origin_key="channel:-10012:89",
            source_chat_id=123, source_message_id=81,
        )
        assert outcome == "queued"
        status, _, first = await _claim_recipient(path, job_id)
        assert status == "recipient" and first == 123
        # Simulated crash before the Telegram API response; never resend to 123.
        status, _, second = await _claim_recipient(path, job_id)
        assert status == "recipient" and second == 456
        status, _, third = await _claim_recipient(path, job_id)
        assert status == "completed" and third is None
        assert await current_job(path) is None
        row = await job_by_id(path, job_id)
        assert row["total"] == 2 and row["last_user_id"] == 456
    asyncio.run(scenario())


def test_cancel_queued_job(tmp_path):
    async def scenario():
        path = str(tmp_path / "vpn.sqlite3")
        db = Database(path)
        await db.init()
        await db.ensure_user(101, None, "User")
        outcome, job_id, _ = await enqueue(
            path, actor_id=101, origin_key="channel:-10012:99",
            source_chat_id=101, source_message_id=19,
        )
        assert outcome == "queued"
        assert await cancel_job(path, job_id)
        assert not await cancel_job(path, job_id)
        assert (await job_by_id(path, job_id))["status"] == "cancelled"
    asyncio.run(scenario())
