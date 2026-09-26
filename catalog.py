from __future__ import annotations

from typing import Any


PLANS: dict[str, dict[str, Any]] = {
    "7": {
        "days": 7,
        "months": 0,
        "name": "7 дней",
        "devices": 1,
        "price_rub": 59,
    },
    "30": {
        "days": 30,
        "months": 1,
        "name": "1 месяц",
        "devices": 1,
        "price_rub": 149,
    },
    "90": {
        "days": 90,
        "months": 3,
        "name": "3 месяца",
        "devices": 1,
        "price_rub": 349,
    },
    "180": {
        "days": 180,
        "months": 6,
        "name": "6 месяцев",
        "devices": 1,
        "price_rub": 599,
    },
    "365": {
        "days": 365,
        "months": 12,
        "name": "1 год",
        "devices": 1,
        "price_rub": 1200,
    },
}

BASE_DEVICES = 1
MAX_DEVICES = 5
DEVICE_PRODUCT_CODE = "device"
EXTRA_DEVICE_PRICE_RUB = 100
CHANNEL_BONUS_DAYS = 1
REFERRAL_REWARD_DAYS = 1
MAX_REFERRAL_REWARDS = 3

STAR_RATE_XTR = 50
STAR_RATE_RUB = 87


def plan_price_rub(config, code: str) -> int:
    return int(PLANS[code]["price_rub"])


def plan_price_stars(config, code: str) -> int:
    return rub_to_stars(plan_price_rub(config, code))


def plan_savings_rub(code: str) -> int:
    months = int(PLANS[code].get("months") or 0)
    if months < 2:
        return 0
    monthly_price = int(PLANS["30"]["price_rub"])
    return max(0, monthly_price * months - plan_price_rub(None, code))


def clamp_device_limit(value: int) -> int:
    return min(MAX_DEVICES, max(BASE_DEVICES, int(value)))


def discounted_price_rub(code: str, discount_percent: int = 0) -> tuple[int, int, int]:
    original = plan_price_rub(None, code)
    percent = min(100, max(0, int(discount_percent)))
    discount = original * percent // 100
    return original, discount, max(0, original - discount)


def rub_to_stars(amount_rub: int) -> int:
    amount = max(0, int(amount_rub))
    return max(1, (amount * STAR_RATE_XTR + STAR_RATE_RUB - 1) // STAR_RATE_RUB)


def extra_device_price_stars() -> int:
    return rub_to_stars(EXTRA_DEVICE_PRICE_RUB)
