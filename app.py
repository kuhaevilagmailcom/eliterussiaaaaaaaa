from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo

from config import Config
from db import Database
from emoji import EmojiBank
from handlers import build_router
from miniapp import MiniAppServer
from vpn import (
    DemoVpnProvider,
    H1CloudVpnProvider,
    VpnProvider,
    WebhookVpnProvider,
    XuiVpnProvider,
)


def _sqlite_backup(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(source)) as src, sqlite3.connect(str(target)) as dst:
        src.backup(dst)


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


def make_provider(config: Config) -> VpnProvider:
    if config.vpn_mode == "h1cloud":
        return H1CloudVpnProvider(
            api_url=config.h1_api_url,
            api_token=config.h1_api_token,
            subscription_template=config.h1_subscription_template,
            server_name=config.vpn_server_name,
            verify_ssl=config.h1_verify_ssl,
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


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    config = Config.from_env()
    logging.getLogger(__name__).info(
        "MGN VPN build: h1cloud-v20-panel-api | vpn_mode=%s",
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
    emoji = EmojiBank(config.emoji_packs)
    provider = make_provider(config)
    miniapp = MiniAppServer(bot, config, db, provider)

    try:
        await emoji.load(bot)
        await miniapp.start()

        if config.miniapp_url:
            try:
                await bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(
                        text="MGN VPN",
                        web_app=WebAppInfo(url=config.miniapp_url),
                    )
                )
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "Could not set Telegram Mini App menu button: %s",
                    exc,
                )

        dp = Dispatcher()
        dp.include_router(build_router(config, db, emoji, provider))
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass
        await miniapp.close()
        await provider.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
