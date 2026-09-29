from __future__ import annotations

import asyncio
import html
import logging
import secrets
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db import Database, from_iso, utcnow
from vpn import VpnProvider


logger = logging.getLogger(__name__)
_finish_locks: dict[int, asyncio.Lock] = {}


def giveaway_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Принять участие",
                    callback_data=f"giveaway:join:{int(giveaway_id)}",
                )
            ]
        ]
    )


def _chat_id(value: str | int) -> str | int:
    raw = str(value)
    if raw.startswith("-100") and raw[1:].isdigit():
        return int(raw)
    return raw


def _days_label(days: int) -> str:
    days = int(days)
    if days % 10 == 1 and days % 100 != 11:
        word = "день"
    elif days % 10 in {2, 3, 4} and days % 100 not in {12, 13, 14}:
        word = "дня"
    else:
        word = "дней"
    return f"{days} {word}"


def render_giveaway_post(
    giveaway: dict[str, Any],
    *,
    participant_count: int,
    winners: list[dict[str, Any]] | None = None,
    display_tz=None,
) -> str:
    base = str(giveaway.get("text_html") or "").strip()
    winners_count = int(giveaway.get("winners_count") or 1)
    prize_days = int(giveaway.get("prize_days") or 1)
    status = str(giveaway.get("status") or "active")

    lines = [
        base,
        "",
        f"🎁 <b>Приз:</b> {winners_count} × {_days_label(prize_days)} MGN VPN",
    ]

    if status in {"active", "finishing"} and winners is None:
        if giveaway.get("end_mode") == "time":
            ends = from_iso(giveaway.get("ends_at"))
            if ends and display_tz is not None:
                ends = ends.astimezone(display_tz)
            end_text = ends.strftime("%d.%m.%Y · %H:%M") if ends else "по времени"
            lines.append(f"⏳ <b>Итоги:</b> {end_text}")
        else:
            lines.append(
                f"👥 <b>Итоги:</b> при {int(giveaway.get('participant_limit') or 0)} участниках"
            )
        lines.append("")
        lines.append("Нажмите кнопку ниже, чтобы участвовать.")
        return "\n".join(lines)

    lines += [
        f"👥 <b>Участников:</b> {int(participant_count)}",
        "",
        "✅ <b>Розыгрыш завершён</b>",
        "",
        "🏆 <b>Победители:</b>",
    ]
    if not winners:
        lines.append("Участников для выбора победителей не было.")
    else:
        for index, item in enumerate(winners, start=1):
            username = str(item.get("username") or "").strip().lstrip("@")
            if username:
                label = "@" + html.escape(username)
            else:
                first_name = str(item.get("first_name") or "").strip()
                label = html.escape(first_name) if first_name else f"Победитель #{index}"
            lines.append(f"{index}. {label}")
        lines += [
            "",
            f"🎉 Каждый победитель получил {_days_label(prize_days)} MGN VPN.",
        ]
    return "\n".join(lines)


async def send_giveaway_post(
    bot,
    giveaway: dict[str, Any],
    chat_id: str | int,
    *,
    display_tz=None,
):
    text = render_giveaway_post(
        giveaway,
        participant_count=int(giveaway.get("participant_count") or 0),
        display_tz=display_tz,
    )
    markup = giveaway_keyboard(int(giveaway["id"]))
    photo = str(giveaway.get("photo_file_id") or "").strip()
    if photo:
        return await bot.send_photo(
            chat_id=_chat_id(chat_id),
            photo=photo,
            caption=text,
            reply_markup=markup,
        )
    return await bot.send_message(
        chat_id=_chat_id(chat_id),
        text=text,
        reply_markup=markup,
    )


async def _finalize_channel_posts(
    bot,
    db: Database,
    config,
    giveaway: dict[str, Any],
    winners: list[dict[str, Any]],
) -> None:
    posts = await db.list_giveaway_posts(int(giveaway["id"]))
    final_text = render_giveaway_post(
        giveaway,
        participant_count=int(giveaway.get("participant_count") or 0),
        winners=winners,
        display_tz=config.display_tz,
    )
    has_photo = bool(str(giveaway.get("photo_file_id") or "").strip())

    for post in posts:
        if post.get("finalized_at"):
            continue
        chat_id = _chat_id(post["chat_id"])
        message_id = int(post["message_id"])
        try:
            if has_photo:
                await bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=message_id,
                    caption=final_text,
                    reply_markup=None,
                )
            else:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=final_text,
                    reply_markup=None,
                )
        except TelegramBadRequest as exc:
            message = str(exc).lower()
            if (
                "message is not modified" not in message
                and "message to edit not found" not in message
                and "message can't be edited" not in message
            ):
                logger.warning(
                    "Could not finalize giveaway %s post in %s: %s",
                    giveaway["id"],
                    chat_id,
                    type(exc).__name__,
                )
                continue
        except TelegramForbiddenError:
            logger.warning(
                "Could not edit giveaway %s post in %s: bot has no access",
                giveaway["id"],
                chat_id,
            )
        except Exception as exc:
            logger.warning(
                "Could not finalize giveaway %s post in %s: %s",
                giveaway["id"],
                chat_id,
                type(exc).__name__,
            )
            continue
        await db.mark_giveaway_post_finalized(int(giveaway["id"]), post["chat_id"])


