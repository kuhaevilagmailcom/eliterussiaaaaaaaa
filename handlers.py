from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import Database, from_iso, utcnow
from emoji import EmojiBank
from vpn import VpnProvider, VpnState


PLANS: dict[str, dict[str, Any]] = {
    "30": {
        "days": 30,
        "name": "30 дней",
        "traffic_gb": 100,
        "devices": 2,
    },
    "90": {
        "days": 90,
        "name": "90 дней",
        "traffic_gb": 300,
        "devices": 3,
    },
    "365": {
        "days": 365,
        "name": "365 дней",
        "traffic_gb": 1000,
        "devices": 5,
    },
}


def is_active(user: dict[str, Any]) -> bool:
    until = from_iso(user.get("subscription_until"))
    return bool(until and until > utcnow())


def plan_price(config: Config, code: str) -> int:
    return {
        "30": config.plan_30_price,
        "90": config.plan_90_price,
        "365": config.plan_365_price,
    }[code]


def format_until(user: dict[str, Any], config: Config) -> str:
    until = from_iso(user.get("subscription_until"))
    if not until:
        return "нет"
    return until.astimezone(config.display_tz).strftime("%d.%m.%Y %H:%M")


def remaining_text(user: dict[str, Any]) -> str:
    until = from_iso(user.get("subscription_until"))
    if not until:
        return "0 мин."
    seconds = max(0, int((until - utcnow()).total_seconds()))
    if seconds == 0:
        return "0 мин."
    if seconds < 3600:
        return f"{max(1, seconds // 60)} мин."
    hours = seconds // 3600
    if hours < 48:
        return f"{hours} ч."
    days = hours // 24
    return f"{days} дн."


def fallback_state(user: dict[str, Any], config: Config) -> VpnState:
    return VpnState(
        subscription_url=(
            f'{config.vpn_sub_base_url}/{user["sub_token"]}'
            if user.get("sub_token")
            else ""
        ),
        server=config.vpn_server_name,
        traffic_used_gb=0.0,
        traffic_limit_gb=float(user.get("traffic_limit_gb") or 0),
        devices=[],
    )


async def load_state(
    user: dict[str, Any],
    provider: VpnProvider,
    config: Config,
) -> tuple[VpnState, bool]:
    if not is_active(user):
        return fallback_state(user, config), True
    try:
        return await provider.get_state(user), True
    except Exception:
        try:
            return await provider.provision(user), True
        except Exception:
            return fallback_state(user, config), False


def profile_text(
    user: dict[str, Any],
    state: VpnState,
    emoji: EmojiBank,
    provider_ok: bool,
    config: Config,
) -> str:
    username = (
        f'@{html.escape(user["username"])}'
        if user.get("username")
        else html.escape(user.get("first_name") or str(user["telegram_id"]))
    )
    active = is_active(user)
    plan = html.escape(user.get("plan_name") or "Нет")
    traffic_limit = state.traffic_limit_gb or float(user.get("traffic_limit_gb") or 0)
    devices_count = len(state.devices)
    max_devices = int(user.get("max_devices") or 1)

    e_user = emoji.icon(0, "👤")
    e_money = emoji.icon(1, "💰")
    e_sub = emoji.icon(2, "🎁")
    e_info = emoji.icon(3, "▦")
    e_type = emoji.icon(4, "💎")
    e_until = emoji.icon(5, "🗓")
    e_left = emoji.icon(6, "⏱")
    e_traffic = emoji.icon(7, "📶")
    e_server = emoji.icon(8, "📍")
    e_device = emoji.icon(9, "▦")
    e_link = emoji.icon(10, "🔗")

    status = "🟢 Активна" if active else "⚪️ Нет активной подписки"
    lines = [
        f"{e_user} <b>{username}</b>",
        f"{e_money} Баланс: <b>0 ⭐</b>",
        f"{e_sub} Подписка: <b>{status}</b>",
        "",
        f"{e_info} <b>Информация о подписке</b>",
        f"{e_type} Тип: <b>{plan}</b>",
        f"{e_until} Действует до: <b>{format_until(user, config)}</b>",
        f"{e_left} Осталось: <b>{remaining_text(user)}</b>",
        f"{e_traffic} Трафик: <b>{state.traffic_used_gb:.1f} / {traffic_limit:g} ГБ</b>",
        f"{e_server} Серверы: <b>{html.escape(state.server)}</b>",
        f"{e_device} Устройства: <b>{devices_count} / {max_devices}</b>",
    ]

    if state.devices:
        lines += ["", f"{e_device} <b>Подключенные устройства:</b>"]
        for item in state.devices[:10]:
            name = html.escape(str(item.get("name") or item.get("device_name") or "Устройство"))
            platform = html.escape(str(item.get("platform") or item.get("os") or ""))
            suffix = f" — {platform}" if platform else ""
            lines.append(f"• {name}{suffix}")

    if active and state.subscription_url:
        lines += [
            "",
            f"{e_link} <b>Ссылка для подключения:</b>",
            f"<code>{html.escape(state.subscription_url)}</code>",
        ]

    if not provider_ok:
        lines += [
            "",
            "⚠️ <i>VPN-панель сейчас не ответила. Показаны сохраненные данные.</i>",
        ]

    return "\n".join(lines)



