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
    include_base: bool = True,
) -> str:
    base = str(giveaway.get("text_html") or "").strip() if include_base else ""
    winners_count = int(giveaway.get("winners_count") or 1)
    prize_days = int(giveaway.get("prize_days") or 1)
    status = str(giveaway.get("status") or "active")

    lines = [
        base,
        "",
        f"🎁 <b>Приз:</b> {winners_count} × {_days_label(prize_days)} MGN VPN",
    ]

    if status == "cancelled":
        lines += [
            f"👥 <b>Участников:</b> {int(participant_count)}",
            "",
            "⚪ <b>Розыгрыш отменён</b>",
        ]
        return "\n".join(line for line in lines if line != "" or base)

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
                label = f"Победитель #{index}"
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
    # Telegram photo captions are much shorter than normal messages. Keep the
    # result publishable even when the original post and winner list are long.
    if has_photo and len(final_text) > 900:
        final_text = render_giveaway_post(
            giveaway,
            participant_count=int(giveaway.get("participant_count") or 0),
            winners=winners,
            display_tz=config.display_tz,
            include_base=False,
        )

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


def _winner_admin_label(item: dict[str, Any], index: int) -> str:
    username = str(item.get("username") or "").strip().lstrip("@")
    telegram_id = int(item.get("telegram_id") or 0)
    if username:
        return f"@{html.escape(username)} · <code>{telegram_id}</code>"
    return f"Победитель #{index} · <code>{telegram_id}</code>"


async def _sync_vpn_users(
    db: Database,
    provider: VpnProvider,
    telegram_ids: list[int] | tuple[int, ...] | set[int],
) -> None:
    if not getattr(provider, "service_ready", True):
        return

    async def sync_one(telegram_id: int) -> None:
        try:
            user = await db.get_user(int(telegram_id))
            await asyncio.wait_for(provider.provision(user), timeout=8.0)
        except Exception as exc:
            logger.warning(
                "Giveaway VPN sync deferred for user %s: %s",
                telegram_id,
                type(exc).__name__,
            )

    unique = sorted({int(value) for value in telegram_ids if int(value) > 0})
    if unique:
        await asyncio.gather(*(sync_one(telegram_id) for telegram_id in unique))


