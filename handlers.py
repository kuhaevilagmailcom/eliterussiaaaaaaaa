from __future__ import annotations

import html
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import Config
from db import Database, from_iso, utcnow
from emoji import EmojiBank
from payments import RollyPayError, create_payment, get_payment
from vpn import VpnProvider, VpnState


PACK_CRYPTO = "CryptoGIFTPODARKI"
PACK_UI = "TgAndroidIcons"
PACK_PROGRESS = "progressBarEmoji"

PLANS: dict[str, dict[str, Any]] = {
    "30": {"days": 30, "name": "30 дней", "devices": 5},
    "90": {"days": 90, "name": "90 дней", "devices": 5},
    "365": {"days": 365, "name": "365 дней", "devices": 5},
}


def is_active(user: dict[str, Any]) -> bool:
    until = from_iso(user.get("subscription_until"))
    return bool(until and until > utcnow())


def plan_price_stars(config: Config, code: str) -> int:
    return {
        "30": config.plan_30_price,
        "90": config.plan_90_price,
        "365": config.plan_365_price,
    }[code]


def plan_price_rub(config: Config, code: str) -> int:
    return {
        "30": config.plan_30_rub,
        "90": config.plan_90_rub,
        "365": config.plan_365_rub,
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
    return f"{hours // 24} дн."


def fallback_state(user: dict[str, Any], config: Config) -> VpnState:
    return VpnState(
        subscription_url=(
            f'{config.vpn_sub_base_url}/{quote(user["sub_token"])}'
            if user.get("sub_token")
            else ""
        ),
        server=config.vpn_server_name,
        traffic_used_gb=0.0,
        traffic_limit_gb=0.0,
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


def strip_custom_emoji(value: str) -> str:
    return re.sub(
        r'<tg-emoji\s+emoji-id="[^"]+">(.*?)</tg-emoji>',
        r"\1",
        value,
        flags=re.DOTALL,
    )


def main_keyboard(
    emoji: EmojiBank,
    *,
    custom_icons: bool = True,
) -> ReplyKeyboardMarkup:
    def button(text: str, index: int, pack: str = PACK_UI) -> KeyboardButton:
        kwargs: dict[str, Any] = {
            "text": text,
            "style": "danger",
        }
        if custom_icons:
            custom_id = emoji.raw_id(index, pack=pack)
            if custom_id:
                kwargs["icon_custom_emoji_id"] = custom_id
        return KeyboardButton(**kwargs)

    return ReplyKeyboardMarkup(
        keyboard=[
            [button("🏠 Главное меню", 0)],
            [
                button("👤 Профиль", 1),
                button("🔗 Подключиться", 2),
            ],
            [
                button("💳 Купить VPN", 3, PACK_CRYPTO),
                button("📱 Устройства", 4),
            ],
            [
                button("👥 Пригласить друга", 5),
                button("🆘 Помощь", 6),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="MGN VPN",
    )


def red_inline_button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style="danger",
    )


def add_nav_buttons(
    kb: InlineKeyboardBuilder,
    *,
    back_data: str = "home",
) -> None:
    kb.row(
        red_inline_button("⬅️ Назад", callback_data=back_data),
        red_inline_button("🏠 Главное меню", callback_data="home"),
    )


def section_nav_keyboard(*, back_data: str = "home") -> Any:
    kb = InlineKeyboardBuilder()
    add_nav_buttons(kb, back_data=back_data)
    return kb.as_markup()


def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        kb.row(
            red_inline_button(
                "💳 "
                + f'{plan["name"]} · до {plan["devices"]} устройств · '
                + f'{plan_price_rub(config, code)} ₽',
                callback_data=f"plan:{code}",
            )
        )
    add_nav_buttons(kb, back_data="home")
    return kb.as_markup()


def payment_methods_keyboard(config: Config, code: str) -> Any:
    kb = InlineKeyboardBuilder()
    kb.row(
        red_inline_button(
            f"🏦 СБП · {plan_price_rub(config, code)} ₽",
            callback_data=f"sbp:{code}",
        )
    )
    kb.row(
        red_inline_button(
            f"⭐ Telegram Stars · {plan_price_stars(config, code)}",
            callback_data=f"stars:{code}",
        )
    )
    add_nav_buttons(kb, back_data="plans")
    return kb.as_markup()


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
        else html.escape(user.get("first_name") or "Пользователь")
    )
    user_id = int(user["telegram_id"])
    active = is_active(user)
    plan = html.escape(user.get("plan_name") or "")
    devices_count = len(state.devices)
    max_devices = int(user.get("max_devices") or 1)

    e_user = emoji.icon(0, pack=PACK_UI)
    e_sub = emoji.icon(1, pack=PACK_CRYPTO)
    e_until = emoji.icon(4, pack=PACK_UI)
    e_left = emoji.icon(5, pack=PACK_PROGRESS)
    e_device = emoji.icon(7, pack=PACK_UI)
    e_server = emoji.icon(6, pack=PACK_UI)

    lines = [
        f"{e_user} <b>Профиль</b>",
        f"<b>{username}</b> · <code>ID {user_id}</code>",
        "",
        f"{e_sub} <b>Подписка</b>",
        f"Статус: <b>{'Активна' if active else 'Не активна'}</b>",
    ]

    if active:
        lines += [
            f"Тариф: <b>{plan or 'VPN'}</b>",
            f"{e_until} До: <b>{format_until(user, config)}</b>",
            f"{e_left} Осталось: <b>{remaining_text(user)}</b>",
        ]
    else:
        trial = "доступен" if not user.get("trial_used") else "использован"
        lines += [f"Пробный доступ: <b>{trial}</b>"]

    lines += [
        "",
        f"{e_device} <b>Устройства</b>",
        f"Подключено: <b>{devices_count} из {max_devices}</b>",
        "",
        f"{e_server} <b>Сервер</b>",
        f"{html.escape(state.server)}",
    ]

    if not provider_ok and active:
        lines += ["", "<i>Сервер временно недоступен. Показаны сохранённые данные.</i>"]

    return "\n".join(lines)


def connection_text(
    user: dict[str, Any],
    state: VpnState,
    emoji: EmojiBank,
) -> str:
    e_link = emoji.icon(10, pack=PACK_UI)
    e_device = emoji.icon(11, pack=PACK_UI)
    return (
        f"{e_link} <b>Подключение к MGN VPN</b>\n"
        "<code>ПЕРСОНАЛЬНЫЙ ДОСТУП</code>\n\n"
        f"{e_device} Доступно устройств: <b>до {int(user.get('max_devices') or 1)}</b>\n\n"
        "Нажмите кнопку ниже — откроется ваша персональная ссылка подключения."
    )


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

    async def safe_delete(chat_id: int, message_id: int | None, bot) -> None:
        if not message_id:
            return
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    async def send_screen(
        message: Message,
        actor,
        text: str,
        *,
        reply_markup=None,
        bottom_menu: bool = False,
    ) -> Message:
        user = await ensure_actor(actor)
        last_id = user.get("last_menu_message_id")

        if message.from_user and not message.from_user.is_bot:
            await safe_delete(message.chat.id, message.message_id, message.bot)

        # ReplyKeyboardMarkup cannot be added by editing an old message.
        # When we need the persistent bottom menu, replace the old screen
        # with a freshly sent message carrying the keyboard.
        if bottom_menu:
            if last_id:
                await safe_delete(message.chat.id, int(last_id), message.bot)

            try:
                sent = await message.bot.send_message(
                    message.chat.id,
                    text,
                    reply_markup=main_keyboard(emoji),
                )
            except TelegramBadRequest:
                sent = await message.bot.send_message(
                    message.chat.id,
                    strip_custom_emoji(text),
                    reply_markup=main_keyboard(emoji, custom_icons=False),
                )

            await db.set_last_menu_message(actor.id, sent.message_id)
            return sent

        # For ordinary sections, keep one bot message and edit it in place.
        if last_id:
            try:
                edited = await message.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=int(last_id),
                    text=text,
                    reply_markup=reply_markup,
                )
                return edited
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return message
                try:
                    edited = await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=int(last_id),
                        text=strip_custom_emoji(text),
                        reply_markup=reply_markup,
                    )
                    return edited
                except Exception:
                    await safe_delete(message.chat.id, int(last_id), message.bot)
            except Exception:
                await safe_delete(message.chat.id, int(last_id), message.bot)

        try:
            sent = await message.bot.send_message(
                message.chat.id,
                text,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest:
            sent = await message.bot.send_message(
                message.chat.id,
                strip_custom_emoji(text),
                reply_markup=reply_markup,
            )

        await db.set_last_menu_message(actor.id, sent.message_id)
        return sent

    async def show_home(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        state, ok = await load_state(user, provider, config)
        count = await db.referral_count(actor.id)
        active = is_active(user)
        max_devices = int(user.get("max_devices") or 1)
        username = (
            f'@{html.escape(user["username"])}'
            if user.get("username")
            else html.escape(user.get("first_name") or "Пользователь")
        )

        e_home = emoji.icon(0, pack=PACK_UI)
        e_user = emoji.icon(1, pack=PACK_UI)
        e_sub = emoji.icon(1, pack=PACK_CRYPTO)
        e_device = emoji.icon(2, pack=PACK_UI)
        e_users = emoji.icon(3, pack=PACK_UI)

        lines = [
            f"{e_home} <b>MGN VPN</b>",
            "<code>ГЛАВНОЕ МЕНЮ</code>",
            "",
            f"{e_user} <b>{username}</b>",
            "",
            f"{e_sub} <b>Подписка</b>",
            f"Статус: <b>{'Активна' if active else 'Не активна'}</b>",
        ]

        if active:
            lines += [
                f"Тариф: <b>{html.escape(user.get('plan_name') or 'VPN')}</b>",
                f"Действует до: <b>{format_until(user, config)}</b>",
            ]
        else:
            trial = "доступен" if not user.get("trial_used") else "использован"
            lines += [f"Пробный доступ: <b>{trial}</b>"]

        lines += [
            "",
            f"{e_device} Устройства: <b>до {max_devices}</b>",
            f"{e_users} Приглашено друзей: <b>{count}</b>",
            "",
            "<i>Выберите нужный раздел кнопками снизу.</i>",
        ]

        if not ok and active:
            lines += ["", "<i>VPN-сервер временно не ответил.</i>"]

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            bottom_menu=True,
        )

    async def show_profile(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        state, ok = await load_state(user, provider, config)
        await send_screen(
            message,
            actor,
            profile_text(user, state, emoji, ok, config),
            reply_markup=section_nav_keyboard(),
        )

    async def activate_paid_plan(telegram_id: int, code: str) -> dict[str, Any]:
        plan = PLANS[code]
        user = await db.extend_subscription(
            telegram_id=telegram_id,
            days=plan["days"],
            plan_name=plan["name"],
            max_devices=plan["devices"],
        )
        try:
            await provider.provision(user)
        except Exception:
            pass
        return user

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        user = await ensure_actor(message.from_user)
        if command.args and command.args.startswith("ref_"):
            raw = command.args.removeprefix("ref_")
            if raw.isdigit():
                await db.set_referrer_once(
                    message.from_user.id,
                    int(raw),
                )
        await show_home(message, message.from_user)

    @router.message(F.text.in_({"🏠 Главное меню", "Главное меню"}))
    async def home(message: Message) -> None:
        await show_home(message, message.from_user)

    @router.callback_query(F.data == "home")
    async def home_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_home(callback.message, callback.from_user)

    @router.message(Command("ping"))
    async def ping(message: Message) -> None:
        await send_screen(
            message,
            message.from_user,
            "<b>MGN VPN работает</b>",
            bottom_menu=True,
        )

    @router.message(Command("profile"))
    @router.message(F.text.in_({"👤 Профиль", "Профиль"}))
    async def profile(message: Message) -> None:
        await show_profile(message, message.from_user)

    @router.message(F.text.in_({"💳 Купить VPN", "Купить VPN"}))
    async def plans_message(message: Message) -> None:
        await ensure_actor(message.from_user)
        e = emoji.icon(0, pack=PACK_CRYPTO)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Подписка MGN VPN</b>\n"
            "<code>ВЫБЕРИТЕ ТАРИФ</code>\n\n"
            "Все тарифы работают одновременно на <b>5 устройствах</b>.\n"
            "Оплата доступна через <b>СБП</b> и <b>Telegram Stars</b>.",
            reply_markup=plans_keyboard(config),
        )

    @router.callback_query(F.data == "plans")
    async def plans_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        e = emoji.icon(0, pack=PACK_CRYPTO)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Подписка MGN VPN</b>\n"
            "<code>ВЫБЕРИТЕ ТАРИФ</code>\n\n"
            "Все тарифы работают одновременно на <b>5 устройствах</b>.\n"
            "Оплата доступна через <b>СБП</b> и <b>Telegram Stars</b>.",
            reply_markup=plans_keyboard(config),
        )

    @router.callback_query(F.data.startswith("plan:"))
    async def choose_plan(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        plan = PLANS.get(code)
        if not plan:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        await callback.answer()
        e = emoji.icon(2, pack=PACK_CRYPTO)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Тариф · {plan['name']}</b>\n"
            "<code>ОФОРМЛЕНИЕ ПОДПИСКИ</code>\n\n"
            f"Устройства: <b>до {plan['devices']}</b>\n"
            f"СБП: <b>{plan_price_rub(config, code)} ₽</b>\n"
            f"Telegram Stars: <b>{plan_price_stars(config, code)} ⭐</b>\n\n"
            "Выберите удобный способ оплаты.",
            reply_markup=payment_methods_keyboard(config, code),
        )

    @router.callback_query(F.data.startswith("sbp:"))
    async def buy_sbp(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        plan = PLANS.get(code)
        if not plan:
            await callback.answer("Тариф не найден", show_alert=True)
            return
        if not config.rollypay_enabled:
            await callback.answer(
                "СБП пока не настроена на этом хостинге.",
                show_alert=True,
            )
            return

        await callback.answer()
        await ensure_actor(callback.from_user)
        order_id = f"vpn-{callback.from_user.id}-{uuid4().hex[:12]}"
        amount = plan_price_rub(config, code)

        try:
            payment = await create_payment(
                config,
                order_id=order_id,
                amount=Decimal(amount),
                description=f"MGN VPN {plan['name']}",
                user_id=callback.from_user.id,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await db.create_sbp_payment(
                payment_id=payment_id,
                order_id=order_id,
                telegram_id=callback.from_user.id,
                plan_code=code,
                amount_rub=amount,
            )
        except (RollyPayError, KeyError):
            await send_screen(
                callback.message,
                callback.from_user,
                "<b>Не удалось создать платёж.</b>\nПопробуйте ещё раз немного позже.",
                bottom_menu=True,
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(red_inline_button("🏦 Оплатить по СБП", url=pay_url))
        kb.row(
            red_inline_button(
                "✅ Проверить оплату",
                callback_data=f"checksbp:{payment_id}",
            )
        )
        add_nav_buttons(kb, back_data=f"plan:{code}")

        e = emoji.icon(4, pack=PACK_CRYPTO)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Оплата по СБП</b>\n"
            "<code>СЧЁТ СОЗДАН</code>\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Устройства: <b>до {plan['devices']}</b>\n"
            f"Сумма: <b>{amount} ₽</b>\n\n"
            "Откройте оплату, затем вернитесь сюда и нажмите «Проверить оплату».",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.startswith("checksbp:"))
    async def check_sbp(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        payment_id = callback.data.split(":", 1)[1]
        local = await db.get_sbp_payment(payment_id)
        if not local or int(local["telegram_id"]) != callback.from_user.id:
            await callback.answer("Платёж не найден", show_alert=True)
            return

        try:
            remote = await get_payment(config, payment_id)
        except RollyPayError:
            await callback.answer(
                "Не удалось проверить платёж. Попробуйте ещё раз.",
                show_alert=True,
            )
            return

        status = str(remote.get("status") or "").lower()
        remote_order = str(remote.get("order_id") or "")
        remote_payment = str(remote.get("payment_id") or "")
        remote_currency = str(
            remote.get("currency") or remote.get("payment_currency") or ""
        ).upper()

        try:
            remote_amount = Decimal(str(remote.get("amount")))
        except (InvalidOperation, ValueError):
            remote_amount = Decimal("-1")

        matches = (
            remote_payment == payment_id
            and remote_order == str(local["order_id"])
            and remote_currency == "RUB"
            and remote_amount == Decimal(int(local["amount_rub"]))
        )
        if not matches:
            await callback.answer(
                "Данные платежа не совпали.",
                show_alert=True,
            )
            return

        if status == "paid":
            fresh = await db.mark_sbp_paid(payment_id)
            if fresh:
                await activate_paid_plan(
                    callback.from_user.id,
                    str(local["plan_code"]),
                )
            await callback.answer("Оплата получена")
            await show_profile(callback.message, callback.from_user)
            return

        await db.set_sbp_status(payment_id, status or "processing")
        if status in {"canceled", "expired", "refunded", "chargeback"}:
            await callback.answer(
                "Этот платёж больше не активен.",
                show_alert=True,
            )
        else:
            await callback.answer(
                "Оплата пока не подтверждена.",
                show_alert=True,
            )

    @router.callback_query(F.data.startswith("stars:"))
    async def buy_stars(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        plan = PLANS.get(code)
        if not plan:
            await callback.answer("Тариф не найден", show_alert=True)
            return

        await callback.answer()
        await callback.message.answer_invoice(
            title=f"MGN VPN — {plan['name']}",
            description=f"Подписка MGN VPN. До {plan['devices']} устройств.",
            payload=f"vpn:{code}",
            provider_token="",
            currency="XTR",
            prices=[
                LabeledPrice(
                    label=f"MGN VPN {plan['name']}",
                    amount=plan_price_stars(config, code),
                )
            ],
        )

    @router.pre_checkout_query()
    async def pre_checkout(query: PreCheckoutQuery) -> None:
        payload = query.invoice_payload
        if not payload.startswith("vpn:"):
            await query.answer(ok=False, error_message="Неизвестный платёж.")
            return
        code = payload.split(":", 1)[1]
        if (
            code not in PLANS
            or query.total_amount != plan_price_stars(config, code)
        ):
            await query.answer(
                ok=False,
                error_message="Тариф изменился. Откройте покупку заново.",
            )
            return
        await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def successful_payment(message: Message) -> None:
        payment = message.successful_payment
        if payment is None:
            return
        payload = payment.invoice_payload
        code = payload.split(":", 1)[1] if payload.startswith("vpn:") else ""
        if code not in PLANS:
            await message.answer("Платёж получен. Обратитесь к администратору.")
            return

        await ensure_actor(message.from_user)
        fresh = await db.record_payment(
            telegram_id=message.from_user.id,
            charge_id=payment.telegram_payment_charge_id,
            payload=payload,
            amount=payment.total_amount,
        )
        if fresh:
            await activate_paid_plan(message.from_user.id, code)

        await show_profile(message, message.from_user)

    @router.message(F.text.in_({"🔗 Подключиться", "Подключиться"}))
    async def connect(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not is_active(user):
            kb = InlineKeyboardBuilder()
            if not user.get("trial_used"):
                kb.row(
                    red_inline_button(
                        "🎁 Получить пробный VPN",
                        callback_data="trial",
                    )
                )
            kb.row(
                red_inline_button(
                    "💳 Купить VPN",
                    callback_data="plans",
                )
            )
            add_nav_buttons(kb, back_data="home")
            e = emoji.icon(5, pack=PACK_UI)
            await send_screen(
                message,
                message.from_user,
                f"{e} <b>Подписка не активна</b>\n\n"
                "Получите пробный доступ или выберите подписку.",
                reply_markup=kb.as_markup(),
            )
            return

        state, ok = await load_state(user, provider, config)
        if not ok or not state.subscription_url:
            await send_screen(
                message,
                message.from_user,
                "<b>Ссылка подключения пока недоступна.</b>\n"
                "Попробуйте ещё раз немного позже.",
                reply_markup=section_nav_keyboard(),
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            red_inline_button(
                "🔗 Открыть подключение",
                url=state.subscription_url,
            )
        )
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            message,
            message.from_user,
            connection_text(user, state, emoji),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "trial")
    async def trial(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if user.get("trial_used"):
            await callback.answer(
                "Пробный период уже использован.",
                show_alert=True,
            )
            return
        if is_active(user):
            await callback.answer(
                "У вас уже есть активная подписка.",
                show_alert=True,
            )
            return

        activated = await db.activate_trial(
            callback.from_user.id,
            config.trial_minutes,
            config.trial_max_devices,
        )
        if not activated:
            await callback.answer(
                "Пробный период уже использован.",
                show_alert=True,
            )
            return

        user = await db.get_user(callback.from_user.id)
        try:
            await provider.provision(user)
        except Exception:
            pass

        await callback.answer("Пробный VPN активирован")
        await show_profile(callback.message, callback.from_user)

    @router.message(F.text.in_({"📱 Устройства", "Устройства"}))
    async def devices(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not is_active(user):
            await send_screen(
                message,
                message.from_user,
                "<b>Нет активной подписки.</b>",
                reply_markup=section_nav_keyboard(),
            )
            return

        state, ok = await load_state(user, provider, config)
        e = emoji.icon(7, pack=PACK_UI)
        lines = [
            f"{e} <b>Устройства</b>",
            "",
            f"Подключено: <b>{len(state.devices)} / {int(user.get('max_devices') or 1)}</b>",
        ]
        kb = InlineKeyboardBuilder()

        if state.devices:
            lines.append("")
            for i, item in enumerate(state.devices[:10], start=1):
                name = html.escape(
                    str(item.get("name") or item.get("device_name") or f"Устройство {i}")
                )
                platform = html.escape(
                    str(item.get("platform") or item.get("os") or "")
                )
                suffix = f" — {platform}" if platform else ""
                lines.append(f"{i}. {name}{suffix}")
                device_id = str(item.get("id") or item.get("device_id") or "")
                if device_id and len(device_id.encode("utf-8")) <= 36:
                    kb.row(
                        red_inline_button(
                            f"❌ Отключить устройство {i}",
                            callback_data=f"deldev:{device_id}",
                        )
                    )
        else:
            lines += ["", "Подключённых устройств пока нет."]

        if not ok:
            lines += ["", "<i>Сервер устройств временно не ответил.</i>"]

        add_nav_buttons(kb, back_data="home")
        await send_screen(
            message,
            message.from_user,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.startswith("deldev:"))
    async def delete_device(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        device_id = callback.data.split(":", 1)[1]
        user = await ensure_actor(callback.from_user)
        try:
            await provider.delete_device(user, device_id)
            await callback.answer("Устройство отключено")
        except Exception:
            await callback.answer(
                "Не удалось отключить устройство.",
                show_alert=True,
            )
            return

        await show_profile(callback.message, callback.from_user)

    @router.message(F.text.in_({"👥 Пригласить друга", "Пригласить друга"}))
    async def invite(message: Message) -> None:
        await ensure_actor(message.from_user)
        bot_info = await message.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{message.from_user.id}"
        count = await db.referral_count(message.from_user.id)
        share_url = (
            "https://t.me/share/url?url="
            + quote(link, safe="")
            + "&text="
            + quote("Подключай MGN VPN", safe="")
        )

        kb = InlineKeyboardBuilder()
        kb.row(red_inline_button("👥 Поделиться", url=share_url))
        add_nav_buttons(kb, back_data="home")

        e = emoji.icon(8, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Пригласить друга</b>\n"
            "<code>РЕФЕРАЛЬНАЯ ССЫЛКА</code>\n\n"
            f"<code>{html.escape(link)}</code>\n\n"
            f"Приглашено друзей: <b>{count}</b>\n"
            "<i>Отправьте ссылку другу — переход будет засчитан автоматически.</i>",
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"🆘 Помощь", "Помощь"}))
    async def help_screen(message: Message) -> None:
        e = emoji.icon(9, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Помощь</b>\n"
            "<code>КАК ПОДКЛЮЧИТЬ VPN</code>\n\n"
            "<b>1.</b> Откройте «Купить VPN» и выберите тариф.\n"
            "<b>2.</b> Оплатите через СБП или Telegram Stars.\n"
            "<b>3.</b> Нажмите «Подключиться».\n"
            "<b>4.</b> Откройте персональную ссылку на нужном устройстве.\n\n"
            "Лишнее устройство можно отключить в разделе <b>«Устройства»</b>.",
            reply_markup=section_nav_keyboard(),
        )

    @router.message(Command("paystatus"))
    async def paystatus(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            return
        status = "настроена" if config.rollypay_enabled else "не настроена"
        mode = "тест" if config.rollypay_test_mode else "боевой"
        await send_screen(
            message,
            message.from_user,
            f"<b>СБП / RollyPay</b>\nСтатус: <b>{status}</b>\nРежим: <b>{mode}</b>",
            bottom_menu=True,
        )

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            return
        total, active = await db.stats()
        await send_screen(
            message,
            message.from_user,
            "<b>MGN VPN</b>\n"
            f"Пользователей: <b>{total}</b>\n"
            f"Активных подписок: <b>{active}</b>",
            bottom_menu=True,
        )

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        if message.from_user.id not in config.admin_ids:
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Использование: /grant TELEGRAM_ID DAYS")
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
            await message.answer("Пользователь ещё не запускал бота.")
            return

        user = await db.extend_subscription(
            telegram_id=telegram_id,
            days=days,
            plan_name=f"{days} дн.",
            max_devices=5,
        )
        try:
            await provider.provision(user)
        except Exception:
            pass

        await send_screen(
            message,
            message.from_user,
            f"Пользователю <code>{telegram_id}</code> выдано <b>{days}</b> дней.",
            bottom_menu=True,
        )

    return router
