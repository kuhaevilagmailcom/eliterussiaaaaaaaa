from __future__ import annotations

from typing import Any


PLANS: dict[str, dict[str, Any]] = {
    "7": {
        "days": 7,
        "months": 0,
        "name": "7 дней",
        "devices": 1,
        "price_rub": 59,
        "display_savings": 0,
    },
    "30": {
        "days": 30,
        "months": 1,
        "name": "1 месяц",
        "devices": 1,
        "price_rub": 149,
        "display_savings": 0,
    },
    "90": {
        "days": 90,
        "months": 3,
        "name": "3 месяца",
        "devices": 1,
        "price_rub": 349,
        "display_savings": 98,
    },
    "180": {
        "days": 180,
        "months": 6,
        "name": "6 месяцев",
        "devices": 1,
        "price_rub": 599,
        "display_savings": 295,
    },
    "365": {
        "days": 365,
        "months": 12,
        "name": "1 год",
        "devices": 1,
        "price_rub": 1200,
        "display_savings": 789,
    },
}

DIAMOND_REWARDS: dict[str, int] = {
    "7": 5,
    "30": 25,
    "90": 75,
    "180": 150,
    "365": 300,
}

DIAMOND_SHOP_DAYS: dict[str, dict[str, int]] = {
    "3": {"days": 3, "cost": 60},
    "7": {"days": 7, "cost": 120},
    "30": {"days": 30, "cost": 400},
}

BASE_DEVICES = 1
MAX_DEVICES = 5
DEVICE_PRODUCT_CODE = "device"
EXTRA_DEVICE_PRICE_RUB = 100

# Optional reward-store price. Paid device slots use EXTRA_DEVICE_PRICE_RUB.
EXTRA_DEVICE_COST = 250

REFERRAL_TRIAL_REWARD = 15
REFERRAL_FIRST_PAID_REWARD = 30

STAR_RATE_XTR = 50
STAR_RATE_RUB = 87


def plan_price_rub(config, code: str) -> int:
    return int(PLANS[code]["price_rub"])


def plan_price_stars(config, code: str) -> int:
    rub = plan_price_rub(config, code)
    return max(1, (rub * STAR_RATE_XTR + STAR_RATE_RUB - 1) // STAR_RATE_RUB)


def plan_savings_rub(code: str) -> int:
    return max(0, int(PLANS[code].get("display_savings") or 0))


def extra_device_price_stars() -> int:
    return max(
        1,
        (EXTRA_DEVICE_PRICE_RUB * STAR_RATE_XTR + STAR_RATE_RUB - 1)
        // STAR_RATE_RUB,
    )
