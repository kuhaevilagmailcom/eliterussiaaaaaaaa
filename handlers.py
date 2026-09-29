from __future__ import annotations

import asyncio
import base64
import csv
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from aiogram import F, Router
import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
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
    ReplyKeyboardRemove,
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
    POPULAR_PLAN_CODE,
    extra_device_price_stars,
    plan_price_rub,
    plan_price_stars,
    plan_savings_rub,
)
from config import Config
from analytics import load_business_analytics
from admin_notify import notify_purchase
from db import Database, from_iso, utcnow
from emoji import EmojiBank
from giveaway import (
    cancel_giveaway,
    delete_giveaway,
    finish_giveaway,
    render_giveaway_post,
    reroll_giveaway_winner,
    send_giveaway_post,
    undo_giveaway_reroll,
)
from payments import RollyPayError, create_payment, get_payment
from legal import agreement_telegram
from vpn import CANONICAL_SERVERS, VpnProvider, VpnState
from vpn_clients import CLIENTS, client_redirect_url


PACK_CRYPTO = "CryptoGIFTPODARKI"
PACK_UI = "TgAndroidIcons"
PACK_PROGRESS = "progressBarEmoji"
PACK_NEWS = "NewsEmoji"

REPLY_NAVIGATION_TEXTS = frozenset(
    {
        "Главное",
        "Главное меню",
        "🏠 Главное",
        "🏠 Главное меню",
        "VPN",
        "Подключить VPN",
        "Подключиться",
        "🔗 Подключить VPN",
        "🔗 Подключиться",
        "Купить VPN",
        "Продлить VPN",
        "💳 Купить VPN",
        "Профиль",
        "👤 Профиль",
        "Рефералы",
        "Друзья",
        "Пригласить друга",
        "👥 Друзья",
        "👥 Пригласить друга",
        "Поддержка",
        "Помощь",
        "🆘 Поддержка",
        "🆘 Помощь",
        "Устройства",
        "📱 Устройства",
        "Информация",
        "ℹ️ Информация",
        "О сервисе",
        "ℹ️ О сервисе",
        "Админ-панель",
        "🛡 Админ-панель",
    }
)

logger = logging.getLogger(__name__)


_button_emoji_bank: EmojiBank | None = None
_BUTTON_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+"
)


