"""Durable admin-confirmed forwarding of channel posts to MGN VPN bot users.

The post is copied from the administrator's private chat. Telegram does not
preserve inline markup when copying, so an explicit NFT participation URL
button is attached to *every* copy. The worker resumes after a restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import aiosqlite
from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageOriginChannel

log = logging.getLogger(__name__)
DB_TIMEOUT = 15.0


def raffle_url() -> str:
    username = os.getenv("NFT_RAFFLE_BOT_USERNAME", "AnonChatMgn_Bot").strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        raise ValueError("NFT_RAFFLE_BOT_USERNAME must be a Telegram bot username")
    return f"https://t.me/{username}?start=nft"


def raffle_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎟 Участвовать в розыгрыше", url=raffle_url())
    ]])


def channel_origin_key(message: Message) -> str | None:
    """Reject arbitrary/private messages: only normal forwards from channels."""
    origin = getattr(message, "forward_origin", None)
    if isinstance(origin, MessageOriginChannel):
        return f"channel:{origin.chat.id}:{origin.message_id}"
    chat = getattr(message, "forward_from_chat", None)
    post_id = getattr(message, "forward_from_message_id", None)
    if chat is not None and getattr(chat, "type", "") == "channel" and post_id:
        return f"channel:{chat.id}:{int(post_id)}"
    return None


async def init_tables(path: str | Path) -> None:
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        await con.execute("PRAGMA busy_timeout=10000")
        await con.execute("""
            CREATE TABLE IF NOT EXISTS vpn_nft_forward_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL,
                origin_key TEXT NOT NULL UNIQUE,
                source_chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                last_user_id INTEGER NOT NULL DEFAULT 0,
                max_user_id INTEGER NOT NULL DEFAULT 0,
                total INTEGER NOT NULL DEFAULT 0,
                sent INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                finished_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        await con.commit()


async def audience_size(path: str | Path) -> int:
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        row = await (await con.execute("SELECT COUNT(*) FROM users")).fetchone()
        return int(row[0])


async def enqueue(
    path: str | Path, *, actor_id: int, origin_key: str,
    source_chat_id: int, source_message_id: int,
) -> tuple[str, int, int]:
    """Atomic dedupe, one simultaneous campaign, fixed recipient snapshot."""
    await init_tables(path)
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        await con.execute("PRAGMA busy_timeout=10000")
        await con.execute("BEGIN IMMEDIATE")
        duplicate = await (await con.execute(
            "SELECT id,total FROM vpn_nft_forward_jobs WHERE origin_key=?",
            (origin_key,),
        )).fetchone()
        if duplicate:
            await con.commit()
            return "duplicate", int(duplicate[0]), int(duplicate[1])
        busy = await (await con.execute(
            "SELECT id,total FROM vpn_nft_forward_jobs "
            "WHERE status IN ('queued','running') ORDER BY id LIMIT 1"
        )).fetchone()
        if busy:
            await con.commit()
            return "busy", int(busy[0]), int(busy[1])
        audience = await (await con.execute(
            "SELECT COUNT(*), COALESCE(MAX(telegram_id),0) FROM users"
        )).fetchone()
        total, max_id = int(audience[0]), int(audience[1])
        cur = await con.execute(
            "INSERT INTO vpn_nft_forward_jobs "
            "(actor_id,origin_key,source_chat_id,source_message_id,"
            "max_user_id,total,created_at) VALUES (?,?,?,?,?,?,?)",
            (actor_id, origin_key, source_chat_id, source_message_id,
             max_id, total, int(time.time())),
        )
        await con.commit()
        return "queued", int(cur.lastrowid), total


async def current_job(path: str | Path) -> dict | None:
    await init_tables(path)
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        con.row_factory = aiosqlite.Row
        row = await (await con.execute(
            "SELECT * FROM vpn_nft_forward_jobs WHERE status IN ('queued','running') "
            "ORDER BY id LIMIT 1"
        )).fetchone()
        return dict(row) if row else None


async def job_by_id(path: str | Path, job_id: int) -> dict | None:
    await init_tables(path)
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        con.row_factory = aiosqlite.Row
        row = await (await con.execute(
            "SELECT * FROM vpn_nft_forward_jobs WHERE id=?", (int(job_id),)
        )).fetchone()
        return dict(row) if row else None


async def cancel_job(path: str | Path, job_id: int) -> bool:
    await init_tables(path)
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        result = await con.execute(
            "UPDATE vpn_nft_forward_jobs SET status='cancelled',finished_at=? "
            "WHERE id=? AND status IN ('queued','running')",
            (int(time.time()), int(job_id)),
        )
        await con.commit()
        return result.rowcount == 1


