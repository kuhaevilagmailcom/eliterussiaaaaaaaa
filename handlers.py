from __future__ import annotations

import base64
import html
import logging
import re
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    BufferedInputFile,
    InlineKeyboardButton,
    InputMediaPhoto,
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

logger = logging.getLogger(__name__)

MAIN_MENU_BANNER_DIR = Path(__file__).with_name("assets") / "banner_parts"
_main_menu_banner_bytes: bytes | None = None


def main_menu_banner() -> BufferedInputFile:
    global _main_menu_banner_bytes

    if _main_menu_banner_bytes is None:
        encoded = "".join(
            (MAIN_MENU_BANNER_DIR / f"{index:02d}.txt")
            .read_text(encoding="utf-8")
            .strip()
            for index in range(1, 19)
        )
        webp_bytes = base64.b64decode(encoded, validate=True)

        with Image.open(BytesIO(webp_bytes)) as image:
            image = image.convert("RGB")
            if image.size != (1600, 900):
                image = image.resize((1600, 900), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=95,
                subsampling=0,
                optimize=False,
                progressive=False,
            )
            _main_menu_banner_bytes = output.getvalue()

        logger.info(
            "Main menu HQ banner loaded: 1600x900, %s bytes",
            len(_main_menu_banner_bytes),
        )

    return BufferedInputFile(
        _main_menu_banner_bytes,
        filename="mgn_vpn_main_menu_1600x900.jpg",
    )


PLANS: dict[str, dict[str, Any]] = {
    "15": {
        "days": 15,
        "name": "15 дней",
        "devices": 5,
        "price_rub": 49,
    },
    "30": {
        "days": 30,
        "name": "1 месяц",
        "devices": 5,
        "price_rub": 99,
    },
    "365": {
        "days": 365,
        "name": "1 год",
        "devices": 5,
        "price_rub": 2000,
    },
    "forever": {
        "days": 36500,
        "name": "Навсегда",
        "devices": 5,
        "price_rub": 3333,
    },
}

DIAMOND_REWARDS: dict[str, int] = {
    "15": 10,
    "30": 25,
    "365": 400,
    "forever": 700,
}

DIAMOND_SHOP_DAYS: dict[str, dict[str, int]] = {
    "3": {"days": 3, "cost": 60},
    "7": {"days": 7, "cost": 120},
    "30": {"days": 30, "cost": 400},
}

EXTRA_DEVICE_COST = 250
REFERRAL_TRIAL_REWARD = 15
REFERRAL_FIRST_PAID_REWARD = 30

STAR_RATE_XTR = 50
STAR_RATE_RUB = 87



def is_active(user: dict[str, Any]) -> bool:
    until = from_iso(user.get("subscription_until"))
    return bool(until and until > utcnow())


def plan_price_rub(config: Config, code: str) -> int:
    return int(PLANS[code]["price_rub"])


