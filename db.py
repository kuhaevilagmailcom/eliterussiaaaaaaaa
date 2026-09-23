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
                    last_menu_message_id INTEGER,
                    diamonds INTEGER NOT NULL DEFAULT 0,
                    bonus_devices INTEGER NOT NULL DEFAULT 0
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

                CREATE TABLE IF NOT EXISTS diamond_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    amount INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    event_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_diamond_ledger_user
                ON diamond_ledger(telegram_id, id DESC);

                CREATE TABLE IF NOT EXISTS promo_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    price_diamonds INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    code TEXT NOT NULL UNIQUE,
                    redeemed_by INTEGER,
                    redeemed_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(product_id) REFERENCES promo_products(id)
                );

                CREATE INDEX IF NOT EXISTS idx_promo_codes_product
                ON promo_codes(product_id, redeemed_by);
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
            if "diamonds" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN diamonds INTEGER NOT NULL DEFAULT 0"
                )
            if "bonus_devices" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN bonus_devices INTEGER NOT NULL DEFAULT 0"
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

        bonus_devices = int(user.get("bonus_devices") or 0)
        effective_devices = max(1, int(max_devices) + bonus_devices)

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
                (to_iso(until), plan_name, effective_devices, telegram_id),
            )
            await db.commit()
        return await self.get_user(telegram_id)


    async def add_diamonds(
        self,
        telegram_id: int,
        amount: int,
        reason: str,
        event_key: str,
    ) -> bool:
        amount = int(amount)
        if amount == 0:
            return False

        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")

            exists = await (
                await db.execute(
                    "SELECT 1 FROM diamond_ledger WHERE event_key=?",
                    (event_key,),
                )
            ).fetchone()
            if exists:
                await db.rollback()
                return False

            row = await (
                await db.execute(
                    "SELECT diamonds FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return False

            old_balance = int(row[0])
            new_balance = max(0, old_balance + amount)
            actual_amount = new_balance - old_balance
            if actual_amount == 0:
                await db.rollback()
                return False

            await db.execute(
                "UPDATE users SET diamonds=? WHERE telegram_id=?",
                (new_balance, telegram_id),
            )
            await db.execute(
                """
                INSERT INTO diamond_ledger (
                    telegram_id, amount, reason, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    actual_amount,
                    reason[:120],
                    event_key[:160],
                    to_iso(utcnow()),
                ),
            )
            await db.commit()
            return True

    async def diamond_balance(self, telegram_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT diamonds FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
        return int(row[0]) if row else 0

    async def diamond_history(
        self,
        telegram_id: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 20))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT amount, reason, created_at
                    FROM diamond_ledger
                    WHERE telegram_id=?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (telegram_id, limit),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def purchase_vpn_days(
        self,
        telegram_id: int,
        days: int,
        cost: int,
        event_key: str,
    ) -> dict[str, Any] | None:
        days = max(1, int(days))
        cost = max(1, int(cost))

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT subscription_until, diamonds, max_devices
                    FROM users
                    WHERE telegram_id=?
                    """,
                    (telegram_id,),
                )
            ).fetchone()
            if row is None or int(row["diamonds"]) < cost:
                await db.rollback()
                return None

            exists = await (
                await db.execute(
                    "SELECT 1 FROM diamond_ledger WHERE event_key=?",
                    (event_key,),
                )
            ).fetchone()
            if exists:
                await db.rollback()
                return None

            current = from_iso(row["subscription_until"])
            start = max(utcnow(), current) if current else utcnow()
            until = start + timedelta(days=days)

            await db.execute(
                """
                UPDATE users
                SET diamonds=diamonds-?,
                    subscription_until=?,
                    plan_name=?,
                    traffic_limit_gb=0
                WHERE telegram_id=?
                """,
                (
                    cost,
                    to_iso(until),
                    f"Бонус: {days} дн.",
                    telegram_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO diamond_ledger (
                    telegram_id, amount, reason, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    -cost,
                    f"Магазин: {days} дн. VPN",
                    event_key[:160],
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

        return await self.get_user(telegram_id)

    async def purchase_extra_device(
        self,
        telegram_id: int,
        cost: int,
        event_key: str,
        max_total_devices: int = 10,
    ) -> dict[str, Any] | None:
        cost = max(1, int(cost))
        max_total_devices = max(2, int(max_total_devices))

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT diamonds, max_devices, bonus_devices
                    FROM users
                    WHERE telegram_id=?
                    """,
                    (telegram_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return None
            if int(row["diamonds"]) < cost:
                await db.rollback()
                return None
            if int(row["max_devices"]) >= max_total_devices:
                await db.rollback()
                return None

            exists = await (
                await db.execute(
                    "SELECT 1 FROM diamond_ledger WHERE event_key=?",
                    (event_key,),
                )
            ).fetchone()
            if exists:
                await db.rollback()
                return None

            await db.execute(
                """
                UPDATE users
                SET diamonds=diamonds-?,
                    bonus_devices=bonus_devices+1,
                    max_devices=max_devices+1
                WHERE telegram_id=?
                """,
                (cost, telegram_id),
            )
            await db.execute(
                """
                INSERT INTO diamond_ledger (
                    telegram_id, amount, reason, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    -cost,
                    "Магазин: +1 устройство",
                    event_key[:160],
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

        return await self.get_user(telegram_id)

    async def create_promo_product(
        self,
        slug: str,
        title: str,
        price_diamonds: int,
    ) -> None:
        slug = slug.strip().lower()[:48]
        if not slug:
            raise ValueError("empty promo slug")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO promo_products (
                    slug, title, price_diamonds, active, created_at
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    title=excluded.title,
                    price_diamonds=excluded.price_diamonds,
                    active=1
                """,
                (
                    slug,
                    title.strip()[:80],
                    max(1, int(price_diamonds)),
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

    async def add_promo_code(self, slug: str, code: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            product = await (
                await db.execute(
                    "SELECT id FROM promo_products WHERE slug=?",
                    (slug.strip().lower(),),
                )
            ).fetchone()
            if not product:
                return False
            try:
                await db.execute(
                    """
                    INSERT INTO promo_codes (
                        product_id, code, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        int(product[0]),
                        code.strip()[:300],
                        to_iso(utcnow()),
                    ),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                await db.rollback()
                return False

    async def promo_products(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT p.id, p.slug, p.title, p.price_diamonds,
                           COUNT(c.id) AS stock
                    FROM promo_products p
                    LEFT JOIN promo_codes c
                      ON c.product_id=p.id
                     AND c.redeemed_by IS NULL
                    WHERE p.active=1
                    GROUP BY p.id
                    HAVING stock > 0
                    ORDER BY p.id
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def redeem_promo(
        self,
        telegram_id: int,
        slug: str,
        event_key: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            product = await (
                await db.execute(
                    """
                    SELECT id, title, price_diamonds
                    FROM promo_products
                    WHERE slug=? AND active=1
                    """,
                    (slug.strip().lower(),),
                )
            ).fetchone()
            if product is None:
                await db.rollback()
                return None

            user = await (
                await db.execute(
                    "SELECT diamonds FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
            if user is None or int(user["diamonds"]) < int(product["price_diamonds"]):
                await db.rollback()
                return None

            code = await (
                await db.execute(
                    """
                    SELECT id, code
                    FROM promo_codes
                    WHERE product_id=? AND redeemed_by IS NULL
                    ORDER BY id
                    LIMIT 1
                    """,
                    (int(product["id"]),),
                )
            ).fetchone()
            if code is None:
                await db.rollback()
                return None

            exists = await (
                await db.execute(
                    "SELECT 1 FROM diamond_ledger WHERE event_key=?",
                    (event_key,),
                )
            ).fetchone()
            if exists:
                await db.rollback()
                return None

            now = to_iso(utcnow())
            price = int(product["price_diamonds"])
            await db.execute(
                "UPDATE users SET diamonds=diamonds-? WHERE telegram_id=?",
                (price, telegram_id),
            )
            await db.execute(
                """
                UPDATE promo_codes
                SET redeemed_by=?, redeemed_at=?
                WHERE id=? AND redeemed_by IS NULL
                """,
                (telegram_id, now, int(code["id"])),
            )
            await db.execute(
                """
                INSERT INTO diamond_ledger (
                    telegram_id, amount, reason, event_key, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    -price,
                    f"Промокод: {product['title']}",
                    event_key[:160],
                    now,
                ),
            )
            await db.commit()
            return {
                "title": str(product["title"]),
                "code": str(code["code"]),
                "price_diamonds": price,
            }

    async def promo_stock_overview(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT p.slug, p.title, p.price_diamonds,
                           SUM(CASE WHEN c.redeemed_by IS NULL THEN 1 ELSE 0 END) AS stock,
                           SUM(CASE WHEN c.redeemed_by IS NOT NULL THEN 1 ELSE 0 END) AS issued
                    FROM promo_products p
                    LEFT JOIN promo_codes c ON c.product_id=p.id
                    GROUP BY p.id
                    ORDER BY p.id
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

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
