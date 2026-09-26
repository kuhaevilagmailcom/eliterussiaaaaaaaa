from __future__ import annotations

import asyncio
import base64
import html
import logging
import re
from datetime import timedelta
from io import BytesIO
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiogram import F, Router
import aiosqlite
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InputMediaPhoto,
    KeyboardButton,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image

from catalog import (
    BASE_DEVICES,
    DEVICE_PRODUCT_CODE,
    EXTRA_DEVICE_PRICE_RUB,
    MAX_DEVICES,
    PLANS,
    extra_device_price_stars,
    plan_price_rub,
    plan_price_stars,
    plan_savings_rub,
)
from config import Config
from db import Database, from_iso, utcnow
from emoji import EmojiBank
from payments import RollyPayError, create_payment, get_payment
from vpn import VpnProvider, VpnState
from vpn_clients import CLIENTS, client_redirect_url


PACK_CRYPTO = "CryptoGIFTPODARKI"
PACK_UI = "TgAndroidIcons"
PACK_PROGRESS = "progressBarEmoji"
PACK_NEWS = "NewsEmoji"

logger = logging.getLogger(__name__)

MAIN_MENU_BANNER_DIR = Path(__file__).with_name("assets") / "banner_parts"
_main_menu_banner_bytes: bytes | None = None


def main_menu_banner() -> BufferedInputFile:
    global _main_menu_banner_bytes

    if _main_menu_banner_bytes is None:
        try:
            encoded = "".join(
                (MAIN_MENU_BANNER_DIR / f"{index:02d}.txt")
                .read_text(encoding="utf-8")
                .strip()
                for index in range(1, 19)
            )
            webp_bytes = base64.b64decode(encoded, validate=True)

            with Image.open(BytesIO(webp_bytes)) as source:
                image = source.convert("RGB")
                if image.size != (1600, 900):
                    image = image.resize((1600, 900), Image.Resampling.LANCZOS)
        except Exception as exc:
            # A broken bundled banner must never take the whole bot down.
            # Keep the menu usable even if one of the base64 asset chunks is
            # malformed or was partially committed.
            logger.error("Bundled main-menu banner is invalid: %s", exc)
            image = Image.new("RGB", (1600, 900), (7, 7, 9))
            image.paste((244, 90, 184), (0, 0, 1600, 10))

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


def is_active(user: dict[str, Any]) -> bool:
    until = from_iso(user.get("subscription_until"))
    return bool(until and until > utcnow())


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
        subscription_url="",
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
    if not getattr(provider, "service_ready", True):
        return fallback_state(user, config), False

    # Fast path for an already provisioned H1 client: opening the bot UI must
    # not wait for every remote federation node.
    try:
        state = await asyncio.wait_for(
            provider.get_state(user),
            timeout=5.0,
        )
        return state, True
    except Exception as state_exc:
        logger.warning(
            "VPN state read failed for user %s: %s",
            user.get("telegram_id"),
            str(state_exc).strip() or type(state_exc).__name__,
        )

    try:
        state = await asyncio.wait_for(
            provider.provision(user),
            timeout=15.0,
        )
        return state, True
    except Exception as exc:
        logger.warning(
            "VPN provisioning failed for user %s: %s",
            user.get("telegram_id"),
            str(exc).strip() or type(exc).__name__,
        )
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
                button("💳 Купить VPN"),
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


def copy_inline_button(text: str, value: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        copy_text=CopyTextButton(text=value),
        style="primary",
    )


def connection_keyboard(
    subscription_url: str,
    *,
    back_data: str = "home",
) -> Any:
    kb = InlineKeyboardBuilder()
    kb.row(
        blue_inline_button(
            "🚀 Открыть VPN",
            url=subscription_url,
        )
    )
    kb.row(
        copy_inline_button(
            "📋 Скопировать ссылку",
            subscription_url,
        )
    )
    for client in CLIENTS:
        target = (
            client_redirect_url(subscription_url, client)
            if client.supports_subscription_import
            else client.download_url
        )
        label = (
            f"{client.icon} Добавить в {client.name}"
            if client.supports_subscription_import
            else f"{client.icon} Скачать {client.name}"
        )
        kb.row(blue_inline_button(label, url=target))
    add_nav_buttons(kb, back_data=back_data)
    return kb.as_markup()


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


def main_menu_inline_keyboard(
    admin_role: str | None = None,
    miniapp_url: str = "",
) -> Any:
    kb = InlineKeyboardBuilder()
    if miniapp_url:
        kb.row(
            InlineKeyboardButton(
                text="🚀 Открыть MGN VPN",
                web_app=WebAppInfo(url=miniapp_url),
                style="primary",
            )
        )
    kb.row(blue_inline_button("🔗 Подключить VPN", callback_data="menu:connect"))
    kb.row(
        blue_inline_button("👤 Профиль", callback_data="menu:profile"),
        blue_inline_button("ℹ️ Информация", callback_data="menu:info"),
    )
    kb.row(
        blue_inline_button("💳 Купить VPN", callback_data="plans"),
        blue_inline_button("📱 Устройства", callback_data="menu:devices"),
    )
    kb.row(
        blue_inline_button("👥 Пригласить друзей", callback_data="menu:friends"),
        blue_inline_button("🎟 Промокод", callback_data="menu:promo"),
    )
    kb.row(
        blue_inline_button("🆘 Поддержка", callback_data="menu:support"),
    )
    if admin_role:
        kb.row(
            blue_inline_button("🛡 Админка", callback_data="admin:home"),
        )
    return kb.as_markup()

def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        savings = plan_savings_rub(code)
        suffix = f" · выгода {savings} ₽" if savings else ""
        kb.row(
            blue_inline_button(
                f"💳 {plan['name']} · {plan_price_rub(config, code)} ₽{suffix}",
                callback_data=f"plan:{code}",
            )
        )
    add_nav_buttons(kb, back_data="home")
    return kb.as_markup()


def device_payment_keyboard() -> Any:
    kb = InlineKeyboardBuilder()
    kb.row(
        blue_inline_button(
            f"🏦 СБП · {EXTRA_DEVICE_PRICE_RUB} ₽",
            callback_data="device:sbp",
        )
    )
    kb.row(
        blue_inline_button(
            f"⭐ Telegram Stars · {extra_device_price_stars()} ⭐",
            callback_data="device:stars",
        )
    )
    add_nav_buttons(kb, back_data="menu:devices")
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

    e_profile = emoji.icon(0, pack=PACK_NEWS)
    e_sub = emoji.icon(1, pack=PACK_NEWS)

    lines = [
        f"{e_profile} <b>Ваш ID:</b> <code>{user_id}</code>",
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
            f"├ Бесплатный день: <b>{trial}</b>",
            f"└ Устройства: <b>до {max_devices}</b>",
        ]

    lines += [
        "",
        "Получить доступ можно кнопкой <b>«🔗 Подключить VPN»</b> ниже.",
    ]

    if not provider_ok and active:
        if config.vpn_mode == "demo":
            lines += ["", "<i>VPN-серверы ещё не подключены. Подписка сохранена.</i>"]
        else:
            lines += ["", "<i>VPN-сервер временно не отвечает. Подписка сохранена.</i>"]

    return "\n".join(lines)


def _https_public_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    if value.startswith("http://"):
        value = "https://" + value[len("http://"):]
    elif value and not value.startswith("https://"):
        value = "https://" + value.lstrip("/")
    return value if value.startswith("https://") else ""


def _saved_public_base_url(config: Config) -> str:
    path = Path(config.db_path).with_name("public_base_url.txt")
    try:
        value = path.read_text(encoding="utf-8").strip().rstrip("/")
    except FileNotFoundError:
        return ""
    except Exception:
        logger.exception("Could not read saved public base URL")
        return ""
    return _https_public_base_url(value)


async def public_subscription_url(
    user: dict[str, Any],
    state: VpnState,
    config: Config,
    *,
    bot=None,
) -> str:
    if config.vpn_mode == "h1cloud" and user.get("sub_token") and is_active(user):
        base_url = _https_public_base_url(config.miniapp_url) or _saved_public_base_url(config)

        # If BotHost did not expose DOMAIN/MINIAPP_URL to the process, Telegram
        # may still have the previously configured Mini App menu button. Reuse
        # its URL so the bot and Mini App return the exact same /sub/<token>.
        if not base_url and bot is not None:
            try:
                menu_button = await bot.get_chat_menu_button()
                web_app = getattr(menu_button, "web_app", None)
                menu_url = str(getattr(web_app, "url", "") or "").strip().rstrip("/")
                menu_url = _https_public_base_url(menu_url)
                if menu_url:
                    base_url = menu_url
            except Exception as exc:
                logger.warning("Could not resolve Mini App URL from Telegram menu: %s", exc)

        if base_url:
            token = quote(str(user["sub_token"]), safe="")
            return f"{base_url}/sub/{token}"

    return state.subscription_url or ""