async def _notify_admins_giveaway_finished(
    bot,
    db: Database,
    config,
    giveaway: dict[str, Any],
    winners: list[dict[str, Any]],
) -> None:
    if giveaway.get("admin_notified_at"):
        return

    giveaway_id = int(giveaway["id"])
    recipients = {int(giveaway.get("created_by") or 0)}
    recipients.update(int(value) for value in getattr(config, "admin_ids", ()) if int(value) > 0)
    try:
        for item in await db.list_admin_roles():
            if str(item.get("role") or "") == "full":
                recipients.add(int(item["telegram_id"]))
    except Exception as exc:
        logger.warning(
            "Could not load dynamic admins for giveaway #%s: %s",
            giveaway_id,
            type(exc).__name__,
        )
    recipients.discard(0)

    lines = [
        f"🎁 <b>Розыгрыш #{giveaway_id} завершён</b>",
        "",
        f"Участников: <b>{int(giveaway.get('participant_count') or 0)}</b>",
        f"Приз: <b>{int(giveaway.get('prize_days') or 0)} дней MGN VPN</b>",
        "",
        "🏆 <b>Победители:</b>",
    ]
    if winners:
        lines.extend(
            f"{index}. {_winner_admin_label(item, index)}"
            for index, item in enumerate(winners, start=1)
        )
    else:
        lines.append("Победителей нет — участников не было.")

    rows = [
        [
            InlineKeyboardButton(
                text=f"👥 Участники ({int(giveaway.get('participant_count') or 0)})",
                callback_data=f"admin:giveaway:participants:{giveaway_id}:0",
            )
        ],
        [
            InlineKeyboardButton(
                text="🔄 Перевыбрать победителя",
                callback_data=f"admin:giveaway:reroll:{giveaway_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text="Открыть розыгрыш",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
            )
        ],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=rows)

    for telegram_id in sorted(recipients):
        try:
            await bot.send_message(
                telegram_id,
                "\n".join(lines),
                reply_markup=markup,
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
        except Exception as exc:
            logger.warning(
                "Admin giveaway notification failed for #%s / admin %s: %s",
                giveaway_id,
                telegram_id,
                type(exc).__name__,
            )

    # Mark after the delivery pass so reconciliation does not spam admins after
    # restarts. The giveaway card remains available in the admin panel.
    await db.mark_giveaway_admin_notified(giveaway_id)


async def reroll_giveaway_winner(
    bot,
    db: Database,
    config,
    provider: VpnProvider,
    giveaway_id: int,
    old_telegram_id: int,
    *,
    rerolled_by: int,
) -> dict[str, Any]:
    """Replace one finished winner, move the prize, refresh posts, and persist everything."""
    giveaway_id = int(giveaway_id)
    old_telegram_id = int(old_telegram_id)
    lock = _finish_locks.setdefault(giveaway_id, asyncio.Lock())

    async with lock:
        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway or str(giveaway.get("status")) != "finished":
            raise ValueError("Розыгрыш ещё не завершён.")

        winners = await db.get_giveaway_winners(giveaway_id)
        if old_telegram_id not in {int(item["telegram_id"]) for item in winners}:
            raise ValueError("Этот пользователь больше не является победителем.")

        participants = await db.list_giveaway_participants(giveaway_id)
        rerolls = await db.list_giveaway_rerolls(giveaway_id)
        excluded = {int(item["telegram_id"]) for item in winners}
        excluded.update(int(item["old_telegram_id"]) for item in rerolls)
        excluded.update(int(item["new_telegram_id"]) for item in rerolls)
        candidates = [
            item for item in participants
            if int(item["telegram_id"]) not in excluded
        ]
        if not candidates:
            raise ValueError("Нет других участников для перевыбора.")

        replacement = secrets.SystemRandom().choice(candidates)
        result = await db.replace_giveaway_winner(
            giveaway_id,
            old_telegram_id=old_telegram_id,
            new_telegram_id=int(replacement["telegram_id"]),
            rerolled_by=int(rerolled_by),
        )

        granted = await db.grant_giveaway_prizes(giveaway_id)
        await _sync_vpn_users(
            db,
            provider,
            {old_telegram_id, int(replacement["telegram_id"]), *granted},
        )

        giveaway = await db.get_giveaway(giveaway_id)
        winners = await db.get_giveaway_winners(giveaway_id)
        if giveaway:
            await _finalize_channel_posts(bot, db, config, giveaway, winners)
            winners = await db.get_giveaway_winners(giveaway_id)
            await _notify_winners(bot, db, giveaway, winners)

        try:
            await bot.send_message(
                old_telegram_id,
                "ℹ️ <b>В розыгрыше MGN VPN проведён перевыбор победителя.</b>\n\n"
                "Ранее начисленный приз этого розыгрыша отозван администратором.",
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            pass
        except Exception as exc:
            logger.warning(
                "Could not notify replaced giveaway winner %s: %s",
                old_telegram_id,
                type(exc).__name__,
            )

        return result


async def cancel_giveaway(
    bot,
    db: Database,
    config,
    giveaway_id: int,
) -> bool:
    """Cancel an active giveaway, keep its history, and disable channel buttons."""
    giveaway_id = int(giveaway_id)
    lock = _finish_locks.setdefault(giveaway_id, asyncio.Lock())
    async with lock:
        changed = await db.cancel_giveaway(giveaway_id)
        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway:
            return False
        if not changed and str(giveaway.get("status")) != "cancelled":
            return False

        posts = await db.list_giveaway_posts(giveaway_id)
        text = render_giveaway_post(
            giveaway,
            participant_count=int(giveaway.get("participant_count") or 0),
            display_tz=config.display_tz,
        )
        has_photo = bool(str(giveaway.get("photo_file_id") or "").strip())
        if has_photo and len(text) > 900:
            text = render_giveaway_post(
                giveaway,
                participant_count=int(giveaway.get("participant_count") or 0),
                display_tz=config.display_tz,
                include_base=False,
            )

        for post in posts:
            if post.get("finalized_at"):
                continue
            try:
                if has_photo:
                    await bot.edit_message_caption(
                        chat_id=_chat_id(post["chat_id"]),
                        message_id=int(post["message_id"]),
                        caption=text,
                        reply_markup=None,
                    )
                else:
                    await bot.edit_message_text(
                        chat_id=_chat_id(post["chat_id"]),
                        message_id=int(post["message_id"]),
                        text=text,
                        reply_markup=None,
                    )
            except (TelegramBadRequest, TelegramForbiddenError):
                logger.warning(
                    "Could not mark cancelled giveaway %s in %s",
                    giveaway_id,
                    post["chat_id"],
                )
                continue
            except Exception as exc:
                logger.warning(
                    "Could not mark cancelled giveaway %s in %s: %s",
                    giveaway_id,
                    post["chat_id"],
                    type(exc).__name__,
                )
                continue
            await db.mark_giveaway_post_finalized(giveaway_id, post["chat_id"])
        return True


async def delete_giveaway(
    bot,
    db: Database,
    giveaway_id: int,
) -> dict[str, int | bool]:
    """Delete channel posts and persistent giveaway data under the same finish lock."""
    giveaway_id = int(giveaway_id)
    lock = _finish_locks.setdefault(giveaway_id, asyncio.Lock())
    async with lock:
        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway:
            return {"deleted": False, "posts_deleted": 0, "posts_failed": 0}

        posts = await db.list_giveaway_posts(giveaway_id)
        posts_deleted = 0
        posts_failed = 0

        for post in posts:
            try:
                await bot.delete_message(
                    chat_id=_chat_id(post["chat_id"]),
                    message_id=int(post["message_id"]),
                )
                posts_deleted += 1
            except TelegramBadRequest as exc:
                message = str(exc).lower()
                if "message to delete not found" in message:
                    posts_deleted += 1
                else:
                    posts_failed += 1
                    logger.warning(
                        "Could not delete giveaway %s post in %s: %s",
                        giveaway_id,
                        post["chat_id"],
                        type(exc).__name__,
                    )
            except TelegramForbiddenError:
                posts_failed += 1
                logger.warning(
                    "Could not delete giveaway %s post in %s: bot has no access",
                    giveaway_id,
                    post["chat_id"],
                )
            except Exception as exc:
                posts_failed += 1
                logger.warning(
                    "Could not delete giveaway %s post in %s: %s",
                    giveaway_id,
                    post["chat_id"],
                    type(exc).__name__,
                )

        deleted = await db.delete_giveaway(giveaway_id)
        _finish_locks.pop(giveaway_id, None)
        return {
            "deleted": bool(deleted),
            "posts_deleted": posts_deleted,
            "posts_failed": posts_failed,
        }


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
            if granted:
                await _sync_vpn_users(db, provider, granted)

        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway:
            return False
        # This also repairs a reroll interrupted after winner replacement but
        # before prize delivery. grant_giveaway_prizes is idempotent.
        repaired_grants = await db.grant_giveaway_prizes(giveaway_id)
        if repaired_grants:
            await _sync_vpn_users(db, provider, repaired_grants)
        winners = await db.get_giveaway_winners(giveaway_id)

        await _finalize_channel_posts(bot, db, config, giveaway, winners)
        winners = await db.get_giveaway_winners(giveaway_id)
        await _notify_winners(bot, db, giveaway, winners)
        giveaway = await db.get_giveaway(giveaway_id) or giveaway
        await _notify_admins_giveaway_finished(bot, db, config, giveaway, winners)
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
