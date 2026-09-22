from __future__ import annotations

import base64
import html
import re
from io import BytesIO
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image

from config import Config
from db import Database, from_iso, utcnow
from emoji import EmojiBank
from payments import RollyPayError, create_payment, get_payment
from vpn import VpnProvider, VpnState


PACK_CRYPTO = "CryptoGIFTPODARKI"
PACK_UI = "TgAndroidIcons"
PACK_PROGRESS = "progressBarEmoji"

MAIN_MENU_BANNER_B64 = """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"""
_main_menu_banner_bytes: bytes | None = None


def main_menu_banner() -> BufferedInputFile:
    global _main_menu_banner_bytes
    if _main_menu_banner_bytes is None:
        source = base64.b64decode(MAIN_MENU_BANNER_B64)
        with Image.open(BytesIO(source)) as image:
            image = image.convert("RGB")
            # Telegram is picky about some JPEG encodings. Re-save as a
            # regular baseline RGB JPEG before every deployment, then cache it.
            if image.width > 1280:
                height = max(1, round(image.height * 1280 / image.width))
                image = image.resize((1280, height), Image.Resampling.LANCZOS)

            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=85,
                optimize=True,
                progressive=False,
                subsampling=2,
            )
            _main_menu_banner_bytes = output.getvalue()

    return BufferedInputFile(
        _main_menu_banner_bytes,
        filename="mgn_vpn_main_menu.jpg",
    )


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
        kwargs: dict[str, Any] = {"text": text, "style": "primary"}
        if custom_icons:
            custom_id = emoji.raw_id(index, pack=pack)
            if custom_id:
                kwargs["icon_custom_emoji_id"] = custom_id
        return KeyboardButton(**kwargs)

    return ReplyKeyboardMarkup(
        keyboard=[
            [button("🔗 Подключить VPN", 2)],
            [
                button("👤 Профиль", 3),
                button("ℹ️ Информация", 6),
            ],
            [
                button("💎 Купить VPN", 1, PACK_CRYPTO),
                button("📱 Устройства", 4),
            ],
            [
                button("👥 Друзья", 5),
                button("🆘 Поддержка", 7),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=False,
        input_field_placeholder="MGN VPN",
    )


def blue_inline_button(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        url=url,
        style="primary",
    )


def add_nav_buttons(
    kb: InlineKeyboardBuilder,
    *,
    back_data: str = "home",
) -> None:
    kb.row(
        blue_inline_button("⬅️ Назад", callback_data=back_data),
        blue_inline_button("🏠 Главное меню", callback_data="home"),
    )


def section_nav_keyboard(*, back_data: str = "home") -> Any:
    kb = InlineKeyboardBuilder()
    add_nav_buttons(kb, back_data=back_data)
    return kb.as_markup()


def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        kb.row(
            blue_inline_button(
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
        blue_inline_button(
            f"🏦 СБП · {plan_price_rub(config, code)} ₽",
            callback_data=f"sbp:{code}",
        )
    )
    kb.row(
        blue_inline_button(
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
    user_id = int(user["telegram_id"])
    active = is_active(user)
    max_devices = int(user.get("max_devices") or 1)
    devices_count = len(state.devices)
    plan = html.escape(user.get("plan_name") or "—")

    e_profile = emoji.icon(0, pack=PACK_UI)
    e_sub = emoji.icon(1, pack=PACK_CRYPTO)

    lines = [
        f"{e_profile} <b>Ваш ID:</b> <code>{user_id}</code>",
        "",
        f"{e_sub} <b>Информация о подписке:</b>",
        f"├ Статус: <b>{'Активна' if active else 'Не активна'}</b>",
    ]

    if active:
        lines += [
            f"├ Тариф: <b>{plan}</b>",
            f"├ Действует до: <b>{format_until(user, config)}</b>",
            f"├ Осталось: <b>{remaining_text(user)}</b>",
            f"└ Устройства: <b>{devices_count}/{max_devices}</b>",
        ]
    else:
        if user.get("trial_used"):
            trial = "использован"
        else:
            trial = "доступен после подписки на канал"
        lines += [
            f"├ Пробный доступ: <b>{trial}</b>",
            f"└ Устройства: <b>до {max_devices}</b>",
        ]

    lines += [
        "",
        "Получить доступ можно кнопкой <b>«🔗 Подключить VPN»</b> ниже.",
    ]

    if not provider_ok and active:
        lines += ["", "<i>Сервер временно не отвечает.</i>"]

    return "\n".join(lines)


def connection_text(
    user: dict[str, Any],
    state: VpnState,
    emoji: EmojiBank,
) -> str:
    e_link = emoji.icon(10, pack=PACK_UI)
    return (
        f"{e_link} <b>Подключение</b>\n\n"
        "Ваша персональная ссылка готова.\n"
        f"Можно использовать на <b>{int(user.get('max_devices') or 1)}</b> устройствах."
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

    async def is_trial_channel_member(bot, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(
                chat_id=config.trial_channel_username,
                user_id=user_id,
            )
        except Exception:
            return False

        status = getattr(member.status, "value", str(member.status))
        return status in {"member", "administrator", "creator"}

    def trial_channel_keyboard() -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "📢 Подписаться на канал",
                url=config.trial_channel_url,
            )
        )
        kb.row(
            blue_inline_button(
                "✅ Проверить подписку",
                callback_data="trialcheck",
            )
        )
        add_nav_buttons(kb, back_data="home")
        return kb.as_markup()

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

        # Main UI is one persistent photo message. Normal user screens edit
        # its caption instead of sending the banner again and again.
        if last_id:
            try:
                edited = await message.bot.edit_message_caption(
                    chat_id=message.chat.id,
                    message_id=int(last_id),
                    caption=text,
                    reply_markup=None if bottom_menu else reply_markup,
                )
                return edited
            except TelegramBadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return message

                # Telegram can reject a custom-emoji entity. Retry with plain
                # fallback emoji while keeping the same photo/message.
                try:
                    edited = await message.bot.edit_message_caption(
                        chat_id=message.chat.id,
                        message_id=int(last_id),
                        caption=strip_custom_emoji(text),
                        reply_markup=None if bottom_menu else reply_markup,
                    )
                    return edited
                except Exception:
                    pass
            except Exception:
                pass

            # If the stored menu is from an older bot version and is a text
            # message, ordinary screens can still edit it in place.
            if not bottom_menu:
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
                        pass
                except Exception:
                    pass

            # Old/uneditable menu: remove it before creating exactly one
            # replacement, so duplicate banners never accumulate.
            await safe_delete(message.chat.id, int(last_id), message.bot)

        if bottom_menu:
            try:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=main_menu_banner(),
                    caption=text,
                    reply_markup=main_keyboard(emoji),
                )
            except TelegramBadRequest:
                try:
                    sent = await message.bot.send_photo(
                        chat_id=message.chat.id,
                        photo=main_menu_banner(),
                        caption=strip_custom_emoji(text),
                        reply_markup=main_keyboard(emoji, custom_icons=False),
                    )
                except Exception:
                    sent = await message.bot.send_message(
                        chat_id=message.chat.id,
                        text=strip_custom_emoji(text),
                        reply_markup=main_keyboard(emoji, custom_icons=False),
                    )
            except Exception:
                sent = await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=strip_custom_emoji(text),
                    reply_markup=main_keyboard(emoji, custom_icons=False),
                )
        else:
            # Direct access to a section before /start: keep old behavior.
            # Once the user opens the main menu, this text message is replaced
            # by the single persistent banner.
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
        active = is_active(user)

        e_logo = emoji.icon(0, pack=PACK_CRYPTO)
        e_sub = emoji.icon(1, pack=PACK_CRYPTO)

        lines = [
            f"{e_logo} <b>MGN VPN</b>",
            "Простой доступ к VPN прямо в Telegram.",
            "",
            f"{e_sub} <b>Подписка:</b>",
            f"├ Статус: <b>{'Активна' if active else 'Не активна'}</b>",
        ]

        if active:
            lines += [
                f"├ Тариф: <b>{html.escape(user.get('plan_name') or 'VPN')}</b>",
                f"├ До: <b>{format_until(user, config)}</b>",
                f"└ Устройства: <b>{len(state.devices)}/{int(user.get('max_devices') or 1)}</b>",
            ]
        elif not user.get("trial_used"):
            channel = html.escape(config.trial_channel_username)
            lines += [
                "└ Пробный доступ: <b>доступен</b>",
                "",
                "🎁 <b>Пробная подписка</b>",
                f"Чтобы активировать её, подпишитесь на канал <b>{channel}</b>.",
                "После подписки нажмите <b>«🔗 Подключить VPN»</b>.",
            ]
        else:
            lines += [
                "└ Пробный доступ: <b>уже использован</b>",
                "",
                "Выберите платную подписку кнопкой <b>«💎 Купить VPN»</b>.",
            ]

        if not ok and active:
            lines += ["", "<i>VPN-сервер временно не отвечает.</i>"]

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

    @router.message(F.text.in_({"🏠 Главное", "Главное", "🏠 Главное меню", "Главное меню"}))
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

    @router.message(F.text.in_({"💎 Купить VPN", "💳 Купить VPN", "Купить VPN"}))
    async def plans_message(message: Message) -> None:
        await ensure_actor(message.from_user)
        e = emoji.icon(0, pack=PACK_CRYPTO)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Выберите подписку</b>\n\n"
            "До <b>5 устройств</b> на каждом тарифе.\n"
            "Оплата через СБП или Telegram Stars.",
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
            f"{e} <b>Выберите подписку</b>\n\n"
            "До <b>5 устройств</b> на каждом тарифе.\n"
            "Оплата через СБП или Telegram Stars.",
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
            f"{e} <b>{plan['name']}</b>\n\n"
            f"До {plan['devices']} устройств\n"
            f"<b>{plan_price_rub(config, code)} ₽</b>  ·  СБП\n"
            f"<b>{plan_price_stars(config, code)} ⭐</b>  ·  Telegram Stars",
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
        kb.row(blue_inline_button("🏦 Оплатить по СБП", url=pay_url))
        kb.row(
            blue_inline_button(
                "✅ Проверить оплату",
                callback_data=f"checksbp:{payment_id}",
            )
        )
        add_nav_buttons(kb, back_data=f"plan:{code}")

        e = emoji.icon(4, pack=PACK_CRYPTO)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Оплата по СБП</b>\n\n"
            f"Тариф — <b>{plan['name']}</b>\n"
            f"Сумма — <b>{amount} ₽</b>\n\n"
            "Оплатите счёт и нажмите «Проверить оплату».",
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

    @router.message(F.text.in_({"🔗 Подключить VPN", "🔗 Подключиться", "Подключить VPN", "Подключиться"}))
    async def connect(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not is_active(user):
            kb = InlineKeyboardBuilder()
            if not user.get("trial_used"):
                kb.row(
                    blue_inline_button(
                        "🎁 Активировать пробный VPN",
                        callback_data="trial",
                    )
                )
            kb.row(
                blue_inline_button(
                    "💎 Купить подписку",
                    callback_data="plans",
                )
            )
            add_nav_buttons(kb, back_data="home")

            trial_note = ""
            if not user.get("trial_used"):
                trial_note = (
                    "\n\nДля пробного доступа сначала подпишитесь на "
                    f"<b>{html.escape(config.trial_channel_username)}</b>."
                )

            await send_screen(
                message,
                message.from_user,
                "🔗 <b>Подключение VPN</b>\n\n"
                "У вас пока нет активной подписки."
                + trial_note,
                reply_markup=kb.as_markup(),
            )
            return

        state, ok = await load_state(user, provider, config)
        if not ok or not state.subscription_url:
            await send_screen(
                message,
                message.from_user,
                "🔗 <b>Подключение VPN</b>\n\n"
                "Ссылка подключения пока недоступна. Попробуйте немного позже.",
                reply_markup=section_nav_keyboard(),
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
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

    @router.callback_query(F.data.in_({"trial", "trialcheck"}))
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

        subscribed = await is_trial_channel_member(
            callback.message.bot,
            callback.from_user.id,
        )
        if not subscribed:
            await callback.answer(
                "Сначала подпишитесь на канал.",
                show_alert=True,
            )
            await send_screen(
                callback.message,
                callback.from_user,
                "🎁 <b>Пробная подписка</b>\n\n"
                f"Для активации подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>.\n"
                "После подписки нажмите <b>«✅ Проверить подписку»</b>.",
                reply_markup=trial_channel_keyboard(),
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
                "<b>Устройства</b>\n\n"
                "Список появится после активации подписки.",
                reply_markup=section_nav_keyboard(),
            )
            return

        state, ok = await load_state(user, provider, config)
        e = emoji.icon(7, pack=PACK_UI)
        lines = [
            f"{e} <b>Устройства</b>",
            "",
            f"Подключено — <b>{len(state.devices)} из {int(user.get('max_devices') or 1)}</b>",
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
                        blue_inline_button(
                            f"❌ Отключить устройство {i}",
                            callback_data=f"deldev:{device_id}",
                        )
                    )
        else:
            lines += [
                "",
                "<i>Подключённых устройств пока нет. Они появятся здесь после первого подключения.</i>",
            ]

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

    @router.message(F.text.in_({"👥 Друзья", "👥 Пригласить друга", "Пригласить друга", "Друзья"}))
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
        kb.row(blue_inline_button("👥 Поделиться", url=share_url))
        add_nav_buttons(kb, back_data="home")

        e = emoji.icon(8, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Друзья</b>\n\n"
            f"Ваша ссылка:\n<code>{html.escape(link)}</code>\n\n"
            f"Приглашено  <b>{count}</b>",
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"ℹ️ Информация", "Информация"}))
    async def information_screen(message: Message) -> None:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "📢 Канал MGN VPN",
                url=config.trial_channel_url,
            )
        )
        add_nav_buttons(kb, back_data="home")

        e = emoji.icon(9, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Информация</b>\n\n"
            "🔐 Доступ выдаётся по персональной ссылке.\n"
            "📱 Платная подписка — до <b>5 устройств</b>.\n"
            "🎁 Пробный доступ можно активировать один раз после подписки на наш Telegram-канал.\n"
            "⚙️ Управление подпиской и устройствами находится прямо в боте.",
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"🆘 Поддержка", "🆘 Помощь", "Поддержка", "Помощь"}))
    async def help_screen(message: Message) -> None:
        e = emoji.icon(9, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Поддержка</b>\n\n"
            "1. Активируйте пробный доступ или купите подписку.\n"
            "2. Нажмите <b>«🔗 Подключить VPN»</b>.\n"
            "3. Откройте персональную ссылку на нужном устройстве.\n\n"
            "Подключённые устройства можно отключить в разделе <b>«📱 Устройства»</b>.",
            reply_markup=section_nav_keyboard(),
        )

    def is_admin(user_id: int) -> bool:
        return user_id in config.admin_ids

    def admin_main_keyboard() -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("📊 Статистика", callback_data="admin:stats"),
            blue_inline_button("👥 Пользователи", callback_data="admin:users"),
        )
        kb.row(
            blue_inline_button("💳 Платежи", callback_data="admin:payments"),
            blue_inline_button("⚙️ Система", callback_data="admin:system"),
        )
        kb.row(
            blue_inline_button("🏠 Главное меню", callback_data="home"),
        )
        return kb.as_markup()

    async def show_admin(message: Message, actor) -> None:
        stats = await db.admin_overview()
        pay_status = "работает" if config.rollypay_enabled else "не настроена"
        vpn_status = config.vpn_mode.upper()

        text = (
            "🛡 <b>Админ-панель MGN VPN</b>\n\n"
            f"👥 Пользователи: <b>{stats['total']}</b>\n"
            f"✅ Активные подписки: <b>{stats['active']}</b>\n"
            f"🆕 За 24 часа: <b>+{stats['new_24h']}</b>\n\n"
            f"💳 СБП: <b>{pay_status}</b>\n"
            f"🌐 VPN: <b>{vpn_status}</b>\n\n"
            "<i>Выберите раздел.</i>"
        )
        await send_screen(
            message,
            actor,
            text,
            reply_markup=admin_main_keyboard(),
        )

    async def show_admin_stats(message: Message, actor) -> None:
        stats = await db.admin_overview()
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:stats"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        text = (
            "📊 <b>Статистика</b>\n\n"
            f"Всего пользователей — <b>{stats['total']}</b>\n"
            f"Активных подписок — <b>{stats['active']}</b>\n"
            f"Новых за 24 часа — <b>{stats['new_24h']}</b>\n"
            f"Новых за 7 дней — <b>{stats['new_7d']}</b>\n"
            f"Пробник использовали — <b>{stats['trials']}</b>\n\n"
            "💰 <b>Оплаты</b>\n"
            f"Успешных СБП — <b>{stats['sbp_paid']}</b>\n"
            f"СБП оборот — <b>{stats['sbp_revenue']} ₽</b>\n"
            f"Telegram Stars — <b>{stats['stars_revenue']} ⭐</b>"
        )
        await send_screen(message, actor, text, reply_markup=kb.as_markup())

    async def show_admin_users(message: Message, actor) -> None:
        users = await db.recent_users(10)
        kb = InlineKeyboardBuilder()
        lines = ["👥 <b>Последние пользователи</b>", ""]

        if not users:
            lines.append("Пользователей пока нет.")
        else:
            for item in users:
                uid = int(item["telegram_id"])
                username = (
                    f'@{item["username"]}'
                    if item.get("username")
                    else item.get("first_name") or str(uid)
                )
                active = bool(
                    from_iso(item.get("subscription_until"))
                    and from_iso(item.get("subscription_until")) > utcnow()
                )
                mark = "✅" if active else "▫️"
                lines.append(f"{mark} {html.escape(str(username))} · <code>{uid}</code>")
                kb.row(
                    blue_inline_button(
                        f"👤 {str(username)[:28]}",
                        callback_data=f"admin:user:{uid}",
                    )
                )

        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:users"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_user(message: Message, actor, telegram_id: int) -> None:
        try:
            user = await db.get_user(telegram_id)
        except KeyError:
            await send_screen(
                message,
                actor,
                "👤 <b>Пользователь не найден</b>",
                reply_markup=admin_main_keyboard(),
            )
            return

        username = (
            f'@{html.escape(user["username"])}'
            if user.get("username")
            else html.escape(user.get("first_name") or "Без имени")
        )
        active = is_active(user)
        referrals = await db.referral_count(telegram_id)

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("+7 дней", callback_data=f"admin:grant:{telegram_id}:7"),
            blue_inline_button("+30 дней", callback_data=f"admin:grant:{telegram_id}:30"),
        )
        kb.row(
            blue_inline_button("+90 дней", callback_data=f"admin:grant:{telegram_id}:90"),
            blue_inline_button("+365 дней", callback_data=f"admin:grant:{telegram_id}:365"),
        )
        kb.row(blue_inline_button("⬅️ Пользователи", callback_data="admin:users"))
        kb.row(blue_inline_button("🏠 Админка", callback_data="admin:home"))

        text = (
            f"👤 <b>{username}</b>\n"
            f"<code>{telegram_id}</code>\n\n"
            f"Подписка — <b>{'активна' if active else 'не активна'}</b>\n"
            f"Тариф — <b>{html.escape(user.get('plan_name') or '—')}</b>\n"
            f"До — <b>{format_until(user, config) if active else '—'}</b>\n"
            f"Устройств — <b>до {int(user.get('max_devices') or 1)}</b>\n"
            f"Пробник — <b>{'использован' if user.get('trial_used') else 'доступен'}</b>\n"
            f"Приглашено — <b>{referrals}</b>\n"
            f"Stars оплачено — <b>{int(user.get('total_paid_stars') or 0)} ⭐</b>"
        )
        await send_screen(message, actor, text, reply_markup=kb.as_markup())

    async def show_admin_payments(message: Message, actor) -> None:
        payments = await db.recent_sbp_payments(10)
        kb = InlineKeyboardBuilder()
        lines = ["💳 <b>Последние платежи СБП</b>", ""]

        if not payments:
            lines.append("Платежей пока нет.")
        else:
            status_names = {
                "paid": "✅ Оплачен",
                "created": "🕓 Создан",
                "processing": "🕓 В обработке",
                "canceled": "❌ Отменён",
                "expired": "⌛ Истёк",
                "refunded": "↩️ Возврат",
                "chargeback": "⚠️ Chargeback",
            }
            for item in payments:
                status = str(item.get("status") or "created").lower()
                label = status_names.get(status, f"▫️ {status}")
                lines += [
                    f"{label} · <b>{int(item['amount_rub'])} ₽</b>",
                    f"<code>{int(item['telegram_id'])}</code> · тариф {html.escape(str(item['plan_code']))}",
                    "",
                ]

        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:payments"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(
            message,
            actor,
            "\n".join(lines).rstrip(),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_system(message: Message, actor) -> None:
        rolly = "✅ настроена" if config.rollypay_enabled else "❌ не настроена"
        rolly_mode = "тест" if config.rollypay_test_mode else "боевой"
        vpn_ready = "✅" if config.vpn_mode != "demo" else "⚠️"

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:system"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        text = (
            "⚙️ <b>Система</b>\n\n"
            f"{vpn_ready} VPN режим — <b>{html.escape(config.vpn_mode)}</b>\n"
            f"🌐 Сервер — <b>{html.escape(config.vpn_server_name)}</b>\n"
            f"💳 RollyPay — <b>{rolly}</b>\n"
            f"🧾 Режим оплаты — <b>{rolly_mode}</b>\n"
            f"🎁 Пробный период — <b>{config.trial_minutes} мин.</b>\n"
            f"📱 Пробник — <b>{config.trial_max_devices} устройство</b>\n\n"
            "<i>Секретные ключи здесь не отображаются.</i>"
        )
        await send_screen(message, actor, text, reply_markup=kb.as_markup())

    @router.message(Command("admin"))
    async def admin_panel(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        await show_admin(message, message.from_user)

    @router.callback_query(F.data == "admin:home")
    async def admin_home(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:stats")
    async def admin_stats_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_stats(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:users")
    async def admin_users_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_users(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:payments")
    async def admin_payments_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_payments(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:system")
    async def admin_system_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_system(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("admin:user:"))
    async def admin_user_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not callback.message:
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await callback.answer("Некорректный ID", show_alert=True)
            return
        await callback.answer()
        await show_admin_user(callback.message, callback.from_user, int(raw))

    @router.callback_query(F.data.startswith("admin:grant:"))
    async def admin_grant_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await callback.answer("Некорректная команда", show_alert=True)
            return

        telegram_id = int(parts[2])
        days = int(parts[3])
        try:
            await db.get_user(telegram_id)
        except KeyError:
            await callback.answer("Пользователь не найден", show_alert=True)
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

        await callback.answer(f"Добавлено {days} дней")
        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.message(Command("user"))
    async def admin_user_command(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Использование: /user TELEGRAM_ID")
            return
        await show_admin_user(message, message.from_user, int(parts[1]))

    @router.message(Command("paystatus"))
    async def paystatus(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        await show_admin_system(message, message.from_user)

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return
        await show_admin_stats(message, message.from_user)

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        if not is_admin(message.from_user.id):
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
            f"✅ Пользователю <code>{telegram_id}</code> добавлено <b>{days}</b> дней.",
            reply_markup=admin_main_keyboard(),
        )

    return router
