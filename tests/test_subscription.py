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


def test_poland_subscription_name_is_normalized_from_h1_inbound_label():
    payload = (
        "vless://uuid@pl-d1.h1cloud.net:443?security=reality"
        "#mgn_8464597898%20%C2%B7%20MGN-PL"
    ).encode()
    rendered, count = prettify_subscription_payload(payload)
    decoded = base64.b64decode(rendered).decode()
    assert count == 1
    assert decoded.endswith("#%F0%9F%87%B5%F0%9F%87%B1%20%D0%9F%D0%BE%D0%BB%D1%8C%D1%88%D0%B0")
    assert "mgn_8464597898" not in decoded


def test_duplicate_us_nodes_are_named_usa_and_usa_2():
    payload = (
        "vless://uuid@us3.h1cloud.net:443?security=reality#MGN-US\n"
        "vless://uuid@us3.h1cloud.net:8443?security=reality#MGN-US\n"
    ).encode()
    rendered, count = prettify_subscription_payload(payload)
    decoded = base64.b64decode(rendered).decode()
    assert count == 2
    assert "%D0%A1%D0%A8%D0%90" in decoded
    assert "%D0%A1%D0%A8%D0%90%202" in decoded


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


def test_h1_manual_upsert_keeps_channels_that_produce_links():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider._inbound_ids = AsyncMock(return_value=[11, 12])
        provider._get_client = AsyncMock(return_value=None)
        provider._request = AsyncMock(
            return_value={"client": {"name": "mgn_test", "uuid": "uuid-1"}}
        )
        await provider._upsert_location(
            name="mgn_test",
            client_uuid="uuid-1",
            expires_at=4102444800,
            traffic_limit=0,
            device_limit=2,
        )
        payload = provider._request.await_args.kwargs["json"]
        assert payload["manual"] is True
        assert payload["channels"] == ["main", "reality", "bs", "wscdn"]
        assert payload["inbound_ids"] == [11, 12]

    asyncio.run(run())


def test_h1_subscription_merges_new_federation_country_even_if_aggregate_is_stale():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider.subscription_template = ""
        provider.server_name = "MGN VPN"

        main = {
            "name": "mgn_private",
            "uuid": "uuid-1",
            "sub_url": "https://nl1.h1cloud.net/sub/test",
            "links": {
                "nl": "vless://uuid-1@nl.example:443#NL",
            },
        }
        germany = {
            "name": "mgn_private",
            "uuid": "uuid-1",
            "links": {
                "de": "vless://uuid-1@de.example:443#DE",
            },
        }

        async def get_client(_name, *, prefix=""):
            return germany if prefix else main

        provider._get_client = AsyncMock(side_effect=get_client)
        provider._fetch_public_subscription = AsyncMock(
            return_value=base64.b64encode(
                b"vless://uuid-1@nl.example:443#NL\n"
                b"vless://uuid-1@us.example:443#US\n"
                b"vless://uuid-1@fi.example:443#FI\n"
            )
        )
        provider._federated_nodes = AsyncMock(
            return_value=[{"id": "de-node", "proxy_kind": "proxy"}]
        )

        payload, _headers = await provider.fetch_subscription(
            {
                "telegram_id": 42,
                "vpn_client_id": "private",
                "max_devices": 1,
                "traffic_limit_gb": 0,
                "subscription_until": "2030-01-01T00:00:00+00:00",
            }
        )
        links = provider._subscription_vless_links(payload)
        assert any("@de.example:" in link for link in links)
        assert len(links) == 4

    asyncio.run(run())


def test_h1_get_state_finds_device_on_remote_federation_node():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider.server_name = "MGN VPN"
        provider.subscription_template = ""

        main_client = {
            "name": "mgn_private",
            "sub_url": "https://nl1.h1cloud.net/sub/test",
            "devices": [],
            "traffic_used_gb": 0,
            "traffic_limit_gb": 0,
        }
        remote_client = {
            "name": "mgn_private",
            "devices": [
                {
                    "hwid": "pc-1",
                    "device_name": "Desktop",
                    "os": "Windows",
                }
            ],
        }

        async def get_client(_name, *, prefix=""):
            return remote_client if prefix else main_client

        provider._get_client = AsyncMock(side_effect=get_client)
        provider._federated_nodes = AsyncMock(
            return_value=[{"id": "us", "proxy_kind": "proxy"}]
        )

        state = await provider.get_state(
            {
                "telegram_id": 42,
                "vpn_client_id": "private",
                "max_devices": 1,
                "traffic_limit_gb": 0,
            }
        )

        assert len(state.devices) == 1
        assert state.devices[0]["name"] == "Desktop"
        assert state.devices[0]["platform"] == "Windows"

    asyncio.run(run())


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


