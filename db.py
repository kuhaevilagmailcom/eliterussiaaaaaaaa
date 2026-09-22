from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        path = Path(self.path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    trial_used INTEGER NOT NULL DEFAULT 0,
                    subscription_until TEXT,
                    plan_name TEXT NOT NULL DEFAULT '',
                    traffic_limit_gb INTEGER NOT NULL DEFAULT 0,
                    max_devices INTEGER NOT NULL DEFAULT 1,
                    sub_token TEXT NOT NULL UNIQUE,
                    total_paid_stars INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_users_subscription_until
                ON users(subscription_until);

                CREATE TABLE IF NOT EXISTS payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,
                    telegram_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            await db.commit()

    async def ensure_user(
        self,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> dict[str, Any]:
        now = to_iso(utcnow())
        token = secrets.token_urlsafe(24)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, created_at, sub_token
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name
                """,
                (telegram_id, username, first_name or "", now, token),
            )
            await db.commit()
        return await self.get_user(telegram_id)

    async def get_user(self, telegram_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
        if row is None:
            raise KeyError(f"User {telegram_id} not found")
        return dict(row)

    async def activate_trial(
        self,
        telegram_id: int,
        minutes: int,
        traffic_limit_gb: int,
        max_devices: int,
    ) -> bool:
        until = to_iso(utcnow() + timedelta(minutes=minutes))
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET trial_used=1,
                    subscription_until=?,
                    plan_name='Пробный',
                    traffic_limit_gb=?,
                    max_devices=?
                WHERE telegram_id=? AND trial_used=0
                """,
                (until, traffic_limit_gb, max_devices, telegram_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def extend_subscription(
        self,
        telegram_id: int,
        days: int,
        plan_name: str,
        traffic_limit_gb: int,
        max_devices: int,
    ) -> dict[str, Any]:
        user = await self.get_user(telegram_id)
        current = from_iso(user["subscription_until"])
        start = max(utcnow(), current) if current else utcnow()
        until = start + timedelta(days=days)

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE users
                SET subscription_until=?,
                    plan_name=?,
                    traffic_limit_gb=?,
                    max_devices=?
                WHERE telegram_id=?
                """,
                (
                    to_iso(until),
                    plan_name,
                    traffic_limit_gb,
                    max_devices,
                    telegram_id,
                ),
            )
            await db.commit()
        return await self.get_user(telegram_id)

    async def record_payment(
        self,
        telegram_id: int,
        charge_id: str,
        payload: str,
        amount: int,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO payments (
                    telegram_payment_charge_id, telegram_id, payload, amount, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (charge_id, telegram_id, payload, amount, to_iso(utcnow())),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    UPDATE users
                    SET total_paid_stars=total_paid_stars+?
                    WHERE telegram_id=?
                    """,
                    (amount, telegram_id),
                )
            await db.commit()
            return cursor.rowcount == 1

    async def stats(self) -> tuple[int, int]:
        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path) as db:
            total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
            active = (
                await (
                    await db.execute(
                        """
                        SELECT COUNT(*) FROM users
                        WHERE subscription_until IS NOT NULL
                          AND subscription_until > ?
                        """,
                        (now,),
                    )
                ).fetchone()
            )[0]
        return int(total), int(active)