def profile_keyboard(
    user: dict[str, Any],
    state: VpnState,
) -> Any:
    kb = InlineKeyboardBuilder()
    if is_active(user) and state.subscription_url:
        kb.row(
            InlineKeyboardButton(
                text="🔗 Подключиться",
                url=state.subscription_url,
            )
        )
        kb.row(
            InlineKeyboardButton(
                text="🔧 Управление устройствами",
                callback_data="devices",
            )
        )
    elif not user.get("trial_used"):
        kb.row(
            InlineKeyboardButton(
                text="🎁 Получить пробный VPN",
                callback_data="trial",
            )
        )

    kb.row(
        InlineKeyboardButton(
            text="🌕 Купить подписку",
            callback_data="plans",
        )
    )
    kb.row(
        InlineKeyboardButton(
            text="🔄 Обновить",
            callback_data="refresh",
        )
    )
    return kb.as_markup()


def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        kb.row(
            InlineKeyboardButton(
                text=f'{plan["name"]} — {plan_price(config, code)} ⭐',
                callback_data=f"buy:{code}",
            )
        )
    kb.row(InlineKeyboardButton(text="← Назад", callback_data="refresh"))
    return kb.as_markup()


def build_router(
    config: Config,
    db: Database,
    emoji: EmojiBank,
    provider: VpnProvider,
) -> Router:
    router = Router()

    async def ensure_actor(actor) -> dict[str, Any]:
        return await db.ensure_user(
            actor.id,
            actor.username,
            actor.first_name,
        )

    async def send_profile_message(
        message: Message,
        actor,
        *,
        edit: bool = False,
    ) -> None:
        user = await ensure_actor(actor)
        state, ok = await load_state(user, provider, config)
        text = profile_text(user, state, emoji, ok, config)
        markup = profile_keyboard(user, state)

        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except Exception:
                pass
        await message.answer(text, reply_markup=markup)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        try:
            await send_profile_message(message, message.from_user)
        except Exception:
            await message.answer(
                "✅ MGN VPN запущен. Профиль временно не загрузился, но бот принимает сообщения."
            )

    @router.message(Command("ping"))
    async def ping(message: Message) -> None:
        await message.answer("✅ MGN VPN работает")

    @router.message(Command("profile"))
    async def profile(message: Message) -> None:
        await send_profile_message(message, message.from_user)

    @router.callback_query(F.data == "refresh")
    async def refresh(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await send_profile_message(callback.message, callback.from_user, edit=True)

    @router.callback_query(F.data == "trial")
    async def trial(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await db.ensure_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.first_name,
        )

        if user.get("trial_used"):
            await callback.answer("Пробный период уже использован.", show_alert=True)
            return
        if is_active(user):
            await callback.answer("У вас уже есть активная подписка.", show_alert=True)
            return

        activated = await db.activate_trial(
            callback.from_user.id,
            config.trial_minutes,
            config.trial_traffic_gb,
            config.trial_max_devices,
        )
        if not activated:
            await callback.answer("Пробный период уже использован.", show_alert=True)
            return

        user = await db.get_user(callback.from_user.id)
        try:
            await provider.provision(user)
            await callback.answer("Пробный VPN активирован!")
        except Exception:
            await callback.answer(
                "Подписка создана, но VPN-панель пока недоступна. Нажмите «Обновить» позже.",
                show_alert=True,
            )
        await send_profile_message(callback.message, callback.from_user, edit=True)

    @router.callback_query(F.data == "plans")
    async def plans(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await callback.message.edit_text(
                "🌕 <b>Выберите срок подписки</b>\n\n"
                "Оплата проходит через Telegram Stars.",
                reply_markup=plans_keyboard(config),
            )

    @router.callback_query(F.data.startswith("buy:"))
    async def buy(callback: CallbackQuery) -> None:
        code = callback.data.split(":", 1)[1]
        plan = PLANS.get(code)
        if not plan or not callback.message:
            await callback.answer("Тариф не найден.", show_alert=True)
            return

        price = plan_price(config, code)
        await callback.answer()
        await callback.message.answer_invoice(
            title=f'MGN VPN — {plan["name"]}',
            description=(
                f'{plan["traffic_gb"]} ГБ трафика, '
                f'до {plan["devices"]} устройств.'
            ),
            payload=f"vpn:{code}",
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f'MGN VPN {plan["name"]}',
                    amount=price,
                )
            ],
        )

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        payload = query.invoice_payload
        if not payload.startswith("vpn:"):
            await query.answer(ok=False, error_message="Неизвестный платеж.")
            return
        code = payload.split(":", 1)[1]
        if code not in PLANS or query.total_amount != plan_price(config, code):
            await query.answer(ok=False, error_message="Тариф изменился. Откройте покупку заново.")
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        payment = message.successful_payment
        if payment is None:
            return

        payload = payment.invoice_payload
        code = payload.split(":", 1)[1] if payload.startswith("vpn:") else ""
        plan = PLANS.get(code)
        if not plan:
            await message.answer("Платеж получен, но тариф не распознан. Напишите администратору.")
            return

        await ensure_actor(message.from_user)
        fresh = await db.record_payment(
            telegram_id=message.from_user.id,
            charge_id=payment.telegram_payment_charge_id,
            payload=payload,
            amount=payment.total_amount,
        )
        if fresh:
            user = await db.extend_subscription(
                telegram_id=message.from_user.id,
                days=plan["days"],
                plan_name=plan["name"],
                traffic_limit_gb=plan["traffic_gb"],
                max_devices=plan["devices"],
            )
            try:
                await provider.provision(user)
            except Exception:
                pass

        await message.answer("✅ Оплата получена. Подписка активирована.")
        await send_profile_message(message, message.from_user)

    @router.callback_query(F.data == "devices")
    async def devices(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return

        user = await db.ensure_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.first_name,
        )
        state, ok = await load_state(user, provider, config)
        kb = InlineKeyboardBuilder()

        if state.devices:
            lines = ["🔧 <b>Подключенные устройства</b>", ""]
            for i, item in enumerate(state.devices[:10], start=1):
                name = html.escape(str(item.get("name") or item.get("device_name") or f"Устройство {i}"))
                platform = html.escape(str(item.get("platform") or item.get("os") or ""))
                suffix = f" — {platform}" if platform else ""
                lines.append(f"{i}. {name}{suffix}")
                device_id = str(item.get("id") or item.get("device_id") or "")
                if device_id and len(device_id.encode("utf-8")) <= 36:
                    kb.row(
                        InlineKeyboardButton(
                            text=f"❌ Отключить {i}",
                            callback_data=f"deldev:{device_id}",
                        )
                    )
        else:
            lines = [
                "🔧 <b>Подключенные устройства</b>",
                "",
                "Список пуст или VPN-панель пока не передала устройства.",
            ]

        if not ok:
            lines += ["", "⚠️ VPN-панель сейчас недоступна."]

        kb.row(InlineKeyboardButton(text="← Назад", callback_data="refresh"))
        await callback.message.edit_text("\n".join(lines), reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("deldev:"))
    async def delete_device(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        device_id = callback.data.split(":", 1)[1]
        user = await db.ensure_user(
            callback.from_user.id,
            callback.from_user.username,
            callback.from_user.first_name,
        )
        try:
            await provider.delete_device(user, device_id)
            await callback.answer("Устройство отключено.")
        except Exception:
            await callback.answer("Не удалось отключить устройство.", show_alert=True)
        await send_profile_message(callback.message, callback.from_user, edit=True)

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            return
        total, active = await db.stats()
        await message.answer(
            f"📊 <b>MGN VPN</b>\n"
            f"Пользователей: <b>{total}</b>\n"
            f"Активных подписок: <b>{active}</b>"
        )

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Использование: <code>/grant TELEGRAM_ID DAYS</code>")
            return
        try:
            telegram_id = int(parts[1])
            days = int(parts[2])
        except ValueError:
            await message.answer("ID и количество дней должны быть числами.")
            return
        if days < 1 or days > 3650:
            await message.answer("Количество дней: от 1 до 3650.")
            return

        try:
            await db.get_user(telegram_id)
        except KeyError:
            await message.answer("Пользователь еще не запускал бота.")
            return

        user = await db.extend_subscription(
            telegram_id=telegram_id,
            days=days,
            plan_name=f"Выдано админом: {days} дн.",
            traffic_limit_gb=max(100, days * 5),
            max_devices=3,
        )
        try:
            await provider.provision(user)
        except Exception:
            pass
        await message.answer(f"✅ Пользователю <code>{telegram_id}</code> выдано {days} дн.")

    return router
