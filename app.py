from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands

from config import Config
from db import Database, from_iso, utcnow
from admin_notify import notify_all_admins, notify_purchase
from emoji import EmojiBank, EmojiFallbackMiddleware
from handlers import build_router
from giveaway import giveaway_reconciliation_loop
from miniapp import MiniAppServer
from payments import RollyPayError, get_payment
from vpn import (
    DemoVpnProvider,
    H1CloudVpnProvider,
    VpnProvider,
    WebhookVpnProvider,
    XuiVpnProvider,
)


class SecretSafeFormatter(logging.Formatter):
    def __init__(self, config):
        super().__init__("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        self.secrets = tuple(value for key, value in vars(config).items()
                             if isinstance(value, str) and value and
                             any(word in key for word in ("token", "secret", "api_key")))

    def format(self, record):
        text = super().format(record)
        for value in self.secrets:
            text = text.replace(value, "[REDACTED]")
        text = re.sub(r"(?:https?|vless|vmess|trojan)://[^\s<>\"']+", "[URL REDACTED]", text)
        text = re.sub(r"/(?:sub|client/[^/]+)/[A-Za-z0-9_%=-]+", "/[PRIVATE LINK]", text)
        return re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b", "[BOT TOKEN]", text)


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.parent.chmod(0o700)
    except OSError:
        pass
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)
    try:
        target.chmod(0o600)
    except OSError:
        pass


def prepare_persistent_database(db_path: str) -> None:
    """Migrate a legacy root DB once and keep the canonical DB in /app/data."""
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        return

    candidates = (
        Path("/app/mgn_vpn.sqlite3"),
        Path.cwd() / "mgn_vpn.sqlite3",
        Path("/app/mgn_vpn.db"),
        Path.cwd() / "mgn_vpn.db",
    )
    seen: set[Path] = set()
    for source in candidates:
        try:
            resolved = source.resolve()
        except OSError:
            resolved = source
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if source.resolve() == target.resolve():
                continue
        except OSError:
            pass
        if not source.exists() or source.stat().st_size <= 0:
            continue

        _sqlite_backup(source, target)
        logging.getLogger(__name__).warning(
            "Migrated legacy SQLite database %s -> %s",
            source,
            target,
        )
        return


