from __future__ import annotations

import html
import logging
from typing import Any

from catalog import DEVICE_PRODUCT_CODE, PLANS

logger = logging.getLogger(__name__)


async def admin_recipient_ids(db, config) -> set[int]:
    recipients = {int(value) for value in getattr(config, "admin_ids", ()) if int(value) > 0}
    try:
        recipients.update(
            int(item["telegram_id"])
            for item in await db.list_admin_roles()
            if int(item["telegram_id"]) > 0
        )
    except Exception:
        logger.exception("Could not load dynamic admin recipients")
    return recipients


async def notify_all_admins(bot, db, config, text: str, *, reply_markup=None) -> int:
    sent = 0
    for admin_id in sorted(await admin_recipient_ids(db, config)):
        try:
            await bot.send_message(admin_id, text, reply_markup=reply_markup)
            sent += 1
        except Exception as exc:
            logger.warning(
                "Admin notification failed for %s: %s",
                admin_id,
                type(exc).__name__,
            )
    return sent


async def notify_purchase(
    bot,
    db,
    config,
    *,
    buyer_id: int,
    target_id: int,
    product_code: str,
    method: str,
    amount_text: str,
    purchase_kind: str,
    promo_code: str | None = None,
) -> None:
    try:
        buyer = await db.get_user(int(buyer_id))
    except Exception:
        buyer = {}
    try:
        target = await db.get_user(int(target_id))
    except Exception:
        target = {}

    buyer_label = (
        f"@{html.escape(str(buyer.get('username')))}"
        if buyer.get("username")
        else html.escape(str(buyer.get("first_name") or buyer_id))
    )
    target_label = (
        f"@{html.escape(str(target.get('username')))}"
        if target.get("username")
        else html.escape(str(target.get("first_name") or target_id))
    )
    if product_code == DEVICE_PRODUCT_CODE:
        product = "+1 устройство"
    else:
        product = html.escape(str(PLANS.get(product_code, {}).get("name") or product_code))

    lines = [
        "💸 <b>Новая покупка MGN VPN</b>",
        "",
        f"Тип: <b>{html.escape(str(purchase_kind))}</b>",
        f"Товар: <b>{product}</b>",
        f"Оплата: <b>{html.escape(str(method))} · {html.escape(str(amount_text))}</b>",
        f"Покупатель: <b>{buyer_label}</b> · <code>{int(buyer_id)}</code>",
    ]
    if int(target_id) != int(buyer_id):
        lines.append(f"Получатель: <b>{target_label}</b> · <code>{int(target_id)}</code>")
    if promo_code:
        lines.append(f"Промокод: <code>{html.escape(str(promo_code))}</code>")
    source = str(buyer.get("attribution_source") or "").strip()
    if source:
        lines.append(f"Источник: <code>{html.escape(source)}</code>")

    await notify_all_admins(bot, db, config, "\n".join(lines))
