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
                    referrer_id INTEGER,
                    last_menu_message_id INTEGER
                );

                CREATE INDEX IF NOT EXISTS idx_users_subscription_until
                ON users(subscription_until);

                CREATE TABLE IF NOT EXISTS sbp_payments (
                    payment_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    telegram_id INTEGER NOT NULL,
                    plan_code TEXT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sbp_payments_user
                ON sbp_payments(telegram_id, created_at);
                """
            )

            columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(users)")
                ).fetchall()
            }
            if "referrer_id" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
            if "last_menu_message_id" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN last_menu_message_id INTEGER"
                )

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id "
                "ON users(referrer_id)"
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

    async def set_referrer_once(
        self,
        telegram_id: int,
        referrer_id: int,
    ) -> bool:
        if telegram_id == referrer_id:
            return False
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET referrer_id=?
                WHERE telegram_id=? AND referrer_id IS NULL
                """,
                (referrer_id, telegram_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def referral_count(self, telegram_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM users WHERE referrer_id=?",
                    (telegram_id,),
                )
            ).fetchone()
        return int(row[0])

    async def set_last_menu_message(
        self,
        telegram_id: int,
        message_id: int | None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE users SET last_menu_message_id=? WHERE telegram_id=?",
                (message_id, telegram_id),
            )
            await db.commit()

    async def activate_trial(
        self,
        telegram_id: int,
        minutes: int,
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
                    traffic_limit_gb=0,
                    max_devices=?
                WHERE telegram_id=? AND trial_used=0
                """,
                (until, max_devices, telegram_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def extend_subscription(
        self,
        telegram_id: int,
        days: int,
        plan_name: str,
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
                    traffic_limit_gb=0,
                    max_devices=?
                WHERE telegram_id=?
                """,
                (to_iso(until), plan_name, max_devices, telegram_id),
            )
            await db.commit()
        return await self.get_user(telegram_id)


    async def create_sbp_payment(
        self,
        payment_id: str,
        order_id: str,
        telegram_id: int,
        plan_code: str,
        amount_rub: int,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sbp_payments (
                    payment_id, order_id, telegram_id, plan_code,
                    amount_rub, status, created_at, paid_at
                ) VALUES (?, ?, ?, ?, ?, 'created', ?, NULL)
                """,
                (
                    payment_id,
                    order_id,
                    telegram_id,
                    plan_code,
                    amount_rub,
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

    async def get_sbp_payment(self, payment_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM sbp_payments WHERE payment_id=?",
                    (payment_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def set_sbp_status(self, payment_id: str, status: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE sbp_payments SET status=? WHERE payment_id=?",
                (status[:32], payment_id),
            )
            await db.commit()

    async def mark_sbp_paid(self, payment_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE sbp_payments
                SET status='paid', paid_at=?
                WHERE payment_id=? AND status!='paid'
                """,
                (to_iso(utcnow()), payment_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def admin_overview(self) -> dict[str, int]:
        now = to_iso(utcnow())
        day_ago = to_iso(utcnow() - timedelta(days=1))
        week_ago = to_iso(utcnow() - timedelta(days=7))

        async with aiosqlite.connect(self.path) as db:
            total = (await (await db.execute(
                "SELECT COUNT(*) FROM users"
            )).fetchone())[0]
            active = (await (await db.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE subscription_until IS NOT NULL
                  AND subscription_until > ?
                """,
                (now,),
            )).fetchone())[0]
            new_24h = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?",
                (day_ago,),
            )).fetchone())[0]
            new_7d = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?",
                (week_ago,),
            )).fetchone())[0]
            trials = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE trial_used=1"
            )).fetchone())[0]
            sbp_paid = (await (await db.execute(
                "SELECT COUNT(*) FROM sbp_payments WHERE status='paid'"
            )).fetchone())[0]
            sbp_revenue = (await (await db.execute(
                "SELECT COALESCE(SUM(amount_rub), 0) FROM sbp_payments WHERE status='paid'"
            )).fetchone())[0]

        return {
            "total": int(total),
            "active": int(active),
            "new_24h": int(new_24h),
            "new_7d": int(new_7d),
            "trials": int(trials),
            "sbp_paid": int(sbp_paid),
            "sbp_revenue": int(sbp_revenue),
        }

    async def recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 20))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT telegram_id, username, first_name, created_at,
                           subscription_until, plan_name, trial_used
                    FROM users
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def recent_sbp_payments(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 20))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT payment_id, telegram_id, plan_code, amount_rub,
                           status, created_at, paid_at
                    FROM sbp_payments
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def revoke_subscription(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET subscription_until=NULL,
                    plan_name='',
                    max_devices=1
                WHERE telegram_id=?
                """,
                (telegram_id,),
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