async def _notify_winners(
    bot,
    db: Database,
    giveaway: dict[str, Any],
    winners: list[dict[str, Any]],
) -> None:
    days = int(giveaway.get("prize_days") or 1)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть VPN", callback_data="menu:connect")]
        ]
    )
    for item in winners:
        if item.get("notified_at"):
            continue
        telegram_id = int(item["telegram_id"])
        try:
            await bot.send_message(
                telegram_id,
                "🎉 <b>Вы выиграли розыгрыш MGN VPN!</b>\n\n"
                f"На ваш аккаунт начислена подписка на <b>{_days_label(days)}</b>.\n"
                "Она уже активна и готова к использованию.",
                reply_markup=markup,
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            # The prize is still granted. Some channel participants may not
            # have opened the bot privately yet, so do not retry forever.
            pass
        except Exception as exc:
            logger.warning(
                "Winner notification failed for giveaway %s / user %s: %s",
                giveaway["id"],
                telegram_id,
                type(exc).__name__,
            )
            continue
        await db.mark_giveaway_winner_notified(int(giveaway["id"]), telegram_id)


async def finish_giveaway(
    bot,
    db: Database,
    config,
    provider: VpnProvider,
    giveaway_id: int,
    *,
    force: bool = False,
) -> bool:
    giveaway_id = int(giveaway_id)
    lock = _finish_locks.setdefault(giveaway_id, asyncio.Lock())
    async with lock:
        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway or str(giveaway.get("status")) == "cancelled":
            return False

        if giveaway.get("status") == "active":
            if not force and not await db.giveaway_is_due(giveaway_id):
                return False
            await db.mark_giveaway_finishing(giveaway_id)
            giveaway = await db.get_giveaway(giveaway_id)
            if not giveaway:
                return False

        if giveaway.get("status") == "finishing":
            winners = await db.get_giveaway_winners(giveaway_id)
            if not winners:
                participants = await db.list_giveaway_participants(giveaway_id)
                count = min(int(giveaway.get("winners_count") or 1), len(participants))
                selected = (
                    secrets.SystemRandom().sample(participants, count)
                    if count > 0
                    else []
                )
                await db.save_giveaway_winners(giveaway_id, selected)
                winners = await db.get_giveaway_winners(giveaway_id)

            granted = await db.grant_giveaway_prizes(giveaway_id)
            await db.mark_giveaway_finished(giveaway_id)

            async def sync_winner(telegram_id: int) -> None:
                try:
                    user = await db.get_user(int(telegram_id))
                    if getattr(provider, "service_ready", True):
                        await asyncio.wait_for(provider.provision(user), timeout=8.0)
                except Exception as exc:
                    logger.warning(
                        "Giveaway VPN sync deferred for user %s: %s",
                        telegram_id,
                        type(exc).__name__,
                    )

            if granted:
                await asyncio.gather(*(sync_winner(telegram_id) for telegram_id in granted))

        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway:
            return False
        winners = await db.get_giveaway_winners(giveaway_id)

        await _finalize_channel_posts(bot, db, config, giveaway, winners)
        winners = await db.get_giveaway_winners(giveaway_id)
        await _notify_winners(bot, db, giveaway, winners)
        return True


async def giveaway_reconciliation_loop(
    bot,
    db: Database,
    config,
    provider: VpnProvider,
) -> None:
    while True:
        try:
            for giveaway_id in await db.list_giveaways_needing_work():
                try:
                    await finish_giveaway(
                        bot,
                        db,
                        config,
                        provider,
                        giveaway_id,
                    )
                except Exception:
                    logger.exception(
                        "Giveaway reconciliation failed for #%s",
                        giveaway_id,
                    )
        except Exception:
            logger.exception("Giveaway reconciliation pass failed")
        await asyncio.sleep(15)