def test_h1_smart_selection_preserves_transient_probe_failures():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider._endpoint_failure_streak = {
            ("de.example", 443): 1,
            ("nl.example", 443): 0,
        }

        async def probe(host, port):
            return 25.0 if host == "nl.example" else None

        provider._probe_vless_endpoint = probe
        links = [
            "vless://uuid@de.example:443#DE",
            "vless://uuid@nl.example:443#NL",
        ]
        ranked = await provider._rank_live_vless_links(links)
        assert len(ranked) == 2
        assert "@nl.example:443" in ranked[0]
        assert any("@de.example:443" in item for item in ranked)

        provider._endpoint_failure_streak[("de.example", 443)] = 3
        ranked = await provider._rank_live_vless_links(links)
        assert len(ranked) == 2
        assert "@nl.example:443" in ranked[0]
        assert any("@de.example:443" in item for item in ranked)

    asyncio.run(run())


def test_h1_server_diagnostics_reports_main_and_federated_nodes():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider.api_url = "https://nl1.h1cloud.net/api"
        provider.server_name = "MGN VPN"

        async def request(method, path, **kwargs):
            assert method == "GET"
            if path == "/health":
                return {"ok": True}
            if path == "/fed/link":
                return {"links": ["MGN-DE"]}
            if path == "/fed/registry":
                return {"nodes": [{"id": "us-node", "name": "MGN-US"}]}
            if path == "/fed/lproxy/MGN-DE/health":
                raise RuntimeError("health endpoint unavailable")
            if path == "/fed/lproxy/MGN-DE/inbounds":
                return {"inbounds": [{"id": "11", "remark": "MGN-DE"}]}
            if path in {
                "/fed/proxy/us-node/health",
                "/fed/proxy/us-node/inbounds",
            }:
                raise RuntimeError("node unavailable")
            raise AssertionError(path)

        provider._request = AsyncMock(side_effect=request)

        report = await provider.server_diagnostics()
        assert report["provider"] == "h1cloud"
        assert report["discovery_ok"] is True
        assert report["sources"]["/fed/link"]["count"] == 1
        assert report["sources"]["/fed/registry"]["count"] == 1

        servers = report["servers"]
        assert len(servers) == 7
        assert [item["name"] for item in servers] == [
            "🇳🇱 Нидерланды",
            "🇵🇰 Пакистан",
            "🇩🇪 Германия",
            "🇵🇱 Польша",
            "🇫🇮 Финляндия",
            "🇺🇸 США",
            "🇺🇸 США 2",
        ]
        assert servers[0]["name"] == "🇳🇱 Нидерланды"
        assert servers[0]["available"] is True

        germany = next(item for item in servers if "Германия" in item["name"])
        assert germany["available"] is True
        assert germany["check"] == "inbounds"

        usa = next(item for item in servers if "США" in item["name"])
        assert usa["available"] is False

    asyncio.run(run())


def test_h1_server_diagnostics_distinguishes_empty_federation_from_main_config():
    async def run():
        provider = object.__new__(H1CloudVpnProvider)
        provider.api_url = "https://nl1.h1cloud.net/api"
        provider.server_name = "Нидерланды"

        async def request(method, path, **kwargs):
            assert method == "GET"
            if path == "/health":
                return {"ok": True}
            if path == "/fed/link":
                return {"links": []}
            if path == "/fed/registry":
                return {"nodes": []}
            if path == "/fed/lagg":
                return None
            raise AssertionError(path)

        provider._request = AsyncMock(side_effect=request)

        report = await provider.server_diagnostics()
        assert report["discovery_ok"] is True
        assert len(report["servers"]) == 7
        assert report["servers"][0]["kind"] == "main"
        assert report["servers"][0]["name"] == "🇳🇱 Нидерланды"
        assert all(
            item["available"] is False
            for item in report["servers"][1:]
        )
        assert report["sources"]["/fed/link"]["count"] == 0
        assert report["sources"]["/fed/registry"]["count"] == 0

    asyncio.run(run())
