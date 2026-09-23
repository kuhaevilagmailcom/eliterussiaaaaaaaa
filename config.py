from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()


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

    vpn_mode: str
    vpn_sub_base_url: str
    vpn_api_url: str
    vpn_api_token: str
    vpn_server_name: str

    xui_url: str
    xui_token: str
    xui_inbound_ids: tuple[int, ...]
    xui_subscription_template: str
    xui_verify_ssl: bool

    trial_minutes: int
    trial_max_devices: int
    trial_channel_username: str
    trial_channel_url: str

    emoji_packs: tuple[str, ...]

    rollypay_api_base: str
    rollypay_terminal_id: str
    rollypay_api_key: str
    rollypay_signing_secret: str
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

        mode = os.getenv("VPN_MODE", "demo").strip().lower()
        if mode not in {"demo", "webhook", "3xui"}:
            raise RuntimeError("VPN_MODE must be demo, webhook or 3xui")

        return cls(
            bot_token=token,
            admin_ids=_ints(os.getenv("ADMIN_IDS", "8464597898")),
            db_path=os.getenv("DB_PATH", "mgn_vpn.sqlite3"),
            display_tz=ZoneInfo(os.getenv("DISPLAY_TZ", "Asia/Yekaterinburg")),
            vpn_mode=mode,
            vpn_sub_base_url=os.getenv(
                "VPN_SUB_BASE_URL",
                "https://vpn.example.com/sub",
            ).rstrip("/"),
            vpn_api_url=os.getenv("VPN_API_URL", "").rstrip("/"),
            vpn_api_token=os.getenv("VPN_API_TOKEN", ""),
            vpn_server_name=os.getenv("VPN_SERVER_NAME", "MGN VPN"),
            xui_url=os.getenv("XUI_URL", "").rstrip("/"),
            xui_token=os.getenv("XUI_TOKEN", "").strip(),
            xui_inbound_ids=_ints(os.getenv("XUI_INBOUND_IDS", "")),
            xui_subscription_template=os.getenv(
                "XUI_SUBSCRIPTION_TEMPLATE",
                "",
            ).strip(),
            xui_verify_ssl=_bool(os.getenv("XUI_VERIFY_SSL", "true")),
            trial_minutes=max(1, int(os.getenv("TRIAL_MINUTES", "60"))),
            trial_max_devices=max(1, int(os.getenv("TRIAL_MAX_DEVICES", "1"))),
            trial_channel_username=os.getenv(
                "TRIAL_CHANNEL_USERNAME",
                "@mgnvpnn",
            ).strip(),
            trial_channel_url=os.getenv(
                "TRIAL_CHANNEL_URL",
                "https://t.me/mgnvpnn",
            ).strip(),
            emoji_packs=tuple(
                x.strip()
                for x in os.getenv(
                    "EMOJI_PACKS",
                    "CryptoGIFTPODARKI,TgAndroidIcons,progressBarEmoji",
                ).split(",")
                if x.strip()
            ),
            rollypay_api_base=os.getenv(
                "ROLLYPAY_API_BASE",
                "https://api.rollypay.io",
            ).rstrip("/"),
            rollypay_terminal_id=os.getenv("ROLLYPAY_TERMINAL_ID", "").strip(),
            rollypay_api_key=os.getenv("ROLLYPAY_API_KEY", "").strip(),
            rollypay_signing_secret=os.getenv(
                "ROLLYPAY_SIGNING_SECRET",
                "",
            ).strip(),
            rollypay_test_mode=_bool(
                os.getenv("ROLLYPAY_TEST_MODE", "false"),
                default=False,
            ),
        )