def create_persistent_backup(db_path: str, *, keep: int = 5) -> Path | None:
    source = Path(db_path)
    if not source.exists() or source.stat().st_size <= 0:
        return None

    backup_dir = source.parent / "backups"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{source.stem}-{stamp}.sqlite3"
    _sqlite_backup(source, target)

    backups = sorted(
        backup_dir.glob(f"{source.stem}-*.sqlite3"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for old in backups[max(1, keep):]:
        try:
            old.unlink()
        except OSError:
            logging.getLogger(__name__).warning(
                "Could not remove old database backup %s",
                old,
            )
    return target


async def database_backup_loop(db_path: str) -> None:
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(6 * 60 * 60)
        try:
            path = await asyncio.to_thread(create_persistent_backup, db_path)
            if path:
                logger.info("SQLite safety backup created: %s", path)
        except Exception:
            logger.exception("Could not create scheduled SQLite safety backup")


async def send_expiry_notifications_once(bot: Bot, db: Database, config: Config) -> int:
    """Send each 3/2/1-day reminder once for the exact subscription expiry."""
    logger = logging.getLogger(__name__)
    sent = 0
    for item in await db.list_due_expiry_notifications():
        user_id = int(item["telegram_id"])
        days = int(item["days_before"])
        expires_at = datetime.fromisoformat(
            str(item["subscription_until"]).replace("Z", "+00:00")
        ).astimezone(config.display_tz)
        if not await db.claim_expiry_notification(
            user_id, str(item["subscription_until"]), days
        ):
            continue
        day_word = "день" if days == 1 else "дня"
        text = (
            "⏳ <b>Подписка MGN VPN скоро закончится</b>\n\n"
            f"До окончания осталось: <b>{days} {day_word}</b>\n"
            f"Работает до: <b>{expires_at:%d.%m.%Y · %H:%M}</b>\n\n"
            "Продлите подписку заранее, чтобы VPN не отключился."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Продлить VPN", callback_data="plans")]]
        )
        try:
            await bot.send_message(user_id, text, reply_markup=keyboard)
            sent += 1
        except Exception as exc:
            await db.release_expiry_notification(
                user_id, str(item["subscription_until"]), days
            )
            logger.warning(
                "Could not send expiry reminder to %s: %s", user_id, type(exc).__name__
            )
    return sent


async def expiry_notification_loop(bot: Bot, db: Database, config: Config) -> None:
    logger = logging.getLogger(__name__)
    while True:
        try:
            sent = await send_expiry_notifications_once(bot, db, config)
            if sent:
                logger.info("Subscription expiry reminders sent: %s", sent)
        except Exception:
            logger.exception("Subscription expiry reminder pass failed")
        await asyncio.sleep(60 * 60)


async def payment_reconciliation_loop(
    bot: Bot,
    config: Config,
    db: Database,
    provider: VpnProvider,
) -> None:
    logger = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(60)
        if not config.rollypay_enabled:
            continue
        for local in await db.list_sbp_for_reconciliation():
            payment_id = str(local["payment_id"])
            try:
                remote = await get_payment(config, payment_id)
                status = str(remote.get("status") or "").lower()
                try:
                    amount = Decimal(str(remote.get("amount")))
                except (InvalidOperation, ValueError):
                    amount = Decimal("-1")
                valid = (
                    str(remote.get("payment_id") or "") == payment_id
                    and str(remote.get("order_id") or "") == str(local["order_id"])
                    and str(remote.get("currency") or remote.get("payment_currency") or "").upper() == "RUB"
                    and amount.is_finite()
                    and amount == Decimal(int(local["amount_rub"]))
                )
                if not valid:
                    logger.error("Payment reconciliation mismatch for local order %s", local["order_id"])
                    continue
                if status == "paid" and str(local["status"]) != "paid":
                    target_id = int(
                        local.get("target_telegram_id") or local["telegram_id"]
                    )
                    before = await db.get_user(target_id)
                    if await db.settle_sbp_payment(payment_id):
                        user = await db.get_user(target_id)
                        try:
                            await asyncio.wait_for(provider.provision(user), 10.0)
                        except Exception as exc:
                            logger.warning("Payment provision deferred: %s", type(exc).__name__)
                        before_until = from_iso(before.get("subscription_until"))
                        was_active = bool(before_until and before_until > utcnow())
                        await notify_purchase(
                            bot,
                            db,
                            config,
                            buyer_id=int(local["telegram_id"]),
                            target_id=target_id,
                            product_code=str(local["plan_code"]),
                            method="СБП",
                            amount_text=f"{int(local['amount_rub'])} ₽",
                            purchase_kind=(
                                "Дополнительное устройство"
                                if str(local["plan_code"]) == "device"
                                else "Продление" if was_active else "Новая подписка"
                            ),
                            promo_code=str(local.get("promo_code") or "") or None,
                        )
                elif status in {"refunded", "chargeback", "canceled", "expired"}:
                    old_status = str(local.get("status") or "").lower()
                    await db.set_sbp_status(payment_id, status)
                    if status in {"refunded", "chargeback"} and old_status != status:
                        await notify_all_admins(
                            bot,
                            db,
                            config,
                            "⚠️ <b>Платёж изменил статус</b>\n\n"
                            f"Платёж: <code>{payment_id}</code>\n"
                            f"Пользователь: <code>{int(local['telegram_id'])}</code>\n"
                            f"Сумма: <b>{int(local['amount_rub'])} ₽</b>\n"
                            f"Статус: <b>{status}</b>\n\n"
                            "<i>Проверьте подписку пользователя и возврат вручную.</i>",
                        )
            except (RollyPayError, ValueError, KeyError) as exc:
                logger.warning("Payment reconciliation failed for %s: %s", local["order_id"], type(exc).__name__)


async def server_status_alert_loop(
    bot: Bot,
    db: Database,
    config: Config,
    provider: VpnProvider,
) -> None:
    """Notify all admins only after a server state change is confirmed twice."""
    logger = logging.getLogger(__name__)
    stable: dict[str, bool] | None = None
    pending: dict[str, tuple[bool, int]] = {}

    while True:
        try:
            sample_users = await db.list_active_users_for_vpn_sync(limit=1)
            sample = sample_users[0] if sample_users else None
            report = await asyncio.wait_for(
                provider.server_diagnostics(sample),
                timeout=15.0,
            )
            current: dict[str, bool] = {}
            labels: dict[str, str] = {}
            for item in report.get("servers") or []:
                key = str(
                    item.get("catalog_id")
                    or item.get("id")
                    or item.get("name")
                    or ""
                ).strip()
                if not key:
                    continue
                current[key] = bool(item.get("available"))
                labels[key] = str(item.get("name") or key)

            if stable is None:
                stable = dict(current)
            else:
                for key, state in current.items():
                    previous = stable.get(key)
                    if previous is None:
                        stable[key] = state
                        continue
                    if previous == state:
                        pending.pop(key, None)
                        continue
                    pending_state, count = pending.get(key, (state, 0))
                    count = count + 1 if pending_state == state else 1
                    pending[key] = (state, count)
                    if count < 2:
                        continue

                    stable[key] = state
                    pending.pop(key, None)
                    icon = "✅" if state else "❌"
                    status = "снова доступен" if state else "стал недоступен"
                    await notify_all_admins(
                        bot,
                        db,
                        config,
                        f"{icon} <b>Изменение VPN-сервера</b>\n\n"
                        f"{labels.get(key, key)} — <b>{status}</b>.\n"
                        "Пользовательские конфиги автоматически не удаляются.",
                    )

                for key in list(stable):
                    if key not in current:
                        # Missing discovery alone is not treated as an outage.
                        pending.pop(key, None)
        except Exception as exc:
            logger.warning(
                "Server status alert check failed: %s",
                type(exc).__name__,
            )
        await asyncio.sleep(2 * 60)


async def sync_active_vpn_users_once(
    db: Database,
    provider: VpnProvider,
    miniapp: MiniAppServer,
) -> tuple[int, int]:
    """Repair every active H1 client across all currently linked locations."""
    logger = logging.getLogger(__name__)
    semaphore = asyncio.Semaphore(3)
    synced = 0
    failed = 0

    async def sync_user(user: dict) -> bool:
        async with semaphore:
            try:
                await asyncio.wait_for(provider.provision(user), timeout=50.0)
                await miniapp.invalidate_subscription_cache(str(user.get("sub_token") or ""))
                return True
            except Exception as exc:
                logger.warning(
                    "H1Cloud active-user sync failed for %s: %s",
                    user.get("telegram_id"),
                    str(exc).strip() or type(exc).__name__,
                )
                return False

    offset = 0
    while True:
        users = await db.list_active_users_for_vpn_sync(limit=100, offset=offset)
        if not users:
            break
        results = await asyncio.gather(*(sync_user(user) for user in users))
        synced += sum(1 for result in results if result)
        failed += sum(1 for result in results if not result)
        offset += len(users)

    logger.info("H1Cloud active-user sync complete: %s synced, %s failed", synced, failed)
    return synced, failed


async def vpn_federation_reconciliation_loop(
    db: Database,
    provider: VpnProvider,
    miniapp: MiniAppServer,
) -> None:
    while True:
        try:
            await sync_active_vpn_users_once(db, provider, miniapp)
        except Exception:
            logging.getLogger(__name__).exception(
                "H1Cloud active-user reconciliation pass failed"
            )
        await asyncio.sleep(15 * 60)


def make_provider(config: Config) -> VpnProvider:
    if config.vpn_mode == "h1cloud":
        return H1CloudVpnProvider(
            api_url=config.h1_api_url,
            api_token=config.h1_api_token,
            subscription_template=config.h1_subscription_template,
            server_name=config.vpn_server_name,
            verify_ssl=config.h1_verify_ssl,
            ca_file=config.h1_ca_file,
            subscription_hosts=config.h1_subscription_hosts,
            allow_insecure=config.allow_insecure_h1,
        )

    if config.vpn_mode == "3xui":
        return XuiVpnProvider(
            panel_url=config.xui_url,
            api_token=config.xui_token,
            inbound_ids=config.xui_inbound_ids,
            subscription_template=config.xui_subscription_template,
            server_name=config.vpn_server_name,
            verify_ssl=config.xui_verify_ssl,
        )

    if config.vpn_mode == "webhook":
        return WebhookVpnProvider(
            api_url=config.vpn_api_url,
            api_token=config.vpn_api_token,
        )

    return DemoVpnProvider(
        base_url=config.vpn_sub_base_url,
        server_name=config.vpn_server_name,
    )


async def notify_admins_restarted(
    bot: Bot,
    config: Config,
    db: Database,
    provider: VpnProvider,
) -> None:
    """Notify every configured/stored admin after a successful startup."""
    logger = logging.getLogger(__name__)
    recipients = {int(value) for value in config.admin_ids}
    try:
        recipients.update(
            int(item["telegram_id"])
            for item in await db.list_admin_roles()
        )
    except Exception:
        logger.exception("Could not load admin recipients for restart notice")

    if not recipients:
        return

    started_at = datetime.now(config.display_tz).strftime("%d.%m.%Y · %H:%M:%S")
    vpn_status = "ожидает подключения"
    if getattr(provider, "service_ready", True):
        try:
            health = await asyncio.wait_for(provider.health(), timeout=2.5)
            if isinstance(health, dict) and "ok" in health:
                provider_ok = bool(health.get("ok"))
            else:
                provider_ok = True
            vpn_status = "работает" if provider_ok else "недоступен"
        except Exception as exc:
            vpn_status = "недоступен"
            logger.warning(
                "Provider readiness check failed during startup: %s",
                type(exc).__name__,
            )
    text = (
        "♻️ <b>MGN VPN перезапущен</b>\n\n"
        f"🕒 <b>Время:</b> {started_at}\n"
        "✅ <b>Бот:</b> запущен\n"
        "🌐 <b>Mini App:</b> запущен\n"
        f"🔐 <b>VPN:</b> {vpn_status}\n\n"
        "<i>Сервис снова принимает команды.</i>"
    )

    for admin_id in sorted(recipients):
        try:
            await bot.send_message(chat_id=admin_id, text=text)
        except Exception as exc:
            logger.warning(
                "Could not send restart notice to admin %s: %s",
                admin_id,
                type(exc).__name__,
            )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = Config.from_env()
    for handler in logging.getLogger().handlers:
        handler.setFormatter(SecretSafeFormatter(config))
    logging.getLogger(__name__).info(
        "MGN VPN build: production | vpn_mode=%s",
        config.vpn_mode,
    )
    prepare_persistent_database(config.db_path)
    db = Database(config.db_path)
    await db.init()
    logging.getLogger(__name__).info("Persistent SQLite path: %s", config.db_path)

    try:
        backup_path = await asyncio.to_thread(create_persistent_backup, config.db_path)
        if backup_path:
            logging.getLogger(__name__).info(
                "SQLite startup backup created: %s",
                backup_path,
            )
    except Exception:
        logging.getLogger(__name__).exception(
            "Could not create SQLite startup backup"
        )

    backup_task = asyncio.create_task(database_backup_loop(config.db_path))

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(EmojiFallbackMiddleware())
    emoji = EmojiBank(config.emoji_packs)
    provider = make_provider(config)
    miniapp = MiniAppServer(bot, config, db, provider)
    payment_task = asyncio.create_task(
        payment_reconciliation_loop(bot, config, db, provider)
    )
    expiry_task = asyncio.create_task(expiry_notification_loop(bot, db, config))
    giveaway_task = asyncio.create_task(
        giveaway_reconciliation_loop(bot, db, config, provider)
    )
    server_alert_task = asyncio.create_task(
        server_status_alert_loop(bot, db, config, provider)
    )
    federation_task = (
        asyncio.create_task(vpn_federation_reconciliation_loop(db, provider, miniapp))
        if config.vpn_mode == "h1cloud"
        else None
    )

    try:
        await emoji.load(bot)
        await miniapp.start()

        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="menu", description="Вернуться в главное меню"),
                    BotCommand(command="sub", description="Моя подписка"),
                    BotCommand(command="plans", description="Тарифы и покупка VPN"),
                    BotCommand(command="profile", description="Мой профиль"),
                ]
            )
            await bot.set_chat_menu_button(
                menu_button=MenuButtonCommands()
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Could not configure Telegram command menu: %s",
                exc,
            )

        await notify_admins_restarted(bot, config, db, provider)

        dp = Dispatcher()
        dp.include_router(build_router(config, db, emoji, provider))
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        backup_task.cancel()
        payment_task.cancel()
        expiry_task.cancel()
        giveaway_task.cancel()
        server_alert_task.cancel()
        if federation_task:
            federation_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass
        try:
            await payment_task
        except asyncio.CancelledError:
            pass
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        try:
            await giveaway_task
        except asyncio.CancelledError:
            pass
        try:
            await server_alert_task
        except asyncio.CancelledError:
            pass
        if federation_task:
            try:
                await federation_task
            except asyncio.CancelledError:
                pass
        await miniapp.close()
        await provider.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