async def _claim_recipient(path: str | Path, job_id: int) -> tuple[str, dict | None, int | None]:
    """Advance cursor BEFORE the Bot API call to avoid duplicate sends on restart.

    If the process exits mid-send, this one recipient may be unconfirmed;
    the final count reports it separately instead of silently resending.
    """
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        con.row_factory = aiosqlite.Row
        await con.execute("PRAGMA busy_timeout=10000")
        await con.execute("BEGIN IMMEDIATE")
        raw = await (await con.execute(
            "SELECT * FROM vpn_nft_forward_jobs WHERE id=?", (job_id,)
        )).fetchone()
        if raw is None:
            await con.commit()
            return "missing", None, None
        job = dict(raw)
        if job["status"] not in ("queued", "running"):
            await con.commit()
            return str(job["status"]), job, None
        row = await (await con.execute(
            "SELECT telegram_id FROM users WHERE telegram_id>? "
            "AND telegram_id<=? ORDER BY telegram_id LIMIT 1",
            (int(job["last_user_id"]), int(job["max_user_id"])),
        )).fetchone()
        if row is None:
            await con.execute(
                "UPDATE vpn_nft_forward_jobs SET status='completed',finished_at=? "
                "WHERE id=? AND status IN ('queued','running')",
                (int(time.time()), job_id),
            )
            await con.commit()
            return "completed", job, None
        uid = int(row[0])
        await con.execute(
            "UPDATE vpn_nft_forward_jobs SET last_user_id=?,status='running' "
            "WHERE id=? AND status IN ('queued','running')",
            (uid, job_id),
        )
        await con.commit()
        return "recipient", job, uid


async def _record_delivery(path: str | Path, job_id: int, delivered: bool) -> None:
    col = "sent" if delivered else "failed"
    async with aiosqlite.connect(path, timeout=DB_TIMEOUT) as con:
        await con.execute(
            f"UPDATE vpn_nft_forward_jobs SET {col}={col}+1 WHERE id=?",
            (job_id,),
        )
        await con.commit()


async def run_worker(bot, path: str | Path, interval_seconds: float = 3.0) -> None:
    """Runs until cancelled; one job at a time, no dependence on admin callback."""
    await init_tables(path)
    while True:
        try:
            job = await current_job(path)
            if job is None:
                await asyncio.sleep(interval_seconds)
                continue
            job_id = int(job["id"])
            while True:
                status, snapshot, uid = await _claim_recipient(path, job_id)
                if status != "recipient" or uid is None or snapshot is None:
                    if status == "completed":
                        result = await job_by_id(path, job_id)
                        if result:
                            uncertain = max(0, int(result["total"]) - int(result["sent"]) - int(result["failed"]))
                            text = (
                                "📣 <b>Рассылка NFT завершена</b>\n\n"
                                f"Отправлено: <b>{result['sent']}</b>\n"
                                f"Не доставлено: <b>{result['failed']}</b>\n"
                                f"Без подтверждения после сбоя: <b>{uncertain}</b>\n"
                                f"Всего адресатов: <b>{result['total']}</b>"
                            )
                            try:
                                await bot.send_message(int(result["actor_id"]), text)
                            except Exception:
                                log.warning("Could not notify NFT broadcaster about job %s", job_id)
                    break
                for retry in range(2):
                    try:
                        await bot.copy_message(
                            chat_id=uid,
                            from_chat_id=int(snapshot["source_chat_id"]),
                            message_id=int(snapshot["source_message_id"]),
                            reply_markup=raffle_keyboard(),
                        )
                        await _record_delivery(path, job_id, True)
                        break
                    except TelegramRetryAfter as exc:
                        if retry == 0:
                            await asyncio.sleep(min(max(1.0, float(exc.retry_after)), 60.0))
                            continue
                        await _record_delivery(path, job_id, False)
                    except (TelegramForbiddenError, TelegramBadRequest):
                        await _record_delivery(path, job_id, False)
                        break
                    except TelegramNetworkError:
                        # Delivery could have succeeded; never auto-resend.
                        log.warning("Ambiguous NFT broadcast delivery, job=%s", job_id)
                        break
                    except Exception:
                        log.exception("NFT broadcast delivery failed job=%s", job_id)
                        await _record_delivery(path, job_id, False)
                        break
                await asyncio.sleep(0.07)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("NFT broadcast worker failed; retrying")
            await asyncio.sleep(5.0)
