from vpn_clients import CLIENTS, client_redirect_url


def test_only_verified_clients_expose_import_links():
    clients = {client.name: client for client in CLIENTS}
    url = "https://vpn.example/sub/personal-token"
    assert clients["Happ"].import_url(url) == (
        "happ://add/https://vpn.example/sub/personal-token"
    )
    assert clients["Hiddify"].import_url(url).startswith("hiddify://install-sub?url=https%3A")
    assert clients["v2rayNG"].import_url(url).startswith("v2rayng://install-sub?url=https%3A")
    assert client_redirect_url(url, clients["Hiddify"]) == (
        "https://vpn.example/client/hiddify/personal-token"
    )
