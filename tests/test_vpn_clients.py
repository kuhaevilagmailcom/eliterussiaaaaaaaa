from vpn_clients import CLIENTS, client_redirect_url


def test_only_verified_clients_expose_import_links():
    clients = {client.name: client for client in CLIENTS}
    url = "https://vpn.example/sub/personal-token"
    assert clients["Hiddify"].import_url(url).startswith("hiddify://import/https://")
    assert clients["v2rayNG"].import_url(url) is None
    assert client_redirect_url(url, clients["Hiddify"]) == (
        "https://vpn.example/client/hiddify/personal-token"
    )
