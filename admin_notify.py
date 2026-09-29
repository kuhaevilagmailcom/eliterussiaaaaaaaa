from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def admin_recipient_ids(db, config) -> list[int]:
    recipients = {int(value) for value in getattr(config, "admin_ids", ())}
    try:
        recipients.update(
            int(item["telegram_id"])
            for item in await db.list_admin_roles()
            if int(item.get("telegram_id") or 0) > 0
        )
    except Exception:
        logger.exception("Could not load stored admin recipients")
    return sorted(value for value in recipients if value > 0)


async def notify_all_admins(
    bot,
    db,
    config,
    text: str,
    *,
    reply_markup: Any = None,
    disable_notification: bool = False,
) -> tuple[int, int]:
    """Best-effort delivery to every configured or database-backed admin."""
    sent = 0
    failed = 0
    for telegram_id in await admin_recipient_ids(db, config):
        try:
            await bot.send_message(
                telegram_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Admin notification failed for %s: %s",
                telegram_id,
                type(exc).__name__,
            )
    return sent, failed


async def notify_purchase(
    bot,
    db,
    config,
    *,
    buyer_id: int,
    target_id: int,
    product_name: str,
    payment_method: str,
    amount_text: str,
    username: str | None = None,
    renewed: bool | None = None,
    promo_code: str | None = None,
) -> tuple[int, int]:
    buyer_label = f"@{username}" if username else f"ID {int(buyer_id)}"
    purchase_kind = (
        "Продление"
        if renewed is True
        else "Новая подписка"
        if renewed is False
        else "Покупка"
    )
    lines = [
        "💰 <b>Новая покупка MGN VPN</b>",
        "",
        f"Тип: <b>{purchase_kind}</b>",
        f"Покупатель: <b>{buyer_label}</b>",
        f"Telegram ID: <code>{int(buyer_id)}</code>",
        f"Продукт: <b>{product_name}</b>",
        f"Оплата: <b>{payment_method}</b>",
        f"Сумма: <b>{amount_text}</b>",
    ]
    if int(target_id) != int(buyer_id):
        lines.append(f"Получатель: <code>{int(target_id)}</code>")
    if promo_code:
        lines.append(f"Промокод: <code>{promo_code}</code>")
    return await notify_all_admins(
        bot,
        db,
        config,
        "\n".join(lines),
    )
