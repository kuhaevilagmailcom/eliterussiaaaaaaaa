from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from catalog import DEVICE_PRODUCT_CODE, PLANS
from db import Database, from_iso

logger = logging.getLogger(__name__)


async def admin_recipients(config, db: Database) -> set[int]:
    recipients = {int(value) for value in getattr(config, "admin_ids", ())}
    try:
        recipients.update(
            int(item["telegram_id"])
            for item in await db.list_admin_roles()
        )
    except Exception:
        logger.exception("Could not load admin notification recipients")
    return {value for value in recipients if value > 0}


def _event_time(config) -> str:
    try:
        return datetime.now(config.display_tz).strftime("%d.%m.%Y · %H:%M")
    except Exception:
        return datetime.now().strftime("%d.%m.%Y · %H:%M")


def _user_label(user: dict[str, Any] | None, user_id: int) -> str:
    if user:
        username = str(user.get("username") or "").strip()
        if username:
            return f"@{html.escape(username)}"
        first_name = str(user.get("first_name") or "").strip()
        if first_name:
            return html.escape(first_name)
    return f"ID {int(user_id)}"


def _short_payment_id(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "—"
    if len(raw) <= 18:
        return raw
    return raw[:8] + "…" + raw[-6:]


def _keyboard(*rows: tuple[str, str]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data)]
            for text, data in rows
        ]
    )


async def _send_all(bot, config, db: Database, text: str, reply_markup=None) -> None:
    for admin_id in sorted(await admin_recipients(config, db)):
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=reply_markup,
            )
        except (TelegramForbiddenError, TelegramBadRequest):
            continue
        except Exception as exc:
            logger.warning(
                "Could not deliver admin notification to %s: %s",
                admin_id,
                type(exc).__name__,
            )


async def notify_purchase_admins(
    bot,
    config,
    db: Database,
    *,
    buyer_id: int,
    target_id: int,
    code: str,
    method: str,
    amount: int,
    payment_id: str | None = None,
) -> None:
    try:
        buyer = await db.get_user(int(buyer_id))
    except KeyError:
        buyer = None
    try:
        target = await db.get_user(int(target_id))
    except KeyError:
        target = None

    plan_name = (
        "Дополнительное устройство"
        if code == DEVICE_PRODUCT_CODE
        else str(PLANS.get(code, {}).get("name") or code)
    )
    price = f"{int(amount)} ₽" if method == "СБП" else f"{int(amount)} ⭐"
    source = str((buyer or {}).get("attribution_source") or "прямой").strip()
    target_until = from_iso((target or {}).get("subscription_until"))
    until_text = (
        target_until.astimezone(config.display_tz).strftime("%d.%m.%Y · %H:%M")
        if target_until
        else "—"
    )

    gift = int(target_id) != int(buyer_id)
    title = "🎁 <b>Оплачен подарок MGN VPN</b>" if gift else "✅ <b>Новая оплата MGN VPN</b>"
    lines = [
        title,
        "",
        f"👤 Покупатель: <b>{_user_label(buyer, buyer_id)}</b>",
        f"🆔 ID: <code>{int(buyer_id)}</code>",
        f"📦 Товар: <b>{html.escape(plan_name)}</b>",
        f"💳 Оплата: <b>{html.escape(method)}</b>",
        f"💰 Сумма: <b>{price}</b>",
        f"📣 Источник: <b>{html.escape(source)}</b>",
    ]
    if gift:
        lines += [
            "",
            f"🎁 Получатель: <b>{_user_label(target, target_id)}</b>",
            f"🆔 ID получателя: <code>{int(target_id)}</code>",
        ]
    if code != DEVICE_PRODUCT_CODE:
        lines.append(f"⏳ Подписка до: <b>{until_text}</b>")
    if payment_id:
        lines.append(f"🧾 Платёж: <code>{html.escape(_short_payment_id(payment_id))}</code>")
    lines += ["", f"🕒 {_event_time(config)}"]

    buttons: list[tuple[str, str]] = [
        ("👤 Открыть пользователя", f"admin:user:{int(target_id)}"),
        ("💳 Платежи", "admin:payments"),
    ]
    await _send_all(
        bot,
        config,
        db,
        "\n".join(lines),
        _keyboard(*buttons),
    )


