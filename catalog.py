from __future__ import annotations

from typing import Any


PLANS: dict[str, dict[str, Any]] = {
    "15": {"days": 15, "name": "15 дней", "devices": 5, "price_rub": 49},
    "30": {"days": 30, "name": "1 месяц", "devices": 5, "price_rub": 99},
    "365": {"days": 365, "name": "1 год", "devices": 5, "price_rub": 2000},
    "forever": {"days": 36500, "name": "Навсегда", "devices": 5, "price_rub": 3333},
}

DIAMOND_REWARDS: dict[str, int] = {
    "15": 10,
    "30": 25,
    "365": 400,
    "forever": 700,
}

DIAMOND_SHOP_DAYS: dict[str, dict[str, int]] = {
    "3": {"days": 3, "cost": 60},
    "7": {"days": 7, "cost": 120},
    "30": {"days": 30, "cost": 400},
}

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
