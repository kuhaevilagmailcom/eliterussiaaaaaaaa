from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit


@dataclass(frozen=True)
class VpnClient:
    name: str
    platform: str
    icon: str
    download_url: str
    deep_link_template: str | None = None
    subscription_url_safe: str = ""

    @property
    def supports_subscription_import(self) -> bool:
        return self.deep_link_template is not None

    def import_url(self, subscription_url: str) -> str | None:
        if not self.deep_link_template:
            return None
        return self.deep_link_template.format(
            subscription_url=quote(subscription_url, safe=self.subscription_url_safe),
        )


# A deep link is listed only when the application's own repository or
# documentation confirms it. Other clients intentionally use copy + download.
CLIENTS: tuple[VpnClient, ...] = (
    VpnClient(
        name="Happ",
        platform="Android · iOS · Windows · macOS · Linux",
        icon="🩷",
        download_url="https://www.happ.su/main/",
        deep_link_template="happ://add/{subscription_url}",
        subscription_url_safe=":/",
    ),
    VpnClient(
        name="Hiddify",
        platform="Android · iOS · Windows · macOS · Linux",
        icon="🔷",
        download_url="https://github.com/hiddify/hiddify-app/releases",
        deep_link_template="hiddify://install-sub?url={subscription_url}#MGN%20VPN",
    ),
    VpnClient(
        name="v2rayNG",
        platform="Android",
        icon="📱",
        download_url="https://github.com/2dust/v2rayNG/releases",
        deep_link_template="v2rayng://install-sub?url={subscription_url}#MGN%20VPN",
    ),
    VpnClient(
        name="NekoBox",
        platform="Android",
        icon="🐱",
        download_url="https://github.com/MatsuriDayo/NekoBoxForAndroid/releases",
    ),
    VpnClient(
        name="Shadowrocket",
        platform="iOS",
        icon="🚀",
        download_url="https://apps.apple.com/app/shadowrocket/id932747118",
    ),
)


def client_registry(subscription_url: str) -> list[dict[str, str | bool | None]]:
    return [
        {
            "name": client.name,
            "platform": client.platform,
            "icon": client.icon,
            "download_url": client.download_url,
            "import_url": client.import_url(subscription_url),
            "supports_subscription_import": client.supports_subscription_import,
        }
        for client in CLIENTS
    ]


def get_client(name: str) -> VpnClient | None:
    normalized = name.strip().casefold()
    return next((item for item in CLIENTS if item.name.casefold() == normalized), None)


def client_redirect_url(subscription_url: str, client: VpnClient) -> str:
    parsed = urlsplit(subscription_url)
    marker = "/sub/"
    if marker not in parsed.path:
        return client.download_url
    token = parsed.path.split(marker, 1)[1].split("/", 1)[0]
    path = f"/client/{quote(client.name.casefold(), safe='')}/{quote(token, safe='')}"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