async def notify_refund_admins(
    bot,
    config,
    db: Database,
    *,
    status: str,
    payment: dict[str, Any],
    target_id: int,
) -> None:
    try:
        target = await db.get_user(int(target_id))
    except KeyError:
        target = None

    code = str(payment.get("plan_code") or "")
    product = (
        "Дополнительное устройство"
        if code == DEVICE_PRODUCT_CODE
        else str(PLANS.get(code, {}).get("name") or code or "VPN")
    )
    status = str(status or "").lower()
    chargeback = status == "chargeback"
    title = "🚨 <b>Chargeback по оплате</b>" if chargeback else "↩️ <b>Возврат по оплате</b>"
    action = (
        "слот устройства отозван"
        if code == DEVICE_PRODUCT_CODE
        else "срок подписки пересчитан"
    )
    amount = int(payment.get("amount_rub") or 0)

    text = "\n".join(
        [
            title,
            "",
            f"👤 Пользователь: <b>{_user_label(target, target_id)}</b>",
            f"🆔 ID: <code>{int(target_id)}</code>",
            f"📦 Товар: <b>{html.escape(product)}</b>",
            f"💰 Сумма: <b>{amount} ₽</b>",
            f"🧾 Платёж: <code>{html.escape(_short_payment_id(str(payment.get('payment_id') or '')))}</code>",
            "",
            f"✅ Действие: <b>{action}</b>",
            f"🕒 {_event_time(config)}",
        ]
    )
    await _send_all(
        bot,
        config,
        db,
        text,
        _keyboard(
            ("👤 Открыть пользователя", f"admin:user:{int(target_id)}"),
            ("💳 Платежи", "admin:payments"),
        ),
    )


async def notify_server_changes(
    bot,
    config,
    db: Database,
    *,
    changes: list[dict[str, Any]],
    online: int,
    total: int,
) -> None:
    if not changes:
        return
    down = [item for item in changes if not item.get("available")]
    up = [item for item in changes if item.get("available")]
    if down and not up:
        title = "🔴 <b>Проблема с сервером MGN VPN</b>"
    elif up and not down:
        title = "🟢 <b>Сервер MGN VPN восстановлен</b>"
    else:
        title = "🌐 <b>Изменился статус серверов MGN VPN</b>"

    lines = [title, ""]
    for item in changes:
        state = bool(item.get("available"))
        name = html.escape(str(item.get("name") or "Сервер"))
        latency = item.get("latency_ms")
        latency_text = (
            f" · {int(latency)} мс"
            if isinstance(latency, (int, float))
            else ""
        )
        lines.append(
            f"{'✅' if state else '❌'} <b>{name}</b> — "
            f"{'онлайн' if state else 'недоступен'}{latency_text}"
        )
    lines += [
        "",
        f"📊 Онлайн: <b>{int(online)}/{int(total)}</b>",
        f"🕒 {_event_time(config)}",
    ]
    await _send_all(
        bot,
        config,
        db,
        "\n".join(lines),
        _keyboard(("🌐 Открыть серверы", "admin:servers")),
    )


async def notify_restart_admins(
    bot,
    config,
    db: Database,
    *,
    vpn_status: str,
) -> None:
    text = "\n".join(
        [
            "♻️ <b>MGN VPN перезапущен</b>",
            "",
            "✅ <b>Бот:</b> запущен",
            "✅ <b>Mini App:</b> запущен",
            f"🌐 <b>VPN:</b> {html.escape(vpn_status)}",
            "",
            f"🕒 {_event_time(config)}",
        ]
    )
    await _send_all(
        bot,
        config,
        db,
        text,
        _keyboard(
            ("🛡 Админка", "admin:home"),
            ("🌐 Серверы", "admin:servers"),
        ),
    )