def _clean_button_text(value: str) -> str:
    cleaned = _BUTTON_EMOJI_RE.sub("", str(value or ""))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _button_icon_id(text: str, index: int | None = None) -> str | None:
    bank = _button_emoji_bank
    if bank is None:
        return None
    label = _clean_button_text(text)
    stable_index = index if index is not None else sum(ord(ch) for ch in label) % 64
    return bank.raw_id(stable_index, pack=PACK_NEWS)


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
    custom_icons: bool = True,
    active: bool = False,
    admin: bool = False,
) -> ReplyKeyboardMarkup:
    bank = emoji or _button_emoji_bank

    def button(text: str, index: int) -> KeyboardButton:
        kwargs: dict[str, Any] = {
            "text": _clean_button_text(text),
            "style": "danger",
        }
        if custom_icons and bank is not None:
            custom_id = bank.raw_id(index, pack=PACK_NEWS)
            if custom_id:
                kwargs["icon_custom_emoji_id"] = custom_id
        return KeyboardButton(**kwargs)

    return ReplyKeyboardMarkup(
        keyboard=[
            [button("VPN", 2), button("Главное меню", 4)],
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
    web_app: WebAppInfo | None = None,
    icon_index: int | None = None,
    premium_icon: bool = True,
) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {
        "text": _clean_button_text(text),
        "callback_data": callback_data,
        "url": url,
        "web_app": web_app,
    }
    if premium_icon:
        custom_id = _button_icon_id(text, icon_index)
        if custom_id:
            kwargs["icon_custom_emoji_id"] = custom_id
    return InlineKeyboardButton(**kwargs)


def copy_inline_button(
    text: str,
    value: str,
    *,
    icon_index: int | None = None,
) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {
        "text": _clean_button_text(text),
        "copy_text": CopyTextButton(text=value),
    }
    custom_id = _button_icon_id(text, icon_index)
    if custom_id:
        kwargs["icon_custom_emoji_id"] = custom_id
    return InlineKeyboardButton(**kwargs)


def connection_keyboard(
    subscription_url: str,
    *,
    back_data: str = "home",
) -> Any:
    kb = InlineKeyboardBuilder()
    for client in CLIENTS[:2]:
        kb.row(
            blue_inline_button(
                f"Добавить в {client.name}",
                url=client_redirect_url(subscription_url, client),
                icon_index=2,
            )
        )
    kb.row(
        copy_inline_button(
            "Скопировать ссылку",
            subscription_url,
            icon_index=3,
        )
    )
    kb.row(
        blue_inline_button(
            "Продлить подписку",
            callback_data="plans",
            icon_index=1,
        )
    )
    kb.row(
        blue_inline_button(
            "Устройства",
            callback_data="menu:devices",
            icon_index=3,
        )
    )
    kb.row(
        blue_inline_button(
            "Назад",
            callback_data=back_data,
            premium_icon=False,
        )
    )
    return kb.as_markup()


def add_nav_buttons(
    kb: InlineKeyboardBuilder,
    *,
    back_data: str = "home",
) -> None:
    kb.row(
        blue_inline_button(
            "Назад",
            callback_data=back_data,
            premium_icon=False,
        ),
    )


def section_nav_keyboard(*, back_data: str = "home") -> Any:
    kb = InlineKeyboardBuilder()
    add_nav_buttons(kb, back_data=back_data)
    return kb.as_markup()


def main_menu_inline_keyboard(
    admin_role: str | None = None,
    miniapp_url: str = "",
    *,
    active: bool = False,
) -> Any:
    kb = InlineKeyboardBuilder()
    if miniapp_url:
        kb.row(
            blue_inline_button(
                "Открыть приложение",
                web_app=WebAppInfo(url=miniapp_url),
                icon_index=9,
            )
        )
    kb.row(
        blue_inline_button(
            "Моя подписка" if active else "Купить подписку",
            callback_data="menu:connect" if active else "plans",
            icon_index=1 if not active else 2,
        )
    )
    kb.row(
        blue_inline_button(
            "Подарить другу",
            callback_data="menu:gift",
            icon_index=4,
        )
    )
    kb.row(
        blue_inline_button(
            "Реферальная система",
            callback_data="menu:friends",
            icon_index=8,
        )
    )
    kb.row(
        blue_inline_button(
            "Статус серверов",
            callback_data="menu:serverstatus",
            icon_index=2,
        )
    )
    kb.row(
        blue_inline_button(
            "О сервисе",
            callback_data="menu:info",
            icon_index=5,
        )
    )
    if admin_role:
        kb.row(
            blue_inline_button(
                "Админ-панель",
                callback_data="admin:home",
                icon_index=10,
            )
        )
    return kb.as_markup()

def plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        savings = plan_savings_rub(code)
        labels: list[str] = []
        if code == POPULAR_PLAN_CODE:
            labels.append("🔥 популярный")
        if savings:
            labels.append(f"выгода {savings} ₽")
        suffix = f" · {' · '.join(labels)}" if labels else ""
        kb.row(
            blue_inline_button(
                f"💳 {plan['name']} · {plan_price_rub(config, code)} ₽{suffix}",
                callback_data=f"plan:{code}",
            )
        )
    add_nav_buttons(kb, back_data="home")
    return kb.as_markup()


def gift_plans_keyboard(config: Config) -> Any:
    kb = InlineKeyboardBuilder()
    for code, plan in PLANS.items():
        savings = plan_savings_rub(code)
        labels: list[str] = []
        if code == POPULAR_PLAN_CODE:
            labels.append("🔥 популярный")
        if savings:
            labels.append(f"выгода {savings} ₽")
        suffix = f" · {' · '.join(labels)}" if labels else ""
        kb.row(
            blue_inline_button(
                f"🎁 {plan['name']} · {plan_price_rub(config, code)} ₽{suffix}",
                callback_data=f"gift:{code}",
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



def format_traffic_gb(used_gb: float, limit_gb: float) -> str:
    used = max(0.0, float(used_gb or 0))
    limit = max(0.0, float(limit_gb or 0))
    used_text = f"{used:.2f}".rstrip("0").rstrip(".")
    if limit <= 0:
        return f"{used_text} ГБ / ∞"
    limit_text = f"{limit:.2f}".rstrip("0").rstrip(".")
    return f"{used_text} ГБ / {limit_text} ГБ"


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
    devices = list(state.devices or [])
    devices_count = len(devices)
    plan = html.escape(user.get("plan_name") or "—")

    e_profile = emoji.icon(0, pack=PACK_NEWS)
    e_sub = emoji.icon(1, pack=PACK_NEWS)
    e_devices = emoji.icon(3, pack=PACK_NEWS)

    lines = [
        f"{e_profile} <b>Профиль</b>",
        f"ID: <code>{user_id}</code>",
        "",
        f"{e_sub} <b>Подписка</b>",
        f"├ Статус: <b>{'Активна' if active else 'Не активна'}</b>",
    ]

    if active:
        lines += [
            f"├ Тариф: <b>{plan}</b>",
            f"├ Действует до: <b>{format_until(user, config)}</b>",
            f"├ Осталось: <b>{remaining_text(user)}</b>",
            f"└ Трафик: <b>{format_traffic_gb(state.traffic_used_gb, state.traffic_limit_gb)}</b>",
            "",
            f"{e_devices} <b>Устройства — {devices_count}/{max_devices}</b>",
        ]
        if devices:
            for index, item in enumerate(devices[:max_devices], start=1):
                name = html.escape(
                    str(
                        item.get("name")
                        or item.get("device_name")
                        or item.get("model")
                        or f"Устройство {index}"
                    )
                )
                platform = html.escape(
                    str(item.get("platform") or item.get("os") or "").strip()
                )
                suffix = f" · {platform}" if platform else ""
                lines.append(f"├ {name}{suffix}")
            free_slots = max(0, max_devices - devices_count)
            lines.append(f"└ Свободно слотов: <b>{free_slots}</b>")
        else:
            lines.append("└ Подключённых устройств пока нет.")
    else:
        lines += [
            "└ Подписка не активна.",
            "",
            "Выберите тариф или пригласите друзей.",
        ]

    if not provider_ok and active:
        lines += [
            "",
            "<i>Подписка сохранена, но VPN-сервер временно не отвечает.</i>",
        ]

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
        base_url = _https_public_base_url(config.vpn_sub_base_url)
        if base_url:
            token = quote(str(user["sub_token"]), safe="")
            return f"{base_url}/{token}"

    return state.subscription_url or ""


def connection_text(
    user: dict[str, Any],
    state: VpnState,
    emoji: EmojiBank,
    subscription_url: str,
) -> str:
    max_devices = int(user.get("max_devices") or 1)
    connected = len(list(state.devices or []))
    plan = html.escape(str(user.get("plan_name") or "VPN"))
    return (
        "🔐 <b>Моя подписка</b>\n\n"
        f"Тариф: <b>{plan}</b>\n"
        f"Осталось: <b>{remaining_text(user)}</b>\n"
        f"Трафик: <b>{format_traffic_gb(state.traffic_used_gb, state.traffic_limit_gb)}</b>\n"
        f"Устройства: <b>{connected}/{max_devices}</b>\n\n"
        "Выберите VPN-клиент или скопируйте персональную ссылку.\n"
        "<i>Не передавайте ссылку другим людям.</i>"
    )


def build_router(
    config: Config,
    db: Database,
    emoji: EmojiBank,
    provider: VpnProvider,
) -> Router:
    global _button_emoji_bank
    _button_emoji_bank = emoji

    router = Router()
    callback_events: dict[int, list[float]] = {}

    async def safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> bool:
        """Answer Telegram callbacks without crashing on an expired query id."""
        try:
            await callback.answer(*args, **kwargs)
            return True
        except TelegramBadRequest as exc:
            error_text = str(exc).lower()
            if (
                "query is too old" in error_text
                or "response timeout expired" in error_text
                or "query id is invalid" in error_text
            ):
                logger.debug(
                    "Ignored expired callback query for user %s",
                    getattr(getattr(callback, "from_user", None), "id", "?"),
                )
                return False
            raise

    async def callback_guard(handler, event, data):
        now = asyncio.get_running_loop().time()
        if len(callback_events) > 4096:
            for key in list(callback_events):
                if not callback_events[key] or callback_events[key][-1] < now - 60:
                    callback_events.pop(key, None)
        uid = event.from_user.id
        recent = [stamp for stamp in callback_events.get(uid, []) if stamp > now - 60]
        callback_events[uid] = recent
        if len(recent) >= 40:
            await safe_callback_answer(event, "Слишком много запросов. Подождите немного.")
            return
        recent.append(now)
        try:
            return await handler(event, data)
        except TelegramForbiddenError:
            logger.info("Telegram delivery unavailable for user %s", uid)
            return

    router.callback_query.outer_middleware(callback_guard)
    support_cooldowns: dict[int, float] = {}

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

    def required_channel_id() -> str | int | None:
        value = str(config.channel_url or "").strip()
        if re.fullmatch(r"-100\d{6,}", value):
            return int(value)
        match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})", value)
        if match:
            return "@" + match.group(1)
        match = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]{5,32})/?", value)
        if match:
            return "@" + match.group(1)
        return None

    def channel_gate_keyboard() -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("Подписаться на канал", url=config.channel_url, icon_index=8)
        )
        kb.row(
            blue_inline_button("Проверить подписку", callback_data="membership:check", icon_index=2)
        )
        return kb.as_markup()

    async def is_channel_member(bot, user_id: int) -> bool:
        channel_id = required_channel_id()
        if channel_id is None:
            logger.error("CHANNEL_URL cannot be used for membership verification")
            return False
        try:
            member = await bot.get_chat_member(chat_id=channel_id, user_id=int(user_id))
        except Exception as exc:
            logger.warning(
                "Could not verify channel membership for %s: %s",
                user_id,
                type(exc).__name__,
            )
            return False
        status = str(getattr(member, "status", "")).lower()
        if status in {"creator", "administrator", "member"}:
            return True
        return status == "restricted" and bool(getattr(member, "is_member", False))

    async def require_channel_membership(message: Message, actor, user: dict[str, Any]) -> bool:
        if user.get("channel_verified_at") or is_owner(int(actor.id)):
            return True
        if await is_channel_member(message.bot, int(actor.id)):
            await db.mark_channel_verified(int(actor.id))
            return True
        await message.answer(
            "📣 <b>Подпишитесь на канал MGN VPN</b>\n\n"
            "Подписка на канал обязательна для доступа к боту. После подписки нажмите «Проверить подписку».",
            reply_markup=channel_gate_keyboard(),
        )
        return False


    async def get_admin_role(user_id: int) -> str | None:
        if user_id in config.admin_ids:
            return "owner"
        return await db.get_admin_role(user_id)

    async def has_admin_access(user_id: int) -> bool:
        return bool(await get_admin_role(user_id))

    async def has_full_admin_access(user_id: int) -> bool:
        # Product rule: every issued admin has the same operational access.
        return await has_admin_access(user_id)

    def is_owner(user_id: int) -> bool:
        return user_id in config.admin_ids

    async def purchases_open() -> tuple[bool, str]:
        state = await db.maintenance_state()
        return (
            not bool(state.get("enabled")),
            str(
                state.get("message")
                or "Покупки временно приостановлены. Активные VPN-подписки продолжают работать."
            ),
        )

    async def guard_purchase_message(message: Message, actor) -> bool:
        opened, reason = await purchases_open()
        if opened:
            return True
        await send_screen(
            message,
            actor,
            "🛠 <b>Технические работы</b>\n\n"
            f"{html.escape(reason)}\n\n"
            "<i>Если VPN уже активен, он продолжает работать как обычно.</i>",
            reply_markup=section_nav_keyboard(back_data="home"),
        )
        return False

    async def guard_purchase_callback(callback: CallbackQuery) -> bool:
        opened, reason = await purchases_open()
        if opened:
            return True
        await safe_callback_answer(callback, reason[:180], show_alert=True)
        return False

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
        home_markup = main_menu_inline_keyboard(
            admin_role,
            config.miniapp_url,
            active=is_active(user),
        )
        if reply_markup is None:
            reply_markup = home_markup

        async def create_first_menu() -> Message:
            if len(text) > 1000:
                sent = await message.bot.send_message(
                    chat_id=message.chat.id,
                    text=strip_custom_emoji(text),
                    reply_markup=reply_markup,
                )
                await db.set_last_menu_message(actor.id, sent.message_id)
                return sent
            banner = current_main_menu_banner()
            try:
                sent = await message.bot.send_photo(
                    chat_id=message.chat.id,
                    photo=banner,
                    caption=text,
                    reply_markup=reply_markup,
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
                        reply_markup=reply_markup,
                    )
                except TelegramBadRequest:
                    sent = await message.bot.send_photo(
                        chat_id=message.chat.id,
                        photo=banner,
                        caption=strip_custom_emoji(text),
                        reply_markup=reply_markup,
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

        # Telegram photo captions are limited to 1024 characters. Long admin
        # and support screens switch the tracked UI message to text so the
        # action does not silently fail and all inline controls stay usable.
        if len(text) > 1000:
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=int(last_id),
                )
            except Exception as exc:
                logger.warning("Could not replace long UI screen for %s: %s", actor.id, type(exc).__name__)
            sent = await message.bot.send_message(
                chat_id=message.chat.id,
                text=text,
                reply_markup=reply_markup,
            )
            await db.set_last_menu_message(actor.id, sent.message_id)
            return sent

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
        # A ReplyKeyboard tap posts a regular user message. Move the tracked
        # bot screen to the bottom and remove that navigation message so the
        # chat continues to look like one compact interface.
        if str(getattr(message, "text", "") or "").strip() in REPLY_NAVIGATION_TEXTS:
            force_new = True
            try:
                await message.bot.delete_message(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                )
            except Exception as exc:
                logger.debug(
                    "Could not remove reply navigation message for %s: %s",
                    actor.id,
                    type(exc).__name__,
                )
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
        ensure_reply_keyboard: bool = False,
    ) -> None:
        user = await ensure_actor(actor)
        active = is_active(user)
        display_name = html.escape(
            str(
                getattr(actor, "first_name", None)
                or (f"@{getattr(actor, 'username', '')}" if getattr(actor, "username", None) else "")
                or "Пользователь"
            )
        )

        lines = [
            f"👤 <b>Профиль: {display_name}</b>",
            f"ID: <code>{int(user['telegram_id'])}</code>",
            "",
        ]

        if active:
            lines += [
                "🔑 <b>Подписка активна</b>",
                f"Тариф: <b>{html.escape(str(user.get('plan_name') or 'VPN'))}</b>",
                f"До: <b>{format_until(user, config)}</b>",
                f"Осталось: <b>{remaining_text(user)}</b>",
                f"Устройства: <b>до {int(user.get('max_devices') or 1)}</b>",
                "",
                "<blockquote>🔧 Нажмите <b>«Моя подписка»</b>, чтобы подключить устройство, "
                "продлить доступ или скопировать персональную ссылку.</blockquote>",
            ]
        else:
            lines += [
                "🔒 <b>Подписка не активна</b>",
                "",
                "<blockquote>🔧 Нажмите <b>«Купить подписку»</b>, чтобы выбрать тариф "
                "и настроить VPN-подключение.</blockquote>",
            ]

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            bottom_menu=True,
            recover_on_edit_failure=recover_on_edit_failure,
            force_new=force_new,
        )

        if ensure_reply_keyboard:
            # Remove the old persistent ReplyKeyboard without leaving a visible
            # service message in the chat. From now on navigation is inline and
            # through /start and /sub only.
            try:
                cleanup = await message.answer(
                    "\u2063",
                    reply_markup=ReplyKeyboardRemove(),
                )
                try:
                    await cleanup.delete()
                except Exception:
                    pass
            except Exception:
                pass

    async def show_profile(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        state, ok = await load_state(user, provider, config)
        kb = InlineKeyboardBuilder()
        if is_active(user):
            kb.row(
                blue_inline_button(
                    "Подключить VPN",
                    callback_data="menu:connect",
                    icon_index=2,
                )
            )
            kb.row(
                blue_inline_button(
                    "Выбор сервера",
                    callback_data="menu:country",
                    icon_index=2,
                ),
                blue_inline_button(
                    "Статус серверов",
                    callback_data="menu:serverstatus",
                    icon_index=3,
                ),
            )
            kb.row(
                blue_inline_button(
                    "Продлить VPN",
                    callback_data="plans",
                    icon_index=1,
                )
            )
        else:
            kb.row(
                blue_inline_button(
                    "Купить VPN",
                    callback_data="plans",
                    icon_index=1,
                )
            )
        kb.row(
            blue_inline_button(
                "История оплат",
                callback_data="menu:paymenthistory",
            ),
            blue_inline_button(
                "Начисления",
                callback_data="menu:accruals",
            ),
        )
        kb.row(
            blue_inline_button(
                "Бонусы",
                callback_data="menu:bonusstats",
            )
        )
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            message,
            actor,
            profile_text(user, state, emoji, ok, config),
            reply_markup=kb.as_markup(),
        )


    def country_label(code: str) -> str:
        labels = dict(CANONICAL_SERVERS)
        labels.update({
            "auto": "⚡ Автоматически",
            "lt": "🇱🇹 Литва",
            "lv": "🇱🇻 Латвия",
        })
        return labels.get(str(code or ""), str(code or "—"))

    async def show_user_server_status(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Выбор сервера",
                callback_data="menu:country",
            )
        )
        kb.row(
            blue_inline_button(
                "Обновить",
                callback_data="menu:serverstatus",
            )
        )
        add_nav_buttons(kb, back_data="menu:profile")
        try:
            report = await asyncio.wait_for(
                provider.server_diagnostics(user),
                timeout=12.0,
            )
        except Exception:
            await send_screen(
                message,
                actor,
                "🌐 <b>Статус серверов</b>\n\n"
                "Сейчас не удалось получить диагностику. Ваша подписка и конфиги не изменялись.",
                reply_markup=kb.as_markup(),
            )
            return

        preferred = str(user.get("preferred_country") or "auto")
        lines = [
            "🌐 <b>Статус серверов MGN VPN</b>",
            f"Режим: <b>{html.escape(country_label(preferred))}</b>",
            "",
        ]
        for item in report.get("servers") or []:
            name = html.escape(str(item.get("name") or "VPN-сервер"))
            configured = bool(item.get("configured") or item.get("available"))
            available = bool(item.get("available"))
            latency = item.get("latency_ms")
            if available:
                state = "🟢 доступен"
            elif configured:
                state = "🟡 настроен, проверка не прошла"
            else:
                state = "⚪ нет данных"
            latency_text = (
                f" · {int(latency)} мс"
                if isinstance(latency, (int, float)) and available
                else ""
            )
            lines.append(f"{state} · <b>{name}</b>{latency_text}")

        lines += [
            "",
            "<i>Статус — это диагностика со стороны MGN VPN. "
            "Мы не удаляем сервер из вашей подписки из-за одной неудачной проверки.</i>",
        ]
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_country_picker(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        if not is_active(user):
            await send_screen(
                message,
                actor,
                "⚡ <b>Выбор сервера</b>\n\nСначала активируйте подписку.",
                reply_markup=section_nav_keyboard(back_data="menu:profile"),
            )
            return

        preferred = str(user.get("preferred_country") or "auto")
        candidates: list[tuple[str, str, bool]] = []
        try:
            report = await asyncio.wait_for(provider.server_diagnostics(user), timeout=10.0)
            for item in report.get("servers") or []:
                code = str(item.get("catalog_id") or item.get("id") or "").strip().lower()
                if code in {"main"}:
                    code = "nl"
                if code not in {"nl", "pk", "de", "pl", "fi", "us", "us2", "lt", "lv"}:
                    continue
                candidates.append(
                    (
                        code,
                        str(item.get("name") or country_label(code)),
                        bool(item.get("available")),
                    )
                )
        except Exception:
            candidates = []

        if not candidates:
            candidates = [(code, label, False) for code, label in CANONICAL_SERVERS]

        dedup: dict[str, tuple[str, str, bool]] = {}
        for item in candidates:
            if item[0] not in dedup or item[2]:
                dedup[item[0]] = item

        kb = InlineKeyboardBuilder()
        auto_prefix = "✓ " if preferred == "auto" else ""
        kb.row(
            blue_inline_button(
                f"{auto_prefix}⚡ Автоматически",
                callback_data="menu:country:set:auto",
            )
        )
        for code, name, available in dedup.values():
            prefix = "✓ " if preferred == code else ""
            state = "🟢 " if available else ""
            kb.row(
                blue_inline_button(
                    f"{prefix}{state}{name}",
                    callback_data=f"menu:country:set:{code}",
                )
            )
        kb.row(
            blue_inline_button(
                "Статус серверов",
                callback_data="menu:serverstatus",
            )
        )
        add_nav_buttons(kb, back_data="menu:profile")
        await send_screen(
            message,
            actor,
            "⚡ <b>Предпочитаемый сервер</b>\n\n"
            "В режиме «Автоматически» проверенные быстрые серверы идут первыми. "
            "При выборе страны она поднимается в начало подписки, <b>остальные страны не удаляются</b>.\n\n"
            f"Сейчас: <b>{html.escape(country_label(preferred))}</b>",
            reply_markup=kb.as_markup(),
        )

    async def show_payment_history(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        rows = await db.list_user_payment_history(int(user["telegram_id"]), limit=12)
        lines = ["💳 <b>История оплат</b>", ""]
        if not rows:
            lines.append("Оплат пока нет.")
        else:
            for item in rows:
                dt = from_iso(item.get("created_at"))
                date_text = (
                    dt.astimezone(config.display_tz).strftime("%d.%m.%Y · %H:%M")
                    if dt else "—"
                )
                code = str(item.get("plan_code") or "")
                product = (
                    "+1 устройство"
                    if code == DEVICE_PRODUCT_CODE
                    else str(PLANS.get(code, {}).get("name") or code)
                )
                amount = int(item.get("amount") or 0)
                currency = "₽" if str(item.get("currency")) == "RUB" else "⭐"
                status = str(item.get("status") or "")
                lines.append(
                    f"• <b>{html.escape(product)}</b> · {amount} {currency}\n"
                    f"  {html.escape(str(item.get('method') or ''))} · {date_text} · {html.escape(status)}"
                )
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=section_nav_keyboard(back_data="menu:profile"),
        )

    async def show_subscription_events(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        rows = await db.list_subscription_events(int(user["telegram_id"]), limit=20)
        lines = ["🎁 <b>История начислений</b>", ""]
        if not rows:
            lines.append("Начислений пока нет.")
        else:
            for item in rows:
                dt = from_iso(item.get("at"))
                date_text = (
                    dt.astimezone(config.display_tz).strftime("%d.%m.%Y · %H:%M")
                    if dt else "—"
                )
                lines.append(
                    f"• <b>+{int(item.get('days') or 0)} дней</b> · "
                    f"{html.escape(str(item.get('source') or ''))} · {date_text}"
                )
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=section_nav_keyboard(back_data="menu:profile"),
        )

    async def show_bonus_stats(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        uid = int(user["telegram_id"])
        referrals = await db.referral_stats(uid)
        promos = await db.user_promo_stats(uid)
        username = (await message.bot.get_me()).username or "mgnvpn_bot"
        text = (
            "🎁 <b>Бонусы</b>\n\n"
            f"👥 Приглашено друзей: <b>{referrals['invited']}</b>\n"
            f"✅ Получено реферальных наград: <b>{referrals['rewarded']}</b> / 3\n"
            f"🎟 Использовано промокодов: <b>{promos['used']}</b>\n\n"
            f"Ваша ссылка:\n<code>https://t.me/{username}?start=ref_{uid}</code>"
        )
        await send_screen(
            message,
            actor,
            text,
            reply_markup=section_nav_keyboard(back_data="menu:profile"),
        )

    async def show_subscription(message: Message, actor) -> None:
        user = await ensure_actor(actor)
        if not is_active(user):
            kb = InlineKeyboardBuilder()
            kb.row(blue_inline_button("Купить подписку", callback_data="plans", icon_index=1))
            add_nav_buttons(kb, back_data="home")
            await send_screen(
                message,
                actor,
                "🔐 <b>Моя подписка</b>\n\n"
                "У вас пока нет активной подписки.",
                reply_markup=kb.as_markup(),
            )
            return

        # The public MGN subscription URL is stable for H1Cloud, so the
        # bot screen must not wait for a slow provision cycle just to display
        # connection controls. Read live device state briefly when available;
        # /sub itself performs the bounded self-heal.
        state = fallback_state(user, config)
        ok = True
        if getattr(provider, "service_ready", True):
            try:
                state = await asyncio.wait_for(provider.get_state(user), 2.0)
            except Exception:
                ok = False
        subscription_url = await public_subscription_url(
            user,
            state,
            config,
            bot=message.bot,
        )
        if not subscription_url:
            await send_screen(
                message,
                actor,
                "🔐 <b>Моя подписка</b>\n\n"
                "Подписка активна, но ссылка подключения временно недоступна. "
                "Попробуйте ещё раз через несколько секунд.",
                reply_markup=section_nav_keyboard(),
            )
            return

        if not ok:
            logger.warning(
                "Showing stable public subscription URL despite provider state failure for user %s",
                user.get("telegram_id"),
            )

        await send_screen(
            message,
            actor,
            connection_text(user, state, emoji, subscription_url),
            reply_markup=connection_keyboard(subscription_url),
        )

    async def apply_paid_purchase(
        buyer_telegram_id: int,
        target_telegram_id: int,
        code: str,
        payment_event_key: str,
    ) -> tuple[dict[str, Any], int]:
        user = await db.get_user(target_telegram_id)
        try:
            await asyncio.wait_for(provider.provision(user), 7.0)
        except Exception as exc:
            logger.warning("Paid VPN provisioning deferred for %s: %s", target_telegram_id, type(exc).__name__)
        return user, 0

    @router.message(Command("setbanner"))
    async def set_banner(message: Message) -> None:
        if not message.from_user or not await has_admin_access(message.from_user.id):
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
        first_bot_start = await db.claim_first_bot_start(message.from_user.id)
        start_arg = str(command.args or "").strip().lower()

        campaign_source = (
            start_arg
            if start_arg in {"anonchat_mgn", "pozor_mgn"}
            or re.fullmatch(r"utm_[a-z0-9_-]{1,60}", start_arg)
            else ""
        )
        if campaign_source:
            await db.set_attribution_source_once(
                message.from_user.id,
                campaign_source,
            )
            user = await db.get_user(message.from_user.id)

        if start_arg.startswith("ref_"):
            raw = start_arg.removeprefix("ref_")
            if raw.isdigit() and first_bot_start:
                await db.set_referrer_once(
                    message.from_user.id,
                    int(raw),
                )
        if not await require_channel_membership(message, message.from_user, user):
            return
        if start_arg in {"renew", "plans"}:
            e = emoji.icon(0, pack=PACK_NEWS)
            await send_screen(
                message,
                message.from_user,
                "💳 <b>Продлить MGN VPN</b>\n\n"
                "Выберите срок продления. Новый период прибавится к текущей подписке — оставшиеся дни не сгорят.",
                reply_markup=plans_keyboard(config),
                recover_on_edit_failure=True,
                force_new=True,
            )
            return
        if start_arg == "support":
            e = emoji.icon(6, pack=PACK_NEWS)
            await send_screen(
                message,
                message.from_user,
                f"{e} <b>Поддержка</b>\n\n"
                "Опишите проблему одним сообщением. Обращение получат администраторы; "
                "в нём будут видны ваш username, Telegram ID, дата и время.",
                reply_markup=support_keyboard(),
                recover_on_edit_failure=True,
                force_new=True,
            )
            return
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
            ensure_reply_keyboard=True,
        )

    @router.callback_query(F.data == "membership:check")
    async def membership_check(callback: CallbackQuery) -> None:
        user = await ensure_actor(callback.from_user)
        if not await is_channel_member(callback.bot, callback.from_user.id):
            await safe_callback_answer(
                callback,
                "Подписка пока не найдена. Подпишитесь на канал и попробуйте снова.",
                show_alert=True,
            )
            return
        await db.mark_channel_verified(callback.from_user.id)
        await safe_callback_answer(callback, "Подписка подтверждена")
        if callback.message:
            await show_home(
                callback.message,
                callback.from_user,
                recover_on_edit_failure=True,
                force_new=True,
                ensure_reply_keyboard=True,
            )

    @router.message(Command("sub"))
    async def subscription_command(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not await require_channel_membership(message, message.from_user, user):
            return
        try:
            cleanup = await message.answer("\u2063", reply_markup=ReplyKeyboardRemove())
            try:
                await cleanup.delete()
            except Exception:
                pass
        except Exception:
            pass
        await show_subscription(message, message.from_user)

    @router.message(Command("menu"))
    async def menu_command(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not await require_channel_membership(message, message.from_user, user):
            return
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
        await safe_callback_answer(callback, )
        if callback.message:
            await show_home(callback.message, callback.from_user)


    @router.callback_query(F.data == "menu:profile")
    async def menu_profile(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if callback.message:
            await show_profile(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:serverstatus")
    async def menu_server_status(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, "Проверяю серверы…")
        if callback.message:
            await show_user_server_status(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:country")
    async def menu_country(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback)
        if callback.message:
            await show_country_picker(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("menu:country:set:"))
    async def menu_country_set(callback: CallbackQuery) -> None:
        code = str(callback.data.rsplit(":", 1)[-1]).lower()
        try:
            updated = await db.set_preferred_country(callback.from_user.id, code)
        except (ValueError, KeyError):
            await safe_callback_answer(callback, "Сервер не поддерживается.", show_alert=True)
            return
        await safe_callback_answer(
            callback,
            f"Выбрано: {country_label(str(updated.get('preferred_country') or 'auto'))}",
        )
        if callback.message:
            await show_country_picker(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:paymenthistory")
    async def menu_payment_history(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback)
        if callback.message:
            await show_payment_history(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:accruals")
    async def menu_accruals(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback)
        if callback.message:
            await show_subscription_events(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:bonusstats")
    async def menu_bonus_stats(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback)
        if callback.message:
            await show_bonus_stats(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:connect")
    async def menu_connect(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if callback.message:
            await show_subscription(callback.message, callback.from_user)

    def user_agreement_text() -> str:
        return agreement_telegram()

    def service_info_text() -> str:
        return (
            "🌐 <b>О MGN VPN</b>\n\n"
            "💳 <b>Прозрачная оплата</b>\n"
            "<blockquote>❤️ Никаких автосписаний и скрытых подписок. "
            "Оплата происходит только после вашего подтверждения.</blockquote>\n\n"
            "⚡ <b>Быстрое подключение</b>\n"
            "<blockquote>📶 Подключение настраивается по персональной ссылке. "
            "Доступные VPN-локации автоматически попадают в приложение.</blockquote>\n\n"
            "🛡 <b>Приватность</b>\n"
            "<blockquote>🔐 MGN VPN не анализирует содержимое вашего интернет-трафика. "
            "Для работы сервиса используются только необходимые технические данные: "
            "Telegram ID, состояние подписки, платёжные метаданные и данные подключений.</blockquote>\n\n"
            "📚 <b>Правила сервиса</b>\n"
            "<blockquote>ℹ️ Используя MGN VPN, вы принимаете правила сервиса. "
            "Персональная ссылка предназначена только для вашего аккаунта, "
            "а количество устройств ограничено выбранным лимитом.</blockquote>\n\n"
            "🔒 <b>Защищённое соединение</b>\n"
            "<blockquote>⚙️ VPN использует современные протоколы защищённого соединения "
            "для передачи данных между вашим устройством и VPN-сервером.</blockquote>\n\n"
            "🔑 <b>Ваша ссылка — ваш доступ</b>\n"
            "<blockquote>⚠️ Не передавайте персональную ссылку другим людям. "
            "Если она попала к постороннему, обратитесь в поддержку.</blockquote>"
        )

    def privacy_policy_url() -> str:
        parsed = urlsplit(config.miniapp_url)
        return urlunsplit((parsed.scheme, parsed.netloc, "/privacy", "", ""))

    def support_keyboard() -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Создать обращение",
                callback_data="support:new",
                icon_index=6,
            )
        )
        kb.row(blue_inline_button("Мои обращения", callback_data="support:list:0"))
        add_nav_buttons(kb, back_data="home")
        return kb.as_markup()

    def support_admin_keyboard(ticket_id: int) -> Any:
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Ответить",
                callback_data=f"support:reply:{int(ticket_id)}",
                icon_index=6,
            )
        )
        kb.row(blue_inline_button("Открыть обращение", callback_data=f"support:view:{int(ticket_id)}"))
        kb.row(
            blue_inline_button("Закрыть", callback_data=f"support:close:{int(ticket_id)}"),
            blue_inline_button("Удалить", callback_data=f"support:deleteconfirm:{int(ticket_id)}"),
        )
        return kb.as_markup()

    def support_user_ticket_keyboard(ticket: dict[str, Any]) -> Any:
        ticket_id = int(ticket["id"])
        kb = InlineKeyboardBuilder()
        if ticket.get("status") != "closed":
            kb.row(blue_inline_button("Написать сообщение", callback_data=f"support:write:{ticket_id}"))
            kb.row(blue_inline_button("Закрыть обращение", callback_data=f"support:close:{ticket_id}"))
        kb.row(blue_inline_button("Назад", callback_data="support:list:0", premium_icon=False))
        return kb.as_markup()

    def support_admin_ticket_keyboard(ticket: dict[str, Any]) -> Any:
        ticket_id = int(ticket["id"])
        kb = InlineKeyboardBuilder()
        if ticket.get("status") == "closed":
            kb.row(blue_inline_button("Переоткрыть", callback_data=f"support:reopen:{ticket_id}"))
        else:
            kb.row(blue_inline_button("Ответить", callback_data=f"support:reply:{ticket_id}"))
            kb.row(blue_inline_button("Закрыть", callback_data=f"support:close:{ticket_id}"))
        kb.row(blue_inline_button("Удалить", callback_data=f"support:deleteconfirm:{ticket_id}"))
        kb.row(blue_inline_button("Назад", callback_data="admin:support", premium_icon=False))
        return kb.as_markup()

    async def show_support_ticket(message: Message, actor, ticket_id: int, *, admin: bool = False) -> None:
        ticket = await db.get_support_ticket(
            ticket_id, owner_id=int(actor.id), is_admin=admin
        )
        if not ticket:
            await send_screen(message, actor, "Обращение не найдено.", reply_markup=support_keyboard())
            return
        messages = await db.list_support_messages(
            ticket_id, owner_id=int(actor.id), is_admin=admin
        )
        status = "Закрыто" if ticket.get("status") == "closed" else "Открыто"
        created = format_joined(ticket.get("created_at"))
        lines = [f"<b>Обращение #{ticket_id}</b>", f"Статус: <b>{status}</b>", f"Создано: <b>{created}</b>", ""]
        support_active: bool | None = None
        if admin:
            username = f"@{ticket['username']}" if ticket.get("username") else "без username"
            try:
                support_user = await db.get_user(int(ticket["telegram_id"]))
                support_active = is_active(support_user)
            except KeyError:
                support_active = False
            lines += [
                f"Пользователь: <b>{html.escape(username)}</b>",
                f"Telegram ID: <code>{ticket['telegram_id']}</code>",
                f"Подписка: <b>{'🟢 активна' if support_active else '🔴 нет активной'}</b>",
                "",
            ]
        for item in messages[-8:]:
            sender = "Поддержка" if item["sender_type"] == "admin" else "Пользователь"
            content = item.get("text") or item.get("caption") or ("Фото" if item["message_type"] == "photo" else "Видео")
            lines.append(f"<b>{sender}:</b> {html.escape(str(content)[:300])}")
        await send_screen(
            message, actor, "\n".join(lines),
            reply_markup=(
                support_admin_ticket_keyboard(ticket)
                if admin
                else support_user_ticket_keyboard(ticket)
            ),
        )

    def support_ticket_admin_text(ticket: dict[str, Any]) -> str:
        username = (
            f"@{html.escape(str(ticket.get('username')))}"
            if ticket.get("username")
            else "без username"
        )
        created = from_iso(ticket.get("created_at"))
        created_text = (
            created.astimezone(config.display_tz).strftime("%d.%m.%Y %H:%M:%S")
            if created
            else "—"
        )
        return (
            f"<b>Новое обращение #{int(ticket['id'])}</b>\n\n"
            f"Пользователь: <b>{username}</b>\n"
            f"Telegram ID: <code>{int(ticket['telegram_id'])}</code>\n"
            f"Дата: <b>{created_text}</b>\n\n"
            f"<b>Сообщение:</b>\n{html.escape(str(ticket.get('message') or ''))}"
        )

    async def notify_support_admins(bot, ticket: dict[str, Any]) -> None:
        recipients = set(int(value) for value in config.admin_ids)
        try:
            recipients.update(
                int(item["telegram_id"])
                for item in await db.list_admin_roles()
            )
        except Exception:
            logger.exception("Could not load support admin recipients")

        support_uid = int(ticket["telegram_id"])
        try:
            support_user = await db.get_user(support_uid)
            support_active = is_active(support_user)
        except KeyError:
            support_active = False

        for admin_id in recipients:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=support_ticket_admin_text(ticket),
                    reply_markup=support_admin_keyboard(int(ticket["id"])),
                )
            except Exception as exc:
                logger.warning(
                    "Could not deliver support ticket %s to admin %s: %s",
                    ticket.get("id"),
                    admin_id,
                    exc,
                )

    @router.callback_query(F.data == "menu:terms")
    async def menu_terms(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if callback.message:
            await send_screen(
                callback.message,
                callback.from_user,
                user_agreement_text(),
                reply_markup=section_nav_keyboard(back_data="menu:info"),
            )

    @router.callback_query(F.data == "menu:info")
    async def menu_info(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if not callback.message:
            return

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Поддержка",
                callback_data="menu:support",
                icon_index=6,
            )
        )
        kb.row(
            blue_inline_button(
                "Канал",
                url=config.channel_url,
                icon_index=8,
            )
        )
        kb.row(
            blue_inline_button(
                "Политика конфиденциальности",
                url=privacy_policy_url(),
                icon_index=11,
            )
        )
        add_nav_buttons(kb, back_data="home")
        text = service_info_text()
        await send_screen(
            callback.message,
            callback.from_user,
            text,
            reply_markup=kb.as_markup(),
        )

    async def refresh_main_keyboard(message: Message, actor) -> None:
        # Labels are stable now, so state changes do not need a noisy message.
        return None

    @router.callback_query(F.data == "menu:support")
    async def menu_support(callback: CallbackQuery) -> None:
        session = await db.get_support_session(callback.from_user.id)
        if session and session.get("mode") in {"new", "user_reply"}:
            await db.clear_support_session(callback.from_user.id)
        await safe_callback_answer(callback, )
        if callback.message:
            e = emoji.icon(6, pack=PACK_NEWS)
            await send_screen(
                callback.message,
                callback.from_user,
                f"{e} <b>Поддержка</b>\n\n"
                "Опишите проблему одним сообщением. Обращение получат администраторы; "
                "в нём будут видны ваш username, Telegram ID, дата и время.",
                reply_markup=support_keyboard(),
            )

    @router.callback_query(F.data == "support:new")
    async def support_new(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if not callback.message:
            return
        await db.set_support_session(int(callback.from_user.id), "new")
        await send_screen(
            callback.message,
            callback.from_user,
            "<b>Новое обращение</b>\n\n"
            "Отправьте проблему одним сообщением — текстом, фото или видео. "
            "После создания обращения его можно дополнить кнопкой «Написать сообщение». "
            "Текст — до 3000 символов. Не отправляйте пароли и другие секреты.",
            reply_markup=section_nav_keyboard(back_data="menu:support"),
        )

    @router.callback_query(F.data.startswith("support:reply:"))
    async def support_reply_start(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        ticket = await db.get_support_ticket(int(raw), is_admin=True)
        if not ticket or ticket.get("status") == "closed":
            await safe_callback_answer(callback, "Обращение не найдено", show_alert=True)
            return
        await db.set_support_session(int(callback.from_user.id), "admin_reply", int(raw))
        await safe_callback_answer(callback, )
        if callback.message:
            await callback.message.answer(
                f"Ответ на обращение <b>#{int(raw)}</b>. "
                "Отправьте текст, фото или видео следующим сообщением."
            )

    @router.callback_query(F.data.startswith("support:list:"))
    async def support_list(callback: CallbackQuery) -> None:
        if not callback.message:
            return
        raw = callback.data.rsplit(":", 1)[-1]
        page = int(raw) if raw.isdigit() else 0
        tickets, total = await db.list_user_support_tickets(callback.from_user.id, page, 8)
        pages = max(1, (total + 7) // 8)
        kb = InlineKeyboardBuilder()
        lines = ["<b>Мои обращения</b>", ""]
        for ticket in tickets:
            status = "Закрыто" if ticket.get("status") == "closed" else "Открыто"
            preview = html.escape(str(ticket.get("message") or "Обращение")[:45])
            lines.append(f"#{ticket['id']} · {status}\n{preview}\n{format_joined(ticket.get('updated_at') or ticket.get('created_at'))}\n")
            kb.row(blue_inline_button(f"#{ticket['id']} · {status}", callback_data=f"support:view:{ticket['id']}"))
        nav = []
        if page > 0:
            nav.append(blue_inline_button("←", callback_data=f"support:list:{page - 1}"))
        if page + 1 < pages:
            nav.append(blue_inline_button("→", callback_data=f"support:list:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.row(blue_inline_button("Новое обращение", callback_data="support:new"))
        kb.row(blue_inline_button("Назад", callback_data="menu:support", premium_icon=False))
        await safe_callback_answer(callback, )
        await send_screen(callback.message, callback.from_user, "\n".join(lines) if tickets else "<b>Мои обращения</b>\n\nОбращений пока нет.", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("support:view:"))
    async def support_view(callback: CallbackQuery) -> None:
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit() or not callback.message:
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        admin = await has_admin_access(callback.from_user.id)
        ticket = await db.get_support_ticket(int(raw), owner_id=callback.from_user.id, is_admin=admin)
        if not ticket:
            await safe_callback_answer(callback, "Обращение не найдено", show_alert=True)
            return
        await safe_callback_answer(callback, )
        await show_support_ticket(callback.message, callback.from_user, int(raw), admin=admin)

    @router.callback_query(F.data.startswith("support:write:"))
    async def support_write(callback: CallbackQuery) -> None:
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        ticket = await db.get_support_ticket(int(raw), owner_id=callback.from_user.id)
        if not ticket or ticket.get("status") == "closed":
            await safe_callback_answer(callback, "Обращение закрыто", show_alert=True)
            return
        await db.set_support_session(callback.from_user.id, "user_reply", int(raw))
        await safe_callback_answer(callback, )
        if callback.message:
            await callback.message.answer(f"Напишите сообщение в обращение #{raw}. Можно отправить текст, фото или видео.")

    @router.callback_query(F.data.startswith("support:close:"))
    async def support_close(callback: CallbackQuery) -> None:
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        admin = await has_admin_access(callback.from_user.id)
        ticket = await db.set_support_status(int(raw), "closed", callback.from_user.id, is_admin=admin)
        if not ticket:
            await safe_callback_answer(callback, "Обращение не найдено", show_alert=True)
            return
        await db.clear_support_session(callback.from_user.id)
        if admin and int(ticket.get("telegram_id") or 0) != int(callback.from_user.id):
            try:
                await callback.bot.send_message(
                    int(ticket["telegram_id"]),
                    f"Обращение <b>#{int(raw)}</b> закрыто поддержкой.",
                    reply_markup=support_user_ticket_keyboard(ticket),
                )
            except Exception as exc:
                logger.warning(
                    "Could not notify user about support close %s: %s",
                    raw,
                    type(exc).__name__,
                )
        await safe_callback_answer(callback, "Обращение закрыто")
        if callback.message:
            await show_support_ticket(callback.message, callback.from_user, int(raw), admin=admin)

    @router.callback_query(F.data.startswith("support:reopen:"))
    async def support_reopen(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        ticket = await db.set_support_status(int(raw), "open", callback.from_user.id, is_admin=True)
        if ticket:
            try:
                await callback.bot.send_message(
                    int(ticket["telegram_id"]),
                    f"Обращение <b>#{int(raw)}</b> снова открыто поддержкой.",
                    reply_markup=support_user_ticket_keyboard(ticket),
                )
            except Exception as exc:
                logger.warning(
                    "Could not notify user about support reopen %s: %s",
                    raw,
                    type(exc).__name__,
                )
        await safe_callback_answer(callback, "Обращение переоткрыто" if ticket else "Обращение не найдено")
        if ticket and callback.message:
            await show_support_ticket(callback.message, callback.from_user, int(raw), admin=True)

    @router.callback_query(F.data.startswith("support:deleteconfirm:"))
    async def support_delete_confirm(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit() or not callback.message:
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("Удалить", callback_data=f"support:delete:{raw}"))
        kb.row(blue_inline_button("Отмена", callback_data=f"support:view:{raw}", premium_icon=False))
        await safe_callback_answer(callback, )
        await send_screen(callback.message, callback.from_user, f"<b>Удалить обращение #{raw}?</b>\n\nЭто действие нельзя отменить.", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("support:delete:"))
    async def support_delete(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректное обращение", show_alert=True)
            return
        deleted = await db.soft_delete_support_ticket(int(raw), callback.from_user.id)
        await safe_callback_answer(callback, "Обращение удалено" if deleted else "Обращение не найдено")
        if deleted and callback.message:
            await show_admin_support(callback.message, callback.from_user)

    @router.callback_query(F.data == "menu:promo")
    async def menu_promo(callback: CallbackQuery) -> None:
        await safe_callback_answer(callback, )
        if not callback.message:
            return
        kb = InlineKeyboardBuilder()
        if config.miniapp_url:
            kb.row(
                blue_inline_button(
                    "Открыть промокоды",
                    web_app=WebAppInfo(url=config.miniapp_url),
                    icon_index=7,
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
        await safe_callback_answer(callback, )
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
        kb.row(blue_inline_button("Пригласить друга", url=share_url))
        kb.row(copy_inline_button("Скопировать ссылку", link))
        add_nav_buttons(kb, back_data="home")
        e = emoji.icon(8, pack=PACK_UI)
        await send_screen(
            callback.message,
            callback.from_user,
            "👥 <b>Реферальная система</b>\n\n"
            "Приглашайте друзей и получайте <b>+1 день VPN</b> за каждого нового пользователя.\n"
            "Максимум — <b>3 дня</b>.\n\n"
            f"Приглашено: <b>{min(stats['invited'], 3)} / 3</b>\n"
            f"Получено: <b>+{stats['rewarded']} дней</b>.",
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
        await safe_callback_answer(callback, )
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
            await safe_callback_answer(callback, 
                "Сначала активируйте подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await safe_callback_answer(callback, 
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, )
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

    @router.message(Command("plans"))
    @router.message(F.text.in_({"Подписка", "💳 Подписка", "💳 Купить VPN", "Купить VPN", "Продлить VPN"}))
    async def plans_message(message: Message) -> None:
        await ensure_actor(message.from_user)
        if not await guard_purchase_message(message, message.from_user):
            return
        e = emoji.icon(0, pack=PACK_NEWS)
        await send_screen(
            message,
            message.from_user,
            "💳 <b>Выберите тариф</b>\n\n"
            "В подписку входит 1 устройство. Оплата — СБП или Telegram Stars.\n"
            "<i>Если подписка уже активна, новый срок прибавится к оставшимся дням — ничего не сгорит.</i>",
            reply_markup=plans_keyboard(config),
        )

    @router.callback_query(F.data == "menu:gift")
    async def gift_menu(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        await safe_callback_answer(callback, )
        if not callback.message:
            return
        await send_screen(
            callback.message,
            callback.from_user,
            "🎁 <b>Подарить подписку</b>\n\nВыберите срок подарка.",
            reply_markup=gift_plans_keyboard(config),
        )

    @router.callback_query(F.data == "plans")
    async def plans_callback(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        await safe_callback_answer(callback, )
        if not callback.message:
            return
        e = emoji.icon(0, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            "💳 <b>Выберите тариф</b>\n\n"
            "В подписку входит 1 устройство. Оплата — СБП или Telegram Stars.\n"
            "<i>Если подписка уже активна, новый срок прибавится к оставшимся дням — ничего не сгорит.</i>",
            reply_markup=plans_keyboard(config),
        )

    @router.callback_query(F.data.startswith("plan:"))
    async def choose_plan(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        plan = PLANS.get(code)
        if not plan:
            await safe_callback_answer(callback, "Тариф не найден", show_alert=True)
            return

        await safe_callback_answer(callback, )
        e = emoji.icon(1, pack=PACK_NEWS)
        await send_screen(
            callback.message,
            callback.from_user,
            f"{e} <b>{plan['name']}</b>"
            + (" · 🔥 <b>Популярный</b>" if code == POPULAR_PLAN_CODE else "")
            + "\n\n"
            + f"📱 Включено устройств — <b>1</b>\n"
            + f"🏦 <b>{plan_price_rub(config, code)} ₽</b> · СБП\n"
            + f"⭐ <b>{plan_price_stars(config, code)} Stars</b>\n"
            + (
                f"💰 Выгода — <b>{plan_savings_rub(code)} ₽</b>\n"
                if plan_savings_rub(code)
                else ""
            )
            + "\n"
            + "<i>Если у вас уже есть подписка, оплаченные дни добавятся к текущему сроку и не сгорят.</i>\n\n"
            + f"Дополнительное устройство — <b>{EXTRA_DEVICE_PRICE_RUB} ₽</b>. "
            f"Максимум — <b>{MAX_DEVICES}</b>.",
            reply_markup=payment_methods_keyboard(config, code),
        )

    @router.callback_query(F.data.startswith("gift:"))
    async def start_gift(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return
        code = callback.data.split(":", 1)[1]
        if code not in PLANS:
            await safe_callback_answer(callback, "Тариф не найден", show_alert=True)
            return

        await db.set_support_session(callback.from_user.id, "gift", payload=code)
        await safe_callback_answer(callback, )
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
        session = await db.get_support_session(message.from_user.id)
        code = str(session.get("payload") or "") if session and session.get("mode") == "gift" else ""
        if code not in PLANS:
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

        await db.clear_support_session(message.from_user.id)
        plan = PLANS[code]

        await send_screen(
            message,
            message.from_user,
            "🎁 <b>Подарочная подписка</b>\n\n"
            f"Получатель — <b>{label}</b>\n"
            f"Тариф — <b>{plan['name']}</b>"
            + (" · 🔥 <b>Популярный</b>" if code == POPULAR_PLAN_CODE else "")
            + "\n"
            + f"🏦 {plan_price_rub(config, code)} ₽\n"
            + f"⭐ {plan_price_stars(config, code)} Stars\n"
            + (f"💰 Выгода — <b>{plan_savings_rub(code)} ₽</b>\n" if plan_savings_rub(code) else "")
            + "\n"
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

    def days_label(days: int) -> str:
        value = abs(int(days))
        if value % 10 == 1 and value % 100 != 11:
            word = "день"
        elif value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
            word = "дня"
        else:
            word = "дней"
        return f"{value} {word}"

    async def notify_subscription_granted(bot, user: dict[str, Any], days: int) -> None:
        telegram_id = int(user["telegram_id"])
        device_limit = max(1, min(MAX_DEVICES, int(user.get("max_devices") or BASE_DEVICES)))
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Подключить VPN",
                callback_data="menu:connect",
                icon_index=2,
            )
        )
        try:
            await bot.send_message(
                chat_id=telegram_id,
                text=(
                    "🎁 <b>Вам подарили подписку MGN VPN "
                    f"на {days_label(days)}</b>\n\n"
                    f"Доступно устройств: <b>до {device_limit}</b>"
                ),
                reply_markup=kb.as_markup(),
            )
        except TelegramForbiddenError:
            logger.info("Could not notify user %s about admin subscription grant: bot is blocked", telegram_id)
        except Exception as exc:
            logger.warning(
                "Could not notify user %s about admin subscription grant: %s",
                telegram_id,
                type(exc).__name__,
            )

    async def grant_paid_device_slot(
        telegram_id: int,
    ) -> dict[str, Any] | None:
        updated = await db.get_user(telegram_id)
        if updated:
            await sync_device_limit(updated)
        return updated

    async def begin_sbp_checkout(
        callback: CallbackQuery,
        code: str,
        target_telegram_id: int,
    ) -> None:
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return
        plan = PLANS.get(code)
        if not plan:
            await safe_callback_answer(callback, "Тариф не найден", show_alert=True)
            return
        if not config.rollypay_enabled:
            await safe_callback_answer(callback, 
                "СБП пока не настроена на этом хостинге.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, )
        await ensure_actor(callback.from_user)

        try:
            target = await db.get_user(target_telegram_id)
        except KeyError:
            await safe_callback_answer(callback, 
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

        local_id = await db.create_sbp_order(
            order_id=order_id, telegram_id=callback.from_user.id,
            target_telegram_id=target_telegram_id, plan_code=code,
            amount_rub=amount, original_amount_rub=amount,
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
            await db.attach_sbp_provider_payment(local_id, payment_id, pay_url)
        except (RollyPayError, KeyError, ValueError):
            await db.set_sbp_status(local_id, "create_failed")
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
            await safe_callback_answer(callback, "Некорректный получатель", show_alert=True)
            return
        await begin_sbp_checkout(callback, parts[1], int(parts[2]))

    @router.callback_query(F.data == "device:sbp")
    async def buy_device_sbp(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await safe_callback_answer(callback, 
                "Сначала активируйте VPN-подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await safe_callback_answer(callback, 
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return
        if not config.rollypay_enabled:
            await safe_callback_answer(callback, 
                "СБП пока не настроена.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, )
        order_id = f"device-{callback.from_user.id}-{uuid4().hex[:12]}"
        local_id = await db.create_sbp_order(
            order_id=order_id, telegram_id=callback.from_user.id,
            target_telegram_id=callback.from_user.id,
            plan_code=DEVICE_PRODUCT_CODE, amount_rub=EXTRA_DEVICE_PRICE_RUB,
            original_amount_rub=EXTRA_DEVICE_PRICE_RUB,
        )
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
            await db.attach_sbp_provider_payment(local_id, payment_id, pay_url)
        except (RollyPayError, KeyError, ValueError):
            await db.set_sbp_status(local_id, "create_failed")
            await safe_callback_answer(callback, 
                "Не удалось создать платёж.",
                show_alert=True,
            )
            return

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button(f"🏦 Оплатить {EXTRA_DEVICE_PRICE_RUB} ₽", url=pay_url))
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
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return

        plan = PLANS.get(code)
        if not plan:
            await safe_callback_answer(callback, "Тариф не найден", show_alert=True)
            return

        try:
            target = await db.get_user(target_telegram_id)
        except KeyError:
            await safe_callback_answer(callback, 
                "Получатель не найден в базе.",
                show_alert=True,
            )
            return

        amount_rub = plan_price_rub(config, code)
        stars = plan_price_stars(config, code)
        intent_id = uuid4().hex
        await db.create_payment_intent(
            intent_id=intent_id,
            buyer_id=callback.from_user.id,
            target_id=target_telegram_id,
            product_code=code,
            original_amount_rub=amount_rub,
            discount_amount_rub=0,
            final_amount_rub=amount_rub,
            currency="XTR",
            currency_amount=stars,
        )
        payload = f"xtr2|{intent_id}"
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
            await safe_callback_answer(callback, 
                "Не удалось создать оплату Stars.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, )
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
            f"Эквивалент тарифа — <b>{amount_rub} ₽</b>\n\n"
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
            await safe_callback_answer(callback, "Некорректный получатель", show_alert=True)
            return
        await begin_stars_checkout(callback, parts[1], int(parts[2]))

    @router.callback_query(F.data == "device:stars")
    async def buy_device_stars(callback: CallbackQuery) -> None:
        if not await guard_purchase_callback(callback):
            return
        if not callback.message:
            return
        user = await ensure_actor(callback.from_user)
        if not is_active(user):
            await safe_callback_answer(callback, 
                "Сначала активируйте VPN-подписку.",
                show_alert=True,
            )
            return
        if int(user.get("max_devices") or BASE_DEVICES) >= MAX_DEVICES:
            await safe_callback_answer(callback, 
                "У вас уже максимум: 5 устройств.",
                show_alert=True,
            )
            return

        stars = extra_device_price_stars()
        intent_id = uuid4().hex
        await db.create_payment_intent(
            intent_id=intent_id,
            buyer_id=callback.from_user.id,
            target_id=callback.from_user.id,
            product_code=DEVICE_PRODUCT_CODE,
            original_amount_rub=EXTRA_DEVICE_PRICE_RUB,
            discount_amount_rub=0,
            final_amount_rub=EXTRA_DEVICE_PRICE_RUB,
            currency="XTR",
            currency_amount=stars,
        )
        payload = f"xtr2|{intent_id}"
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
            await safe_callback_answer(callback, 
                "Не удалось создать оплату Stars.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, )
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
        opened, maintenance_reason = await purchases_open()
        if not opened:
            await pre_checkout_query.answer(
                ok=False,
                error_message=maintenance_reason[:180],
            )
            return
        payload = pre_checkout_query.invoice_payload or ""
        parts = payload.split("|")
        if len(parts) == 2 and parts[0] == "xtr2":
            intent = await db.get_payment_intent(parts[1])
            product_code = str(intent.get("product_code") or "") if intent else ""
            expected_original = (
                EXTRA_DEVICE_PRICE_RUB
                if product_code == DEVICE_PRODUCT_CODE
                else plan_price_rub(config, product_code)
                if product_code in PLANS
                else -1
            )
            valid = bool(
                intent
                and intent["status"] == "created"
                and from_iso(intent.get("expires_at"))
                and from_iso(intent.get("expires_at")) > utcnow()
                and int(intent["buyer_telegram_id"]) == pre_checkout_query.from_user.id
                and intent["currency"] == "XTR"
                and pre_checkout_query.currency == "XTR"
                and int(intent["currency_amount"]) == pre_checkout_query.total_amount
                and int(intent["original_amount_rub"]) == expected_original
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
            intent_target_id = int(intent["target_telegram_id"])
            before_purchase = await db.get_user(intent_target_id)
            try:
                fresh_charge = await db.settle_star_payment(
                    telegram_payment_charge_id=charge_id,
                    buyer_telegram_id=message.from_user.id,
                    target_telegram_id=intent_target_id,
                    plan_code=str(intent["product_code"]),
                    stars=int(payment.total_amount),
                    intent_id=parts[1],
                )
            except ValueError as exc:
                logger.error("Paid Stars intent requires review: %s", type(exc).__name__)
                await message.answer("Платёж получен, но требует проверки. Напишите в поддержку.")
                return
            if fresh_charge:
                product_code = str(intent["product_code"])
                await apply_paid_purchase(
                    message.from_user.id,
                    intent_target_id,
                    product_code,
                    f"stars:{charge_id}",
                )
                await notify_purchase(
                    message.bot,
                    db,
                    config,
                    buyer_id=message.from_user.id,
                    target_id=intent_target_id,
                    product_code=product_code,
                    method="Telegram Stars",
                    amount_text=f"{int(payment.total_amount)} ⭐",
                    purchase_kind=(
                        "Дополнительное устройство"
                        if product_code == DEVICE_PRODUCT_CODE
                        else "Продление" if is_active(before_purchase) else "Новая подписка"
                    ),
                )
            await show_home(message, message.from_user, force_new=True)
            await refresh_main_keyboard(message, message.from_user)
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
            before_purchase = await db.get_user(buyer_id)
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
            fresh = await db.settle_star_payment(
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
                await notify_purchase(
                    message.bot,
                    db,
                    config,
                    buyer_id=buyer_id,
                    target_id=buyer_id,
                    product_code=DEVICE_PRODUCT_CODE,
                    method="Telegram Stars",
                    amount_text=f"{int(payment.total_amount)} ⭐",
                    purchase_kind="Дополнительное устройство",
                )

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

        before_purchase = await db.get_user(target_id)
        charge_id = payment.telegram_payment_charge_id
        fresh = await db.settle_star_payment(
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
            await notify_purchase(
                message.bot,
                db,
                config,
                buyer_id=buyer_id,
                target_id=target_id,
                product_code=code,
                method="Telegram Stars",
                amount_text=f"{int(payment.total_amount)} ⭐",
                purchase_kind="Продление" if is_active(before_purchase) else "Новая подписка",
            )

        if target_id == buyer_id:
            await show_profile(message, message.from_user)
            await refresh_main_keyboard(message, message.from_user)
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
            await safe_callback_answer(callback, "Платёж не найден", show_alert=True)
            return

        try:
            remote = await get_payment(config, payment_id)
        except RollyPayError:
            await safe_callback_answer(callback, 
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
            and remote_amount.is_finite()
            and remote_amount == Decimal(int(local["amount_rub"]))
        )
        if not matches:
            await safe_callback_answer(callback, 
                "Данные платежа не совпали.",
                show_alert=True,
            )
            return

        if status == "paid":
            target_id = int(
                local.get("target_telegram_id")
                or callback.from_user.id
            )
            before_purchase = await db.get_user(target_id)
            try:
                fresh = await db.settle_sbp_payment(payment_id)
            except ValueError as exc:
                logger.error("Paid SBP order requires review: %s", type(exc).__name__)
                await safe_callback_answer(callback, 
                    "Оплата получена, но требует проверки. Напишите в поддержку.",
                    show_alert=True,
                )
                return
            code = str(local["plan_code"])

            if code == DEVICE_PRODUCT_CODE:
                if fresh:
                    updated = await grant_paid_device_slot(
                        callback.from_user.id
                    )
                    if updated is None:
                        await safe_callback_answer(callback, 
                            "Оплата получена, но слот не добавлен. Напишите в поддержку.",
                            show_alert=True,
                        )
                        return
                    await notify_purchase(
                        callback.bot,
                        db,
                        config,
                        buyer_id=callback.from_user.id,
                        target_id=target_id,
                        product_code=code,
                        method="СБП",
                        amount_text=f"{int(local['amount_rub'])} ₽",
                        purchase_kind="Дополнительное устройство",
                        promo_code=str(local.get("promo_code") or "") or None,
                    )
                await safe_callback_answer(callback, "Оплата получена · +1 устройство")
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
                await notify_purchase(
                    callback.bot,
                    db,
                    config,
                    buyer_id=callback.from_user.id,
                    target_id=target_id,
                    product_code=code,
                    method="СБП",
                    amount_text=f"{int(local['amount_rub'])} ₽",
                    purchase_kind="Продление" if is_active(before_purchase) else "Новая подписка",
                    promo_code=str(local.get("promo_code") or "") or None,
                )

            await safe_callback_answer(callback, "Оплата получена")

            if target_id == callback.from_user.id:
                await show_profile(callback.message, callback.from_user)
                await refresh_main_keyboard(callback.message, callback.from_user)
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
            await safe_callback_answer(callback, 
                "Этот платёж больше не активен.",
                show_alert=True,
            )
        else:
            await safe_callback_answer(callback, 
                "Оплата пока не подтверждена.",
                show_alert=True,
            )

    @router.message(F.text.in_({"VPN", "🔗 Подключить VPN", "🔗 Подключиться", "Подключить VPN", "Подключиться"}))
    async def connect(message: Message) -> None:
        user = await ensure_actor(message.from_user)
        if not is_active(user):
            kb = InlineKeyboardBuilder()
            kb.row(blue_inline_button("Купить VPN", callback_data="plans"))
            kb.row(blue_inline_button("Пригласить друзей", callback_data="menu:friends"))
            add_nav_buttons(kb, back_data="home")
            await send_screen(
                message,
                message.from_user,
                "🔗 <b>Подключение VPN</b>\n\nВыберите тариф или пригласите друзей.",
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
            connection_text(user, state, emoji, subscription_url),
            reply_markup=connection_keyboard(subscription_url),
        )

    @router.message(F.text.in_({"📱 Устройства", "Устройства"}))
    async def devices(message: Message) -> None:
        await show_devices_panel(
            message,
            message.from_user,
            back_data="home",
        )

    @router.message(F.text.in_({"👥 Друзья", "👥 Пригласить друга", "Пригласить друга", "Друзья", "Рефералы"}))
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
        kb.row(blue_inline_button("Пригласить друга", url=share_url))
        kb.row(copy_inline_button("Скопировать ссылку", link))
        add_nav_buttons(kb, back_data="home")

        e = emoji.icon(8, pack=PACK_UI)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Пригласить друзей</b>\n\n"
            "За каждого нового друга получаете +1 день MGN VPN.\n\n"
            "1 друг — 1 день\n2 друга — 2 дня\n3 друга — 3 дня\n\n"
            f"Приглашено: <b>{min(stats['invited'], 3)} / 3</b>\n"
            f"Получено: <b>+{stats['rewarded']} дней</b>\n\n"
            f"Ваша ссылка:\n<code>{html.escape(link)}</code>",
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"ℹ️ О сервисе", "О сервисе", "ℹ️ Информация", "Информация"}))
    async def information_screen(message: Message) -> None:
        await ensure_actor(message.from_user)
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("Поддержка", callback_data="menu:support", icon_index=6))
        kb.row(blue_inline_button("Канал", url=config.channel_url, icon_index=8))
        kb.row(
            blue_inline_button(
                "Политика конфиденциальности",
                url=privacy_policy_url(),
                icon_index=11,
            )
        )
        add_nav_buttons(kb, back_data="home")
        await send_screen(
            message,
            message.from_user,
            service_info_text(),
            reply_markup=kb.as_markup(),
        )

    @router.message(F.text.in_({"🆘 Поддержка", "🆘 Помощь", "Поддержка", "Помощь"}))
    async def help_screen(message: Message) -> None:
        e = emoji.icon(6, pack=PACK_NEWS)
        await send_screen(
            message,
            message.from_user,
            f"{e} <b>Поддержка</b>\n\n"
            "Создайте обращение и опишите проблему одним сообщением. "
            "Администратор сможет ответить вам прямо через бота.",
            reply_markup=support_keyboard(),
        )

    def admin_role_label(role: str | None) -> str:
        return {
            "owner": "Владелец",
            "full": "Администратор",
            "limited": "Администратор",
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
        kb.row(
            blue_inline_button("Обращения", callback_data="admin:support", icon_index=6),
        )
        if role:
            kb.row(
                blue_inline_button("📈 Аналитика", callback_data="admin:analytics"),
            )
            kb.row(
                blue_inline_button("🎟 Промокоды", callback_data="admin:bonuses"),
                blue_inline_button("⚙️ Система", callback_data="admin:system"),
            )
            kb.row(
                blue_inline_button("🌐 Серверы VPN", callback_data="admin:servers"),
            )
            kb.row(
                blue_inline_button("📣 Рассылка", callback_data="admin:broadcast:start"),
                blue_inline_button("Публикация в канал", callback_data="admin:ad:start"),
            )
            kb.row(
                blue_inline_button("🎁 Розыгрыши", callback_data="admin:giveaways"),
            )
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
        anonchat = await db.attribution_stats("anonchat_mgn")
        pozor = await db.attribution_stats("pozor_mgn")
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
            f"├ Платных всего: <b>{stats['paid_total']}</b>",
            f"├ Платных активных: <b>{stats['active_paid']}</b>",
            f"├ Активных без личной оплаты: <b>{stats['active_without_self_payment']}</b>",
            f"├ Выдано админом: <b>{stats['admin_granted_total']}</b>",
            f"├ Из них активны: <b>{stats['active_admin_granted']}</b>",
            f"├ Новых за 24 часа: <b>+{stats['new_24h']}</b>",
            f"├ Новых за 7 дней: <b>+{stats['new_7d']}</b>",
            f"└ Новых за 30 дней: <b>+{stats['new_30d']}</b>",
            "",
            "💰 <b>Оплаты</b>",
            f"├ СБП: <b>{pay_status}</b> · {stats['sbp_revenue']} ₽",
            f"└ Stars: <b>{stats['star_revenue']} ⭐</b>",
            "",
            "📣 <b>Anon Chat MGN</b>",
            f"├ Пришло: <b>{anonchat['arrived']}</b>",
            f"├ Купили VPN: <b>{anonchat['buyers']}</b>",
            f"└ Конверсия: <b>{anonchat['conversion']:.1f}%</b>",
            "",
            "📣 <b>Позор МГН</b>",
            f"├ Пришло: <b>{pozor['arrived']}</b>",
            f"├ Купили VPN: <b>{pozor['buyers']}</b>",
            f"└ Конверсия: <b>{pozor['conversion']:.1f}%</b>",
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

    def analytics_bar(value: int | float, maximum: int | float, width: int = 10) -> str:
        maximum = max(float(maximum or 0), 1.0)
        ratio = max(0.0, min(1.0, float(value or 0) / maximum))
        filled = int(round(ratio * width))
        return "█" * filled + "░" * (width - filled)

    async def show_admin_analytics(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
            return
        data = await load_business_analytics(db)
        revenue = data["revenue"]
        purchases = data["purchases"]
        avg = data["average_check_30d"]
        expiring = data["expiring"]
        usage = data["observed_usage"]

        max_rub = max(
            int(revenue["day"]["rub"]),
            int(revenue["week"]["rub"]),
            int(revenue["month"]["rub"]),
            1,
        )
        max_sales = max(
            int(purchases["day"]["new"]) + int(purchases["day"]["renewal"]),
            int(purchases["week"]["new"]) + int(purchases["week"]["renewal"]),
            int(purchases["month"]["new"]) + int(purchases["month"]["renewal"]),
            1,
        )

        lines = [
            "📈 <b>Бизнес-аналитика MGN VPN</b>",
            "",
            f"👥 Пользователей: <b>{data['total_users']}</b> · активных: <b>{data['active_subscriptions']}</b>",
            "",
            "💰 <b>Выручка</b>",
            "<blockquote>",
            f"24ч  <code>{analytics_bar(revenue['day']['rub'], max_rub)}</code> <b>{revenue['day']['rub']} ₽</b> · {revenue['day']['stars']} ⭐",
            f"7д   <code>{analytics_bar(revenue['week']['rub'], max_rub)}</code> <b>{revenue['week']['rub']} ₽</b> · {revenue['week']['stars']} ⭐",
            f"30д  <code>{analytics_bar(revenue['month']['rub'], max_rub)}</code> <b>{revenue['month']['rub']} ₽</b> · {revenue['month']['stars']} ⭐",
            "</blockquote>",
            "",
            "🛒 <b>Покупки: новые / продления</b>",
            "<blockquote>",
            f"24ч  <code>{analytics_bar(purchases['day']['new'] + purchases['day']['renewal'], max_sales)}</code> {purchases['day']['new']} / {purchases['day']['renewal']}",
            f"7д   <code>{analytics_bar(purchases['week']['new'] + purchases['week']['renewal'], max_sales)}</code> {purchases['week']['new']} / {purchases['week']['renewal']}",
            f"30д  <code>{analytics_bar(purchases['month']['new'] + purchases['month']['renewal'], max_sales)}</code> {purchases['month']['new']} / {purchases['month']['renewal']}",
            "</blockquote>",
            "",
            f"🧾 Средний чек 30д: <b>{avg.get('rub', 0):g} ₽</b> · <b>{avg.get('stars', 0):g} ⭐</b>",
            f"🔁 Retention 30д: <b>{data['retention_30d']:.1f}%</b> <i>(выборка {data['retention_sample']})</i>",
            "",
            "⏳ <b>Скоро закончатся</b>",
            f"1 день — <b>{expiring['1']}</b> · 3 дня — <b>{expiring['3']}</b> · 7 дней — <b>{expiring['7']}</b>",
            "",
            "📡 <b>Наблюдаемая активность VPN</b>",
            f"24ч — <b>{usage['day']}</b> · 7д — <b>{usage['week']}</b> · 30д — <b>{usage['month']}</b>",
            "<i>Считаются пользователи, по которым получался ненулевой traffic sample.</i>",
        ]

        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("📣 Источники", callback_data="admin:analytics:sources"),
            blue_inline_button("🎟 Промокоды", callback_data="admin:analytics:promos"),
        )
        kb.row(
            blue_inline_button("🌐 VPN / Support", callback_data="admin:analytics:vpn"),
        )
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:analytics"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(message, actor, "\n".join(lines), reply_markup=kb.as_markup())

    async def show_admin_analytics_sources(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
            return
        data = await load_business_analytics(db)
        sources = list(data.get("sources") or [])
        max_arrived = max([int(item["arrived"]) for item in sources] or [1])

        lines = ["📣 <b>Источники и конверсия</b>", ""]
        priority = {"anonchat_mgn": 0, "pozor_mgn": 1}
        sources.sort(key=lambda item: (priority.get(str(item["source"]), 9), -int(item["arrived"])))
        if not sources:
            lines.append("Данных по источникам пока нет.")
        else:
            for item in sources[:20]:
                source = html.escape(str(item["source"]))
                arrived = int(item["arrived"])
                buyers = int(item["buyers"])
                lines += [
                    f"<b>{source}</b>",
                    f"<code>{analytics_bar(arrived, max_arrived, 12)}</code> {arrived} пришли · {buyers} купили · <b>{float(item['conversion']):.1f}%</b>",
                    "",
                ]
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("⬅️ Аналитика", callback_data="admin:analytics"))
        await send_screen(message, actor, "\n".join(lines).rstrip(), reply_markup=kb.as_markup())

    async def show_admin_analytics_promos(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
            return
        data = await load_business_analytics(db)
        promos = list(data.get("promos") or [])
        lines = ["🎟 <b>Промокоды — аналитика</b>", ""]
        if not promos:
            lines.append("Промокодов пока нет.")
        else:
            for item in promos[:20]:
                state = "🟢" if item.get("active") else "⚪"
                lines += [
                    f"{state} <code>{html.escape(str(item['code']))}</code> · {html.escape(str(item['type']))} {int(item['value'])}",
                    f"использований <b>{int(item['uses'])}</b> · пользователей <b>{int(item['buyers'])}</b> · попыток <b>{int(item['attempts'])}</b> · конверсия <b>{float(item['conversion']):.1f}%</b>",
                    "",
                ]
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("⬅️ Аналитика", callback_data="admin:analytics"))
        await send_screen(message, actor, "\n".join(lines).rstrip(), reply_markup=kb.as_markup())

    async def show_admin_analytics_vpn(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
            return
        data = await load_business_analytics(db)
        country_names = {
            "auto": "⚡ Авто",
            "nl": "🇳🇱 Нидерланды",
            "pk": "🇵🇰 Пакистан",
            "de": "🇩🇪 Германия",
            "pl": "🇵🇱 Польша",
            "fi": "🇫🇮 Финляндия",
            "us": "🇺🇸 США",
            "us2": "🇺🇸 США 2",
            "lt": "🇱🇹 Литва",
            "lv": "🇱🇻 Латвия",
        }
        preferences = list(data.get("country_preferences") or [])
        max_pref = max([int(item["count"]) for item in preferences] or [1])
        lines = [
            "🌐 <b>VPN и поддержка</b>",
            "",
            "⚡ <b>Предпочтения активных пользователей</b>",
        ]
        if preferences:
            for item in preferences:
                code = str(item["country"])
                count = int(item["count"])
                lines.append(
                    f"{country_names.get(code, html.escape(code))}: "
                    f"<code>{analytics_bar(count, max_pref, 10)}</code> <b>{count}</b>"
                )
        else:
            lines.append("Нет данных.")

        lines += ["", "🆘 <b>Обращения по серверу</b>"]
        support_rows = list(data.get("support_servers") or [])
        if support_rows:
            for item in support_rows:
                code = str(item["server_code"])
                lines.append(
                    f"{country_names.get(code, html.escape(code))}: <b>{int(item['count'])}</b>"
                )
        else:
            lines.append("Пока нет обращений с привязкой к серверу.")

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🌐 Состояние серверов", callback_data="admin:servers"))
        kb.row(blue_inline_button("⬅️ Аналитика", callback_data="admin:analytics"))
        await send_screen(message, actor, "\n".join(lines), reply_markup=kb.as_markup())

    async def show_admin_support(message: Message, actor, status: str = "all", page: int = 0) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return
        status = status if status in {"all", "open", "answered", "closed"} else "all"
        tickets, total = await db.list_support_tickets(
            page=page, page_size=10, status=None if status == "all" else status
        )
        pages = max(1, (total + 9) // 10)
        page = min(max(0, page), pages - 1)
        if not tickets and total:
            tickets, total = await db.list_support_tickets(
                page=page, page_size=10, status=None if status == "all" else status
            )
        status_label = {
            "all": "все", "open": "открытые", "answered": "с ответом", "closed": "закрытые"
        }[status]
        kb = InlineKeyboardBuilder()
        lines = [
            "<b>Обращения в поддержку</b>",
            f"Фильтр: <b>{status_label}</b> · страница {page + 1}/{pages}",
            "",
        ]
        if not tickets:
            lines.append("Обращений пока нет.")
        else:
            for ticket in tickets:
                ticket_id = int(ticket["id"])
                created = from_iso(ticket.get("created_at"))
                created_text = (
                    created.astimezone(config.display_tz).strftime("%d.%m %H:%M")
                    if created
                    else "—"
                )
                username = (
                    f"@{html.escape(str(ticket.get('username')))}"
                    if ticket.get("username")
                    else html.escape(str(ticket.get("first_name") or "без username"))
                )
                ticket_status = {"open": "открыто", "answered": "с ответом", "closed": "закрыто"}.get(str(ticket.get("status")), "открыто")
                preview = html.escape(str(ticket.get("message") or "")[:120])
                lines += [
                    f"<b>#{ticket_id}</b> · {ticket_status} · {created_text}",
                    f"{username} · <code>{int(ticket['telegram_id'])}</code>",
                    preview,
                    "",
                ]
                kb.row(
                    blue_inline_button(
                        f"Открыть #{ticket_id}",
                        callback_data=f"support:view:{ticket_id}",
                        icon_index=6,
                    ),
                    blue_inline_button(
                        "Удалить",
                        callback_data=f"support:deleteconfirm:{ticket_id}",
                    ),
                )
        kb.row(
            blue_inline_button("Все", callback_data="admin:support:all:0"),
            blue_inline_button("Открытые", callback_data="admin:support:open:0"),
        )
        kb.row(
            blue_inline_button("С ответом", callback_data="admin:support:answered:0"),
            blue_inline_button("Закрытые", callback_data="admin:support:closed:0"),
        )
        nav = []
        if page > 0:
            nav.append(blue_inline_button("←", callback_data=f"admin:support:{status}:{page - 1}"))
        if page + 1 < pages:
            nav.append(blue_inline_button("→", callback_data=f"admin:support:{status}:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.row(blue_inline_button("Админка", callback_data="admin:home"))
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_stats(message: Message, actor) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return
        stats = await db.admin_overview()
        anonchat = await db.attribution_stats("anonchat_mgn")
        pozor = await db.attribution_stats("pozor_mgn")
        utm_sources = await db.list_attribution_stats()
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
            f"├ Платные всего — <b>{stats['paid_total']}</b>",
            f"├ Платные активные — <b>{stats['active_paid']}</b>",
            f"├ Активные без личной оплаты — <b>{stats['active_without_self_payment']}</b>",
            f"├ Выданы админом — <b>{stats['admin_granted_total']}</b>",
            f"├ Выданные активные — <b>{stats['active_admin_granted']}</b>",
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
            "📣 <b>Источник: Anon Chat MGN</b>",
            f"├ Перешли и запустили бота — <b>{anonchat['arrived']}</b>",
            f"├ Совершили оплату — <b>{anonchat['buyers']}</b>",
            f"└ Конверсия в покупку — <b>{anonchat['conversion']:.1f}%</b>",
            "",
            "📣 <b>Источник: Позор МГН</b>",
            f"├ Перешли и запустили бота — <b>{pozor['arrived']}</b>",
            f"├ Совершили оплату — <b>{pozor['buyers']}</b>",
            f"└ Конверсия в покупку — <b>{pozor['conversion']:.1f}%</b>",
            "",
        ]

        if utm_sources:
            lines += ["🏷 <b>UTM-метки</b>"]
            for source in utm_sources:
                label = html.escape(str(source["source"]).removeprefix("utm_"))
                lines.append(
                    f"• <code>{label}</code>: <b>{source['arrived']}</b> пришли · "
                    f"<b>{source['buyers']}</b> купили · {source['conversion']:.1f}%"
                )
            lines.append("")

        lines.append("🕒 <b>Последние регистрации</b>")

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

    async def show_admin_users(message: Message, actor, page: int = 0) -> None:
        role = await get_admin_role(actor.id)
        if not role:
            return
        page_size = 10
        users, total = await db.list_users_page(page, page_size)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(0, page), pages - 1)
        if not users and total:
            users, total = await db.list_users_page(page, page_size)
        kb = InlineKeyboardBuilder()
        lines = [
            "👥 <b>Пользователи</b>",
            "",
            f"Страница {page + 1} из {pages} · всего {total}",
            "",
        ]

        if not users:
            lines.append("Пользователей пока нет.")
        else:
            for item in users:
                uid = int(item["telegram_id"])
                raw_username = str(item.get("username") or "").lstrip("@")
                username = f"@{raw_username[:20]}" if raw_username else "без username"
                first_name = str(item.get("first_name") or "Без имени")[:24]
                subscription_until = from_iso(item.get("subscription_until"))
                active = bool(subscription_until and subscription_until > utcnow())
                status_icon = "🟢" if active else "🔴"
                status = (
                    f"активна до {subscription_until.astimezone(config.display_tz).strftime('%d.%m.%Y')}"
                    if active and subscription_until else "нет подписки"
                )
                lines.append(
                    f"{status_icon} <b>{html.escape(first_name)}</b> · {html.escape(username)}\n"
                    f"<code>{uid}</code> · {status}"
                )
                kb.row(
                    blue_inline_button(
                        f"{status_icon} {username[:22]}",
                        callback_data=f"admin:user:{uid}",
                    )
                )

        nav = []
        if page > 0:
            nav.append(blue_inline_button("←", callback_data=f"admin:users:{page - 1}"))
        if page + 1 < pages:
            nav.append(blue_inline_button("→", callback_data=f"admin:users:{page + 1}"))
        if nav:
            kb.row(*nav)
        kb.row(blue_inline_button("Поиск", callback_data="admin:usersearch"))
        kb.row(blue_inline_button("Обновить", callback_data=f"admin:users:{page}"))
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
        last_payment = await db.get_last_payment(telegram_id)
        until = from_iso(user.get("subscription_until"))
        remaining_days = max(0, int(((until - utcnow()).total_seconds() + 86399) // 86400)) if until else 0
        device_count = 0
        if active and getattr(provider, "service_ready", True):
            try:
                state = await asyncio.wait_for(provider.get_state(user), 5.0)
                device_count = len(state.devices)
            except Exception:
                pass

        kb = InlineKeyboardBuilder()

        # Every admin role may issue a subscription. Advanced subscription
        # management remains restricted to owner/full admins.
        kb.row(
            blue_inline_button("Выдать подписку", callback_data=f"admin:grantmenu:{telegram_id}"),
        )

        if actor_role:
            kb.row(
                blue_inline_button("Добавить дни", callback_data=f"admin:daysmenu:{telegram_id}:add"),
                blue_inline_button("Списать дни", callback_data=f"admin:daysmenu:{telegram_id}:sub"),
            )
            kb.row(
                blue_inline_button("Устройства", callback_data=f"admin:devicemenu:{telegram_id}"),
            )
            kb.row(blue_inline_button("Отключить подписку", callback_data=f"admin:revokeconfirm:{telegram_id}"))

        if actor_role and telegram_id not in config.admin_ids:
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
            f"👤 <b>{html.escape(user.get('first_name') or 'Без имени')}</b>",
            f"Username — <b>{username}</b>",
            f"Telegram ID — <code>{telegram_id}</code>",
            "",
            f"Пришёл — <b>{format_joined(user.get('created_at'))}</b>",
            f"Админ-доступ — <b>{admin_role_label(target_role)}</b>",
            f"Подписка — <b>{'🟢 активна' if active else '🔴 нет активной'}</b>",
            f"Тариф — <b>{html.escape(user.get('plan_name') or '—')}</b>",
            f"До — <b>{format_until(user, config) if active else '—'}</b>",
            f"Осталось — <b>{remaining_days} дней</b>",
            f"Устройства — <b>{device_count} / {int(user.get('max_devices') or BASE_DEVICES)}</b>",
            f"Лимит устройств — <b>{int(user.get('max_devices') or BASE_DEVICES)}</b>",
            f"Trial — <b>{'Использован' if user.get('trial_used') else 'Не использован'}</b>",
            f"Приглашено — <b>{referrals}</b>",
            "Последний платёж — <b>нет</b>" if not last_payment else (
                f"Последний платёж — <b>{int(last_payment['amount'])} {last_payment['currency']}</b>\n"
                f"Способ — <b>{html.escape(last_payment['method'])}</b>"
            ),
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
        maintenance = await db.maintenance_state()
        maintenance_enabled = bool(maintenance.get("enabled"))

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🌐 Серверы VPN", callback_data="admin:servers"))
        kb.row(
            blue_inline_button(
                "🟢 Выключить техработы" if maintenance_enabled else "🟠 Включить техработы",
                callback_data=(
                    "admin:maintenance:off"
                    if maintenance_enabled
                    else "admin:maintenance:on"
                ),
            )
        )
        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:system"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))

        text = (
            "⚙️ <b>Система</b>\n\n"
            f"{vpn_ready} VPN-система — <b>{'доступна' if getattr(provider, 'service_ready', True) else 'недоступна'}</b>\n"
            f"🔗 Реальные подключения — <b>{'готовы' if getattr(provider, 'service_ready', True) else 'ожидают серверы'}</b>\n"
            f"🌐 Основной сервер — <b>{html.escape(config.vpn_server_name)}</b>\n"
            f"💳 RollyPay — <b>{rolly}</b>\n"
            f"🧾 Режим оплаты — <b>{rolly_mode}</b>\n"
            f"🛠 Техработы — <b>{'ВКЛЮЧЕНЫ' if maintenance_enabled else 'выключены'}</b>\n\n"
            "<i>Техработы блокируют только новые покупки. Уже активные VPN-подписки и /sub продолжают работать.</i>\n\n"
            "<i>Основной сервер — это только базовая H1-нода. "
            "Список стран федерации смотрите в «Серверы VPN».</i>"
        )
        await send_screen(message, actor, text, reply_markup=kb.as_markup())

    async def show_admin_servers(message: Message, actor) -> None:
        if not await has_full_admin_access(actor.id):
            return

        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🔄 Проверить ещё раз", callback_data="admin:servers"))
        kb.row(blue_inline_button("⬅️ Система", callback_data="admin:system"))

        try:
            sample_users = await db.list_active_users_for_vpn_sync(limit=1)
            sample_user = sample_users[0] if sample_users else None
            report = await asyncio.wait_for(
                provider.server_diagnostics(sample_user),
                timeout=12.0,
            )
        except asyncio.TimeoutError:
            await send_screen(
                message,
                actor,
                "🌐 <b>Серверы VPN</b>\n\n"
                "⚠️ Диагностика H1 не успела завершиться за 12 секунд.\n"
                "Пользовательские конфиги при этом не изменялись.",
                reply_markup=kb.as_markup(),
            )
            return
        except Exception as exc:
            logger.exception("Admin VPN server diagnostics failed: %s", type(exc).__name__)
            await send_screen(
                message,
                actor,
                "🌐 <b>Серверы VPN</b>\n\n"
                "❌ Не удалось получить список серверов.\n"
                f"Ошибка: <code>{html.escape(type(exc).__name__)}</code>\n\n"
                "<i>Проверка только читает H1 и ничего не меняет у пользователей.</i>",
                reply_markup=kb.as_markup(),
            )
            return

        servers = list(report.get("servers") or [])
        available = sum(1 for item in servers if item.get("available"))
        configured = sum(1 for item in servers if item.get("configured") or item.get("available"))
        unknown = len(servers) - configured
        remote_count = sum(1 for item in servers if item.get("kind") == "federation")

        lines = [
            "🌐 <b>Серверы MGN VPN</b>",
            "",
            f"Провайдер: <b>{html.escape(str(report.get('provider') or 'VPN'))}</b>",
            f"Всего серверов: <b>{len(servers)}</b>",
            f"Проверено онлайн: <b>{available}</b> · в подписке/настроено: <b>{configured}</b> · без данных: <b>{unknown}</b>",
            f"Федеративных H1-узлов: <b>{remote_count}</b>",
            "",
            "🖥 <b>Состояние серверов</b>",
        ]

        for index, item in enumerate(servers, start=1):
            ok = bool(item.get("available"))
            configured = bool(item.get("configured"))
            icon = "✅" if ok else ("⚠️" if configured else "➖")
            name = html.escape(str(item.get("name") or f"Сервер {index}"))
            kind = "основной" if item.get("kind") == "main" else "federation"
            latency = item.get("latency_ms")
            latency_text = f" · {int(latency)} мс" if isinstance(latency, (int, float)) else ""
            lines.append(f"{icon} <b>{name}</b>{latency_text}")

            details: list[str] = [kind]
            host = str(item.get("host") or "").strip()
            if host:
                details.append(html.escape(host))
            node_id = str(item.get("id") or "").strip()
            if node_id and node_id != "main":
                safe_id = node_id if len(node_id) <= 28 else node_id[:12] + "…" + node_id[-6:]
                details.append(f"ID <code>{html.escape(safe_id)}</code>")
            proxy_kind = str(item.get("proxy_kind") or "").strip()
            if proxy_kind and proxy_kind not in {"direct", ""}:
                details.append(html.escape(proxy_kind))
            check = str(item.get("check") or "").strip()
            if check:
                details.append(f"проверка: {html.escape(check)}")
            lines.append("   " + " · ".join(details))

            error = str(item.get("error") or "").strip()
            if not ok:
                if configured and error == "probe_unverified":
                    lines.append("   ↳ конфиг есть в подписке · ping с BotHost не подтверждён")
                elif configured and error:
                    lines.append(f"   ↳ конфиг есть · проверка: <code>{html.escape(error)}</code>")
                elif not configured:
                    lines.append("   ↳ нет данных для проверки, сервер не скрыт")

        sources = dict(report.get("sources") or {})
        if sources:
            lines += ["", "🔗 <b>H1 federation discovery</b>"]
            for path in ("/fed/link", "/fed/registry", "/fed/lagg"):
                item = sources.get(path)
                if not isinstance(item, dict):
                    continue
                icon = "✅" if item.get("available") else "❌"
                count = int(item.get("count") or 0)
                line = f"{icon} <code>{html.escape(path)}</code> — <b>{count}</b>"
                error = str(item.get("error") or "").strip()
                if error and not item.get("available"):
                    line += f" · {html.escape(error)}"
                lines.append(line)

        if remote_count == 0:
            lines += [
                "",
                "⚠️ <b>H1 сейчас не отдал удалённые узлы.</b>",
                "Каталог MGN VPN всё равно показывает все серверы; "
                "недоступность влияет только на статус, а не скрывает страну.",
            ]
        if not report.get("discovery_ok", True):
            error = str(report.get("discovery_error") or "federation unavailable")
            lines += [
                "",
                "❌ <b>Не удалось нормально прочитать federation H1.</b>",
                f"<code>{html.escape(error)}</code>",
            ]

        lines += [
            "",
            "<i>Проверка read-only: пользователей, UUID, подписки и серверные настройки не меняет.</i>",
        ]

        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    async def show_admin_admins(message: Message, actor) -> None:
        if not await has_admin_access(actor.id):
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
            "Все выданные администраторы имеют полный операционный доступ.",
            "Владельцы из конфигурации нельзя удалить из панели.",
        ]

        kb.row(blue_inline_button("🔄 Обновить", callback_data="admin:admins"))
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(
            message,
            actor,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    def ad_draft(session: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(str(session.get("payload") or "{}"))
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def ad_markup(draft: dict[str, Any]) -> Any | None:
        url = str(draft.get("button_url") or "").strip()
        if not url:
            return None
        kwargs: dict[str, Any] = {
            "text": str(draft.get("button_text") or "Открыть")[:64],
            "url": url,
        }
        custom_id = str(draft.get("button_emoji_id") or "").strip()
        if custom_id:
            kwargs["icon_custom_emoji_id"] = custom_id
        return InlineKeyboardBuilder().row(InlineKeyboardButton(**kwargs)).as_markup()

    async def send_ad_post(bot, chat_id: int | str, draft: dict[str, Any]):
        text = str(draft.get("text_html") or "").strip()
        markup = ad_markup(draft)
        photo = str(draft.get("photo_file_id") or "").strip()
        if photo:
            return await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                reply_markup=markup,
            )
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=markup,
        )

    async def broadcast_ad_post(bot, draft: dict[str, Any]) -> tuple[int, int]:
        sent = 0
        failed = 0
        page = 0
        page_size = 20
        while True:
            users, total = await db.list_users_page(page, page_size)
            if not users:
                break
            for user in users:
                try:
                    await send_ad_post(bot, int(user["telegram_id"]), draft)
                    sent += 1
                except (TelegramForbiddenError, TelegramBadRequest):
                    failed += 1
                except Exception as exc:
                    failed += 1
                    logger.warning(
                        "Broadcast delivery failed for %s: %s",
                        user.get("telegram_id"),
                        type(exc).__name__,
                    )
                await asyncio.sleep(0.05)
            page += 1
            if page * page_size >= total:
                break
        return sent, failed

    def normalize_ad_url(raw: str) -> str | None:
        value = str(raw or "").strip()
        if value == "-":
            return ""
        parsed = urlsplit(value)
        if parsed.scheme in {"https", "http", "tg"} and parsed.netloc:
            return value
        return None

    def normalize_channel(raw: str) -> int | str | None:
        value = str(raw or "").strip()
        if re.fullmatch(r"-100\d{6,}", value):
            return int(value)
        match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})", value)
        if match:
            return "@" + match.group(1)
        match = re.fullmatch(r"https?://t\.me/([A-Za-z0-9_]{5,32})/?", value)
        if match:
            return "@" + match.group(1)
        return None

    def giveaway_draft(session: dict[str, Any]) -> dict[str, Any]:
        try:
            value = json.loads(str(session.get("payload") or "{}"))
        except (TypeError, ValueError):
            value = {}
        return value if isinstance(value, dict) else {}

    def parse_giveaway_channels(raw: str) -> list[int | str] | None:
        parts = [part for part in re.split(r"[\s,;]+", str(raw or "").strip()) if part]
        if not parts or len(parts) > 10:
            return None
        result: list[int | str] = []
        seen: set[str] = set()
        for part in parts:
            channel = normalize_channel(part)
            if channel is None:
                return None
            key = str(channel).lower()
            if key not in seen:
                seen.add(key)
                result.append(channel)
        return result or None

    def giveaway_end_label(item: dict[str, Any]) -> str:
        if item.get("end_mode") == "time":
            ends = from_iso(item.get("ends_at"))
            if ends:
                return ends.astimezone(config.display_tz).strftime("%d.%m.%Y · %H:%M")
            return "по времени"
        return f"{int(item.get('participant_limit') or 0)} участников"

    async def show_admin_giveaways(message: Message, actor) -> None:
        if not await has_full_admin_access(actor.id):
            return
        items = await db.list_giveaways(12)
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("➕ Новый розыгрыш", callback_data="admin:giveaway:new"))
        lines = [
            "🎁 <b>Розыгрыши MGN VPN</b>",
            "",
            "Розыгрыши хранятся в базе и продолжают работать после перезапуска бота.",
            "",
        ]
        if not items:
            lines.append("Розыгрышей пока нет.")
        else:
            status_labels = {
                "active": "🟢 идёт",
                "finishing": "🟡 подводим итоги",
                "finished": "✅ завершён",
                "cancelled": "⚪ отменён",
            }
            for item in items:
                giveaway_id = int(item["id"])
                lines.append(
                    f"<b>#{giveaway_id}</b> · {status_labels.get(str(item.get('status')), str(item.get('status')))}\n"
                    f"Призов: <b>{int(item.get('winners_count') or 0)}</b> × "
                    f"<b>{int(item.get('prize_days') or 0)} дн.</b> · "
                    f"участников: <b>{int(item.get('participant_count') or 0)}</b>\n"
                    f"Финиш: <b>{html.escape(giveaway_end_label(item))}</b>"
                )
                kb.row(
                    blue_inline_button(
                        f"Открыть #{giveaway_id}",
                        callback_data=f"admin:giveaway:view:{giveaway_id}",
                    )
                )
        kb.row(blue_inline_button("⬅️ Админка", callback_data="admin:home"))
        await send_screen(message, actor, "\n\n".join(lines), reply_markup=kb.as_markup())

    async def show_admin_giveaway(message: Message, actor, giveaway_id: int) -> None:
        if not await has_full_admin_access(actor.id):
            return
        item = await db.get_giveaway(giveaway_id)
        if not item:
            await message.answer("Розыгрыш не найден.")
            return
        posts = await db.list_giveaway_posts(giveaway_id)
        winners = await db.get_giveaway_winners(giveaway_id)
        rerolls = await db.list_giveaway_rerolls(giveaway_id)
        active_rerolls = [row for row in rerolls if not row.get("undone_at")]
        latest_reroll = active_rerolls[-1] if active_rerolls else None
        can_undo_reroll = False
        if latest_reroll:
            rerolled_at = from_iso(latest_reroll.get("created_at"))
            can_undo_reroll = bool(
                rerolled_at
                and rerolled_at >= utcnow() - timedelta(minutes=10)
            )
        status_labels = {
            "active": "🟢 Идёт",
            "finishing": "🟡 Подводятся итоги",
            "finished": "✅ Завершён",
            "cancelled": "⚪ Отменён",
        }
        lines = [
            f"🎁 <b>Розыгрыш #{giveaway_id}</b>",
            "",
            f"Статус: <b>{status_labels.get(str(item.get('status')), str(item.get('status')))}</b>",
            f"Участников: <b>{int(item.get('participant_count') or 0)}</b>",
            f"Победителей: <b>{int(item.get('winners_count') or 0)}</b>",
            f"Приз: <b>{int(item.get('prize_days') or 0)} дней MGN VPN</b>",
            f"Условие завершения: <b>{html.escape(giveaway_end_label(item))}</b>",
            f"Публикаций: <b>{len(posts)}</b>",
            f"Перевыборов: <b>{len(active_rerolls)}</b>"
            + (f" · отменено: <b>{len(rerolls) - len(active_rerolls)}</b>" if len(rerolls) != len(active_rerolls) else ""),
        ]
        if winners:
            lines += ["", "🏆 <b>Победители</b>"]
            for index, winner in enumerate(winners, start=1):
                username = str(winner.get("username") or "").strip()
                label = f"@{username}" if username else f"Победитель #{index}"
                lines.append(f"{index}. {html.escape(label)}")
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                f"👥 Участники ({int(item.get('participant_count') or 0)})",
                callback_data=f"admin:giveaway:participants:{giveaway_id}:0",
            )
        )
        if int(item.get("participant_count") or 0) > 0:
            kb.row(
                blue_inline_button(
                    "📥 CSV участников",
                    callback_data=f"admin:giveaway:export:{giveaway_id}",
                )
            )
        if (
            str(item.get("status")) == "finished"
            and winners
            and int(item.get("participant_count") or 0) > len(winners)
        ):
            kb.row(
                blue_inline_button(
                    "🔄 Перевыбрать победителя",
                    callback_data=f"admin:giveaway:reroll:{giveaway_id}",
                )
            )
            if can_undo_reroll:
                kb.row(
                    blue_inline_button(
                        "↩️ Отменить последний перевыбор · 10 мин",
                        callback_data=f"admin:giveaway:rerollundo:{giveaway_id}",
                    )
                )
        if str(item.get("status")) == "active":
            kb.row(
                blue_inline_button(
                    "🏁 Завершить сейчас",
                    callback_data=f"admin:giveaway:finish:{giveaway_id}",
                )
            )
            kb.row(
                blue_inline_button(
                    "⚪ Отменить розыгрыш",
                    callback_data=f"admin:giveaway:cancelrunconfirm:{giveaway_id}",
                )
            )
        if str(item.get("status")) != "finishing":
            kb.row(
                blue_inline_button(
                    "🗑 Удалить розыгрыш",
                    callback_data=f"admin:giveaway:deleteconfirm:{giveaway_id}",
                )
            )
        kb.row(blue_inline_button("⬅️ Розыгрыши", callback_data="admin:giveaways"))
        await send_screen(message, actor, "\n".join(lines), reply_markup=kb.as_markup())

    async def ask_giveaway_winners(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "giveaway_winners", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("1", callback_data="admin:giveaway:winners:1"),
            blue_inline_button("2", callback_data="admin:giveaway:winners:2"),
            blue_inline_button("3", callback_data="admin:giveaway:winners:3"),
            blue_inline_button("5", callback_data="admin:giveaway:winners:5"),
        )
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await message.answer(
            "<b>Новый розыгрыш · 3/7</b>\n\n"
            "Сколько подписок разыграть? Выберите кнопку или отправьте число от 1 до 10.",
            reply_markup=kb.as_markup(),
        )

    async def ask_giveaway_days(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "giveaway_days", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("30 дней", callback_data="admin:giveaway:days:30"),
            blue_inline_button("90 дней", callback_data="admin:giveaway:days:90"),
        )
        kb.row(
            blue_inline_button("180 дней", callback_data="admin:giveaway:days:180"),
            blue_inline_button("365 дней", callback_data="admin:giveaway:days:365"),
        )
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await message.answer(
            "<b>Новый розыгрыш · 4/7</b>\n\n"
            "На какой срок выдать подписку каждому победителю? Можно отправить своё количество дней.",
            reply_markup=kb.as_markup(),
        )

    async def ask_giveaway_end_mode(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "giveaway_end_value", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button("📅 По дате и времени", callback_data="admin:giveaway:end:time"),
        )
        kb.row(
            blue_inline_button("👥 По числу участников", callback_data="admin:giveaway:end:participants"),
        )
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await message.answer(
            "<b>Новый розыгрыш · 5/7</b>\n\nКогда завершить розыгрыш?",
            reply_markup=kb.as_markup(),
        )

    async def ask_giveaway_channels(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "giveaway_channels", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        if normalize_channel(config.channel_url) is not None:
            kb.row(
                blue_inline_button(
                    "Основной канал",
                    callback_data="admin:giveaway:channel:main",
                )
            )
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await message.answer(
            "<b>Новый розыгрыш · 7/7</b>\n\n"
            "Выберите основной канал или отправьте один/несколько каналов через пробел, запятую или с новой строки.\n"
            "Поддерживаются <code>@username</code>, ссылка <code>t.me/...</code> и ID <code>-100…</code>.",
            reply_markup=kb.as_markup(),
        )

    async def show_giveaway_preview(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "giveaway_confirm", payload=json.dumps(draft))
        preview = {
            **draft,
            "id": 0,
            "status": "active",
            "participant_count": 0,
        }
        channels = ", ".join(html.escape(str(value)) for value in draft.get("channels") or [])
        await message.answer(
            "<b>Предпросмотр розыгрыша</b>\n\n"
            f"Каналы: <code>{channels}</code>\n"
            f"Призов: <b>{int(draft.get('winners_count') or 0)}</b>\n"
            f"Срок каждой подписки: <b>{int(draft.get('prize_days') or 0)} дней</b>\n"
            f"Завершение: <b>{html.escape(giveaway_end_label(preview))}</b>"
        )
        preview_text = render_giveaway_post(
            preview,
            participant_count=0,
            display_tz=config.display_tz,
        )
        if draft.get("photo_file_id"):
            await message.answer_photo(
                str(draft["photo_file_id"]),
                caption=preview_text,
            )
        else:
            await message.answer(preview_text)
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("🚀 Запустить", callback_data="admin:giveaway:send"))
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await message.answer("Всё готово. Запустить розыгрыш?", reply_markup=kb.as_markup())

    def first_custom_emoji_id(message: Message) -> str:
        for entity in message.entities or []:
            if str(entity.type) in {"custom_emoji", "MessageEntityType.CUSTOM_EMOJI"}:
                return str(entity.custom_emoji_id or "")
        return ""

    async def ask_ad_url(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "ad_url", payload=json.dumps(draft))
        await message.answer(
            "<b>Ссылка кнопки</b>\n\nОтправьте http(s):// или tg:// ссылку. Отправьте <code>-</code>, если кнопка не нужна."
        )

    async def finish_ad_target(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        if draft.get("broadcast_only"):
            draft["channel"] = "all_users"
            await show_ad_preview(message, actor_id, draft)
            return
        await ask_ad_channel(message, actor_id, draft)

    async def ask_ad_channel(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "ad_channel", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Всем пользователям бота",
                callback_data="admin:ad:channel:users",
            )
        )
        if normalize_channel(config.channel_url) is not None:
            kb.row(blue_inline_button("Основной канал", callback_data="admin:ad:channel:main"))
        kb.row(blue_inline_button("Отмена", callback_data="admin:ad:cancel", premium_icon=False))
        await message.answer(
            "<b>Куда отправить</b>\n\nВыберите рассылку всем пользователям, основной канал или отправьте @username / ID вида <code>-100…</code>.",
            reply_markup=kb.as_markup(),
        )

    async def show_ad_preview(message: Message, actor_id: int, draft: dict[str, Any]) -> None:
        await db.set_support_session(actor_id, "ad_confirm", payload=json.dumps(draft))
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("Опубликовать", callback_data="admin:ad:send"))
        kb.row(blue_inline_button("Отмена", callback_data="admin:ad:cancel", premium_icon=False))
        target = (
            "все пользователи бота"
            if draft.get("channel") == "all_users"
            else str(draft.get("channel") or "—")
        )
        await message.answer(
            f"<b>Предпросмотр рассылки</b>\nПолучатели: <code>{html.escape(target)}</code>"
        )
        await send_ad_post(message.bot, actor_id, draft)
        await message.answer("Проверьте публикацию и подтвердите отправку.", reply_markup=kb.as_markup())

    @router.message(Command("admin"))
    async def admin_panel(message: Message) -> None:
        if not await has_admin_access(message.from_user.id):
            return
        await show_admin(message, message.from_user)

    @router.message(F.text.in_({"Админ-панель", "🛡 Админ-панель"}))
    async def admin_panel_button(message: Message) -> None:
        if not await has_admin_access(message.from_user.id):
            return
        await show_admin(message, message.from_user)

    @router.callback_query(F.data == "admin:home")
    async def admin_home(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:stats")
    async def admin_stats_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin_stats(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:giveaways")
    async def admin_giveaways_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нужна полная админка.", show_alert=True)
            return
        await safe_callback_answer(callback)
        if callback.message:
            await show_admin_giveaways(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:giveaway:new")
    async def admin_giveaway_new(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нужна полная админка.", show_alert=True)
            return
        await db.set_support_session(callback.from_user.id, "giveaway_text", payload="{}")
        await safe_callback_answer(callback)
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
        await callback.message.answer(
            "<b>Новый розыгрыш · 1/7</b>\n\n"
            "Отправьте текст поста. Форматирование сохраняется. Максимум 650 символов.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data == "admin:giveaway:skip-photo")
    async def admin_giveaway_skip_photo(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_photo":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        await safe_callback_answer(callback)
        await ask_giveaway_winners(callback.message, callback.from_user.id, giveaway_draft(session))

    @router.callback_query(F.data.regexp(r"^admin:giveaway:winners:\d+$"))
    async def admin_giveaway_winners(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_winners":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        value = int(callback.data.rsplit(":", 1)[-1])
        if not 1 <= value <= 10:
            await safe_callback_answer(callback, "Допустимо от 1 до 10.", show_alert=True)
            return
        draft = giveaway_draft(session)
        draft["winners_count"] = value
        await safe_callback_answer(callback)
        await ask_giveaway_days(callback.message, callback.from_user.id, draft)

    @router.callback_query(F.data.regexp(r"^admin:giveaway:days:\d+$"))
    async def admin_giveaway_days(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_days":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        value = int(callback.data.rsplit(":", 1)[-1])
        if not 1 <= value <= 3650:
            await safe_callback_answer(callback, "Некорректный срок.", show_alert=True)
            return
        draft = giveaway_draft(session)
        draft["prize_days"] = value
        await safe_callback_answer(callback)
        await ask_giveaway_end_mode(callback.message, callback.from_user.id, draft)

    @router.callback_query(F.data == "admin:giveaway:end:time")
    async def admin_giveaway_end_time(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_end_value":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        draft = giveaway_draft(session)
        draft["end_mode"] = "time"
        await db.set_support_session(callback.from_user.id, "giveaway_end_value", payload=json.dumps(draft))
        await safe_callback_answer(callback)
        await callback.message.answer(
            "<b>Новый розыгрыш · 6/7</b>\n\n"
            "Отправьте дату и время окончания по времени сервиса в формате:\n"
            "<code>30.09.2026 21:30</code>"
        )

    @router.callback_query(F.data == "admin:giveaway:end:participants")
    async def admin_giveaway_end_participants(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_end_value":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        draft = giveaway_draft(session)
        draft["end_mode"] = "participants"
        await db.set_support_session(callback.from_user.id, "giveaway_end_value", payload=json.dumps(draft))
        await safe_callback_answer(callback)
        await callback.message.answer(
            "<b>Новый розыгрыш · 6/7</b>\n\n"
            "Отправьте количество участников, после которого розыгрыш завершится автоматически."
        )

    @router.callback_query(F.data == "admin:giveaway:channel:main")
    async def admin_giveaway_main_channel(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        channel = normalize_channel(config.channel_url)
        if not session or session.get("mode") != "giveaway_channels" or channel is None:
            await safe_callback_answer(callback, "Основной канал не настроен.", show_alert=True)
            return
        draft = giveaway_draft(session)
        draft["channels"] = [channel]
        await safe_callback_answer(callback)
        await show_giveaway_preview(callback.message, callback.from_user.id, draft)

    @router.callback_query(F.data == "admin:giveaway:send")
    async def admin_giveaway_send(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "giveaway_confirm":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        draft = giveaway_draft(session)
        channels = list(draft.get("channels") or [])
        if not channels:
            await safe_callback_answer(callback, "Каналы не выбраны.", show_alert=True)
            return

        try:
            giveaway = await db.create_giveaway(
                created_by=callback.from_user.id,
                text_html=str(draft.get("text_html") or ""),
                text_plain=str(draft.get("text_plain") or ""),
                photo_file_id=str(draft.get("photo_file_id") or "") or None,
                winners_count=int(draft.get("winners_count") or 0),
                prize_days=int(draft.get("prize_days") or 0),
                end_mode=str(draft.get("end_mode") or ""),
                ends_at=draft.get("ends_at"),
                participant_limit=draft.get("participant_limit"),
                activate=False,
            )
        except Exception as exc:
            logger.warning("Could not create giveaway: %s", type(exc).__name__)
            await safe_callback_answer(callback, "Не удалось создать розыгрыш.", show_alert=True)
            return

        sent_count = 0
        failed: list[str] = []
        for channel in channels:
            try:
                sent = await send_giveaway_post(
                    callback.bot,
                    {**giveaway, "status": "active"},
                    channel,
                    display_tz=config.display_tz,
                )
                await db.add_giveaway_post(int(giveaway["id"]), channel, int(sent.message_id))
                sent_count += 1
            except Exception as exc:
                failed.append(str(channel))
                logger.warning(
                    "Giveaway #%s publish failed for %s: %s",
                    giveaway["id"],
                    channel,
                    type(exc).__name__,
                )

        if sent_count == 0:
            await db.delete_giveaway(int(giveaway["id"]))
            await safe_callback_answer(callback, "Не удалось опубликовать ни в один канал.", show_alert=True)
            return

        if not await db.activate_giveaway(int(giveaway["id"])):
            await delete_giveaway(callback.bot, db, int(giveaway["id"]))
            await safe_callback_answer(
                callback,
                "Публикация не активировалась. Посты удалены, подписки не затронуты.",
                show_alert=True,
            )
            return
        giveaway = await db.get_giveaway(int(giveaway["id"])) or giveaway

        await db.clear_support_session(callback.from_user.id)
        await safe_callback_answer(callback, "Розыгрыш запущен!")
        text = (
            f"✅ <b>Розыгрыш #{int(giveaway['id'])} запущен</b>\n\n"
            f"Опубликовано каналов: <b>{sent_count}</b>"
        )
        if failed:
            text += "\nНе удалось: <code>" + html.escape(", ".join(failed)) + "</code>"
        await callback.message.answer(
            text,
            reply_markup=admin_main_keyboard(await get_admin_role(callback.from_user.id) or "full"),
        )

    @router.callback_query(F.data == "admin:giveaway:cancel")
    async def admin_giveaway_cancel(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await db.clear_support_session(callback.from_user.id)
        await safe_callback_answer(callback, "Черновик розыгрыша удалён.")
        if callback.message:
            await show_admin_giveaways(callback.message, callback.from_user)

    @router.callback_query(F.data.regexp(r"^admin:giveaway:view:\d+$"))
    async def admin_giveaway_view(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        await safe_callback_answer(callback)
        await show_admin_giveaway(callback.message, callback.from_user, giveaway_id)

    @router.callback_query(
        F.data.regexp(r"^admin:giveaway:participants:\d+:\d+$")
    )
    async def admin_giveaway_participants(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        giveaway_id = int(parts[-2])
        requested_page = max(0, int(parts[-1]))
        item = await db.get_giveaway(giveaway_id)
        if not item:
            await safe_callback_answer(callback, "Розыгрыш не найден.", show_alert=True)
            return

        page_size = 20
        rows, total = await db.list_giveaway_participants_page(
            giveaway_id,
            page=requested_page,
            page_size=page_size,
        )
        max_page = max(0, (total - 1) // page_size) if total else 0
        page = min(requested_page, max_page)
        if page != requested_page:
            rows, total = await db.list_giveaway_participants_page(
                giveaway_id,
                page=page,
                page_size=page_size,
            )

        lines = [
            f"👥 <b>Участники розыгрыша #{giveaway_id}</b>",
            "",
            f"Всего: <b>{total}</b>",
            f"Страница: <b>{page + 1}/{max_page + 1}</b>",
            "",
        ]
        if not rows:
            lines.append("Участников пока нет.")
        else:
            start = page * page_size
            for offset, participant in enumerate(rows, start=1):
                number = start + offset
                username = str(participant.get("username") or "").strip()
                first_name = str(participant.get("first_name") or "").strip()
                if username:
                    label = f"@{username}"
                elif first_name:
                    label = first_name
                else:
                    label = "Без username"
                crown = " 🏆" if int(participant.get("is_winner") or 0) else ""
                joined = from_iso(participant.get("joined_at"))
                joined_text = (
                    joined.astimezone(config.display_tz).strftime("%d.%m · %H:%M")
                    if joined
                    else "—"
                )
                lines.append(
                    f"{number}. {html.escape(label)}{crown}\n"
                    f"   <code>{int(participant['telegram_id'])}</code> · {joined_text}"
                )

        kb = InlineKeyboardBuilder()
        nav = []
        if page > 0:
            nav.append(
                blue_inline_button(
                    "←",
                    callback_data=f"admin:giveaway:participants:{giveaway_id}:{page - 1}",
                    premium_icon=False,
                )
            )
        if page < max_page:
            nav.append(
                blue_inline_button(
                    "→",
                    callback_data=f"admin:giveaway:participants:{giveaway_id}:{page + 1}",
                    premium_icon=False,
                )
            )
        if nav:
            kb.row(*nav)
        kb.row(
            blue_inline_button(
                "⬅️ К розыгрышу",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
            )
        )
        await safe_callback_answer(callback)
        await send_screen(
            callback.message,
            callback.from_user,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.regexp(r"^admin:giveaway:export:\d+$"))
    async def admin_giveaway_export(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        item = await db.get_giveaway(giveaway_id)
        if not item:
            await safe_callback_answer(callback, "Розыгрыш не найден.", show_alert=True)
            return
        participants = await db.list_giveaway_participants(giveaway_id)
        winners = {
            int(row["telegram_id"]): int(row["position"])
            for row in await db.get_giveaway_winners(giveaway_id)
        }
        output = StringIO()
        writer = csv.writer(output, delimiter=";")
        writer.writerow([
            "telegram_id",
            "username",
            "first_name",
            "joined_at",
            "winner_position",
        ])
        for row in participants:
            writer.writerow([
                int(row["telegram_id"]),
                str(row.get("username") or ""),
                str(row.get("first_name") or ""),
                str(row.get("joined_at") or ""),
                winners.get(int(row["telegram_id"]), ""),
            ])
        payload = ("\ufeff" + output.getvalue()).encode("utf-8")
        await safe_callback_answer(callback, "Формирую CSV…")
        await callback.bot.send_document(
            chat_id=callback.message.chat.id,
            document=BufferedInputFile(
                payload,
                filename=f"giveaway_{giveaway_id}_participants.csv",
            ),
            caption=(
                f"📥 Участники розыгрыша #{giveaway_id}\n"
                f"Всего: <b>{len(participants)}</b>"
            ),
        )

    @router.callback_query(F.data.regexp(r"^admin:giveaway:rerollundo:\d+$"))
    async def admin_giveaway_reroll_undo(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        await safe_callback_answer(callback, "Отменяю перевыбор…")
        try:
            result = await undo_giveaway_reroll(
                callback.bot,
                db,
                config,
                provider,
                giveaway_id,
                undone_by=callback.from_user.id,
                max_age_seconds=600,
            )
        except ValueError as exc:
            await callback.message.answer(
                f"Не удалось отменить перевыбор: {html.escape(str(exc))}"
            )
            return
        except Exception as exc:
            logger.exception(
                "Giveaway reroll undo failed for #%s: %s",
                giveaway_id,
                type(exc).__name__,
            )
            await callback.message.answer(
                "Не удалось отменить перевыбор. Откройте розыгрыш и проверьте состояние."
            )
            return
        await callback.message.answer(
            "✅ <b>Перевыбор отменён</b>\n\n"
            f"Исходный победитель <code>{int(result['restored_telegram_id'])}</code> восстановлен.\n"
            f"Начисление пользователю <code>{int(result['removed_telegram_id'])}</code> отозвано.\n"
            "Итоговый пост обновлён."
        )
        await show_admin_giveaway(callback.message, callback.from_user, giveaway_id)

    @router.callback_query(F.data.regexp(r"^admin:giveaway:reroll:\d+$"))
    async def admin_giveaway_reroll(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        item = await db.get_giveaway(giveaway_id)
        if not item or str(item.get("status")) != "finished":
            await safe_callback_answer(
                callback,
                "Перевыбор доступен только после завершения розыгрыша.",
                show_alert=True,
            )
            return
        winners = await db.get_giveaway_winners(giveaway_id)
        if not winners:
            await safe_callback_answer(callback, "Победителей нет.", show_alert=True)
            return

        participants = await db.list_giveaway_participants(giveaway_id)
        rerolls = await db.list_giveaway_rerolls(giveaway_id)
        active_rerolls = [row for row in rerolls if not row.get("undone_at")]
        excluded = {int(winner["telegram_id"]) for winner in winners}
        excluded.update(int(row["old_telegram_id"]) for row in active_rerolls)
        excluded.update(int(row["new_telegram_id"]) for row in active_rerolls)
        eligible = [
            row for row in participants
            if int(row["telegram_id"]) not in excluded
        ]
        if not eligible:
            await safe_callback_answer(
                callback,
                "Других участников для перевыбора уже нет.",
                show_alert=True,
            )
            return

        kb = InlineKeyboardBuilder()
        lines = [
            f"🔄 <b>Перевыбор победителя · #{giveaway_id}</b>",
            "",
            "Выберите победителя, которого нужно заменить.",
            "Его приз будет отозван, а случайному другому участнику сразу выдастся такой же срок.",
            "",
        ]
        for index, winner in enumerate(winners, start=1):
            username = str(winner.get("username") or "").strip()
            label = f"@{username}" if username else f"Победитель #{index}"
            lines.append(f"{index}. {html.escape(label)}")
            kb.row(
                blue_inline_button(
                    f"🔄 {label[:38]}",
                    callback_data=(
                        f"admin:giveaway:rerollpick:{giveaway_id}:"
                        f"{int(winner['telegram_id'])}"
                    ),
                )
            )
        kb.row(
            blue_inline_button(
                "Отмена",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
                premium_icon=False,
            )
        )
        await safe_callback_answer(callback)
        await send_screen(
            callback.message,
            callback.from_user,
            "\n".join(lines),
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(
        F.data.regexp(r"^admin:giveaway:rerollpick:\d+:\d+$")
    )
    async def admin_giveaway_reroll_pick(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        giveaway_id = int(parts[-2])
        old_telegram_id = int(parts[-1])
        await safe_callback_answer(callback, "Перевыбираю…")
        try:
            result = await reroll_giveaway_winner(
                callback.bot,
                db,
                config,
                provider,
                giveaway_id,
                old_telegram_id,
                rerolled_by=callback.from_user.id,
            )
        except ValueError as exc:
            await callback.message.answer(
                f"Не удалось выполнить перевыбор: {html.escape(str(exc))}"
            )
            return
        except Exception as exc:
            logger.exception(
                "Giveaway reroll failed for #%s: %s",
                giveaway_id,
                type(exc).__name__,
            )
            await callback.message.answer(
                "Не удалось выполнить перевыбор. Данные сохранены; проверьте розыгрыш ещё раз."
            )
            return

        old_username = str(result.get("old_username") or "").strip()
        new_username = str(result.get("new_username") or "").strip()
        new_first_name = str(result.get("new_first_name") or "").strip()
        old_label = (
            f"@{old_username}"
            if old_username
            else f"ID {int(result['old_telegram_id'])}"
        )
        new_label = (
            f"@{new_username}"
            if new_username
            else (new_first_name or f"ID {int(result['new_telegram_id'])}")
        )
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "↩️ Отменить перевыбор · 10 мин",
                callback_data=f"admin:giveaway:rerollundo:{giveaway_id}",
            )
        )
        kb.row(
            blue_inline_button(
                "🔄 Ещё перевыбор",
                callback_data=f"admin:giveaway:reroll:{giveaway_id}",
            )
        )
        kb.row(
            blue_inline_button(
                "👥 Участники",
                callback_data=f"admin:giveaway:participants:{giveaway_id}:0",
            )
        )
        kb.row(
            blue_inline_button(
                "Открыть розыгрыш",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
            )
        )
        await callback.message.answer(
            f"✅ <b>Победитель перевыбран</b>\n\n"
            f"Был: <b>{html.escape(old_label)}</b>\n"
            f"Стал: <b>{html.escape(new_label)}</b>\n\n"
            f"Приз на <b>{int(result['prize_days'])} дней</b> перенесён новому победителю. "
            "Итоговый пост в канале обновлён.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.regexp(r"^admin:giveaway:finish:\d+$"))
    async def admin_giveaway_finish(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        await safe_callback_answer(callback, "Подвожу итоги…")
        await finish_giveaway(
            callback.bot,
            db,
            config,
            provider,
            giveaway_id,
            force=True,
        )
        await show_admin_giveaway(callback.message, callback.from_user, giveaway_id)

    @router.callback_query(F.data.regexp(r"^admin:giveaway:cancelrunconfirm:\d+$"))
    async def admin_giveaway_cancel_run_confirm(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        item = await db.get_giveaway(giveaway_id)
        if not item or str(item.get("status")) != "active":
            await safe_callback_answer(callback, "Розыгрыш уже не активен.", show_alert=True)
            return
        await safe_callback_answer(callback)
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Да, отменить",
                callback_data=f"admin:giveaway:cancelrun:{giveaway_id}",
            )
        )
        kb.row(
            blue_inline_button(
                "Назад",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
                premium_icon=False,
            )
        )
        await callback.message.answer(
            f"⚪ <b>Отменить розыгрыш #{giveaway_id}?</b>\n\n"
            "Пост останется в канале с пометкой «Розыгрыш отменён». "
            "Победители не будут выбраны, подписки не будут выданы, история останется в базе.",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.regexp(r"^admin:giveaway:cancelrun:\d+$"))
    async def admin_giveaway_cancel_run(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        await safe_callback_answer(callback, "Отменяю…")
        changed = await cancel_giveaway(
            callback.bot,
            db,
            config,
            giveaway_id,
        )
        if changed:
            await callback.message.answer(f"⚪ Розыгрыш #{giveaway_id} отменён.")
        else:
            await callback.message.answer("Розыгрыш уже не активен или не найден.")
        await show_admin_giveaway(callback.message, callback.from_user, giveaway_id)

    @router.callback_query(F.data.regexp(r"^admin:giveaway:deleteconfirm:\d+$"))
    async def admin_giveaway_delete_confirm(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        item = await db.get_giveaway(giveaway_id)
        if not item:
            await safe_callback_answer(callback, "Розыгрыш уже удалён.", show_alert=True)
            return
        if str(item.get("status")) == "finishing":
            await safe_callback_answer(
                callback,
                "Сейчас подводятся итоги. Подождите завершения и удалите после.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback)
        kb = InlineKeyboardBuilder()
        kb.row(
            blue_inline_button(
                "Да, удалить",
                callback_data=f"admin:giveaway:delete:{giveaway_id}",
            )
        )
        kb.row(
            blue_inline_button(
                "Отмена",
                callback_data=f"admin:giveaway:view:{giveaway_id}",
                premium_icon=False,
            )
        )
        note = (
            "Посты розыгрыша будут удалены из каналов, а участники и история розыгрыша — из базы.\n\n"
            "<b>Уже выданные победителям подписки останутся</b> — удаление их не отзывает."
        )
        await callback.message.answer(
            f"🗑 <b>Удалить розыгрыш #{giveaway_id}?</b>\n\n{note}",
            reply_markup=kb.as_markup(),
        )

    @router.callback_query(F.data.regexp(r"^admin:giveaway:delete:\d+$"))
    async def admin_giveaway_delete(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        item = await db.get_giveaway(giveaway_id)
        if not item:
            await safe_callback_answer(callback, "Розыгрыш уже удалён.", show_alert=True)
            return
        if str(item.get("status")) == "finishing":
            await safe_callback_answer(
                callback,
                "Сейчас подводятся итоги. Попробуйте удалить через несколько секунд.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, "Удаляю…")
        result = await delete_giveaway(callback.bot, db, giveaway_id)
        if not result.get("deleted"):
            await callback.message.answer("Розыгрыш уже удалён или не найден.")
        else:
            text = (
                f"✅ <b>Розыгрыш #{giveaway_id} удалён</b>\n\n"
                f"Постов удалено из каналов: <b>{int(result.get('posts_deleted') or 0)}</b>"
            )
            failed = int(result.get("posts_failed") or 0)
            if failed:
                text += (
                    f"\nНе удалось удалить постов: <b>{failed}</b> "
                    "(например, если у бота больше нет прав в канале)."
                )
            await callback.message.answer(text)
        await show_admin_giveaways(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:broadcast:start")
    async def admin_broadcast_start(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нужна полная админка.", show_alert=True)
            return
        await db.set_support_session(
            callback.from_user.id,
            "ad_text",
            payload=json.dumps({"broadcast_only": True}),
        )
        await safe_callback_answer(callback)
        if callback.message:
            kb = InlineKeyboardBuilder()
            kb.row(blue_inline_button("Отмена", callback_data="admin:ad:cancel", premium_icon=False))
            await callback.message.answer(
                "<b>Новая рассылка · 1/4</b>\n\n"
                "Отправьте текст рассылки. Фото и кнопка — необязательные.",
                reply_markup=kb.as_markup(),
            )

    @router.callback_query(F.data == "admin:ad:start")
    async def admin_ad_start(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нужна полная админка.", show_alert=True)
            return
        await db.set_support_session(callback.from_user.id, "ad_text", payload="{}")
        await safe_callback_answer(callback)
        if callback.message:
            kb = InlineKeyboardBuilder()
            kb.row(blue_inline_button("Отмена", callback_data="admin:ad:cancel", premium_icon=False))
            await callback.message.answer(
                "<b>Новая реклама · 1/5</b>\n\nОтправьте текст публикации. Форматирование и premium emoji сохранятся.",
                reply_markup=kb.as_markup(),
            )

    @router.callback_query(F.data == "admin:ad:skip-photo")
    async def admin_ad_skip_photo(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "ad_photo":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        await safe_callback_answer(callback)
        await ask_ad_url(callback.message, callback.from_user.id, ad_draft(session))

    @router.callback_query(F.data == "admin:ad:channel:main")
    async def admin_ad_main_channel(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        channel = normalize_channel(config.channel_url)
        if not session or session.get("mode") != "ad_channel" or channel is None:
            await safe_callback_answer(callback, "Канал не настроен", show_alert=True)
            return
        draft = ad_draft(session)
        draft["channel"] = channel
        await safe_callback_answer(callback)
        await show_ad_preview(callback.message, callback.from_user.id, draft)

    @router.callback_query(F.data == "admin:ad:channel:users")
    async def admin_ad_all_users(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "ad_channel":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        draft = ad_draft(session)
        draft["channel"] = "all_users"
        await safe_callback_answer(callback)
        await show_ad_preview(callback.message, callback.from_user.id, draft)

    @router.callback_query(F.data == "admin:ad:send")
    async def admin_ad_send(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id) or not callback.message:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        session = await db.get_support_session(callback.from_user.id)
        if not session or session.get("mode") != "ad_confirm":
            await safe_callback_answer(callback, "Черновик устарел", show_alert=True)
            return
        draft = ad_draft(session)
        if draft.get("channel") == "all_users":
            await safe_callback_answer(callback, "Запускаю рассылку…")
            sent_count, failed_count = await broadcast_ad_post(callback.bot, draft)
            await db.clear_support_session(callback.from_user.id)
            await callback.message.answer(
                "✅ <b>Рассылка завершена</b>\n\n"
                f"Доставлено: <b>{sent_count}</b>\n"
                f"Не доставлено: <b>{failed_count}</b>",
                reply_markup=admin_main_keyboard(await get_admin_role(callback.from_user.id) or "full"),
            )
            return

        await safe_callback_answer(callback, "Публикую…")
        try:
            sent = await send_ad_post(callback.bot, draft["channel"], draft)
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            logger.warning("Advertising publish failed: %s", type(exc).__name__)
            await callback.message.answer(
                "Не удалось опубликовать. Проверьте канал, ссылку и права бота на публикацию."
            )
            return
        await db.clear_support_session(callback.from_user.id)
        await callback.message.answer(
            f"✅ Реклама опубликована. ID сообщения: <code>{sent.message_id}</code>",
            reply_markup=admin_main_keyboard(await get_admin_role(callback.from_user.id) or "full"),
        )

    @router.callback_query(F.data == "admin:ad:cancel")
    async def admin_ad_cancel(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await db.clear_support_session(callback.from_user.id)
        await safe_callback_answer(callback, "Черновик удалён")
        if callback.message:
            await show_admin(callback.message, callback.from_user)

    @router.callback_query(F.data.regexp(r"^giveaway:join:\d+$"))
    async def giveaway_join(callback: CallbackQuery) -> None:
        giveaway_id = int(callback.data.rsplit(":", 1)[-1])
        giveaway = await db.get_giveaway(giveaway_id)
        if not giveaway:
            await safe_callback_answer(callback, "Розыгрыш не найден.", show_alert=True)
            return
        if str(giveaway.get("status")) != "active":
            await safe_callback_answer(callback, "Этот розыгрыш уже завершён.", show_alert=True)
            return

        if not await is_channel_member(callback.bot, callback.from_user.id):
            await safe_callback_answer(
                callback,
                "Для участия сначала подпишитесь на обязательный канал MGN VPN и нажмите кнопку ещё раз.",
                show_alert=True,
            )
            return

        await db.mark_channel_verified(callback.from_user.id)
        await ensure_actor(callback.from_user)
        result = await db.add_giveaway_participant(
            giveaway_id=giveaway_id,
            telegram_id=callback.from_user.id,
            username=callback.from_user.username,
            first_name=callback.from_user.first_name,
        )
        state = str(result.get("state") or "")
        if state == "missing":
            await safe_callback_answer(callback, "Розыгрыш не найден.", show_alert=True)
            return
        if state == "ended":
            await safe_callback_answer(callback, "Приём участников уже завершён.", show_alert=True)
            asyncio.create_task(
                finish_giveaway(callback.bot, db, config, provider, giveaway_id)
            )
            return

        winners_count = int(giveaway.get("winners_count") or 1)
        prize_days = int(giveaway.get("prize_days") or 1)
        end_label = giveaway_end_label(giveaway)
        if state == "already":
            message_text = (
                f"Вы уже участвуете в розыгрыше!\n"
                f"Призов: {winners_count}, по {prize_days} дней MGN VPN.\n"
                f"Завершение: {end_label}."
            )
        else:
            message_text = (
                f"🎉 Вы участвуете в розыгрыше!\n"
                f"Призов: {winners_count}, по {prize_days} дней MGN VPN.\n"
                f"Завершение: {end_label}. Участников: {int(result.get('count') or 0)}."
            )
        await safe_callback_answer(callback, message_text, show_alert=True)
        if state == "joined":
            try:
                await callback.bot.send_message(
                    callback.from_user.id,
                    message_text,
                )
            except (TelegramForbiddenError, TelegramBadRequest):
                # A channel callback can arrive before the user has ever opened
                # the bot privately. The callback alert still confirms entry.
                pass
            except Exception as exc:
                logger.warning(
                    "Could not send giveaway join confirmation to %s: %s",
                    callback.from_user.id,
                    type(exc).__name__,
                )
        if result.get("due"):
            asyncio.create_task(
                finish_giveaway(callback.bot, db, config, provider, giveaway_id)
            )

    @router.callback_query(F.data.regexp(r"^admin:users(?::\d+)?$"))
    async def admin_users_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            parts = callback.data.split(":")
            page = int(parts[2]) if len(parts) == 3 and parts[2].isdigit() else 0
            await show_admin_users(callback.message, callback.from_user, page)

    @router.callback_query(F.data == "admin:usersearch")
    async def admin_user_search(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await db.set_support_session(callback.from_user.id, "admin_search")
        await safe_callback_answer(callback, )
        if callback.message:
            await callback.message.answer("Отправьте Telegram ID или @username.")

    @router.callback_query(F.data.startswith("admin:support"))
    async def admin_support_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            parts = callback.data.split(":")
            status = parts[2] if len(parts) > 2 else "all"
            page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            await show_admin_support(callback.message, callback.from_user, status, page)

    @router.callback_query(F.data == "admin:analytics")
    async def admin_analytics_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback)
        if callback.message:
            await show_admin_analytics(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:analytics:sources")
    async def admin_analytics_sources_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback)
        if callback.message:
            await show_admin_analytics_sources(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:analytics:promos")
    async def admin_analytics_promos_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback)
        if callback.message:
            await show_admin_analytics_promos(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:analytics:vpn")
    async def admin_analytics_vpn_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback)
        if callback.message:
            await show_admin_analytics_vpn(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:payments")
    async def admin_payments_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin_payments(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:bonuses")
    async def admin_bonuses_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, 
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin_bonuses(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:system")
    async def admin_system_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, 
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin_system(callback.message, callback.from_user)

    @router.callback_query(F.data.in_({"admin:maintenance:on", "admin:maintenance:off"}))
    async def admin_maintenance_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа.", show_alert=True)
            return
        enabled = callback.data.endswith(":on")
        state = await db.set_maintenance(
            enabled,
            updated_by=callback.from_user.id,
        )
        await safe_callback_answer(
            callback,
            "Техработы включены: новые покупки остановлены."
            if state.get("enabled")
            else "Техработы выключены: покупки снова доступны.",
            show_alert=True,
        )
        if callback.message:
            await show_admin_system(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:servers")
    async def admin_servers_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(
                callback,
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        await safe_callback_answer(callback, "Проверяю H1…")
        if callback.message:
            await show_admin_servers(callback.message, callback.from_user)

    @router.callback_query(F.data == "admin:admins")
    async def admin_admins_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа.", show_alert=True)
            return
        await safe_callback_answer(callback, )
        if callback.message:
            await show_admin_admins(callback.message, callback.from_user)

    @router.callback_query(F.data.startswith("admin:user:"))
    async def admin_user_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        if not callback.message:
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректный ID", show_alert=True)
            return
        await safe_callback_answer(callback, )
        await show_admin_user(callback.message, callback.from_user, int(raw))

    @router.callback_query(F.data.startswith("admin:role:"))
    async def admin_role_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа.", show_alert=True)
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            await safe_callback_answer(callback, "Некорректные данные", show_alert=True)
            return

        telegram_id = int(parts[2])
        role = parts[3]
        if telegram_id in config.admin_ids:
            await safe_callback_answer(callback, 
                "Доступ владельца нельзя изменить из панели.",
                show_alert=True,
            )
            return

        try:
            await db.get_user(telegram_id)
        except KeyError:
            await safe_callback_answer(callback, 
                "Пользователь ещё не запускал бота.",
                show_alert=True,
            )
            return

        if role == "remove":
            await db.remove_admin_role(telegram_id)
            await safe_callback_answer(callback, "Админка забрана")
        elif role in {"full", "limited"}:
            await db.set_admin_role(
                telegram_id=telegram_id,
                role=role,
                granted_by=callback.from_user.id,
            )
            await safe_callback_answer(callback, 
                "Выдана полная админка"
                if role == "full"
                else "Выдана ограниченная админка"
            )
        else:
            await safe_callback_answer(callback, "Неизвестная роль", show_alert=True)
            return

        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.callback_query(F.data.startswith("admin:grantmenu:"))
    async def admin_grant_menu(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit() or not callback.message:
            await safe_callback_answer(callback, "Некорректный пользователь", show_alert=True)
            return
        uid = int(raw)
        kb = InlineKeyboardBuilder()
        for days in (30, 90, 180, 365):
            kb.button(text=f"{days} дней", callback_data=f"admin:grant:{uid}:{days}")
        kb.adjust(2)
        kb.row(blue_inline_button("Ввести вручную", callback_data=f"admin:manualdays:{uid}:grant"))
        kb.row(blue_inline_button("Назад", callback_data=f"admin:user:{uid}", premium_icon=False))
        await safe_callback_answer(callback, )
        await send_screen(callback.message, callback.from_user, "<b>Выдать подписку</b>\n\nВыберите срок.", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("admin:daysmenu:"))
    async def admin_days_menu(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit() or parts[3] not in {"add", "sub"} or not callback.message:
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return
        uid, action = int(parts[2]), parts[3]
        kb = InlineKeyboardBuilder()
        for days in (1, 3, 14, 30):
            kb.button(text=str(days), callback_data=f"admin:adjust:{uid}:{action}:{days}")
        kb.adjust(3)
        kb.row(blue_inline_button("Ввести вручную", callback_data=f"admin:manualdays:{uid}:{action}"))
        kb.row(blue_inline_button("Назад", callback_data=f"admin:user:{uid}", premium_icon=False))
        await safe_callback_answer(callback, )
        title = "Добавить дни" if action == "add" else "Списать дни"
        await send_screen(callback.message, callback.from_user, f"<b>{title}</b>", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("admin:manualdays:"))
    async def admin_manual_days(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit() or parts[3] not in {"grant", "add", "sub"}:
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return
        action = parts[3]
        allowed = (
            await has_admin_access(callback.from_user.id)
            if action == "grant"
            else await has_full_admin_access(callback.from_user.id)
        )
        if not allowed:
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        await db.set_support_session(callback.from_user.id, "admin_days", payload=f"{parts[2]}:{action}")
        await safe_callback_answer(callback, )
        if callback.message:
            await callback.message.answer("Отправьте целое количество дней от 1 до 3650.")

    @router.callback_query(F.data.startswith("admin:adjust:"))
    async def admin_adjust_days(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        if len(parts) != 5 or not parts[2].isdigit() or parts[3] not in {"add", "sub"} or not parts[4].isdigit():
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return
        uid, days = int(parts[2]), int(parts[4])
        if days not in {1, 3, 14, 30}:
            await safe_callback_answer(callback, "Некорректный срок", show_alert=True)
            return
        await safe_callback_answer(callback, "Обновляю срок…")
        if parts[3] == "add":
            current_user = await db.get_user(uid)
            updated = await db.grant_subscription_by_admin(
                uid,
                days,
                current_user.get("plan_name") or "Админская выдача",
                callback.from_user.id,
                action="add",
            )
        else:
            updated = await db.adjust_subscription_days(uid, -days)
        await sync_device_limit(updated)
        if callback.message:
            await show_admin_user(callback.message, callback.from_user, uid)

    @router.callback_query(F.data.startswith("admin:adjustconfirmed:"))
    async def admin_adjust_days_confirmed(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        if (
            len(parts) != 5 or not parts[2].isdigit()
            or parts[3] != "sub" or not parts[4].isdigit()
        ):
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return
        uid, days = int(parts[2]), int(parts[4])
        if not 31 <= days <= 3650:
            await safe_callback_answer(callback, "Некорректный срок", show_alert=True)
            return
        await safe_callback_answer(callback, "Обновляю срок…")
        updated = await db.adjust_subscription_days(uid, -days)
        await sync_device_limit(updated)
        if callback.message:
            await show_admin_user(callback.message, callback.from_user, uid)

    @router.callback_query(F.data.startswith("admin:devicemenu:"))
    async def admin_device_menu(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit() or not callback.message:
            await safe_callback_answer(callback, "Некорректный пользователь", show_alert=True)
            return
        uid = int(raw)
        kb = InlineKeyboardBuilder()
        for limit in range(1, 6):
            kb.button(text=str(limit), callback_data=f"admin:setdevice:{uid}:{limit}")
        kb.adjust(3)
        kb.row(blue_inline_button("Назад", callback_data=f"admin:user:{uid}", premium_icon=False))
        await safe_callback_answer(callback, )
        await send_screen(callback.message, callback.from_user, "<b>Лимит устройств</b>\n\nВыберите значение от 1 до 5.", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("admin:setdevice:"))
    async def admin_set_device(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await safe_callback_answer(callback, "Некорректное значение", show_alert=True)
            return
        uid, limit = int(parts[2]), int(parts[3])
        if not 1 <= limit <= 5:
            await safe_callback_answer(callback, "Допустимо от 1 до 5", show_alert=True)
            return
        await safe_callback_answer(callback, "Обновляю лимит…")
        updated = await db.set_device_limit(uid, limit)
        await sync_device_limit(updated)
        if callback.message:
            await show_admin_user(callback.message, callback.from_user, uid)

    @router.callback_query(F.data.startswith("admin:revokeconfirm:"))
    async def admin_revoke_confirm(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit() or not callback.message:
            await safe_callback_answer(callback, "Некорректный пользователь", show_alert=True)
            return
        uid = int(raw)
        kb = InlineKeyboardBuilder()
        kb.row(blue_inline_button("Отключить", callback_data=f"admin:revoke:{uid}"))
        kb.row(blue_inline_button("Отмена", callback_data=f"admin:user:{uid}", premium_icon=False))
        await safe_callback_answer(callback, )
        await send_screen(callback.message, callback.from_user, f"<b>Отключить подписку?</b>\n\nПользователь <code>{uid}</code> потеряет доступ.", reply_markup=kb.as_markup())

    @router.callback_query(F.data.startswith("admin:revoke:"))
    async def admin_revoke(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, "Нет доступа", show_alert=True)
            return
        raw = callback.data.rsplit(":", 1)[-1]
        if not raw.isdigit():
            await safe_callback_answer(callback, "Некорректный пользователь", show_alert=True)
            return
        uid = int(raw)
        await db.revoke_subscription(uid)
        await safe_callback_answer(callback, "Подписка отключена")
        if callback.message:
            await show_admin_user(callback.message, callback.from_user, uid)

    @router.callback_query(F.data.startswith("admin:device:"))
    async def admin_device_callback(callback: CallbackQuery) -> None:
        if not await has_full_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, 
                "Нужна полная админка.",
                show_alert=True,
            )
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit():
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return

        telegram_id = int(parts[2])
        action = parts[3]
        try:
            current = await db.get_user(telegram_id)
        except KeyError:
            await safe_callback_answer(callback, "Пользователь не найден", show_alert=True)
            return

        current_limit = int(current.get("max_devices") or BASE_DEVICES)

        if action == "add":
            if current_limit >= MAX_DEVICES:
                await safe_callback_answer(callback, 
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
                await safe_callback_answer(callback, 
                    "Нельзя опустить ниже 1 устройства.",
                    show_alert=True,
                )
                return
            updated = await db.revoke_extra_device(telegram_id)
            success_text = "−1 устройство"
        else:
            await safe_callback_answer(callback, "Неизвестное действие", show_alert=True)
            return

        if not updated:
            await safe_callback_answer(callback, 
                "Не удалось изменить лимит.",
                show_alert=True,
            )
            return

        await safe_callback_answer(callback, success_text)
        await sync_device_limit(updated)
        await show_admin_user(
            callback.message,
            callback.from_user,
            telegram_id,
        )

    @router.callback_query(F.data.startswith("admin:grant:"))
    async def admin_grant_callback(callback: CallbackQuery) -> None:
        if not await has_admin_access(callback.from_user.id):
            await safe_callback_answer(callback, 
                "Нет доступа.",
                show_alert=True,
            )
            return
        if not callback.message:
            return

        parts = callback.data.split(":")
        if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
            await safe_callback_answer(callback, "Некорректная команда", show_alert=True)
            return

        telegram_id = int(parts[2])
        days = int(parts[3])
        if days not in {30, 90, 180, 365}:
            await safe_callback_answer(callback, "Некорректный срок", show_alert=True)
            return
        try:
            await db.get_user(telegram_id)
        except KeyError:
            await safe_callback_answer(callback, "Пользователь не найден", show_alert=True)
            return

        await safe_callback_answer(callback, f"Добавляю {days} дней…")
        user = await db.grant_subscription_by_admin(
            telegram_id=telegram_id,
            days=days,
            plan_name=f"{days} дн.",
            granted_by=callback.from_user.id,
        )
        try:
            await asyncio.wait_for(provider.provision(user), 7.0)
        except Exception as exc:
            logger.warning(
                "Admin grant provisioning deferred for user %s: %s",
                telegram_id,
                exc,
            )

        await notify_subscription_granted(callback.message.bot, user, days)
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
                "PLANS: all или 30,90,180,365"
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
        if not await has_admin_access(message.from_user.id):
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

        user = await db.grant_subscription_by_admin(
            telegram_id=telegram_id,
            days=days,
            plan_name=f"{days} дн.",
            granted_by=message.from_user.id,
        )
        try:
            await asyncio.wait_for(provider.provision(user), 7.0)
        except Exception as exc:
            logger.warning(
                "Admin command provisioning deferred for user %s: %s",
                telegram_id,
                exc,
            )

        await notify_subscription_granted(message.bot, user, days)
        role = await get_admin_role(message.from_user.id)
        await send_screen(
            message,
            message.from_user,
            f"✅ Пользователю <code>{telegram_id}</code> добавлено <b>{days}</b> дней.",
            reply_markup=admin_main_keyboard(role or "full"),
        )

    async def deliver_support_message(bot, chat_id: int, ticket_id: int, payload: dict[str, Any], *, admin_reply: bool) -> None:
        body = html.escape(str(payload.get("text") or payload.get("caption") or ""))
        if admin_reply:
            prefix = f"<b>Ответ поддержки\nОбращение #{ticket_id}</b>"
            markup = support_user_ticket_keyboard({"id": ticket_id, "status": "open"})
        else:
            ticket = await db.get_support_ticket(ticket_id, is_admin=True)
            support_uid = int(ticket.get("telegram_id") or 0) if ticket else 0
            support_active = False
            username = "без username"
            if ticket and ticket.get("username"):
                username = f"@{html.escape(str(ticket['username']))}"
            if support_uid:
                try:
                    support_user = await db.get_user(support_uid)
                    support_active = is_active(support_user)
                except KeyError:
                    pass
            prefix = (
                f"<b>Новое сообщение\nОбращение #{ticket_id}</b>\n\n"
                f"Пользователь: <b>{username}</b>\n"
                f"Telegram ID: <code>{support_uid}</code>\n"
                f"Подписка: <b>{'🟢 активна' if support_active else '🔴 нет активной'}</b>"
            )
            markup = support_admin_keyboard(ticket_id)
        caption = (prefix + (f"\n\n{body}" if body else ""))[:1024]
        if payload["message_type"] == "photo":
            await bot.send_photo(chat_id=chat_id, photo=payload["file_id"], caption=caption, reply_markup=markup)
        elif payload["message_type"] == "video":
            await bot.send_video(chat_id=chat_id, video=payload["file_id"], caption=caption, reply_markup=markup)
        else:
            await bot.send_message(chat_id=chat_id, text=prefix + f"\n\n{body}", reply_markup=markup)

    def support_payload(message: Message) -> dict[str, Any] | None:
        if message.photo:
            media = message.photo[-1]
            return {"message_type": "photo", "file_id": media.file_id, "file_unique_id": media.file_unique_id, "caption": message.caption}
        if message.video:
            return {"message_type": "video", "file_id": message.video.file_id, "file_unique_id": message.video.file_unique_id, "caption": message.caption}
        text = str(message.text or "").strip()
        if text:
            return {"message_type": "text", "text": text}
        return None

    async def handle_support_input(message: Message) -> bool:
        if not message.from_user:
            return False
        user_id = int(message.from_user.id)
        session = await db.get_support_session(user_id)
        if not session:
            return False

        if session["mode"] == "admin_search" and await has_admin_access(user_id):
            query = str(message.text or "").strip()
            user = await db.get_user_by_username(query) if query.startswith("@") else None
            if query.isdigit():
                try:
                    user = await db.get_user(int(query))
                except KeyError:
                    user = None
            await db.clear_support_session(user_id)
            if not user:
                await message.answer("Пользователь не найден.")
            else:
                await show_admin_user(message, message.from_user, int(user["telegram_id"]))
            return True

        if session["mode"] == "admin_days":
            raw = str(message.text or "").strip()
            payload_value = str(session.get("payload") or "")
            if not raw.isdigit() or not 1 <= int(raw) <= 3650 or ":" not in payload_value:
                await message.answer("Введите целое число от 1 до 3650.")
                return True
            uid_raw, action = payload_value.split(":", 1)
            if not uid_raw.isdigit() or action not in {"grant", "add", "sub"}:
                await db.clear_support_session(user_id)
                await message.answer("Команда устарела. Откройте карточку пользователя заново.")
                return True
            allowed = (
                await has_admin_access(user_id)
                if action == "grant"
                else await has_full_admin_access(user_id)
            )
            if not allowed:
                await db.clear_support_session(user_id)
                await message.answer("Нет доступа к этой операции.")
                return True
            uid, days = int(uid_raw), int(raw)
            if action == "sub" and days > 30:
                await db.clear_support_session(user_id)
                kb = InlineKeyboardBuilder()
                kb.row(blue_inline_button("Подтвердить списание", callback_data=f"admin:adjustconfirmed:{uid}:sub:{days}"))
                kb.row(blue_inline_button("Отмена", callback_data=f"admin:user:{uid}", premium_icon=False))
                await message.answer(
                    f"<b>Списать {days} дней?</b>\n\nПользователь: <code>{uid}</code>",
                    reply_markup=kb.as_markup(),
                )
                return True
            if action in {"grant", "add"}:
                current_user = await db.get_user(uid)
                plan_name = (
                    f"{days} дн."
                    if action == "grant"
                    else current_user.get("plan_name") or "Админская выдача"
                )
                updated = await db.grant_subscription_by_admin(
                    uid,
                    days,
                    plan_name,
                    user_id,
                    action=action,
                )
            else:
                updated = await db.adjust_subscription_days(uid, -days)
            await sync_device_limit(updated)
            await db.clear_support_session(user_id)
            if action == "grant":
                await notify_subscription_granted(message.bot, updated, days)
            await show_admin_user(message, message.from_user, uid)
            return True

        if str(session["mode"]).startswith("giveaway_"):
            if not await has_full_admin_access(user_id):
                await db.clear_support_session(user_id)
                return True
            mode = str(session["mode"])
            draft = giveaway_draft(session)

            if mode == "giveaway_text":
                text_html = str(message.html_text or "").strip() if message.text else ""
                text_plain = str(message.text or "").strip()
                if not text_html or not text_plain or len(text_plain) > 650:
                    await message.answer("Отправьте текст длиной от 1 до 650 символов.")
                    return True
                draft["text_html"] = text_html
                draft["text_plain"] = text_plain
                await db.set_support_session(user_id, "giveaway_photo", payload=json.dumps(draft))
                kb = InlineKeyboardBuilder()
                kb.row(blue_inline_button("Без изображения", callback_data="admin:giveaway:skip-photo"))
                kb.row(blue_inline_button("Отмена", callback_data="admin:giveaway:cancel", premium_icon=False))
                await message.answer(
                    "<b>Новый розыгрыш · 2/7</b>\n\n"
                    "Отправьте изображение или продолжите без него.",
                    reply_markup=kb.as_markup(),
                )
                return True

            if mode == "giveaway_photo":
                if not message.photo:
                    await message.answer("Отправьте изображение или нажмите «Без изображения».")
                    return True
                if len(str(draft.get("text_plain") or "")) > 550:
                    await message.answer(
                        "Для поста с изображением текст должен быть короче 550 символов, "
                        "чтобы после итогов поместились победители. Нажмите «Без изображения» "
                        "или начните заново с более коротким текстом."
                    )
                    return True
                draft["photo_file_id"] = message.photo[-1].file_id
                await ask_giveaway_winners(message, user_id, draft)
                return True

            if mode == "giveaway_winners":
                raw = str(message.text or "").strip()
                if not raw.isdigit() or not 1 <= int(raw) <= 10:
                    await message.answer("Отправьте число победителей от 1 до 10.")
                    return True
                draft["winners_count"] = int(raw)
                await ask_giveaway_days(message, user_id, draft)
                return True

            if mode == "giveaway_days":
                raw = str(message.text or "").strip()
                if not raw.isdigit() or not 1 <= int(raw) <= 3650:
                    await message.answer("Отправьте срок подписки от 1 до 3650 дней.")
                    return True
                draft["prize_days"] = int(raw)
                await ask_giveaway_end_mode(message, user_id, draft)
                return True

            if mode == "giveaway_end_value":
                end_mode = str(draft.get("end_mode") or "")
                raw = str(message.text or "").strip()
                if end_mode == "time":
                    try:
                        local_dt = datetime.strptime(raw, "%d.%m.%Y %H:%M").replace(
                            tzinfo=config.display_tz
                        )
                    except ValueError:
                        await message.answer(
                            "Неверный формат. Пример: <code>30.09.2026 21:30</code>"
                        )
                        return True
                    if local_dt <= datetime.now(config.display_tz):
                        await message.answer("Дата окончания должна быть в будущем.")
                        return True
                    draft["ends_at"] = local_dt.astimezone(timezone.utc).isoformat()
                    draft.pop("participant_limit", None)
                    await ask_giveaway_channels(message, user_id, draft)
                    return True
                if end_mode == "participants":
                    if not raw.isdigit():
                        await message.answer("Отправьте количество участников числом.")
                        return True
                    limit = int(raw)
                    winners_count = int(draft.get("winners_count") or 1)
                    if limit < winners_count or limit > 100000:
                        await message.answer(
                            f"Количество участников должно быть от {winners_count} до 100000."
                        )
                        return True
                    draft["participant_limit"] = limit
                    draft.pop("ends_at", None)
                    await ask_giveaway_channels(message, user_id, draft)
                    return True
                await message.answer("Сначала выберите способ завершения кнопкой.")
                return True

            if mode == "giveaway_channels":
                channels = parse_giveaway_channels(str(message.text or ""))
                if not channels:
                    await message.answer(
                        "Не удалось прочитать каналы. Отправьте @username, t.me-ссылку "
                        "или ID -100… Можно несколько через пробел/запятую."
                    )
                    return True
                draft["channels"] = channels
                await show_giveaway_preview(message, user_id, draft)
                return True

            await message.answer("Используйте кнопки предпросмотра или отмены.")
            return True

        if str(session["mode"]).startswith("ad_"):
            if not await has_full_admin_access(user_id):
                await db.clear_support_session(user_id)
                return True
            mode = str(session["mode"])
            draft = ad_draft(session)

            if mode == "ad_text":
                text_html = str(message.html_text or "").strip() if message.text else ""
                if not text_html or len(str(message.text or "")) > 4096:
                    await message.answer("Отправьте текст длиной до 4096 символов.")
                    return True
                draft["text_html"] = text_html
                draft["text_plain"] = str(message.text or "")
                await db.set_support_session(user_id, "ad_photo", payload=json.dumps(draft))
                kb = InlineKeyboardBuilder()
                kb.row(blue_inline_button("Без изображения", callback_data="admin:ad:skip-photo"))
                kb.row(blue_inline_button("Отмена", callback_data="admin:ad:cancel", premium_icon=False))
                title = "Новая рассылка · 2/4" if draft.get("broadcast_only") else "Новая реклама · 2/5"
                await message.answer(
                    f"<b>{title}</b>\n\nОтправьте изображение или продолжите без него.",
                    reply_markup=kb.as_markup(),
                )
                return True

            if mode == "ad_photo":
                if not message.photo:
                    await message.answer("Отправьте изображение или нажмите «Без изображения».")
                    return True
                if len(str(message.text or message.caption or "")) > 0:
                    await message.answer("Текст изображения не нужен: будет использован текст публикации.")
                if len(str(draft.get("text_plain") or "")) > 1024:
                    await message.answer(
                        "С изображением Telegram допускает до 1024 символов. Отмените черновик и отправьте более короткий текст."
                    )
                    return True
                draft["photo_file_id"] = message.photo[-1].file_id
                await ask_ad_url(message, user_id, draft)
                return True

            if mode == "ad_url":
                url = normalize_ad_url(str(message.text or ""))
                if url is None:
                    await message.answer("Отправьте корректную http(s):// или tg:// ссылку либо <code>-</code>.")
                    return True
                draft["button_url"] = url
                if not url:
                    await finish_ad_target(message, user_id, draft)
                    return True
                await db.set_support_session(user_id, "ad_button", payload=json.dumps(draft))
                title = "Новая рассылка · 4/4" if draft.get("broadcast_only") else "Новая реклама · 4/5"
                await message.answer(
                    f"<b>{title}</b>\n\nОтправьте текст кнопки. Можно добавить один premium emoji."
                )
                return True

            if mode == "ad_button":
                raw_text = str(message.text or "").strip()
                clean_text = _clean_button_text(raw_text)
                if not clean_text or len(clean_text) > 64:
                    await message.answer("Текст кнопки должен содержать от 1 до 64 символов.")
                    return True
                draft["button_text"] = clean_text
                custom_id = first_custom_emoji_id(message)
                if custom_id:
                    draft["button_emoji_id"] = custom_id
                await finish_ad_target(message, user_id, draft)
                return True

            if mode == "ad_channel":
                channel = normalize_channel(str(message.text or ""))
                if channel is None:
                    await message.answer("Отправьте @username канала, ссылку t.me или ID вида <code>-100…</code>.")
                    return True
                draft["channel"] = channel
                await show_ad_preview(message, user_id, draft)
                return True

            await message.answer("Используйте кнопки предпросмотра для публикации или отмены.")
            return True

        payload = support_payload(message)
        if not payload:
            await message.answer("Можно отправить текст, фото или видео.")
            return True
        now_mono = asyncio.get_running_loop().time()
        if now_mono - support_cooldowns.get(user_id, 0.0) < 2.0:
            await message.answer("Слишком быстро. Подождите пару секунд.")
            return True
        support_cooldowns[user_id] = now_mono

        if session["mode"] == "admin_reply" and await has_admin_access(user_id):
            ticket_id = int(session["ticket_id"] or 0)
            ticket = await db.get_support_ticket(ticket_id, is_admin=True)
            if not ticket or ticket.get("status") == "closed":
                await db.clear_support_session(user_id)
                await message.answer("Обращение закрыто или удалено.")
                return True
            await db.add_support_message(ticket_id=ticket_id, sender_type="admin", sender_telegram_id=user_id, is_admin=True, **payload)
            await db.clear_support_session(user_id)
            try:
                await deliver_support_message(message.bot, int(ticket["telegram_id"]), ticket_id, payload, admin_reply=True)
            except Exception as exc:
                logger.warning("Support delivery failed for ticket %s: %s", ticket_id, type(exc).__name__)
                await message.answer("Ответ сохранён в обращении, но Telegram не смог доставить уведомление пользователю.")
                return True
            await message.answer(f"Ответ по обращению #{ticket_id} отправлен.")
            return True

        if session["mode"] == "new":
            if await db.recent_support_ticket_count(user_id) >= 3:
                await message.answer("Слишком много новых обращений. Продолжите одно из уже созданных.")
                return True
            support_user = await ensure_actor(message.from_user)
            ticket = await db.create_support_thread(
                telegram_id=user_id,
                username=message.from_user.username,
                first_name=message.from_user.first_name,
                server_code=str(support_user.get("preferred_country") or "auto"),
                **payload,
            )
            ticket_id = int(ticket["id"])
            await db.clear_support_session(user_id)
            for admin_id in {*(int(v) for v in config.admin_ids), *(int(a["telegram_id"]) for a in await db.list_admin_roles())}:
                try:
                    await deliver_support_message(message.bot, admin_id, ticket_id, payload, admin_reply=False)
                except Exception as exc:
                    logger.warning("Support notification failed for admin %s: %s", admin_id, type(exc).__name__)
            await message.answer(
                f"<b>Обращение #{ticket_id} создано.</b>\n\n"
                "Статус: <b>Открыто</b>\n"
                "Если нужно что-то добавить — откройте обращение и нажмите «Написать сообщение».",
                reply_markup=support_user_ticket_keyboard(ticket),
            )
            return True

        if session["mode"] == "user_reply":
            ticket_id = int(session["ticket_id"] or 0)
            try:
                ticket = await db.add_support_message(
                    ticket_id=ticket_id, sender_type="user", sender_telegram_id=user_id,
                    owner_id=user_id, **payload,
                )
            except ValueError:
                await db.clear_support_session(user_id)
                await message.answer("Обращение закрыто. Создайте новое, если нужна помощь.")
                return True
            if not ticket:
                await db.clear_support_session(user_id)
                await message.answer("Обращение не найдено.")
                return True
            await db.clear_support_session(user_id)
            for admin_id in {*(int(v) for v in config.admin_ids), *(int(a["telegram_id"]) for a in await db.list_admin_roles())}:
                try:
                    await deliver_support_message(message.bot, admin_id, ticket_id, payload, admin_reply=False)
                except Exception as exc:
                    logger.warning("Support notification failed for admin %s: %s", admin_id, type(exc).__name__)
            await message.answer(
                f"Сообщение добавлено в обращение <b>#{ticket_id}</b>.",
                reply_markup=support_user_ticket_keyboard(ticket),
            )
            return True
        return False

    @router.message(F.photo | F.video)
    async def support_media_router(message: Message) -> None:
        if not await handle_support_input(message):
            await message.answer("Фото и видео можно отправить после открытия обращения в разделе «Поддержка».")

    @router.message(F.text)
    async def support_text_router(message: Message) -> None:
        await handle_support_input(message)

    @router.message()
    async def unsupported_support_input(message: Message) -> None:
        if message.from_user and await db.get_support_session(message.from_user.id):
            await message.answer("Можно отправить текст, фото или видео.")


    return router