def connection_text(
    user: dict[str, Any],
    state: VpnState,
    emoji: EmojiBank,
    subscription_url: str,
) -> str:
    e_link = emoji.icon(2, pack=PACK_NEWS)
    server = html.escape(state.server or "MGN VPN")
    safe_url = html.escape(subscription_url)
    return (
        f"{e_link} <b>Подключение</b>\n\n"
        "Ваша персональная HTTPS-ссылка готова.\n"
        f"<code>{safe_url}</code>\n\n"
        f"Сервер — <b>{server}</b>\n"
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
        # A Telegram file_id is the preferred source: no decoding, no image
        # processing and no quality loss. /setbanner persists it next to DB.
        try:
            file_id = banner_file_id_path.read_text(encoding="utf-8").strip()
            if file_id:
                return file_id
        except FileNotFoundError:
            pass
        except Exception:
            logger.exception("Could not read saved main-menu banner file_id")

        if config.main_menu_banner_file_id:
            return config.main_menu_banner_file_id

        return main_menu_banner()

    def save_main_menu_banner_file_id(file_id: str) -> None:
        banner_file_id_path.parent.mkdir(parents=True, exist_ok=True)
        banner_file_id_path.write_text(file_id.strip(), encoding="utf-8")


    def clear_main_menu_banner_file_id() -> None:
        try:
            banner_file_id_path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Could not clear saved main-menu banner file_id")

    async def ensure_actor(actor) -> dict[str, Any]:
        return await db.ensure_user(
            actor.id,
            actor.username,
            actor.first_name,
        )


    async def get_admin_role(user_id: int) -> str | None:
        if user_id in config.admin_ids:
            return "owner"
        return await db.get_admin_role(user_id)

    async def has_admin_access(user_id: int) -> bool:
        return bool(await get_admin_role(user_id))

    async def has_full_admin_access(user_id: int) -> bool:
        return (await get_admin_role(user_id)) in {"owner", "full"}

    def is_owner(user_id: int) -> bool:
        return user_id in config.admin_ids

    async def is_trial_channel_member(
        bot,
        user_id: int,
        *,
        retries: int = 3,
    ) -> bool:
        retries = max(1, min(int(retries), 4))
        for attempt in range(retries):
            try:
                member = await bot.get_chat_member(
                    chat_id=config.trial_channel_username,
                    user_id=user_id,
                )
                status = getattr(member.status, "value", str(member.status))
                if status in {"member", "administrator", "creator"}:
                    return True
                if status == "restricted" and bool(getattr(member, "is_member", False)):
                    return True
            except Exception as exc:
                logger.warning(
                    "Trial channel membership check failed for user %s in %s: %s",
                    user_id,
                    config.trial_channel_username,
                    exc,
                )

            # Telegram may need a short moment after the user joins a channel.
            if attempt + 1 < retries:
                await asyncio.sleep(0.6)

        return False

    def trial_channel_keyboard(*, subscribed: bool = False) -> Any:
        kb = InlineKeyboardBuilder()
        if not subscribed:
            kb.row(
                blue_inline_button(
                    "📢 Подписаться на канал",
                    url=config.trial_channel_url,
                )
            )
        kb.row(
            blue_inline_button(
                "🎁 Забрать 1 день",
                callback_data="trial:claim",
            )
        )
        add_nav_buttons(kb, back_data="home")
        return kb.as_markup()

    async def _send_screen_unlocked(
        message: Message,
        actor,
        text: str,
        *,
        reply_markup=None,
        bottom_menu: bool = False,
        recover_on_edit_failure: bool = False,
        force_new: bool = False,
    ) -> Message:
        user = await ensure_actor(actor)
        last_id = user.get("last_menu_message_id")
        admin_role = await get_admin_role(int(actor.id))
        home_markup = main_menu_inline_keyboard(admin_role, config.miniapp_url)
        if reply_markup is None:
            reply_markup = home_markup

        async def create_first_menu() -> Message:
            banner = current_main_menu_banner()
            try:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=banner,
                    caption=text,
                    reply_markup=home_markup,
                )
            except TelegramBadRequest as exc:
                logger.warning("Initial main-menu photo failed: %s", exc)

                # A Telegram file_id belongs to the bot that uploaded it and
                # can become unusable after token/bot changes. Only discard
                # the saved banner when Telegram specifically reports a media
                # or file-reference problem; caption/entity errors must not
                # wipe a valid custom banner.
                error_text = str(exc).lower()
                media_error = any(
                    marker in error_text
                    for marker in (
                        "image_process_failed",
                        "wrong file identifier",
                        "file reference",
                        "failed to get http url",
                        "photo_invalid",
                        "wrong type of the web page content",
                    )
                )
                if isinstance(banner, str) and media_error:
                    clear_main_menu_banner_file_id()
                    banner = main_menu_banner()

                try:
                    sent = await message.bot.send_photo(
                        chat_id=message.chat.id,
                        photo=banner,
                        caption=text,
                        reply_markup=home_markup,
                    )
                except TelegramBadRequest:
                    sent = await message.bot.send_photo(
                        chat_id=message.chat.id,
                        photo=banner,
                        caption=strip_custom_emoji(text),
                        reply_markup=home_markup,
                    )

            await db.set_last_menu_message(actor.id, sent.message_id)
            logger.info(
                "Persistent menu created for %s as message %s",
                actor.id,
                sent.message_id,
            )
            return sent

        if force_new:
            # Explicit /start should bring the menu back to the bottom of the
            # chat while keeping one tracked bot UI message. Telegram may
            # refuse deletion of very old messages; in that rare case we keep
            # and edit the existing menu instead of creating a duplicate.
            can_create_fresh = not last_id
            if last_id:
                try:
                    await message.bot.delete_message(
                        chat_id=message.chat.id,
                        message_id=int(last_id),
                    )
                    can_create_fresh = True
                except TelegramBadRequest as exc:
                    error_text = str(exc).lower()
                    if (
                        "message to delete not found" in error_text
                        or "message not found" in error_text
                    ):
                        can_create_fresh = True
                    else:
                        logger.warning(
                            "Could not remove previous menu %s for user %s: %s",
                            last_id,
                            actor.id,
                            exc,
                        )
                except Exception as exc:
                    logger.warning(
                        "Could not remove previous menu %s for user %s: %s",
                        last_id,
                        actor.id,
                        exc,
                    )

            if can_create_fresh:
                await db.set_last_menu_message(actor.id, None)
                return await create_first_menu()

            # Keep exactly one bot UI message if Telegram refuses deletion.
            force_new = False

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
                    reply_markup=home_markup,
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

                if recover_on_edit_failure:
                    logger.info(
                        "Recovering stale main menu for user %s after /start",
                        actor.id,
                    )
                    await db.set_last_menu_message(actor.id, None)
                    return await create_first_menu()

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
                        reply_markup=home_markup,
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
                if recover_on_edit_failure:
                    await db.set_last_menu_message(actor.id, None)
                    return await create_first_menu()
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

    ui_locks: dict[int, asyncio.Lock] = {}

    def get_ui_lock(user_id: int) -> asyncio.Lock:
        lock = ui_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            ui_locks[user_id] = lock
        return lock

    async def send_screen(
        message: Message,
        actor,
        text: str,
        *,
        reply_markup=None,
        bottom_menu: bool = False,
        recover_on_edit_failure: bool = False,
        force_new: bool = False,
    ) -> Message:
        # Serialize all UI mutations per user. This prevents double taps or
        # repeated /start commands from creating multiple bot menu messages.
        async with get_ui_lock(int(actor.id)):
            return await _send_screen_unlocked(
                message,
                actor,
                text,
                reply_markup=reply_markup,
                bottom_menu=bottom_menu,
                recover_on_edit_failure=recover_on_edit_failure,
                force_new=force_new,
            )

    async def show_home(
        message: Message,
        actor,
        *,
        recover_on_edit_failure: bool = False,
        force_new: bool = False,
    ) -> None:
        user = await ensure_actor(actor)
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
                f"└ Устройства: <b>до {int(user.get('max_devices') or 1)}</b>",
            ]
        elif not user.get("trial_used"):
            channel = html.escape(config.trial_channel_username)
            channel_member = await is_trial_channel_member(
                message.bot,
                int(actor.id),
                retries=1,
            )
            lines += [
                "└ Бесплатный день: <b>доступен</b>",
                "",
                "🎁 <b>Бесплатный день VPN</b>",
            ]
            if channel_member:
                lines += [
                    "✅ Подписка на канал подтверждена.",
                    "Откройте <b>«🔗 Подключить VPN»</b> и нажмите <b>«🎁 Забрать 1 день»</b>.",
                ]
            else:
                lines += [
                    f"Подпишитесь на канал <b>{channel}</b>.",
                    "Затем откройте <b>«🔗 Подключить VPN»</b> и нажмите <b>«🎁 Забрать 1 день»</b>.",
                ]
        else:
            lines += [
                "└ Бесплатный день: <b>уже использован</b>",
                "",
                "Выберите платную подписку кнопкой <b>«💳 Купить VPN»</b>.",
            ]

        if active and not getattr(provider, "service_ready", True):
            lines += ["", "<i>VPN-серверы ещё не подключены.</i>"]

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            bottom_menu=True,
            recover_on_edit_failure=recover_on_edit_failure,
            force_new=force_new,
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
        except Exception as exc:
            logger.warning(
                "VPN provisioning deferred for user %s: %s",
                telegram_id,
                exc,
            )
        return user


    async def apply_paid_purchase(
        buyer_telegram_id: int,
        target_telegram_id: int,
        code: str,
        payment_event_key: str,
    ) -> tuple[dict[str, Any], int]:
        user = await activate_paid_plan(target_telegram_id, code)
        return user, 0

    @router.message(Command("setbanner"))
    async def set_banner(message: Message) -> None:
        if not message.from_user or message.from_user.id not in config.admin_ids:
            return

        source_message = message.reply_to_message or message
        file_id = ""

        if source_message.photo:
            file_id = source_message.photo[-1].file_id
        else:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) == 2:
                file_id = parts[1].strip()

        if not file_id:
            await message.answer(
                "Пришли нужную картинку как фото с подписью /setbanner, "
                "ответь /setbanner на фото или передай Telegram file_id после команды."
            )
            return

        save_main_menu_banner_file_id(file_id)

        # Refresh the tracked menu immediately. Telegram keeps using its own
        # file_id afterwards, so the image is not re-uploaded on every screen.
        await show_home(message, message.from_user)

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject) -> None:
        user = await ensure_actor(message.from_user)
        if command.args and command.args.startswith("ref_"):
            raw = command.args.removeprefix("ref_")
            if raw.isdigit() and bool(user.get("_is_new")):
                await db.set_referrer_once(
                    message.from_user.id,
                    int(raw),
                )
        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id,
                action="typing",
            )
        except Exception:
            pass

        await show_home(
            message,
            message.from_user,
            recover_on_edit_failure=True,
            force_new=True,
        )

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
                subscribed = await is_trial_channel_member(
                    callback.message.bot,
                    callback.from_user.id,
                    retries=2,
                )
                text = (
                    "🎁 <b>Бесплатный день VPN</b>\n\n"
                    + (
                        "✅ Подписка на канал подтверждена.\n"
                        "Нажмите <b>«🎁 Забрать 1 день»</b>."
                        if subscribed
                        else (
                            f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                            "вернитесь в бот и нажмите <b>«🎁 Забрать 1 день»</b>."
                        )
                    )
                )
                await send_screen(
                    callback.message,
                    callback.from_user,
                    text,
                    reply_markup=trial_channel_keyboard(subscribed=subscribed),
                )
            else:
                kb = InlineKeyboardBuilder()
                kb.row(blue_inline_button("💳 Купить подписку", callback_data="plans"))
                add_nav_buttons(kb, back_data="home")
                await send_screen(
                    callback.message,
                    callback.from_user,
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Бесплатный день уже использован. Выберите подписку.",
                    reply_markup=kb.as_markup(),
                )
            return

        state, ok = await load_state(user, provider, config)
        subscription_url = await public_subscription_url(
            user,
            state,
            config,
            bot=callback.message.bot,
        )

        # For H1Cloud the public MGN /sub/<token> URL is stable and does not
        # depend on a successful live panel read. The proxy can provision,
        # retry and serve its short stale cache itself, so the bot must not
        # hide the copy/open buttons just because H1 timed out for this screen.
        if not subscription_url:
            if not getattr(provider, "service_ready", True):
                text = (
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Подписка активна, но VPN-серверы пока ещё не подключены."
                )
            else:
                text = (
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Не удалось сформировать персональную ссылку. "
                    "Попробуйте ещё раз через несколько секунд."
                )
            await send_screen(
                callback.message,
                callback.from_user,
                text,
                reply_markup=section_nav_keyboard(),
            )
            return

        if not ok:
            logger.warning(
                "Showing stable public subscription URL despite H1 state failure for user %s",
                user.get("telegram_id"),
            )

        await send_screen(
            callback.message,
            callback.from_user,
            connection_text(user, state, emoji, subscription_url),
            reply_markup=connection_keyboard(subscription_url),
        )

    @router.callback_query(F.data == "menu:info")
    async def menu_info(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("📢 Канал MGN VPN", url=config.trial_channel_url))
        add_nav_buttons(kb, back_data="home")
        e = emoji.icon(5, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Информация</b>\n\n"
            "🔐 Доступ выдаётся по персональной ссылке.\n"
            f"📱 В тариф входит <b>1 устройство</b>, можно докупить до <b>{MAX_DEVICES}</b>.\n"
            "🎁 Бесплатный день можно активировать один раз после подписки на наш Telegram-канал.\n"
            "⚙️ Управление подпиской и устройствами находится прямо в боте.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            e = emoji.icon(5, pack=PACK_NEWS)
            await send_screen(
                callback.message,
                callback.from_user,
                f"{e} <b>Поддержка</b>\n\n"
                "1. Активируйте бесплатный день или купите подписку.\n"
                "2. Нажмите <b>«🔗 Подключить VPN»</b>.\n"
                "3. Откройте персональную ссылку на нужном устройстве.\n\n"
                "Лимит устройств и дополнительные слоты находятся в разделе <b>«📱 Устройства»</b>.",
                reply_markup=section_nav_keyboard(),
            )

    @router.callback_query(F.data == "menu:promo")
    async def menu_promo(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        kb = InlineKeyboardBuilder()
        if config.miniapp_url:
            kb.row(
                InlineKeyboardButton(
                    text="🎟 Открыть промокоды",
                    web_app=WebAppInfo(url=config.miniapp_url),
                    style="primary",
                )
            )
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            callback.message,
            callback.from_user,
            "🎟 <b>Промокод</b>\n\n"
            "Активируйте бесплатные дни или примените скидку перед оплатой "
            "в Mini App. Итоговую цену всегда рассчитывает сервер.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:friends")
    async def menu_friends(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        bot_info = await callback.message.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
        stats = await db.referral_stats(callback.from_user.id)
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
            f"{e} <b>Пригласить друзей</b>\n\n"
            "Пригласите до 3 друзей и получите до 3 бесплатных дней VPN.\n\n"
            f"Приглашено: <b>{min(stats['invited'], 3)} / 3</b>\n"
            f"Получено: <b>+{stats['rewarded']} дней</b>\n"
            + (
                "До следующего бонуса: <b>1 друг</b>\n\n"
                if stats["rewarded"] < 3
                else "🎉 <b>Максимальный бонус получен</b>\n\n"
            )
            + f"Ваша ссылка:\n<code>{html.escape(link)}</code>",
            reply_markup=kb.as_markup(),
        )

    async def show_devices_panel(
        message: Message,
        actor,
        *,
        back_data: str = "home",
    ) -> None:
        user = await ensure_actor(actor)
        e = emoji.icon(3, pack=PACK_NEWS)

        if not is_active(user):
            await send_screen(
                message,
                actor,
                f"{e} <b>Устройства</b>\n\n"
                "Сначала активируйте VPN-подписку.\n"
                "В любой тариф входит <b>1 устройство</b>.",
                reply_markup=section_nav_keyboard(back_data=back_data),
            )
            return

        state, ok = await load_state(user, provider, config)
        limit = min(
            MAX_DEVICES,
            max(BASE_DEVICES, int(user.get("max_devices") or BASE_DEVICES)),
        )
        bonus = max(0, limit - BASE_DEVICES)

        lines = [
            f"{e} <b>Устройства</b>",
            "",
            f"Лимит — <b>{limit} из {MAX_DEVICES}</b>",
            f"├ В тарифе — <b>{BASE_DEVICES}</b>",
            f"└ Дополнительных слотов — <b>{bonus}</b>",
        ]

        if state.devices:
            lines += [
                "",
                f"Активных подключений по данным панели — <b>{len(state.devices)}</b>",
            ]

        lines += [
            "",
            f"+1 устройство — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>.",
            "Купленный слот сохраняется при продлении тарифа.",
        ]

        if not ok:
            lines += [
                "",
                "<i>Панель устройств временно отвечает медленно, "
                "но ваш лимит сохранён.</i>",
            ]

        kb = InlineKeyboardBuilder()
        if limit < MAX_DEVICES:
            kb.row(
                blue_inline_button(
                    f"➕ +1 устройство · {EXTRA_DEVICE_PRICE_RUB} ₽",
                    callback_data="device:buy",
                )
            )
        else:
            lines += ["", "<b>Достигнут максимум: 5 устройств.</b>"]

        add_nav_buttons(kb, back_data=back_data)
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "menu:devices")
    async def menu_devices(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message:
            await show_devices_panel(
                callback.message,
                callback.from_user,
                back_data="home",
            )

    @router.callback_query(F.data == "device:buy")
    async def device_buy(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await callback.answer(
                "Сначала активируйте подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await callback.answer(
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return

        await callback.answer()
        e = emoji.icon(4, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Дополнительное устройство</b>\n\n"
            f"+1 слот к текущему лимиту — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>.\n"
            f"Слот остаётся на аккаунте при продлении VPN.\n"
            f"Максимум — <b>{MAX_DEVICES}</b> устройств.\n\n"
            "Выберите способ оплаты.",
            reply_markup=device_payment_keyboard(),
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

    @router.message(F.text.in_({"💳 Купить VPN", "Купить VPN"}))
    async def plans_message(message: Message) -> None:
        await ensure_actor(message.from_user)
        e = emoji.icon(0, pack=PACK_NEWS)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Выберите подписку</b>\n\n"
            "В каждый тариф входит <b>1 устройство</b>.\n"
            f"Дополнительный слот — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>, максимум <b>{MAX_DEVICES}</b>.\n"
            "Оплата через СБП или Telegram Stars.",
            reply_markup=plans_keyboard(config),
        )

    @router.callback_query(F.data == "plans")
    async def plans_callback(callback: CallbackQuery) -> None:
        await callback.answer()
        if not callback.message:
            return
        e = emoji.icon(0, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>Выберите подписку</b>\n\n"
            "В каждый тариф входит <b>1 устройство</b>.\n"
            f"Дополнительный слот — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>, максимум <b>{MAX_DEVICES}</b>.\n"
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
        e = emoji.icon(1, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>{plan['name']}</b>\n\n"
            f"📱 Включено устройств — <b>1</b>\n"
            f"🏦 <b>{plan_price_rub(config, code)} ₽</b> · СБП\n"
            f"⭐ <b>{plan_price_stars(config, code)} Stars</b>\n"
            + (
                f"🔥 Выгода — <b>{plan_savings_rub(code)} ₽</b>\n"
                if plan_savings_rub(code)
                else ""
            )
            + "\n"
            + f"Дополнительное устройство — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>. "
            f"Максимум — <b>{MAX_DEVICES}</b>.",
            reply_markup=payment_methods_keyboard(config, code),
        )

    @router.callback_query(F.data.startswith("gift:"))
    async def start_gift(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        if code not in PLANS:
            await callback.answer("Тариф не найден", show_alert=True)
            return

        pending_gift_plans[callback.from_user.id] = code
        await callback.answer()
        await send_screen(
            callback.message,
            callback.from_user,
            "🎁 <b>Подписка другому человеку</b>\n\n"
            "Отправьте следующим сообщением <b>@username</b> получателя.\n\n"
            "<i>Получатель должен хотя бы один раз запустить @mgnvpn_bot.</i>",
            reply_markup=section_nav_keyboard(back_data=f"plan:{code}"),
        )

    @router.message(F.text.regexp(r"^@[A-Za-z0-9_]{3,32}$"))
    async def gift_username(message: Message) -> None:
        code = pending_gift_plans.get(message.from_user.id)
        if not code:
            return

        target = await db.get_user_by_username(message.text or "")
        if not target:
            await send_screen(
                message,
                message.from_user,
                "🎁 <b>Пользователь не найден</b>\n\n"
                "Попросите человека сначала запустить <b>@mgnvpn_bot</b>, "
                "после этого снова отправьте его @username.",
                reply_markup=section_nav_keyboard(back_data=f"plan:{code}"),
            )
            return

        target_id = int(target["telegram_id"])
        target_username = target.get("username")
        label = (
            f"@{html.escape(str(target_username))}"
            if target_username
            else f"<code>{target_id}</code>"
        )

        pending_gift_plans.pop(message.from_user.id, None)
        plan = PLANS[code]

        await send_screen(
            message,
            message.from_user,
            "🎁 <b>Подарочная подписка</b>\n\n"
            f"Получатель — <b>{label}</b>\n"
            f"Тариф — <b>{plan['name']}</b>\n"
            f"🏦 {plan_price_rub(config, code)} ₽\n"
            f"⭐ {plan_price_stars(config, code)} Stars\n\n"
            "Выберите способ оплаты.",
            reply_markup=payment_methods_keyboard(
                config,
                code,
                target_telegram_id=target_id,
            ),
        )

    async def sync_device_limit(user: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(
                provider.provision(user),
                timeout=15.0,
            )
        except Exception as exc:
            logger.warning(
                "Device limit provisioning deferred for user %s: %s",
                user.get("telegram_id"),
                str(exc).strip() or type(exc).__name__,
            )

    async def grant_paid_device_slot(
        telegram_id: int,
    ) -> dict[str, Any] | None:
        updated = await db.grant_extra_device(
            telegram_id=telegram_id,
            max_total_devices=MAX_DEVICES,
        )
        if updated:
            await sync_device_limit(updated)
        return updated

    async def begin_sbp_checkout(
        callback: CallbackQuery,
        code: str,
        target_telegram_id: int,
    ) -> None:
        if not callback.message:
            return
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

        try:
            target = await db.get_user(target_telegram_id)
        except KeyError:
            await callback.answer(
                "Получатель больше не найден в базе.",
                show_alert=True,
            )
            return

        order_id = f"vpn-{callback.from_user.id}-{uuid4().hex[:12]}"
        amount = plan_price_rub(config, code)
        target_label = (
            f"@{target['username']}"
            if target.get("username")
            else str(target_telegram_id)
        )

        try:
            payment = await create_payment(
                config,
                order_id=order_id,
                amount=Decimal(amount),
                description=(
                    f"MGN VPN {plan['name']}"
                    if target_telegram_id == callback.from_user.id
                    else f"MGN VPN {plan['name']} для {target_label}"
                ),
                user_id=callback.from_user.id,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await db.create_sbp_payment(
                payment_id=payment_id,
                order_id=order_id,
                telegram_id=callback.from_user.id,
                target_telegram_id=target_telegram_id,
                plan_code=code,
                amount_rub=amount,
            )
        except (RollyPayError, KeyError):
            await send_screen(
                callback.message,
                callback.from_user,
                "<b>Не удалось создать платёж.</b>\nПопробуйте ещё раз немного позже.",
                reply_markup=section_nav_keyboard(back_data=f"plan:{code}"),
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

        gift_line = (
            ""
            if target_telegram_id == callback.from_user.id
            else f"Получатель — <b>{html.escape(target_label)}</b>\n"
        )
        await send_screen(
            callback.message,
            callback.from_user,
            "🏦 <b>Оплата по СБП</b>\n\n"
            f"{gift_line}"
            f"Тариф — <b>{plan['name']}</b>\n"
            f"Сумма — <b>{amount} ₽</b>\n\n"
            "Оплатите счёт и нажмите «Проверить оплату».",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.startswith("sbp:"))
    async def buy_sbp(callback: CallbackQuery) -> None:
        code = callback.data.split(":", 1)[1]
        await begin_sbp_checkout(
            callback,
            code,
            callback.from_user.id,
        )

    @router.callback_query(F.data.startswith("sbpgift:"))
    async def buy_sbp_gift(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.answer("Некорректный получатель", show_alert=True)
            return
        await begin_sbp_checkout(callback, parts[1], int(parts[2]))

    @router.callback_query(F.data == "device:sbp")
    async def buy_device_sbp(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await callback.answer(
                "Сначала активируйте VPN-подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await callback.answer(
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return
        if not config.rollypay_enabled:
            await callback.answer(
                "СБП пока не настроена.",
                show_alert=True,
            )
            return

        await callback.answer()
        order_id = f"device-{callback.from_user.id}-{uuid4().hex[:12]}"
        try:
            payment = await create_payment(
                config,
                order_id=order_id,
                amount=Decimal(EXTRA_DEVICE_PRICE_RUB),
                description="MGN VPN · +1 устройство",
                user_id=callback.from_user.id,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await db.create_sbp_payment(
                payment_id=payment_id,
                order_id=order_id,
                telegram_id=callback.from_user.id,
                target_telegram_id=callback.from_user.id,
                plan_code=DEVICE_PRODUCT_CODE,
                amount_rub=EXTRA_DEVICE_PRICE_RUB,
            )
        except (RollyPayError, KeyError):
            await callback.answer(
                "Не удалось создать платёж.",
                show_alert=True,
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🏦 Оплатить 100 ₽", url=pay_url))
        kb.row(
            blue_inline_button(
                "✅ Проверить оплату",
                callback_data=f"checksbp:{payment_id}",
            )
        )
        add_nav_buttons(kb, back_data="menu:devices")
        await send_screen(
            callback.message,
            callback.from_user,
            "🏦 <b>+1 устройство</b>\n\n"
            f"Стоимость — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>.\n"
            "После подтверждения оплаты лимит увеличится автоматически.",
            reply_markup=kb.as_markup(),
        )

    async def begin_stars_checkout(
        callback: CallbackQuery,
        code: str,
        target_telegram_id: int,
    ) -> None:
        if not callback.message:
            return

        plan = PLANS.get(code)
        if not plan:
            await callback.answer("Тариф не найден", show_alert=True)
            return

        try:
            target = await db.get_user(target_telegram_id)
        except KeyError:
            await callback.answer(
                "Получатель не найден в базе.",
                show_alert=True,
            )
            return

        stars = plan_price_stars(config, code)
        payload = (
            f"xtr|{code}|{callback.from_user.id}|"
            f"{target_telegram_id}|{uuid4().hex[:12]}"
        )
        target_label = (
            f"@{target['username']}"
            if target.get("username")
            else str(target_telegram_id)
        )

        try:
            invoice_url = await callback.message.bot.create_invoice_link(
                title=f"MGN VPN · {plan['name']}",
                description=(
                    f"Подписка MGN VPN: {plan['name']}"
                    if target_telegram_id == callback.from_user.id
                    else f"Подписка MGN VPN для {target_label}: {plan['name']}"
                ),
                payload=payload,
                currency="XTR",
                prices=[
                    LabeledPrice(
                        label=f"MGN VPN · {plan['name']}",
                        amount=stars,
                    )
                ],
            )
        except Exception as exc:
            logger.exception("Stars invoice creation failed: %s", exc)
            await callback.answer(
                "Не удалось создать оплату Stars.",
                show_alert=True,
            )
            return

        await callback.answer()
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                f"⭐ Оплатить {stars} Stars",
                url=invoice_url,
            )
        )
        add_nav_buttons(kb, back_data=f"plan:{code}")

        gift_line = (
            ""
            if target_telegram_id == callback.from_user.id
            else f"Получатель — <b>{html.escape(target_label)}</b>\n"
        )
        await send_screen(
            callback.message,
            callback.from_user,
            "⭐ <b>Оплата Telegram Stars</b>\n\n"
            f"{gift_line}"
            f"Тариф — <b>{plan['name']}</b>\n"
            f"Стоимость — <b>{stars} ⭐</b>\n"
            f"Эквивалент тарифа — <b>{plan_price_rub(config, code)} ₽</b>\n\n"
            "Нажмите кнопку ниже и подтвердите оплату в Telegram.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.startswith("stars:"))
    async def buy_stars(callback: CallbackQuery) -> None:
        code = callback.data.split(":", 1)[1]
        await begin_stars_checkout(
            callback,
            code,
            callback.from_user.id,
        )

    @router.callback_query(F.data.startswith("starsgift:"))
    async def buy_stars_gift(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3 or not parts[2].isdigit():
            await callback.answer("Некорректный получатель", show_alert=True)
            return
        await begin_stars_checkout(callback, parts[1], int(parts[2]))

    @router.callback_query(F.data == "device:stars")
    async def buy_device_stars(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await callback.answer(
                "Сначала активируйте VPN-подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await callback.answer(
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return

        stars = extra_device_price_stars()
        payload = (
            f"xtr|{DEVICE_PRODUCT_CODE}|{callback.from_user.id}|"
            f"{callback.from_user.id}|{uuid4().hex[:12]}"
        )
        try:
            invoice_url = await callback.message.bot.create_invoice_link(
                title="MGN VPN · +1 устройство",
                description="Постоянный дополнительный слот устройства",
                payload=payload,
                currency="XTR",
                prices=[
                    LabeledPrice(
                        label="MGN VPN · +1 устройство",
                        amount=stars,
                    )
                ],
            )
        except Exception as exc:
            logger.exception("Device Stars invoice failed: %s", exc)
            await callback.answer(
                "Не удалось создать оплату Stars.",
                show_alert=True,
            )
            return

        await callback.answer()
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                f"⭐ Оплатить {stars} Stars",
                url=invoice_url,
            )
        )
        add_nav_buttons(kb, back_data="menu:devices")
        await send_screen(
            callback.message,
            callback.from_user,
            "⭐ <b>+1 устройство</b>\n\n"
            f"Стоимость — <b>{stars} Stars</b> "
            f"(эквивалент {EXTRA_DEVICE_PRICE_RUB} ₽).\n"
            "После оплаты лимит увеличится автоматически.",
            reply_markup=kb.as_markup(),
        )

    @router.pre_checkout_query()
    async def pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
        payload = pre_checkout_query.invoice_payload or ""
        parts = payload.split("|")
        if len(parts) == 2 and parts[0] == "xtr2":
            intent = await db.get_payment_intent(parts[1])
            valid = bool(
                intent
                and intent["status"] == "created"
                and int(intent["buyer_telegram_id"]) == pre_checkout_query.from_user.id
                and intent["currency"] == "XTR"
                and pre_checkout_query.currency == "XTR"
                and int(intent["currency_amount"]) == pre_checkout_query.total_amount
            )
            await pre_checkout_query.answer(
                ok=valid,
                error_message=None if valid else "Параметры оплаты изменились. Откройте тариф заново.",
            )
            return
        if len(parts) != 5 or parts[0] != "xtr":
            await pre_checkout_query.answer(
                ok=False,
                error_message="Некорректный платёж.",
            )
            return

        _, code, buyer_raw, target_raw, _nonce = parts
        if (
            not buyer_raw.isdigit()
            or not target_raw.isdigit()
            or int(buyer_raw) != pre_checkout_query.from_user.id
            or pre_checkout_query.currency != "XTR"
        ):
            await pre_checkout_query.answer(
                ok=False,
                error_message="Параметры оплаты изменились.",
            )
            return

        buyer_id = int(buyer_raw)
        target_id = int(target_raw)

        if code == DEVICE_PRODUCT_CODE:
            if (
                target_id != buyer_id
                or pre_checkout_query.total_amount != extra_device_price_stars()
            ):
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="Параметры покупки устройства изменились.",
                )
                return
            try:
                user = await db.get_user(buyer_id)
            except KeyError:
                user = None
            if not user or not is_active(user):
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="Сначала активируйте VPN-подписку.",
                )
                return
            if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="У вас уже максимум устройств.",
                )
                return
        else:
            if (
                code not in PLANS
                or pre_checkout_query.total_amount
                != plan_price_stars(config, code)
            ):
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="Параметры оплаты изменились. Откройте тариф заново.",
                )
                return
            try:
                await db.get_user(target_id)
            except KeyError:
                await pre_checkout_query.answer(
                    ok=False,
                    error_message="Получатель не найден.",
                )
                return

        await pre_checkout_query.answer(ok=True)

    @router.message(F.successful_payment)
    async def stars_success(message: Message) -> None:
        payment = message.successful_payment
        if not payment or payment.currency != "XTR":
            return

        parts = (payment.invoice_payload or "").split("|")
        if len(parts) == 2 and parts[0] == "xtr2":
            intent = await db.get_payment_intent(parts[1])
            if not intent or (
                int(intent["buyer_telegram_id"]) != message.from_user.id
                or intent["currency"] != "XTR"
                or int(intent["currency_amount"]) != int(payment.total_amount)
            ):
                logger.error("Rejected Stars payment intent %s", parts[1])
                return
            charge_id = payment.telegram_payment_charge_id
            fresh_charge = await db.record_star_payment(
                telegram_payment_charge_id=charge_id,
                buyer_telegram_id=message.from_user.id,
                target_telegram_id=int(intent["target_telegram_id"]),
                plan_code=str(intent["product_code"]),
                stars=int(payment.total_amount),
            )
            fresh_intent = await db.mark_payment_intent_paid(parts[1])
            if fresh_charge and fresh_intent:
                if intent.get("promo_id"):
                    await db.consume_promo(
                        promo_id=int(intent["promo_id"]),
                        telegram_id=message.from_user.id,
                        payment_id=charge_id,
                    )
                await apply_paid_purchase(
                    message.from_user.id,
                    int(intent["target_telegram_id"]),
                    str(intent["product_code"]),
                    f"stars:{charge_id}",
                )
            await show_home(message, message.from_user, force_new=True)
            return
        if len(parts) != 5 or parts[0] != "xtr":
            return

        _, code, buyer_raw, target_raw, _nonce = parts
        if (
            not buyer_raw.isdigit()
            or not target_raw.isdigit()
            or int(buyer_raw) != message.from_user.id
        ):
            logger.error(
                "Rejected malformed Stars success payload: %s",
                payment.invoice_payload,
            )
            return

        buyer_id = int(buyer_raw)
        target_id = int(target_raw)

        if code == DEVICE_PRODUCT_CODE:
            if (
                target_id != buyer_id
                or payment.total_amount != extra_device_price_stars()
            ):
                logger.error(
                    "Rejected malformed device Stars payment: %s",
                    payment.invoice_payload,
                )
                return

            charge_id = payment.telegram_payment_charge_id
            fresh = await db.record_star_payment(
                telegram_payment_charge_id=charge_id,
                buyer_telegram_id=buyer_id,
                target_telegram_id=buyer_id,
                plan_code=DEVICE_PRODUCT_CODE,
                stars=int(payment.total_amount),
            )
            if fresh:
                updated = await grant_paid_device_slot(buyer_id)
                if updated is None:
                    await send_screen(
                        message,
                        message.from_user,
                        "⚠️ <b>Оплата получена</b>\n\n"
                        "Слот не удалось добавить автоматически. "
                        "Обратитесь в поддержку — платёж сохранён.",
                        reply_markup=section_nav_keyboard(back_data="home"),
                    )
                    return

            await show_devices_panel(
                message,
                message.from_user,
                back_data="home",
            )
            return

        if (
            code not in PLANS
            or payment.total_amount != plan_price_stars(config, code)
        ):
            logger.error(
                "Rejected malformed Stars success payload: %s",
                payment.invoice_payload,
            )
            return

        charge_id = payment.telegram_payment_charge_id
        fresh = await db.record_star_payment(
            telegram_payment_charge_id=charge_id,
            buyer_telegram_id=buyer_id,
            target_telegram_id=target_id,
            plan_code=code,
            stars=int(payment.total_amount),
        )

        reward_amount = 0
        if fresh:
            _target_user, reward_amount = await apply_paid_purchase(
                buyer_telegram_id=buyer_id,
                target_telegram_id=target_id,
                code=code,
                payment_event_key=f"stars:{charge_id}",
            )

        if target_id == buyer_id:
            await show_profile(message, message.from_user)
            return

        try:
            target = await db.get_user(target_id)
            target_label = (
                f"@{target['username']}"
                if target.get("username")
                else str(target_id)
            )
        except KeyError:
            target_label = str(target_id)

        await send_screen(
            message,
            message.from_user,
            "✅ <b>Подарок активирован</b>\n\n"
            f"Получатель — <b>{html.escape(target_label)}</b>\n"
            f"Тариф — <b>{PLANS[code]['name']}</b>\n"
            f"Оплачено — <b>{payment.total_amount} ⭐</b>",
            reply_markup=section_nav_keyboard(back_data="home"),
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
            code = str(local["plan_code"])
            target_id = int(
                local.get("target_telegram_id")
                or callback.from_user.id
            )

            if code == DEVICE_PRODUCT_CODE:
                if fresh:
                    updated = await grant_paid_device_slot(
                        callback.from_user.id
                    )
                    if updated is None:
                        await callback.answer(
                            "Оплата получена, но слот не добавлен. Напишите в поддержку.",
                            show_alert=True,
                        )
                        return
                await callback.answer("Оплата получена · +1 устройство")
                await show_devices_panel(
                    callback.message,
                    callback.from_user,
                    back_data="home",
                )
                return

            reward_amount = 0
            if fresh:
                _target_user, reward_amount = await apply_paid_purchase(
                    buyer_telegram_id=callback.from_user.id,
                    target_telegram_id=target_id,
                    code=code,
                    payment_event_key=f"sbp:{payment_id}",
                )

            await callback.answer("Оплата получена")

            if target_id == callback.from_user.id:
                await show_profile(callback.message, callback.from_user)
                return

            try:
                target = await db.get_user(target_id)
                target_label = (
                    f"@{target['username']}"
                    if target.get("username")
                    else str(target_id)
                )
            except KeyError:
                target_label = str(target_id)

            await send_screen(
                callback.message,
                callback.from_user,
                "✅ <b>Подарок активирован</b>\n\n"
                f"Получатель — <b>{html.escape(target_label)}</b>\n"
                f"Тариф — <b>{PLANS[code]['name']}</b>\n"
                f"Оплачено — <b>{int(local['amount_rub'])} ₽</b>",
                reply_markup=section_nav_keyboard(back_data="home"),
            )
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
                subscribed = await is_trial_channel_member(
                    message.bot,
                    message.from_user.id,
                    retries=2,
                )
                text = (
                    "🎁 <b>Бесплатный день VPN</b>\n\n"
                    + (
                        "✅ Подписка на канал подтверждена.\n"
                        "Нажмите <b>«🎁 Забрать 1 день»</b>."
                        if subscribed
                        else (
                            f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                            "вернитесь в бот и нажмите <b>«🎁 Забрать 1 день»</b>."
                        )
                    )
                )
                await send_screen(
                    message,
                    message.from_user,
                    text,
                    reply_markup=trial_channel_keyboard(subscribed=subscribed),
                )
            else:
                kb = InlineKeyboardBuilder()
                kb.row(
                    blue_inline_button(
                        "💳 Купить подписку",
                        callback_data="plans",
                    )
                )
                add_nav_buttons(kb, back_data="home")
                await send_screen(
                    message,
                    message.from_user,
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Бесплатный день уже использован. Выберите подписку.",
                    reply_markup=kb.as_markup(),
                )
            return

        state, ok = await load_state(user, provider, config)
        subscription_url = await public_subscription_url(
            user,
            state,
            config,
            bot=message.bot,
        )

        if not subscription_url:
            if not getattr(provider, "service_ready", True):
                text = (
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Подписка активна, но VPN-серверы пока ещё не подключены."
                )
            else:
                text = (
                    "🔗 <b>Подключение VPN</b>\n\n"
                    "Не удалось сформировать персональную ссылку. "
                    "Попробуйте ещё раз через несколько секунд."
                )
            await send_screen(
                message,
                message.from_user,
                text,
                reply_markup=section_nav_keyboard(),
            )
            return

        if not ok:
            logger.warning(
                "Showing stable public subscription URL despite H1 state failure for user %s",
                user.get("telegram_id"),
            )

        await send_screen(
            message,
            message.from_user,
            connection_text(user, state, emoji),
            reply_markup=connection_keyboard(subscription_url),
        )

    @router.callback_query(F.data.in_({"trial", "trialcheck", "trial:claim"}))
    async def trial(callback: CallbackQuery) -> None:
        if not callback.message:
            return

        user = await ensure_actor(callback.from_user)
        if user.get("trial_used"):
            await callback.answer(
                "Бесплатный день уже использован.",
                show_alert=True,
            )
            return
        subscribed = await is_trial_channel_member(
            callback.message.bot,
            callback.from_user.id,
        )
        if not subscribed:
            await callback.answer(
                "Подписка пока не найдена. Если только что подписались — нажмите ещё раз через секунду.",
                show_alert=True,
            )
            await send_screen(
                callback.message,
                callback.from_user,
                "🎁 <b>Бесплатный день VPN</b>\n\n"
                f"Подпишитесь на <b>{html.escape(config.trial_channel_username)}</b>, "
                "вернитесь сюда и нажмите <b>«🎁 Забрать 1 день»</b>.",
                reply_markup=trial_channel_keyboard(subscribed=False),
            )
            return

        activated = await db.activate_trial(
            callback.from_user.id,
            config.trial_minutes,
            config.trial_max_devices,
        )
        if not activated:
            await callback.answer(
                "Бесплатный день уже использован.",
                show_alert=True,
            )
            return

        user = await db.get_user(callback.from_user.id)

        try:
            await provider.provision(user)
        except Exception as exc:
            logger.warning(
                "Trial VPN provisioning deferred for user %s: %s",
                callback.from_user.id,
                exc,
            )

        await callback.answer("Бесплатный день активирован")
        await show_home(callback.message, callback.from_user)

    @router.message(F.text.in_({"📱 Устройства", "Устройства"}))
    async def devices(message: Message) -> None:
        await show_devices_panel(
            message,
            message.from_user,
            back_data="home",
        )

    @router.message(F.text.in_({"👥 Друзья", "👥 Пригласить друга", "Пригласить друга", "Друзья"}))
    async def invite(message: Message) -> None:
        await ensure_actor(message.from_user)
        bot_info = await message.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=ref_{message.from_user.id}"
        stats = await db.referral_stats(message.from_user.id)
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
            f"{e} <b>Пригласить друзей</b>\n\n"
            "За каждого нового друга, который подпишется на канал и активирует бесплатный день, вы получите +1 день VPN.\n\n"
            f"Приглашено: <b>{min(stats['invited'], 3)} / 3</b>\n"
            f"Получено: <b>+{stats['rewarded']} дней</b>\n\n"
            f"Ваша ссылка:\n<code>{html.escape(link)}</code>",
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

        e = emoji.icon(5, pack=PACK_NEWS)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Информация</b>\n\n"
            "🔐 Доступ выдаётся по персональной ссылке.\n"
            f"📱 В тариф входит <b>1 устройство</b>; дополнительные — по <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>, максимум <b>{MAX_DEVICES}</b>.\n"
            "🎁 Бесплатный день можно активировать один раз после подписки на наш Telegram-канал.\n"
            "⚙️ Управление подпиской и устройствами находится прямо в боте.",
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"🆘 Поддержка", "🆘 Помощь", "Поддержка", "Помощь"}))
    async def help_screen(message: Message) -> None:
        e = emoji.icon(6, pack=PACK_NEWS)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Поддержка</b>\n\n"
            "1. Активируйте бесплатный день или купите подписку.\n"
            "2. Нажмите <b>«🔗 Подключить VPN»</b>.\n"
            "3. Скопируйте ссылку одной кнопкой или сразу откройте её.\n\n"
            "Дополнительные слоты находятся в разделе <b>«📱 Устройства»</b>.",
            reply_markup=section_nav_keyboard(),
        )

    def admin_role_label(role: str | None) -> str:
        return {
            "owner": "Владелец",
            "full": "Полная",
            "limited": "Ограниченная",
        }.get(role or "", "Нет")

    def format_joined(value: str | None) -> str:
        dt = from_iso(value)
        if not dt:
            return "—"
        return dt.astimezone(config.display_tz).strftime("%d.%m.%Y %H:%M")

    def admin_main_keyboard(role: str) -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("📊 Сводка", callback_data="admin:stats"),
            blue_inline_button("👥 Пользователи", callback_data="admin:users"),
        )
        kb.row(
            blue_inline_button("💳 Платежи", callback_data="admin:payments"),
        )
        if role in {"owner", "full"}:
            kb.row(
                blue_inline_button("🎟 Промокоды", callback_data="admin:bonuses"),
                blue_inline_button("⚙️ Система", callback_data="admin:system"),
            )
        if role == "owner":
            kb.row(
                blue_inline_button("🛡 Администраторы", callback_data="admin:admins"),
            )
        kb.row(
            blue_inline_button("🏠 Главное меню", callback_data="home"),
        )
        return kb.as_markup()

    async def show_admin(message: Message, actor) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return

        stats = await db.admin_overview()
        recent = await db.recent_users(5)
        pay_status = "работает" if config.rollypay_enabled else "не настроена"
        vpn_status = (
            "готов"
            if getattr(provider, "service_ready", True)
            else "ожидает серверы"
        )

        lines = [
            "🛡 <b>Админ-панель MGN VPN</b>",
            f"Доступ: <b>{admin_role_label(role)}</b>",
            "",
            "📊 <b>Сводка</b>",
            f"├ Всего пользователей: <b>{stats['total']}</b>",
            f"├ Активных подписок: <b>{stats['active']}</b>",
            f"├ Платных активных: <b>{stats['active_paid']}</b>",
            f"├ Новых за 24 часа: <b>+{stats['new_24h']}</b>",
            f"├ Новых за 7 дней: <b>+{stats['new_7d']}</b>",
            f"└ Новых за 30 дней: <b>+{stats['new_30d']}</b>",
            "",
            "💰 <b>Оплаты</b>",
            f"├ СБП: <b>{pay_status}</b> · {stats['sbp_revenue']} ₽",
            f"└ Stars: <b>{stats['star_revenue']} ⭐</b>",
            "",
            f"🌐 VPN: <b>{vpn_status}</b>",
            "",
            "🆕 <b>Последние пользователи</b>",
        ]

        if recent:
            for item in recent:
                uid = int(item["telegram_id"])
                name = (
                    f'@{item["username"]}'
                    if item.get("username")
                    else item.get("first_name") or str(uid)
                )
                lines.append(
                    f"• {html.escape(str(name))} · "
                    f"<code>{uid}</code> · {format_joined(item.get('created_at'))}"
                )
        else:
            lines.append("Пока никого.")

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=admin_main_keyboard(role),
        )

    async def show_admin_stats(message: Message, actor) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return
        stats = await db.admin_overview()
        recent = await db.recent_users(10)

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:stats"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        lines = [
            "📊 <b>Полная сводка</b>",
            "",
            "👥 <b>Пользователи</b>",
            f"├ Всего — <b>{stats['total']}</b>",
            f"├ Активные — <b>{stats['active']}</b>",
            f"├ Платные активные — <b>{stats['active_paid']}</b>",
            f"├ За 24 часа — <b>+{stats['new_24h']}</b>",
            f"├ За 7 дней — <b>+{stats['new_7d']}</b>",
            f"├ За 30 дней — <b>+{stats['new_30d']}</b>",
            f"└ Использовали пробник — <b>{stats['trials']}</b>",
            "",
            "💰 <b>Оплаты</b>",
            f"├ СБП оплат — <b>{stats['sbp_paid']}</b>",
            f"├ СБП оборот — <b>{stats['sbp_revenue']} ₽</b>",
            f"├ Stars оплат — <b>{stats['star_paid']}</b>",
            f"└ Stars получено — <b>{stats['star_revenue']} ⭐</b>",
            "",
            "🕒 <b>Последние регистрации</b>",
        ]

        for item in recent:
            uid = int(item["telegram_id"])
            name = (
                f'@{item["username"]}'
                if item.get("username")
                else item.get("first_name") or str(uid)
            )
            lines.append(
                f"• {format_joined(item.get('created_at'))} — "
                f"{html.escape(str(name))} · <code>{uid}</code>"
            )

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_users(message: Message, actor) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return
        users = await db.recent_users(20)
        kb = InlineKeyboardBuilder()
        lines = [
            "👥 <b>Пользователи</b>",
            "",
            "Последние 20 регистраций:",
            "",
        ]

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
                lines.append(
                    f"{mark} {html.escape(str(username))} · "
                    f"<code>{uid}</code>\n"
                    f"   └ пришёл {format_joined(item.get('created_at'))}"
                )
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
        actor_role = await get_admin_role(actor.id)
        if not actor_role:
            return

        try:
            user = await db.get_user(telegram_id)
        except KeyError:
            await send_screen(
                message,
                actor,
                "👤 <b>Пользователь не найден</b>",
                reply_markup=admin_main_keyboard(actor_role),
            )
            return

        target_role = await get_admin_role(telegram_id)
        username = (
            f'@{html.escape(user["username"])}'
            if user.get("username")
            else html.escape(user.get("first_name") or "Без имени")
        )
        active = is_active(user)
        referrals = await db.referral_count(telegram_id)

        kb = InlineKeyboardBuilder()

        if actor_role in {"owner", "full"}:
            kb.row(
                blue_inline_button("+7 дней", callback_data=f"admin:grant:{telegram_id}:7"),
                blue_inline_button("+30 дней", callback_data=f"admin:grant:{telegram_id}:30"),
            )
            kb.row(
                blue_inline_button("+90 дней", callback_data=f"admin:grant:{telegram_id}:90"),
                blue_inline_button("+365 дней", callback_data=f"admin:grant:{telegram_id}:365"),
            )
            kb.row(
                blue_inline_button(
                    "📱 +1 слот",
                    callback_data=f"admin:device:{telegram_id}:add",
                ),
                blue_inline_button(
                    "📱 −1 слот",
                    callback_data=f"admin:device:{telegram_id}:remove",
                ),
            )

        if actor_role == "owner" and telegram_id not in config.admin_ids:
            kb.row(
                blue_inline_button(
                    "🛡 Полная админка",
                    callback_data=f"admin:role:{telegram_id}:full",
                ),
                blue_inline_button(
                    "👁 Ограниченная",
                    callback_data=f"admin:role:{telegram_id}:limited",
                ),
            )
            if target_role:
                kb.row(
                    blue_inline_button(
                        "❌ Забрать админку",
                        callback_data=f"admin:role:{telegram_id}:remove",
                    )
                )

        kb.row(blue_inline_button("⬅️ Пользователи", callback_data="admin:users"))
        kb.row(blue_inline_button("🏠 Админка", callback_data="admin:home"))

        lines = [
            f"👤 <b>{username}</b>",
            f"<code>{telegram_id}</code>",
            "",
            f"Пришёл — <b>{format_joined(user.get('created_at'))}</b>",
            f"Админ-доступ — <b>{admin_role_label(target_role)}</b>",
            f"Подписка — <b>{'активна' if active else 'не активна'}</b>",
            f"Тариф — <b>{html.escape(user.get('plan_name') or '—')}</b>",
            f"До — <b>{format_until(user, config) if active else '—'}</b>",
            f"Устройств — <b>{int(user.get('max_devices') or BASE_DEVICES)}/{MAX_DEVICES}</b>",
            f"Доп. слотов — <b>{max(0, int(user.get('bonus_devices') or 0))}</b>",
            f"Бесплатный день — <b>{'использован' if user.get('trial_used') else 'доступен'}</b>",
            f"Приглашено — <b>{referrals}</b>",
        ]
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_payments(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
            return
        payments = await db.recent_sbp_payments(10)
        overview = await db.admin_overview()
        kb = InlineKeyboardBuilder()
        lines = [
            "💳 <b>Последние платежи</b>",
            f"⭐ Всего оплат Stars: <b>{overview['star_paid']}</b>",
            f"⭐ Получено Stars: <b>{overview['star_revenue']} ⭐</b>",
            "",
            "🏦 <b>СБП</b>",
            "",
        ]

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
                    f"<code>{int(item['telegram_id'])}</code> · "
                    f"тариф {html.escape(str(item['plan_code']))}",
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
        if not await has_full_admin_access(actor.id):
            return
        stock = await db.list_service_promos()
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:bonuses"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        lines = [
            "🎟 <b>Промокоды</b>",
            "",
            "Команды:",
            "<code>/promocreate CODE TYPE VALUE MAX_USES DAYS_VALID PLANS</code>",
        ]

        if not stock:
            lines.append("Промокодов пока нет.")
        else:
            for item in stock:
                lines.append(
                    f"• <code>{html.escape(str(item['code']))}</code> · "
                    f"{html.escape(str(item['type']))} {int(item['value'])} · "
                    f"использовано {int(item['used_count'] or 0)}"
                )

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_system(message: Message, actor) -> None:
        if not await has_full_admin_access(actor.id):
            return
        rolly = "✅ настроена" if config.rollypay_enabled else "❌ не настроена"
        rolly_mode = "тест" if config.rollypay_test_mode else "боевой"
        vpn_ready = "✅" if getattr(provider, "service_ready", True) else "⚠️"

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:system"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        text = (
            "⚙️ <b>Система</b>\n\n"
            f"{vpn_ready} VPN режим — <b>{html.escape(config.vpn_mode)}</b>\n"
            f"🔗 Реальные подключения — <b>{'готовы' if getattr(provider, 'service_ready', True) else 'ожидают серверы'}</b>\n"
            f"🌐 Сервер — <b>{html.escape(config.vpn_server_name)}</b>\n"
            f"💳 RollyPay — <b>{rolly}</b>\n"
            f"🧾 Режим оплаты — <b>{rolly_mode}</b>\n"
            "🎁 Бесплатный доступ — <b>1 день, один раз</b>\n"
            f"📱 Лимит бесплатного доступа — <b>{config.trial_max_devices} устройство</b>\n\n"
            "<i>Секретные ключи здесь не отображаются.</i>"
        )
        await send_screen(message, actor, text, reply_markup=kb.as_markup())

    async def show_admin_admins(message: Message, actor) -> None:
        if not is_owner(actor.id):
            return

        dynamic_admins = await db.list_admin_roles()
        kb = InlineKeyboardBuilder()
        lines = [
            "🛡 <b>Администраторы</b>",
            "",
            "👑 <b>Владельцы</b>",
        ]

        for owner_id in config.admin_ids:
            try:
                user = await db.get_user(owner_id)
                name = (
                    f'@{user["username"]}'
                    if user.get("username")
                    else user.get("first_name") or str(owner_id)
                )
            except KeyError:
                name = str(owner_id)
            lines.append(f"• {html.escape(str(name))} · <code>{owner_id}</code>")

        lines += ["", "🛡 <b>Выданные админки</b>"]
        if not dynamic_admins:
            lines.append("Пока нет.")
        else:
            for item in dynamic_admins:
                uid = int(item["telegram_id"])
                name = (
                    f'@{item["username"]}'
                    if item.get("username")
                    else item.get("first_name") or str(uid)
                )
                role = str(item["role"])
                icon = "🛡" if role == "full" else "👁"
                lines.append(
                    f"{icon} {html.escape(str(name))} · <code>{uid}</code> — "
                    f"<b>{admin_role_label(role)}</b>"
                )
                kb.row(
                    blue_inline_button(
                        f"👤 {str(name)[:28]}",
                        callback_data=f"admin:user:{uid}",
                    )
                )

        lines += [
            "",
            "<i>Выдать доступ можно из карточки пользователя: "
            "Пользователи → выбрать человека.</i>",
            "",
            "Полная — управление подписками, бонусами и системой.",
            "Ограниченная — просмотр сводки, пользователей и платежей.",
        ]

        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:admins"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.message(Command("admin"))
    async def admin_panel(message: Message) -> None:
        if not await has_admin_access(message.from_user.id):
            return
        await show_admin(message, message.from_user)

    @router.callback_query(F.data == "admin:home")
    async def admin_home(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:stats")
    async def admin_stats_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_stats(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:users")
    async def admin_users_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_users(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:payments")
    async def admin_payments_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await callback.answer("Нет доступа", show_alert=True)
            return
        await callback.answer()
        if callback.message:
            await show_admin_payments(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:bonuses")
    async def admin_bonuses_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await callback.answer(
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        await callback.answer()
        if callback.message:
            await show_admin_bonuses(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:system")
    async def admin_system_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await callback.answer(
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        await callback.answer()
        if callback.message:
            await show_admin_system(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:admins")
    async def admin_admins_callback(callback: CallbackQuery) -> None:
        if not is_owner(callback.from_user.id):
            await callback.answer(
                "Управление администраторами доступно только владельцу.",
                show_alert=True,
            )
            return
        await callback.answer()
        if callback.message:
            await show_admin_admins(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("admin:user:"))
    async def admin_user_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
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

    @router.callback_query(F.data.startswith("admin:role:"))
    async def admin_role_callback(callback: CallbackQuery) -> None:
        if not is_owner(callback.from_user.id):
            await callback.answer(
                "Выдавать админки может только владелец.",
                show_alert=True,
            )
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            await callback.answer("Некорректные данные", show_alert=True)
            return

        telegram_id = int(parts[2])
        role = parts[3]
        if telegram_id in config.admin_ids:
            await callback.answer(
                "Доступ владельца нельзя изменить из панели.",
                show_alert=True,
            )
            return

        try:
            await db.get_user(telegram_id)
        except KeyError:
            await callback.answer(
                "Пользователь ещё не запускал бота.",
                show_alert=True,
            )
            return

        if role == "remove":
            await db.remove_admin_role(telegram_id)
            await callback.answer("Админка забрана")
        elif role in {"full", "limited"}:
            await db.set_admin_role(
                telegram_id=telegram_id,
                role=role,
                granted_by=callback.from_user.id,
            )
            await callback.answer(
                "Выдана полная админка"
                if role == "full"
                else "Выдана ограниченная админка"
            )
        else:
            await callback.answer("Неизвестная роль", show_alert=True)
            return

        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.callback_query(F.data.startswith("admin:device:"))
    async def admin_device_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await callback.answer(
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            await callback.answer("Некорректная команда", show_alert=True)
            return

        telegram_id = int(parts[2])
        action = parts[3]
        try:
            current = await db.get_user(telegram_id)
        except KeyError:
            await callback.answer("Пользователь не найден", show_alert=True)
            return

        current_limit = int(current.get("max_devices") or BASE_DEVICES)

        if action == "add":
            if current_limit >= MAX_DEVICES:
                await callback.answer(
                    "Уже максимум: 5 устройств.",
                    show_alert=True,
                )
                return
            updated = await db.grant_extra_device(
                telegram_id,
                max_total_devices=MAX_DEVICES,
            )
            success_text = "+1 устройство выдано"
        elif action == "remove":
            if current_limit <= BASE_DEVICES:
                await callback.answer(
                    "Нельзя опустить ниже 1 устройства.",
                    show_alert=True,
                )
                return
            updated = await db.revoke_extra_device(telegram_id)
            success_text = "−1 устройство"
        else:
            await callback.answer("Неизвестное действие", show_alert=True)
            return

        if not updated:
            await callback.answer(
                "Не удалось изменить лимит.",
                show_alert=True,
            )
            return

        await sync_device_limit(updated)
        await callback.answer(success_text)
        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.callback_query(F.data.startswith("admin:grant:"))
    async def admin_grant_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await callback.answer(
                "Нужна полная админка.",
                show_alert=True,
            )
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
            max_devices=BASE_DEVICES,
        )
        try:
            await provider.provision(user)
        except Exception as exc:
            logger.warning(
                "Admin grant provisioning deferred for user %s: %s",
                telegram_id,
                exc,
            )

        await callback.answer(f"Добавлено {days} дней")
        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.message(Command("user"))
    async def admin_user_command(message: Message) -> None:
        if not await has_admin_access(message.from_user.id):
            return
        parts = (message.text or "").split()
        if len(parts) != 2 or not parts[1].isdigit():
            await message.answer("Использование: /user TELEGRAM_ID")
            return
        await show_admin_user(message, message.from_user, int(parts[1]))

    @router.message(Command("paystatus"))
    async def paystatus(message: Message) -> None:
        if not await has_full_admin_access(message.from_user.id):
            return
        await show_admin_system(message, message.from_user)

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not await has_admin_access(message.from_user.id):
            return
        await show_admin_stats(message, message.from_user)

    @router.message(Command("promocreate"))
    async def admin_promo_create(message: Message) -> None:
        if not await has_full_admin_access(message.from_user.id):
            return

        parts = (message.text or "").split()
        if len(parts) != 7:
            await message.answer(
                "Использование: /promocreate CODE TYPE VALUE MAX_USES DAYS_VALID PLANS\n"
                "TYPE: discount или free_days; 0 в MAX_USES — без лимита; "
                "PLANS: all или 7,30,90,180,365"
            )
            return

        code, promo_type, value_raw, max_raw, valid_raw, plans = parts[1:]
        try:
            value = int(value_raw)
            max_uses = int(max_raw)
            valid_days = int(valid_raw)
        except ValueError:
            await message.answer("VALUE, MAX_USES и DAYS_VALID должны быть числами.")
            return
        expires_at = None
        if valid_days > 0:
            from db import to_iso
            expires_at = to_iso(utcnow() + timedelta(days=valid_days))
        try:
            promo = await db.create_service_promo(
                code=code,
                promo_type=promo_type,
                value=value,
                max_uses=max_uses or None,
                expires_at=expires_at,
                applicable_plans=plans,
                created_by=message.from_user.id,
            )
        except (ValueError, aiosqlite.IntegrityError):
            await message.answer("Проверьте параметры: код должен быть уникальным.")
            return
        role = await get_admin_role(message.from_user.id)
        await send_screen(
            message,
            message.from_user,
            f"🎟 Промокод <code>{html.escape(str(promo['code']))}</code> создан.\n"
            f"Тип: <b>{html.escape(str(promo['type']))}</b> · значение: <b>{promo['value']}</b>",
            reply_markup=admin_main_keyboard(role or "full"),
        )

    @router.message(Command("grant"))
    async def grant(message: Message) -> None:
        if not await has_full_admin_access(message.from_user.id):
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
            max_devices=BASE_DEVICES,
        )
        try:
            await provider.provision(user)
        except Exception as exc:
            logger.warning(
                "Admin command provisioning deferred for user %s: %s",
                telegram_id,
                exc,
            )

        role = await get_admin_role(message.from_user.id)
        await send_screen(
            message,
            message.from_user,
            f"✅ Пользователю <code>{telegram_id}</code> добавлено <b>{days}</b> дней.",
            reply_markup=admin_main_keyboard(role or "full"),
        )

    return router
