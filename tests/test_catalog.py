from catalog import (
    PLANS,
    clamp_device_limit,
    discounted_price_rub,
    plan_price_rub,
    plan_savings_rub,
)


def test_prices_and_savings_are_centralized():
    assert {code: plan_price_rub(None, code) for code in PLANS} == {
        "7": 59,
        "30": 149,
        "90": 349,
        "180": 599,
        "365": 1200,
    }
    assert {code: plan_savings_rub(code) for code in ("90", "180", "365")} == {
        "90": 98,
        "180": 295,
        "365": 588,
    }


def test_device_limits_and_discount_math():
    assert clamp_device_limit(0) == 1
    assert clamp_device_limit(6) == 5
    assert discounted_price_rub("90", 20) == (349, 69, 280)
