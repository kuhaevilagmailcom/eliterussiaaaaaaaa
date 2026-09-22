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
    trial_traffic_gb: int
    trial_max_devices: int

    plan_30_price: int
    plan_90_price: int
    plan_365_price: int

    emoji_packs: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is empty. Fill .env first.")

        mode = os.getenv("VPN_MODE", "demo").strip().lower()
        if mode not in {"demo", "webhook", "3xui"}:
            raise RuntimeError("VPN_MODE must be demo, webhook or 3xui")

        return cls(
            bot_token=token,
            admin_ids=_ints(os.getenv("ADMIN_IDS", "")),
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
            trial_traffic_gb=max(1, int(os.getenv("TRIAL_TRAFFIC_GB", "10"))),
            trial_max_devices=max(1, int(os.getenv("TRIAL_MAX_DEVICES", "1"))),
            plan_30_price=max(1, int(os.getenv("PLAN_30_PRICE", "150"))),
            plan_90_price=max(1, int(os.getenv("PLAN_90_PRICE", "350"))),
            plan_365_price=max(1, int(os.getenv("PLAN_365_PRICE", "990"))),
            emoji_packs=tuple(
                x.strip()
                for x in os.getenv(
                    "EMOJI_PACKS",
                    "CryptoGIFTPODARKI,TgAndroidIcons,progressBarEmoji",
                ).split(",")
                if x.strip()
            ),
        )
