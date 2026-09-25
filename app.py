from __future__ import annotations

import asyncio
import logging

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
        "MGN VPN build: h1cloud-v3-inbounds | vpn_mode=%s",
        config.vpn_mode,
    )
    db = Database(config.db_path)
    await db.init()

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
        await miniapp.close()
        await provider.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
