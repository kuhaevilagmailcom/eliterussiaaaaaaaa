from device_identity import (
    device_fingerprint_key,
    h1_panel_device_id,
    enrich_h1_devices,
    subscription_device_hint,
    subscription_public_ip,
    merge_profiles_with_panel_ips,
)


def test_subscription_requires_hwid_but_not_optional_model_or_os():
    assert subscription_device_hint({"X-Device-Model": "iPhone 13 Pro"}) is None
    assert subscription_device_hint({"X-HWID": "valid-123"}) == {
        "hwid": "valid-123", "name": "Устройство", "platform": "",
    }
    assert subscription_device_hint({"X-HWID": "valid-123", "User-Agent": "Happ/3.14"}) == {
        "hwid": "valid-123", "name": "Устройство Happ", "platform": "",
    }
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
    assert enrich_h1_devices(7, count, [one])[0]["name"] == "Подключённое устройство"
    assert enrich_h1_devices(7, count, [one, two])[0]["name"] == "Подключённое устройство"


def test_happ_base64_like_hwid_is_supported():
    parsed = subscription_device_hint({
        "X-HWID": "UE42LJXu4DbiCaBv==",
        "X-Device-Model": "iPhone 13 Pro",
    })
    assert parsed is not None
    assert parsed["name"] == "iPhone 13 Pro"


def test_native_h1_sha256_identifier_matches_its_metadata():
    hwid = "Happ-Device-2026"
    hint = {
        "fingerprint_key": device_fingerprint_key(100, hwid),
        "panel_id": h1_panel_device_id(hwid),
        "name": "Samsung Galaxy S25",
        "platform": "Android 16",
        "updated_at": "2026-10-14T00:00:00+00:00",
    }
    h1_row = {"id": h1_panel_device_id(hwid), "name": "Устройство", "fingerprint": ""}
    named = enrich_h1_devices(100, [h1_row], [hint])
    assert named[0]["name"] == "Samsung Galaxy S25"
    assert named[0]["identity_source"] == "client-reported"


def test_h1_unrelated_fallback_device_not_relabelled():
    hint = {
        "fingerprint_key": "somehashedkey",
        "panel_id": h1_panel_device_id("Phone-Correct"),
        "name": "iPhone 13 Pro",
        "platform": "iOS",
        "updated_at": "2026-10-14T00:00:00+00:00",
    }
    unrelated = [{"id": "fallback:123abc", "name": "Устройство", "fingerprint": ""}]
    assert enrich_h1_devices(1, unrelated, [hint])[0]["name"] == "Устройство"


def test_device_alias_header_and_user_agent_os_fallback():
    result = subscription_device_hint({
        "X-Happ-HWID": "happ-device-012345",
        "User-Agent": "Happ/3.0 (Android 15)",
    })
    assert result == {
        "hwid": "happ-device-012345",
        "name": "Android",
        "platform": "Android",
    }


def test_model_is_cleaned_for_html_and_long_inputs():
    result = subscription_device_hint({
        "X-HWID": "hwid-1234567",
        "X-Device-Model": "<script>iPhone\\n13 Pro</script>",
        "X-Device-OS": "iOS",
    })
    assert result is not None
    assert "<" not in result["name"]
    assert ">" not in result["name"]
    assert len(result["name"]) <= 80

def test_device_ip_overlay_requires_matching_identity():
    hwid = "phone-one-2026"
    hint = {
        "fingerprint_key": device_fingerprint_key(777, hwid),
        "panel_id": h1_panel_device_id(hwid),
        "name": "Samsung Galaxy",
        "platform": "Android",
        "ip_address": "8.8.8.8",
        "updated_at": "2026-10-15T00:00:00+00:00",
    }
    device = {"id": hint["panel_id"], "name": "Устройство"}
    matched = enrich_h1_devices(777, [device], [hint])
    assert matched[0]["name"] == "Samsung Galaxy"
    assert matched[0]["ip_address"] == "8.8.8.8"
    assert matched[0]["ip_source"] == "subscription_refresh"
    vpn_ip = enrich_h1_devices(777, [dict(device, ip_address="1.1.1.1")], [hint])
    assert vpn_ip[0]["ip_address"] == "1.1.1.1"
    unrelated = enrich_h1_devices(777, [{"id": "different"}], [hint])
    assert not unrelated[0].get("ip_address")



