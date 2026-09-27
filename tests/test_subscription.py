import base64
import asyncio
from unittest.mock import AsyncMock

import pytest

from vpn import H1CloudVpnProvider, prettify_subscription_payload


VLESS = "vless://uuid@de5.h1cloud.net:443?security=tls#old-name"


def test_plain_and_base64_subscriptions_get_pretty_names():
    plain, plain_count = prettify_subscription_payload(VLESS.encode())
    encoded, encoded_count = prettify_subscription_payload(base64.b64encode(VLESS.encode()))
    assert plain_count == encoded_count == 1
    assert b"#%F0%9F" in base64.b64decode(plain)
    assert b"#%F0%9F" in base64.b64decode(encoded)


def test_h1_legacy_h1cloud_http_api_is_accepted_without_extra_env_flag():
    async def run():
        provider = H1CloudVpnProvider(
            api_url="http://nl1.h1cloud.net:25364/api",
            api_token="test-token",
            subscription_template="",
            server_name="MGN VPN",
            verify_ssl=True,
            allow_insecure=False,
        )
        try:
            assert provider.api_url == "http://nl1.h1cloud.net:25364/api"
            assert provider.verify_ssl is False
            assert provider.ssl_context is False
        finally:
            await provider.close()

    asyncio.run(run())


def test_h1_arbitrary_third_party_http_api_still_requires_explicit_override():
    async def run():
        with pytest.raises(RuntimeError, match="legacy"):
            H1CloudVpnProvider(
                api_url="http://example.invalid/api",
                api_token="test-token",
                subscription_template="",
                server_name="MGN VPN",
                verify_ssl=True,
                allow_insecure=False,
            )

    asyncio.run(run())


def test_h1_capabilities_are_explicit():
    capabilities = H1CloudVpnProvider.capabilities
    assert capabilities.supports_federation
    assert capabilities.supports_subscription_proxy
    assert capabilities.supports_device_list
    assert not capabilities.supports_device_removal
    assert capabilities.supports_device_reset


def test_h1_converts_absolute_expiry_to_panel_days():
    assert H1CloudVpnProvider._desired_expiry(
        {"subscription_until": "1970-01-02T00:00:00+00:00"}
    ) == 86400
    assert H1CloudVpnProvider._days_until(100 + 86400, since=100) == 1
    assert H1CloudVpnProvider._days_until(100 + 86401, since=100) == 2
    assert H1CloudVpnProvider._expiry_timestamp({"expires_at": "1970-01-02T00:00:00+00:00"}) == 86400


def test_h1_uses_private_client_id_with_legacy_fallback():
    assert H1CloudVpnProvider._name({"telegram_id": 42, "vpn_client_id": "abc123"}) == "mgn_abc123"
    assert H1CloudVpnProvider._name({"telegram_id": 42, "vpn_client_id": None}) == "mgn_42"


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


def test_h1_decodes_base64_subscription_links():
    payload = base64.b64encode(
        b"vless://one@example.com:443#one\nvless://two@example.com:443#two\n"
    )
    assert H1CloudVpnProvider._subscription_vless_links(payload) == [
        "vless://one@example.com:443#one",
        "vless://two@example.com:443#two",
    ]


def test_h1_rejects_insecure_transport_configuration():
    with pytest.raises(RuntimeError, match="HTTPS"):
        H1CloudVpnProvider("http://panel.example/api", "token", "", "MGN")
    with pytest.raises(RuntimeError, match="verification"):
        H1CloudVpnProvider(
            "https://panel.example/api", "token", "", "MGN", verify_ssl=False
        )


@pytest.mark.parametrize(
    "url,hosts",
    [
        ("https://localhost/sub/x", ("localhost",)),
        ("https://127.0.0.1/sub/x", ("127.0.0.1",)),
        ("https://[::1]/sub/x", ("::1",)),
        ("http://evil.example/sub/x", (".h1cloud.net",)),
        ("https://evil.example/sub/x", (".h1cloud.net",)),
    ],
)
def test_h1_subscription_ssrf_targets_are_rejected(url, hosts):
    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)
        provider.subscription_hosts = hosts
        with pytest.raises(RuntimeError):
            await provider._validate_subscription_url(url)

    asyncio.run(scenario())


def test_h1_subscription_allows_legacy_h1cloud_http_host(monkeypatch):
    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)
        provider.subscription_hosts = (".h1cloud.net",)
        provider.allow_insecure = False
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "getaddrinfo",
            AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 80))]),
        )
        url = "http://us3.h1cloud.net/sub/token"
        assert await provider._validate_subscription_url(url) == url

    asyncio.run(scenario())


def test_h1_subscription_allows_public_allowlisted_host(monkeypatch):
    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)
        provider.subscription_hosts = (".h1cloud.net",)
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(
            loop,
            "getaddrinfo",
            AsyncMock(return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]),
        )
        url = "https://us3.h1cloud.net/sub/token"
        assert await provider._validate_subscription_url(url) == url

    asyncio.run(scenario())


def test_h1_subscription_revalidates_redirect_destination():
    class RedirectResponse:
        status = 302
        headers = {"Location": "https://127.0.0.1/private"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Session:
        def get(self, *_args, **_kwargs):
            return RedirectResponse()

    async def scenario():
        provider = object.__new__(H1CloudVpnProvider)
        provider.public_session = Session()
        provider.ssl_context = True
        provider._validate_subscription_url = AsyncMock(
            side_effect=["https://us3.h1cloud.net/sub/token", RuntimeError("private")]
        )
        with pytest.raises(RuntimeError, match="private"):
            await provider._fetch_public_subscription("https://us3.h1cloud.net/sub/token")
        assert provider._validate_subscription_url.await_count == 2

    asyncio.run(scenario())
