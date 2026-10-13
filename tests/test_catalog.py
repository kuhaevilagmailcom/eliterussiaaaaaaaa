from catalog import (
    PLANS,
    clamp_device_limit,
    discounted_price_rub,
    plan_price_rub,
    plan_savings_rub,
)


def test_prices_and_savings_are_centralized():
    assert {code: plan_price_rub(None, code) for code in PLANS} == {
        "30": 70,
        "90": 170,
        "180": 420,
        "365": 920,
    }
    assert {code: plan_savings_rub(code) for code in ("90", "180", "365")} == {
        "90": 40,
        "180": 0,
        "365": 0,
    }


def test_device_limits_and_discount_math():
    assert clamp_device_limit(0) == 2
    assert clamp_device_limit(6) == 5
    assert discounted_price_rub("90", 20) == (170, 34, 136)


def test_base_plan_includes_two_devices_and_extra_slot_pricing():
    from catalog import BASE_DEVICES, MAX_DEVICES, plan_total_price_rub, plan_total_price_stars, rub_to_stars
    assert BASE_DEVICES == 2
    assert MAX_DEVICES == 5
    for code, base in {"30": 70, "90": 170, "180": 420, "365": 920}.items():
        for selected in range(2, 6):
            expected = base + (selected - 2) * 50
            assert plan_total_price_rub(None, code, selected) == expected
            assert plan_total_price_stars(None, code, selected) == rub_to_stars(expected)
