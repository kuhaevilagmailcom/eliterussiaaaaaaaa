import base64

from vpn import H1CloudVpnProvider, prettify_subscription_payload


VLESS = "vless://uuid@de5.h1cloud.net:443?security=tls#old-name"


def test_plain_and_base64_subscriptions_get_pretty_names():
    plain, plain_count = prettify_subscription_payload(VLESS.encode())
    encoded, encoded_count = prettify_subscription_payload(base64.b64encode(VLESS.encode()))
    assert plain_count == encoded_count == 1
    assert b"#%F0%9F" in plain
    assert b"#%F0%9F" in base64.b64decode(encoded)


def test_h1_capabilities_are_explicit():
    capabilities = H1CloudVpnProvider.capabilities
    assert capabilities.supports_federation
    assert capabilities.supports_subscription_proxy
    assert capabilities.supports_device_list
    assert capabilities.supports_device_removal
