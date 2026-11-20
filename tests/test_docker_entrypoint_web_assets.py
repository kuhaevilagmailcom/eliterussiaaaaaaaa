"""Runtime web assets must remain readable after BotHost mounts the checkout on /app."""
import stat
from pathlib import Path

from docker_entrypoint import _make_public_web_assets_readable


def test_repairs_site_traversal_without_exposing_sibling_secrets(tmp_path: Path):
    app_dir = tmp_path / "app"
    miniapp_dir = app_dir / "miniapp"
    web_dir = miniapp_dir / "web"
    site_dir = web_dir / "site"
    site_dir.mkdir(parents=True)
    index = site_dir / "index.html"
    index.write_text("<html>MGN VPN</html>", encoding="utf-8")
    secret = app_dir / ".env"
    secret.write_text("DO_NOT_EXPOSE", encoding="utf-8")

    for directory in (app_dir, miniapp_dir, web_dir, site_dir):
        directory.chmod(0o700)
    index.chmod(0o600)
    secret.chmod(0o600)

    _make_public_web_assets_readable(web_dir)

    for directory in (app_dir, miniapp_dir, web_dir, site_dir):
        assert stat.S_IMODE(directory.stat().st_mode) & 0o111 == 0o111
    assert stat.S_IMODE(index.stat().st_mode) & 0o444 == 0o444
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_missing_web_dir_is_noop(tmp_path: Path):
    _make_public_web_assets_readable(tmp_path / "missing" / "web")