def test_bothost_internal_proxy_resolves_client_ip():
    headers = {"X-Forwarded-For": "8.8.8.8", "X-Real-IP": "8.8.8.8"}
    assert subscription_public_ip("172.18.0.3", headers, bothost=True) == "8.8.8.8"
    assert subscription_public_ip("127.0.0.1", headers, bothost=True) == "8.8.8.8"
    assert subscription_public_ip("172.18.0.3", headers, bothost=False) == ""
    assert subscription_public_ip("172.18.0.3", headers, trusted_proxy_ips=("172.18.0.3",)) == "8.8.8.8"


def test_client_ip_does_not_trust_headers_from_public_peer():
    headers = {"X-Forwarded-For": "1.1.1.1", "CF-Connecting-IP": "9.9.9.9"}
    assert subscription_public_ip("8.8.8.8", headers, bothost=True) == "8.8.8.8"


def test_forwarded_chain_prefers_last_public_hop_not_spoofed_prefix():
    headers = {"X-Forwarded-For": "1.1.1.1, 8.8.8.8, 172.18.0.4"}
    assert subscription_public_ip("172.18.0.3", headers, bothost=True) == "8.8.8.8"


def test_proxy_ip_resolution_rejects_invalid_and_private_addresses():
    assert subscription_public_ip(
        "172.18.0.3",
        {"X-Forwarded-For": "192.168.0.1", "X-Real-IP": "bad"},
        bothost=True,
    ) == ""
    assert subscription_public_ip("unknown", {"X-Forwarded-For": "8.8.8.8"}, bothost=True) == ""
    assert subscription_public_ip(
        "172.18.0.3", {"X-Real-IP": "9.9.9.9"}, bothost=True
    ) == "9.9.9.9"



def test_panel_ip_fallback_requires_matching_hwid_for_same_name_devices():
    one = "phone-hwid-one"
    two = "phone-hwid-two"
    profiles = [
        {
            "panel_id": h1_panel_device_id(one),
            "fingerprint_key": device_fingerprint_key(7, one),
            "name": "TECNO LH7n", "platform": "Android 14",
            "ip_address": "", "updated_at": "2026-10-15",
        },
        {
            "panel_id": h1_panel_device_id(two),
            "fingerprint_key": device_fingerprint_key(7, two),
            "name": "TECNO LH7n", "platform": "Android 14",
            "ip_address": "", "updated_at": "2026-10-15",
        },
    ]
    panel = [
        {"id": h1_panel_device_id(one), "ip_address": "8.8.8.8"},
        {"id": h1_panel_device_id(two), "ip_address": "9.9.9.9"},
    ]
    merged = merge_profiles_with_panel_ips(7, profiles, panel)
    assert [row["ip_address"] for row in merged] == ["8.8.8.8", "9.9.9.9"]
    assert all(row["ip_source"] == "vpn_connection" for row in merged)
    assert not profiles[0]["ip_address"]
    assert merge_profiles_with_panel_ips(7, profiles, [{"id": "count-1", "ip_address": "1.1.1.1"}]) == profiles


def test_panel_ip_cannot_leak_across_fingerprint_namespaces():
    hwid = "phone-hwid-actual"
    profile = {
        "panel_id": "",
        "fingerprint_key": device_fingerprint_key(7, hwid),
        "ip_address": "",
    }
    panel = [{"id": "opaque", "fingerprint": hwid, "ip_address": "8.8.4.4"}]
    assert merge_profiles_with_panel_ips(7, [profile], panel)[0]["ip_address"] == "8.8.4.4"
    assert merge_profiles_with_panel_ips(8, [profile], panel)[0]["ip_address"] == ""
    assert merge_profiles_with_panel_ips(
        7, [profile], [{"id": "opaque", "fingerprint": hwid, "ip_address": "10.1.1.1"}]
    )[0]["ip_address"] == ""
