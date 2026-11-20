from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def runtime_db_path(raw_value: str | None = None) -> Path:
    raw = (raw_value if raw_value is not None else os.getenv("DB_PATH", "")).strip()
    if not raw:
        return Path("/app/data/mgn_vpn.sqlite3")
    requested = Path(raw)
    if not requested.is_absolute() or (
        str(requested).startswith("/app/")
        and not str(requested).startswith("/app/data/")
    ):
        return Path("/app/data") / requested.name
    return requested


def _chown_tree(path: Path, uid: int, gid: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid, follow_symlinks=False)
    for root, directories, files in os.walk(path, followlinks=False):
        for name in [*directories, *files]:
            item = Path(root) / name
            try:
                os.chown(item, uid, gid, follow_symlinks=False)
            except FileNotFoundError:
                continue


def _make_public_web_assets_readable(web_dir: Path = Path("/app/miniapp/web")) -> None:
    """Fix BotHost's runtime /app bind mount, which overrides Docker build chmod."""
    if not web_dir.is_dir() or web_dir.is_symlink():
        return

    # Give the unprivileged web process traversal access to the ancestors.
    for parent in (web_dir.parent.parent, web_dir.parent):
        if not parent.is_symlink():
            parent.chmod(stat.S_IMODE(parent.stat().st_mode) | 0o111)

    for root, directories, files in os.walk(web_dir, followlinks=False):
        current = Path(root)
        if current.is_symlink():
            continue
        current.chmod(stat.S_IMODE(current.stat().st_mode) | 0o555)
        for filename in files:
            asset = current / filename
            if not asset.is_symlink():
                asset.chmod(stat.S_IMODE(asset.stat().st_mode) | 0o444)


def main() -> None:
    import pwd

    account = pwd.getpwnam("mgn")
    db_parent = runtime_db_path().parent.resolve()
    if db_parent == Path("/"):
        raise RuntimeError("DB_PATH cannot point directly into the filesystem root")

    if os.geteuid() == 0:
        _chown_tree(db_parent, account.pw_uid, account.pw_gid)
        try:
            _make_public_web_assets_readable()
        except OSError as exc:
            # A web-asset permission error must not take the VPN bot offline.
            print(f"Unable to fix public web asset permissions: {exc}", file=sys.stderr)
        os.initgroups(account.pw_name, account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)

    os.execv(sys.executable, [sys.executable, "/app/app.py"])


if __name__ == "__main__":
    main()
