import base64

from vpn import H1CloudVpnProvider, prettify_subscription_payload


VLESS = "vless://uuid@de5.h1cloud.net:443?security=tls#old-name"


def test_plain_and_base64_subscriptions_get_pretty_names():
    plain, plain_count = prettify_subscription_payload(VLESS.encode())
    encoded, encoded_count = prettify_subscription_payload(base64.b64encode(VLESS.encode()))
    assert plain_count == encoded_count == 1
    assert b"#%F0%9F" in base64.b64decode(plain)
    assert b"#%F0%9F" in base64.b64decode(encoded)


def test_h1_capabilities_are_explicit():
    capabilities = H1CloudVpnProvider.capabilities
    assert capabilities.supports_federation
    assert capabilities.supports_subscription_proxy
    assert capabilities.supports_device_list
    assert not capabilities.supports_device_removal
    assert capabilities.supports_device_reset


def test_h1_converts_absolute_expiry_to_panel_days():
    assert H1CloudVpnProvider._days_until(100 + 86400, since=100) == 1
    assert H1CloudVpnProvider._days_until(100 + 86401, since=100) == 2
    assert H1CloudVpnProvider._expiry_timestamp({"expires_at": "1970-01-02T00:00:00+00:00"}) == 86400


def test_h1_extracts_vless_links_from_client_payload():
    client = {
        "link": "vless://one@example.com:443?security=tls#one",
        "links": {
            "ws": "vless://two@example.com:443?security=tls#two",
            "ignored": "https://example.com/sub",
        },
        "inbound_links": [
            {"url": "vless://three@example.com:443?security=tls#three"},
            {"link": "vless://two@example.com:443?security=tls#two"},
        ],
    }
    assert H1CloudVpnProvider._client_vless_links(client) == [
        "vless://two@example.com:443?security=tls#two",
        "vless://one@example.com:443?security=tls#one",
        "vless://three@example.com:443?security=tls#three",
    ]


def test_h1_extracts_nested_client_and_link_payloads():
    payload = {
        "data": {
            "result": {
                "client": {
                    "name": "mgn_42",
                    "links": {
                        "reality": {"uri": "VLESS://four@example.com:443#four"},
                        "groups": [
                            {"connection": {"url": "vless://five@example.com:443#five"}}
                        ],
                    },
                }
            }
        }
    }
    client = H1CloudVpnProvider._extract_client(payload)
    assert H1CloudVpnProvider._client_vless_links(client) == [
        "VLESS://four@example.com:443#four",
        "vless://five@example.com:443#five",
    ]