def plan_price_stars(config: Config, code: str) -> int:
    rub = plan_price_rub(config, code)
    return max(1, (rub * STAR_RATE_XTR + STAR_RATE_RUB - 1) // STAR_RATE_RUB)


def format_until(user: dict[str, Any], config: Config) -> str:
    if user.get("plan_name") == "Навсегда":
        return "Навсегда"
    until = from_iso(user.get("subscription_until"))
    if not until:
        return "нет"
    return until.astimezone(config.display_tz).strftime("%d.%m.%Y %H:%M")


def remaining_text(user: dict[str, Any]) -> str:
    if user.get("plan_name") == "Навсегда":
        return "Без ограничений"
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
    emoji: EmojiBank | None = None,
    *,
    custom_icons: bool = False,
) -> ReplyKeyboardMarkup:
    def button(text: str) -> KeyboardButton:
        return KeyboardButton(text=text, style="primary")

    return ReplyKeyboardMarkup(
        keyboard=[
            [button("🔗 Подключить VPN")],
            [
                button("👤 Профиль"),
                button("ℹ️ Информация"),
            ],
            [
                button("💎 Купить VPN"),
                button("📱 Устройства"),
            ],
            [
                button("👥 Друзья"),
                button("🆘 Поддержка"),
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
    )


def section_nav_keyboard(*, back_data: str = "home") -> Any:
    kb = InlineKeyboardBuilder()
    add_nav_buttons(kb, back_data=back_data)
    return kb.as_markup()


def main_menu_inline_keyboard() -> Any:
    kb = InlineKeyboardBuilder()
    kb.row(blue_inline_button("🔗 Подключить VPN", callback_data="menu:connect"))
    kb.row(
        blue_inline_button("👤 Профиль", callback_data="menu:profile"),
        blue_inline_button("ℹ️ Информация", callback_data="menu:info"),
    )
    kb.row(
        blue_inline_button("💎 Купить VPN", callback_data="plans"),
        blue_inline_button("📱 Устройства", callback_data="menu:devices"),
    )
    kb.row(
        blue_inline_button("💎 Алмазы", callback_data="diamonds"),
        blue_inline_button("👥 Друзья", callback_data="menu:friends"),
    )
    kb.row(
        blue_inline_button("🆘 Поддержка", callback_data="menu:support"),
    )
    return kb.as_markup()

def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        kb.row(
            blue_inline_button(
                "💳 "
                + f'{plan["name"]} · {plan_price_rub(config, code)} ₽ · '
                + f'+{DIAMOND_REWARDS.get(code, 0)} 💎',
                callback_data=f"plan:{code}",
            )
        )
    add_nav_buttons(kb, back_data="home")
    return kb.as_markup()


def payment_methods_keyboard(
    config: Config,
    code: str,
    *,
    target_telegram_id: int | None = None,
) -> Any:
    kb = InlineKeyboardBuilder()

    if target_telegram_id is None:
        sbp_data = f"sbp:{code}"
        stars_data = f"stars:{code}"
    else:
        sbp_data = f"sbpgift:{code}:{target_telegram_id}"
        stars_data = f"starsgift:{code}:{target_telegram_id}"

    kb.row(
        blue_inline_button(
            f"🏦 СБП · {plan_price_rub(config, code)} ₽",
            callback_data=sbp_data,
        )
    )
    kb.row(
        blue_inline_button(
            f"⭐ Telegram Stars · {plan_price_stars(config, code)} ⭐",
            callback_data=stars_data,
        )
    )

    if target_telegram_id is None:
        kb.row(
            blue_inline_button(
                "🎁 Купить другому",
                callback_data=f"gift:{code}",
            )
        )
    else:
        kb.row(
            blue_inline_button(
                "🎁 Другой получатель",
                callback_data=f"gift:{code}",
            )
        )

    add_nav_buttons(kb, back_data=f"plan:{code}")
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
        f"💎 <b>Алмазы:</b> {int(user.get('diamonds') or 0)}",
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
    pending_gift_plans: dict[int, str] = {}

    banner_file_id_path = Path(config.db_path).with_name("main_menu_banner_file_id.txt")

    def current_main_menu_banner():
        try:
            file_id = banner_file_id_path.read_text(encoding="utf-8").strip()
            if file_id:
                return file_id
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Could not read saved main-menu banner file_id")
        return main_menu_banner()

    def save_main_menu_banner_file_id(file_id: str) -> None:
        banner_file_id_path.parent.mkdir(parents=True, exist_ok=True)
        banner_file_id_path.write_text(file_id.strip(), encoding="utf-8")

    async def ensure_actor(actor) -> dict[str, Any]:
        return await db.ensure_user(
            actor.id,
            actor.username,
            actor.first_name,
        )

    async def is_trial_channel_member(bot, user_id: int) -> bool:
        try:
            member = await bot.get_chat_member(
                chat_id=config.trial_channel_username,
                user_id=user_id,
            )
        except Exception as exc:
            logger.exception(
                "Trial channel membership check failed for user %s in %s: %s",
                user_id,
                config.trial_channel_username,
                exc,
            )
            return False

        status = getattr(member.status, "value", str(member.status))
        if status in {"member", "administrator", "creator"}:
            return True

        # Restricted members may still be members of the channel.
        if status == "restricted" and bool(getattr(member, "is_member", False)):
            return True

        return False

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
        reply_markup=main_menu_inline_keyboard(),
        bottom_menu: bool = False,
    ) -> Message:
        user = await ensure_actor(actor)
        last_id = user.get("last_menu_message_id")

        async def create_first_menu() -> Message:
            try:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=current_main_menu_banner(),
                    caption=text,
                    reply_markup=main_menu_inline_keyboard(),
                )
            except TelegramBadRequest as exc:
                logger.warning("Initial main-menu photo failed: %s", exc)
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=current_main_menu_banner(),
                    caption=strip_custom_emoji(text),
                    reply_markup=main_menu_inline_keyboard(),
                )

            await db.set_last_menu_message(actor.id, sent.message_id)
            logger.info(
                "Persistent menu created for %s as message %s",
                actor.id,
                sent.message_id,
            )
            return sent

        if not last_id:
            return await create_first_menu()

        # Keep one persistent message. We never delete it.
        if bottom_menu:
            try:
                edited = await message.bot.edit_message_media(
                    chat_id=message.chat.id,
                    message_id=int(last_id),
                    media=InputMediaPhoto(
                        media=current_main_menu_banner(),
                        caption=text,
                    ),
                    reply_markup=main_menu_inline_keyboard(),
                )
                return edited
            except TelegramBadRequest as exc:
                error_text = str(exc).lower()
                if "message is not modified" in error_text:
                    return message

                logger.warning(
                    "Could not edit main-menu media in place: %s",
                    exc,
                )

                # Stale id from an already deleted old menu: there is no
                # visible message to preserve, so create the single menu again.
                if (
                    "message to edit not found" in error_text
                    or "message not found" in error_text
                ):
                    await db.set_last_menu_message(actor.id, None)
                    return await create_first_menu()

                try:
                    edited = await message.bot.edit_message_media(
                        chat_id=message.chat.id,
                        message_id=int(last_id),
                        media=InputMediaPhoto(
                            media=current_main_menu_banner(),
                            caption=strip_custom_emoji(text),
                        ),
                        reply_markup=None,
                    )
                    return edited
                except TelegramBadRequest as retry_exc:
                    retry_text = str(retry_exc).lower()
                    logger.warning(
                        "Main-menu media retry failed: %s",
                        retry_exc,
                    )
                    if (
                        "message to edit not found" in retry_text
                        or "message not found" in retry_text
                    ):
                        await db.set_last_menu_message(actor.id, None)
                        return await create_first_menu()
                except Exception as retry_exc:
                    logger.warning(
                        "Main-menu media retry failed: %s",
                        retry_exc,
                    )

                # Old text-only menu from a previous version cannot be turned
                # into a photo via Telegram API. Keep that exact message id and
                # at least update its text instead of silently doing nothing.
                try:
                    edited = await message.bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=int(last_id),
                        text=strip_custom_emoji(text),
                        reply_markup=main_menu_inline_keyboard(),
                    )
                    return edited
                except TelegramBadRequest as text_exc:
                    text_error = str(text_exc).lower()
                    if "message is not modified" in text_error:
                        return message
                    if (
                        "message to edit not found" in text_error
                        or "message not found" in text_error
                    ):
                        await db.set_last_menu_message(actor.id, None)
                        return await create_first_menu()
                    logger.warning("Legacy menu text edit failed: %s", text_exc)
                except Exception as text_exc:
                    logger.warning("Legacy menu text edit failed: %s", text_exc)

                return message
            except Exception as exc:
                logger.warning(
                    "Could not edit main-menu media in place: %s",
                    exc,
                )
                return message

        # Normal sections edit the caption of the same photo message.
        try:
            edited = await message.bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=int(last_id),
                caption=text,
                reply_markup=reply_markup,
            )
            return edited
        except TelegramBadRequest as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                return message
            if (
                "message to edit not found" in error_text
                or "message not found" in error_text
            ):
                await db.set_last_menu_message(actor.id, None)
                sent = await create_first_menu()
                try:
                    return await message.bot.edit_message_caption(
                        chat_id=message.chat.id,
                        message_id=sent.message_id,
                        caption=text,
                        reply_markup=reply_markup,
                    )
                except Exception:
                    return sent

            try:
                edited = await message.bot.edit_message_caption(
                    chat_id=message.chat.id,
                    message_id=int(last_id),
                    caption=strip_custom_emoji(text),
                    reply_markup=reply_markup,
                )
                return edited
            except Exception as retry_exc:
                logger.warning("Caption edit retry failed: %s", retry_exc)
        except Exception as exc:
            logger.warning("Caption edit failed: %s", exc)

        # Legacy text-only menu: edit it in place, do not delete it.
        try:
            edited = await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(last_id),
                text=strip_custom_emoji(text),
                reply_markup=reply_markup,
            )
            return edited
        except TelegramBadRequest as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                return message
            if (
                "message to edit not found" in error_text
                or "message not found" in error_text
            ):
                await db.set_last_menu_message(actor.id, None)
                return await create_first_menu()
            logger.warning("Legacy text menu edit failed: %s", exc)
        except Exception as exc:
            logger.warning("Legacy text menu edit failed: %s", exc)

        return message

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

    async def show_diamonds(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        balance = int(user.get("diamonds") or 0)

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("🛍 Магазин", callback_data="diamonds:shop"),
            blue_inline_button("👥 Заработать", callback_data="diamonds:earn"),
        )
        kb.row(
            blue_inline_button("📜 История", callback_data="diamonds:history"),
        )
        add_nav_buttons(kb, back_data="home")

        await send_screen(
            message,
            actor,
            "💎 <b>Алмазы MGN VPN</b>\n\n"
            f"Баланс — <b>{balance} 💎</b>\n\n"
            "Получайте алмазы за покупки и приглашённых друзей, "
            "а затем меняйте их на дни VPN, дополнительные устройства "
            "и промокоды.",
            reply_markup=kb.as_markup(),
        )

    async def show_diamond_shop(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        balance = int(user.get("diamonds") or 0)
        kb = InlineKeyboardBuilder()

        for key, item in DIAMOND_SHOP_DAYS.items():
            kb.row(
                blue_inline_button(
                    f"⏳ {item['days']} дн. VPN · {item['cost']} 💎",
                    callback_data=f"shop:days:{key}",
                )
            )

        kb.row(
            blue_inline_button(
                f"📱 +1 устройство · {EXTRA_DEVICE_COST} 💎",
                callback_data="shop:device",
            )
        )

        promos = await db.promo_products()
        for item in promos[:12]:
            slug = str(item["slug"])
            title = str(item["title"])
            price = int(item["price_diamonds"])
            stock = int(item["stock"])
            kb.row(
                blue_inline_button(
                    f"🎟 {title[:28]} · {price} 💎 ({stock})",
                    callback_data=f"shop:promo:{slug}",
                )
            )

        add_nav_buttons(kb, back_data="diamonds")
        promo_note = (
            "\n\n🎟 Доступные промокоды показаны ниже."
            if promos
            else "\n\n🎟 Промокодов сейчас нет в наличии."
        )
        await send_screen(
            message,
            actor,
            "🛍 <b>Магазин за алмазы</b>\n\n"
            f"Ваш баланс — <b>{balance} 💎</b>\n\n"
            "Выберите награду. Покупки списываются с баланса сразу."
            + promo_note,
            reply_markup=kb.as_markup(),
        )

    async def show_diamond_earn(message: Message, actor) -> None:
        await ensure_actor(actor)
        bot_info = await message.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{actor.id}"
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "👥 Поделиться ссылкой",
                url=(
                    "https://t.me/share/url?url="
                    + quote(link, safe="")
                    + "&text="
                    + quote("Подключай MGN VPN", safe="")
                ),
            )
        )
        add_nav_buttons(kb, back_data="diamonds")

        await send_screen(
            message,
            actor,
            "💎 <b>Как заработать алмазы</b>\n\n"
            f"15 дней VPN — <b>+{DIAMOND_REWARDS['15']} 💎</b>\n"
            f"1 месяц — <b>+{DIAMOND_REWARDS['30']} 💎</b>\n"
            f"1 год — <b>+{DIAMOND_REWARDS['365']} 💎</b>\n"
            f"Навсегда — <b>+{DIAMOND_REWARDS['forever']} 💎</b>\n\n"
            f"Друг активировал пробник — <b>+{REFERRAL_TRIAL_REWARD} 💎</b>\n"
            f"Друг впервые купил VPN — <b>+{REFERRAL_FIRST_PAID_REWARD} 💎</b>\n\n"
            f"Ваша ссылка:\n<code>{html.escape(link)}</code>",
            reply_markup=kb.as_markup(),
        )

    async def show_diamond_history(message: Message, actor) -> None:
        await ensure_actor(actor)
        history = await db.diamond_history(actor.id, 10)
        lines = ["📜 <b>История алмазов</b>", ""]

        if not history:
            lines.append("Операций пока нет.")
        else:
            for item in history:
                amount = int(item["amount"])
                sign = "+" if amount > 0 else ""
                lines.append(
                    f"{sign}{amount} 💎 · {html.escape(str(item['reason']))}"
                )

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=section_nav_keyboard(back_data="diamonds"),
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


    async def apply_paid_purchase(
        buyer_telegram_id: int,
        target_telegram_id: int,
        code: str,
        payment_event_key: str,
    ) -> tuple[dict[str, Any], int]:
        user = await activate_paid_plan(target_telegram_id, code)

        reward_amount = int(DIAMOND_REWARDS.get(code, 0))
        if reward_amount:
            await db.add_diamonds(
                telegram_id=buyer_telegram_id,
                amount=reward_amount,
                reason=f"Покупка VPN: {PLANS[code]['name']}",
                event_key=f"payment-reward:{payment_event_key}",
            )

        referrer_id = user.get("referrer_id")
        if referrer_id:
            await db.add_diamonds(
                telegram_id=int(referrer_id),
                amount=REFERRAL_FIRST_PAID_REWARD,
                reason="Друг впервые купил VPN",
                event_key=f"referral-first-paid:{target_telegram_id}",
            )

        return user, reward_amount

    @router.message(Command("setbanner"))
    async def set_banner(message: Message) -> None:
        if not message.from_user or message.from_user.id not in config.admin_ids:
            return

        source_message = message.reply_to_message or message
        if not source_message.photo:
            await message.answer(
                "Пришли нужную картинку как фото с подписью /setbanner "
                "или ответь командой /setbanner на фото."
            )
            return

        photo = source_message.photo[-1]
        save_main_menu_banner_file_id(photo.file_id)

        # Update the existing main-menu message in place. No delete, no new
        # confirmation message, no duplicate banner.
        await show_home(message, message.from_user)

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


    @router.callback_query(F.data == "menu:profile")
    async def menu_profile(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_profile(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:connect")
    async def menu_connect(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            if not user.get("trial_used"):
                await send_screen(
                    callback.message,
                    callback.from_user,
                    "🎁 <b>Пробная подписка</b>\n\n"
                    f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                    "затем нажмите <b>«✅ Проверить подписку»</b>.",
                    reply_markup=trial_channel_keyboard(),
                )
            else:
                kb = InlineKeyboardBuilder()
                kb.row(blue_inline_button("💎 Купить подписку", callback_data="plans"))
                add_nav_buttons(kb, back_data="home")
                await send_screen(
                    callback.message,
                    callback.from_user,
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Пробный период уже использован. Выберите подписку.",
                    reply_markup=kb.as_markup(),
                )
            return

        state, ok = await load_state(user, provider, config)
        if not ok or not state.subscription_url:
            await send_screen(
                callback.message,
                callback.from_user,
                "🔗 <b>Подключение VPN</b>\n\n"
                "Ссылка подключения пока недоступна. Попробуйте немного позже.",
                reply_markup=section_nav_keyboard(),
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔗 Открыть подключение", url=state.subscription_url))
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            callback.message,
            callback.from_user,
            connection_text(user, state, emoji),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:info")
    async def menu_info(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("📢 Канал MGN VPN", url=config.trial_channel_url))
        add_nav_buttons(kb, back_data="home")
        e = emoji.icon(9, pack=PACK_UI)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Информация</b>\n\n"
            "🔐 Доступ выдаётся по персональной ссылке.\n"
            "📱 Платная подписка — до <b>5 устройств</b>.\n"
            "🎁 Пробный доступ можно активировать один раз после подписки на наш Telegram-канал.\n"
            "⚙️ Управление подпиской и устройствами находится прямо в боте.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            e = emoji.icon(9, pack=PACK_UI)
            await send_screen(
                callback.message,
                callback.from_user,
                f"{e} <b>Поддержка</b>\n\n"
                "1. Активируйте пробный доступ или купите подписку.\n"
                "2. Нажмите <b>«🔗 Подключить VPN»</b>.\n"
                "3. Откройте персональную ссылку на нужном устройстве.\n\n"
                "Подключённые устройства можно отключить в разделе <b>«📱 Устройства»</b>.",
                reply_markup=section_nav_keyboard(),
            )

    @router.callback_query(F.data == "menu:friends")
    async def menu_friends(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        bot_info = await callback.message.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
        count = await db.referral_count(callback.from_user.id)
        share_url = (
            "https://t.me/share/url?url=" + quote(link, safe="")
            + "&text=" + quote("Подключай MGN VPN", safe="")
        )
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("👥 Поделиться", url=share_url))
        add_nav_buttons(kb, back_data="home")
        e = emoji.icon(8, pack=PACK_UI)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Друзья</b>\n\n"
            f"Ваша ссылка:\n<code>{html.escape(link)}</code>\n\n"
            f"Приглашено — <b>{count}</b>\n\n"
            f"Друг активировал пробник — <b>+{REFERRAL_TRIAL_REWARD} 💎</b>\n"
            f"Первая покупка друга — <b>+{REFERRAL_FIRST_PAID_REWARD} 💎</b>",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:devices")
    async def menu_devices(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await send_screen(
                callback.message,
                callback.from_user,
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
                name = html.escape(str(item.get("name") or item.get("device_name") or f"Устройство {i}"))
                platform = html.escape(str(item.get("platform") or item.get("os") or ""))
                suffix = f" — {platform}" if platform else ""
                lines.append(f"{i}. {name}{suffix}")
                device_id = str(item.get("id") or item.get("device_id") or "")
                if device_id and len(device_id.encode("utf-8")) <= 36:
                    kb.row(blue_inline_button(
                        f"❌ Отключить устройство {i}",
                        callback_data=f"deldev:{device_id}",
                    ))
        else:
            lines += ["", "<i>Подключённых устройств пока нет.</i>"]
        if not ok:
            lines += ["", "<i>Сервер устройств временно не ответил.</i>"]
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            callback.message,
            callback.from_user,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "diamonds")
    async def diamonds_home(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_diamonds(callback.message, callback.from_user)

    @router.callback_query(F.data == "diamonds:shop")
    async def diamonds_shop(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_diamond_shop(callback.message, callback.from_user)

    @router.callback_query(F.data == "diamonds:earn")
    async def diamonds_earn(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_diamond_earn(callback.message, callback.from_user)

    @router.callback_query(F.data == "diamonds:history")
    async def diamonds_history(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_diamond_history(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("shop:days:"))
    async def buy_shop_days(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        key = callback.data.rsplit(":", 1)[-1]
        item = DIAMOND_SHOP_DAYS.get(key)
        if not item:
            await callback.answer("Товар не найден", show_alert=True)
            return

        balance = await db.diamond_balance(callback.from_user.id)
        cost = int(item["cost"])
        if balance < cost:
            await callback.answer(
                f"Не хватает {cost - balance} 💎",
                show_alert=True,
            )
            return

        user = await db.purchase_vpn_days(
            telegram_id=callback.from_user.id,
            days=int(item["days"]),
            cost=cost,
            event_key=f"shop-days:{callback.from_user.id}:{uuid4().hex}",
        )
        if not user:
            await callback.answer("Не удалось выполнить покупку", show_alert=True)
            return

        try:
            await provider.provision(user)
        except Exception:
            pass

        await callback.answer(f"+{item['days']} дней VPN")
        await show_diamond_shop(callback.message, callback.from_user)

    @router.callback_query(F.data == "shop:device")
    async def buy_shop_device(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        balance = int(user.get("diamonds") or 0)
        if balance < EXTRA_DEVICE_COST:
            await callback.answer(
                f"Не хватает {EXTRA_DEVICE_COST - balance} 💎",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or 1) >= 10:
            await callback.answer(
                "Достигнут лимит: 10 устройств.",
                show_alert=True,
            )
            return

        user = await db.purchase_extra_device(
            telegram_id=callback.from_user.id,
            cost=EXTRA_DEVICE_COST,
            event_key=f"shop-device:{callback.from_user.id}:{uuid4().hex}",
            max_total_devices=10,
        )
        if not user:
            await callback.answer("Не удалось выполнить покупку", show_alert=True)
            return

        try:
            await provider.provision(user)
        except Exception:
            pass

        await callback.answer("+1 устройство")
        await show_diamond_shop(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("shop:promo:"))
    async def buy_shop_promo(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        slug = callback.data.split(":", 2)[-1]
        product_list = await db.promo_products()
        product = next(
            (item for item in product_list if str(item["slug"]) == slug),
            None,
        )
        if not product:
            await callback.answer(
                "Промокоды закончились или товар недоступен.",
                show_alert=True,
            )
            await show_diamond_shop(callback.message, callback.from_user)
            return

        balance = await db.diamond_balance(callback.from_user.id)
        cost = int(product["price_diamonds"])
        if balance < cost:
            await callback.answer(
                f"Не хватает {cost - balance} 💎",
                show_alert=True,
            )
            return

        reward = await db.redeem_promo(
            telegram_id=callback.from_user.id,
            slug=slug,
            event_key=f"shop-promo:{callback.from_user.id}:{uuid4().hex}",
        )
        if not reward:
            await callback.answer(
                "Промокод уже закончился. Баланс не списан.",
                show_alert=True,
            )
            await show_diamond_shop(callback.message, callback.from_user)
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("⬅️ Магазин", callback_data="diamonds:shop"),
        )
        await callback.answer("Промокод получен")
        await send_screen(
            callback.message,
            callback.from_user,
            "🎟 <b>Покупка готова</b>\n\n"
            f"{html.escape(str(reward['title']))}\n"
            f"Списано: <b>{int(reward['price_diamonds'])} 💎</b>\n\n"
            "Ваш промокод:\n"
            f"<code>{html.escape(str(reward['code']))}</code>\n\n"
            "<i>Сохраните код. Он также останется в истории алмазов.</i>",
            reply_markup=kb.as_markup(),
        )

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
            "Оплата через СБП.",
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
            "Оплата через СБП.",
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
            f"<b>{plan_price_rub(config, code)} ₽</b> · СБП",
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
            reward_amount = 0
            if fresh:
                plan_code = str(local["plan_code"])
                await activate_paid_plan(
                    callback.from_user.id,
                    plan_code,
                )

                reward_amount = int(DIAMOND_REWARDS.get(plan_code, 0))
                if reward_amount:
                    await db.add_diamonds(
                        telegram_id=callback.from_user.id,
                        amount=reward_amount,
                        reason=f"Покупка VPN: {PLANS[plan_code]['name']}",
                        event_key=f"payment-reward:{payment_id}",
                    )

                paid_user = await db.get_user(callback.from_user.id)
                referrer_id = paid_user.get("referrer_id")
                if referrer_id:
                    await db.add_diamonds(
                        telegram_id=int(referrer_id),
                        amount=REFERRAL_FIRST_PAID_REWARD,
                        reason="Друг впервые купил VPN",
                        event_key=f"referral-first-paid:{callback.from_user.id}",
                    )

            if reward_amount:
                await callback.answer(f"Оплата получена · +{reward_amount} 💎")
            else:
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

    @router.message(F.text.in_({"🔗 Подключить VPN", "🔗 Подключиться", "Подключить VPN", "Подключиться"}))
    async def connect(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not is_active(user):
            if not user.get("trial_used"):
                await send_screen(
                    message,
                    message.from_user,
                    "🎁 <b>Пробная подписка</b>\n\n"
                    f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                    "затем нажмите <b>«✅ Проверить подписку»</b>.",
                    reply_markup=trial_channel_keyboard(),
                )
            else:
                kb = InlineKeyboardBuilder()
                kb.row(
                    blue_inline_button(
                        "💎 Купить подписку",
                        callback_data="plans",
                    )
                )
                add_nav_buttons(kb, back_data="home")
                await send_screen(
                    message,
                    message.from_user,
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Пробный период уже использован. Выберите подписку.",
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
            await callback.answer("Подписка пока не найдена.")
            await send_screen(
                callback.message,
                callback.from_user,
                "🎁 <b>Пробная подписка</b>\n\n"
                f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                "затем нажмите <b>«✅ Проверить подписку»</b>.",
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

        referrer_id = user.get("referrer_id")
        if referrer_id:
            await db.add_diamonds(
                telegram_id=int(referrer_id),
                amount=REFERRAL_TRIAL_REWARD,
                reason="Друг активировал пробный VPN",
                event_key=f"referral-trial:{callback.from_user.id}",
            )

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
            f"Приглашено — <b>{count}</b>\n\n"
            f"Друг активировал пробник — <b>+{REFERRAL_TRIAL_REWARD} 💎</b>\n"
            f"Первая покупка друга — <b>+{REFERRAL_FIRST_PAID_REWARD} 💎</b>",
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
            blue_inline_button("💎 Бонусы", callback_data="admin:bonuses"),
        )
        kb.row(
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
            f"СБП оборот — <b>{stats['sbp_revenue']} ₽</b>"
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
            f"💎 Алмазы — <b>{int(user.get('diamonds') or 0)}</b>\n"
            f"Пробник — <b>{'использован' if user.get('trial_used') else 'доступен'}</b>\n"
            f"Приглашено — <b>{referrals}</b>"
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

    async def show_admin_bonuses(message: Message, actor) -> None:
        stock = await db.promo_stock_overview()
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:bonuses"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        lines = [
            "💎 <b>Бонусная система</b>",
            "",
            "Команды:",
            "<code>/diamonds ID AMOUNT</code>",
            "<code>/promoproduct SLUG PRICE TITLE</code>",
            "<code>/promocode SLUG CODE</code>",
            "",
            "🎟 <b>Промокоды</b>",
        ]

        if not stock:
            lines.append("Товаров пока нет.")
        else:
            for item in stock:
                lines.append(
                    f"• {html.escape(str(item['title']))} "
                    f"(<code>{html.escape(str(item['slug']))}</code>) — "
                    f"<b>{int(item['price_diamonds'])} 💎</b> · "
                    f"остаток {int(item['stock'] or 0)} · "
                    f"выдано {int(item['issued'] or 0)}"
                )

        await send_screen(
            message,
            actor,
            "\n".join(lines),
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

    @router.callback_query(F.data == "admin:bonuses")
    async def admin_bonuses_callback(callback: CallbackQuery) -> None:
        if not is_admin(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_bonuses(callback.message, callback.from_user)

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

    @router.message(Command("diamonds"))
    async def admin_diamonds(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return

        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Использование: /diamonds TELEGRAM_ID AMOUNT")
            return

        try:
            telegram_id = int(parts[1])
            amount = int(parts[2])
        except ValueError:
            await message.answer("ID и AMOUNT должны быть числами.")
            return

        if amount == 0 or abs(amount) > 1_000_000:
            await message.answer("AMOUNT: от -1000000 до 1000000, кроме 0.")
            return

        try:
            await db.get_user(telegram_id)
        except KeyError:
            await message.answer("Пользователь ещё не запускал бота.")
            return

        changed = await db.add_diamonds(
            telegram_id=telegram_id,
            amount=amount,
            reason="Изменение администратором",
            event_key=f"admin-diamonds:{message.from_user.id}:{telegram_id}:{uuid4().hex}",
        )
        balance = await db.diamond_balance(telegram_id)
        if not changed:
            await message.answer("Баланс не изменился.")
            return

        await send_screen(
            message,
            message.from_user,
            f"💎 Баланс <code>{telegram_id}</code> изменён.\n"
            f"Теперь: <b>{balance} 💎</b>",
            reply_markup=admin_main_keyboard(),
        )

    @router.message(Command("promoproduct"))
    async def admin_promo_product(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return

        parts = (message.text or "").split(maxsplit=3)
        if len(parts) != 4:
            await message.answer(
                "Использование: /promoproduct SLUG PRICE TITLE\n"
                "Пример: /promoproduct yandex_plus 500 Яндекс Плюс 60 дней"
            )
            return

        slug = parts[1].strip().lower()
        try:
            price = int(parts[2])
        except ValueError:
            await message.answer("PRICE должен быть числом.")
            return
        title = parts[3].strip()

        if not re.fullmatch(r"[a-z0-9_-]{2,48}", slug):
            await message.answer("SLUG: только a-z, 0-9, _ и -, длина 2–48.")
            return
        if price < 1 or price > 1_000_000 or not title:
            await message.answer("Проверь цену и название товара.")
            return

        await db.create_promo_product(slug, title, price)
        await send_screen(
            message,
            message.from_user,
            f"✅ Товар <b>{html.escape(title)}</b> сохранён за <b>{price} 💎</b>.\n"
            f"Теперь добавляй коды: <code>/promocode {html.escape(slug)} CODE</code>",
            reply_markup=admin_main_keyboard(),
        )

    @router.message(Command("promocode"))
    async def admin_promo_code(message: Message) -> None:
        if not is_admin(message.from_user.id):
            return

        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            await message.answer("Использование: /promocode SLUG CODE")
            return

        slug = parts[1].strip().lower()
        code = parts[2].strip()
        if not code:
            await message.answer("CODE пустой.")
            return

        added = await db.add_promo_code(slug, code)
        if not added:
            await message.answer(
                "Не удалось добавить код: товар не найден или такой код уже есть."
            )
            return

        await send_screen(
            message,
            message.from_user,
            f"✅ Код добавлен в товар <code>{html.escape(slug)}</code>.",
            reply_markup=admin_main_keyboard(),
        )

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
