from device_identity import (
    device_fingerprint_key,
    enrich_h1_devices,
    subscription_device_hint,
)


def test_subscription_requires_hwid_and_metadata():
    assert subscription_device_hint({"X-Device-Model": "iPhone 13 Pro"}) is None
    assert subscription_device_hint({"X-HWID": "valid-123"}) is None
    assert subscription_device_hint({"X-HWID": "bad hwid", "X-Device-Model": "iPhone"}) is None


def test_happ_headers_get_model_and_os():
    result = subscription_device_hint({
        "x-hwid": "ABC-123456",
        "x-device-model": "iPhone 13 Pro",
        "x-device-os": "iOS",
        "x-ver-os": "18.3",
    })
    assert result == {"hwid": "ABC-123456", "name": "iPhone 13 Pro", "platform": "iOS 18.3"}


def test_unknown_os_has_safe_fallback():
    assert subscription_device_hint({"X-HWID": "abc12345", "X-Device-OS": "Windows", "X-Ver-OS": "11"}) == {
        "hwid": "abc12345", "name": "Windows PC", "platform": "Windows 11",
    }


def test_enrich_requires_confirmed_h1_device():
    hint = {
        "fingerprint_key": device_fingerprint_key(123, "hwid-abcdef"),
        "name": "iPhone 13 Pro",
        "platform": "iOS 18.3",
        "updated_at": "2026-10-14T00:00:00+00:00",
    }
    assert enrich_h1_devices(123, [], [hint]) == []
    result = enrich_h1_devices(123, [{"id": "1", "fingerprint": "hwid-abcdef", "name": "Устройство"}], [hint])
    assert result[0]["name"] == "iPhone 13 Pro"
    assert result[0]["platform"] == "iOS 18.3"
    assert result[0]["profile_updated_at"] == hint["updated_at"]
    assert enrich_h1_devices(999, [{"id": "1", "fingerprint": "hwid-abcdef", "name": "Устройство"}], [hint])[0]["name"] == "Устройство"


def test_count_only_fallback_does_not_invent_extra_devices():
    one = {"fingerprint_key": "a", "name": "iPhone", "platform": "iOS", "updated_at": "today"}
    two = {"fingerprint_key": "b", "name": "Windows", "platform": "Windows", "updated_at": "today"}
    count = [{"id": "count-1", "name": "Подключённое устройство", "fingerprint": ""}]
    assert enrich_h1_devices(7, count, [one])[0]["name"] == "iPhone"
    assert enrich_h1_devices(7, count, [one, two])[0]["name"] == "Подключённое устройство"


def test_happ_base64_like_hwid_is_supported():
    parsed = subscription_device_hint({
        "X-HWID": "UE42LJXu4DbiCaBv==",
        "X-Device-Model": "iPhone 13 Pro",
    })
    assert parsed is not None
    assert parsed["name"] == "iPhone 13 Pro"
