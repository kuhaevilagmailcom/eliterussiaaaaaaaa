from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# One canonical HTTPS origin for the whole product. BotHost issues SSL for
# mgnvpn.ru, so Mini App, API, client redirects and subscriptions all stay
# under this host.
DEFAULT_PUBLIC_BASE_URL = "https://mgnvpn.ru"
DEFAULT_MINIAPP_URL = "https://mgnvpn.ru/app"
DEFAULT_SUBSCRIPTION_BASE_URL = "https://mgnvpn.ru/sub"
LEGACY_PUBLIC_BASE_URLS = {
    "https://bot-1789383103-4489-furadev.bothost.tech",
    "http://bot-1789383103-4489-furadev.bothost.tech",
    "https://bot-1790078948-4568-furadev.bothost.tech",
    "http://bot-1790078948-4568-furadev.bothost.tech",
    "https://sub.mgnvpn.ru",
    "http://sub.mgnvpn.ru",
}



def _ints(value: str) -> tuple[int, ...]:
    result: list[int] = []
    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(int(item))
    return tuple(result)


def _bool(value: str, default: bool = True) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: tuple[int, ...]
    db_path: str
    display_tz: ZoneInfo

    miniapp_url: str
    channel_url: str
    main_menu_banner_file_id: str
    miniapp_host: str
    miniapp_port: int
    miniapp_initdata_max_age: int

    vpn_mode: str
    vpn_sub_base_url: str
    vpn_api_url: str
    vpn_api_token: str
    vpn_server_name: str

    h1_api_url: str
    h1_api_token: str
    h1_subscription_template: str
    h1_verify_ssl: bool
    h1_ca_file: str
    h1_subscription_hosts: tuple[str, ...]
    allow_insecure_h1: bool
    trusted_proxy_ips: tuple[str, ...]
    happ_provider_id: str

    xui_url: str
    xui_token: str
    xui_inbound_ids: tuple[int, ...]
    xui_subscription_template: str
    xui_verify_ssl: bool

    emoji_packs: tuple[str, ...]

    rollypay_api_base: str
    rollypay_terminal_id: str
    rollypay_api_key: str
    rollypay_test_mode: bool

    @property
    def rollypay_enabled(self) -> bool:
        return bool(
            self.rollypay_api_key
            and self.rollypay_api_key.upper() not in {"CHANGE_ME", "YOUR_TOKEN"}
        )

    @classmethod
    def from_env(cls) -> "Config":
        token = (
            os.getenv("BOT_TOKEN", "").strip()
            or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            or os.getenv("API_TOKEN", "").strip()
            or os.getenv("TOKEN", "").strip()
        )
        if not token:
            raise RuntimeError("Telegram bot token is empty.")

        mode = os.getenv("VPN_MODE", "h1cloud").strip().lower()
        if mode not in {"demo", "webhook", "h1cloud", "3xui"}:
            raise RuntimeError(
                "VPN_MODE must be demo, webhook, h1cloud or 3xui"
            )

        raw_db_path = os.getenv("DB_PATH", "").strip()
        if Path("/app").exists():
            # Bothost preserves /app/data across Git deploys/restarts. Never
            # keep SQLite in /app root inside the disposable container.
            if raw_db_path:
                requested = Path(raw_db_path)
                if (
                    not requested.is_absolute()
                    or (
                        str(requested).startswith("/app/")
                        and not str(requested).startswith("/app/data/")
                    )
                ):
                    db_path = str(Path("/app/data") / requested.name)
                else:
                    db_path = str(requested)
            else:
                db_path = "/app/data/mgn_vpn.sqlite3"
        else:
            db_path = raw_db_path or "mgn_vpn.sqlite3"

        # Public credentials must never inherit a hosting or request domain.
        miniapp_url = DEFAULT_MINIAPP_URL
        configured_subscription = DEFAULT_SUBSCRIPTION_BASE_URL

        return cls(
            bot_token=token,
            admin_ids=_ints(os.getenv("ADMIN_IDS", "")),
            db_path=db_path,
            display_tz=ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Yekaterinburg")),
            miniapp_url=miniapp_url,
            channel_url=os.getenv(
                "CHANNEL_URL",
                "https://t.me/mgnvpnn",
            ).strip() or "https://t.me/mgnvpnn",
            main_menu_banner_file_id=os.getenv(
                "MAIN_MENU_BANNER_FILE_ID",
                "",
            ).strip(),
            miniapp_host=os.getenv("MINIAPP_HOST", "0.0.0.0").strip() or "0.0.0.0",
            miniapp_port=max(
                1,
                int(os.getenv("PORT", os.getenv("MINIAPP_PORT", "3000"))),
            ),
            miniapp_initdata_max_age=max(
                60,
                int(os.getenv("MINIAPP_INITDATA_MAX_AGE", "3600")),
            ),
            vpn_mode=mode,
            vpn_sub_base_url=configured_subscription.rstrip("/"),
            vpn_api_url=os.getenv("VPN_API_URL", "").rstrip("/"),
            vpn_api_token=os.getenv("VPN_API_TOKEN", ""),
            vpn_server_name=os.getenv("VPN_SERVER_NAME", "MGN VPN"),
            h1_api_url=os.getenv("H1_API_URL", "").rstrip("/"),
            h1_api_token=os.getenv("H1_API_TOKEN", "").strip(),
            h1_subscription_template=os.getenv(
                "H1_SUBSCRIPTION_TEMPLATE",
                "",
            ).strip(),
            h1_verify_ssl=_bool(os.getenv("H1_VERIFY_SSL", "true"), default=True),
            h1_ca_file=os.getenv("H1_CA_FILE", "").strip(),
            h1_subscription_hosts=tuple(
                host.strip().lower().rstrip(".")
                for host in os.getenv("H1_SUBSCRIPTION_HOSTS", ".h1cloud.net").split(",")
                if host.strip()
            ),
            allow_insecure_h1=_bool(os.getenv("ALLOW_INSECURE_H1", "false"), default=False),
            trusted_proxy_ips=tuple(
                value.strip()
                for value in os.getenv("TRUSTED_PROXY_IPS", "").split(",")
                if value.strip()
            ),
            happ_provider_id=os.getenv("HAPP_PROVIDER_ID", "").strip(),
            xui_url=os.getenv("XUI_URL", "").rstrip("/"),
            xui_token=os.getenv("XUI_TOKEN", "").strip(),
            xui_inbound_ids=_ints(os.getenv("XUI_INBOUND_IDS", "")),
            xui_subscription_template=os.getenv(
                "XUI_SUBSCRIPTION_TEMPLATE",
                "",
            ).strip(),
            xui_verify_ssl=_bool(os.getenv("XUI_VERIFY_SSL", "true")),
            emoji_packs=tuple(
                dict.fromkeys(
                    [
                        x.strip()
                        for x in os.getenv(
                            "EMOJI_PACKS",
                            "CryptoGIFTPODARKI,TgAndroidIcons,progressBarEmoji,NewsEmoji",
                        ).split(",")
                        if x.strip()
                    ]
                    + ["NewsEmoji"]
                )
            ),
            rollypay_api_base=os.getenv(
                "ROLLYPAY_API_BASE",
                "https://api.rollypay.io",
            ).rstrip("/"),
            rollypay_terminal_id=os.getenv("ROLLYPAY_TERMINAL_ID", "").strip(),
            rollypay_api_key=os.getenv("ROLLYPAY_API_KEY", "").strip(),
            rollypay_test_mode=_bool(
                os.getenv("ROLLYPAY_TEST_MODE", "false"),
                default=False,
            ),
        )
