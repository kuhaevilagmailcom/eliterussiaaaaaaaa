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
                    bonus_devices INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_users_subscription_until
                ON users(subscription_until);

                CREATE TABLE IF NOT EXISTS sbp_payments (
                    payment_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL UNIQUE,
                    telegram_id INTEGER NOT NULL,
                    target_telegram_id INTEGER,
                    plan_code TEXT NOT NULL,
                    amount_rub INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_sbp_payments_user
                ON sbp_payments(telegram_id, created_at);

                CREATE TABLE IF NOT EXISTS star_payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,
                    buyer_telegram_id INTEGER NOT NULL,
                    target_telegram_id INTEGER NOT NULL,
                    plan_code TEXT NOT NULL,
                    stars INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_star_payments_buyer
                ON star_payments(buyer_telegram_id, created_at);

                CREATE TABLE IF NOT EXISTS admin_roles (
                    telegram_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK(role IN ('full', 'limited')),
                    granted_by INTEGER NOT NULL,
                    granted_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_admin_roles_role
                ON admin_roles(role);

                CREATE TABLE IF NOT EXISTS referrals (
                    referrer_id INTEGER NOT NULL,
                    referred_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    qualified_at TEXT,
                    rewarded_at TEXT,
                    PRIMARY KEY (referrer_id, referred_id)
                );

                CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                ON referrals(referrer_id, rewarded_at);

                CREATE TABLE IF NOT EXISTS service_promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    type TEXT NOT NULL CHECK(type IN ('discount', 'free_days')),
                    value INTEGER NOT NULL,
                    max_uses INTEGER,
                    used_count INTEGER NOT NULL DEFAULT 0,
                    per_user_limit INTEGER NOT NULL DEFAULT 1,
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    applicable_plans TEXT NOT NULL DEFAULT 'all',
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS promo_uses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    promo_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    payment_id TEXT,
                    used_at TEXT NOT NULL,
                    UNIQUE(promo_id, telegram_id, payment_id),
                    FOREIGN KEY(promo_id) REFERENCES service_promo_codes(id)
                );

                CREATE INDEX IF NOT EXISTS idx_promo_uses_user
                ON promo_uses(promo_id, telegram_id);

                CREATE TABLE IF NOT EXISTS payment_intents (
                    intent_id TEXT PRIMARY KEY,
                    buyer_telegram_id INTEGER NOT NULL,
                    target_telegram_id INTEGER NOT NULL,
                    product_code TEXT NOT NULL,
                    original_amount_rub INTEGER NOT NULL,
                    discount_amount_rub INTEGER NOT NULL DEFAULT 0,
                    final_amount_rub INTEGER NOT NULL,
                    currency TEXT NOT NULL,
                    currency_amount INTEGER NOT NULL,
                    promo_id INTEGER,
                    promo_code TEXT,
                    status TEXT NOT NULL DEFAULT 'created',
                    created_at TEXT NOT NULL,
                    paid_at TEXT
                );
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
            if "bonus_devices" not in columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN bonus_devices INTEGER NOT NULL DEFAULT 0"
                )

            sbp_columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(sbp_payments)")
                ).fetchall()
            }
            if "target_telegram_id" not in sbp_columns:
                await db.execute(
                    "ALTER TABLE sbp_payments ADD COLUMN target_telegram_id INTEGER"
                )
            for name, declaration in (
                ("original_amount_rub", "INTEGER"),
                ("discount_amount_rub", "INTEGER NOT NULL DEFAULT 0"),
                ("promo_id", "INTEGER"),
                ("promo_code", "TEXT"),
            ):
                if name not in sbp_columns:
                    await db.execute(
                        f"ALTER TABLE sbp_payments ADD COLUMN {name} {declaration}"
                    )

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id "
                "ON users(referrer_id)"
            )

            # One device is included in every plan. Preserve only explicit
            # bonus slots and keep the total within 1..5.
            await db.execute(
                """
                UPDATE users
                SET bonus_devices=MIN(
                        4,
                        MAX(0, COALESCE(bonus_devices, 0))
                    ),
                    max_devices=MIN(
                        5,
                        MAX(
                            1,
                            1 + MIN(
                                4,
                                MAX(0, COALESCE(bonus_devices, 0))
                            )
                        )
                    )
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
            existed = await (
                await db.execute(
                    "SELECT 1 FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
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
        result = await self.get_user(telegram_id)
        result["_is_new"] = existed is None
        return result

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


    async def get_user_by_sub_token(
        self,
        sub_token: str,
    ) -> dict[str, Any] | None:
        token = str(sub_token or "").strip()
        if not token:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM users WHERE sub_token=? LIMIT 1",
                    (token,),
                )
            ).fetchone()
        return dict(row) if row else None


    async def get_user_by_username(
        self,
        username: str,
    ) -> dict[str, Any] | None:
        username = username.strip().lstrip("@")
        if not username:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT * FROM users
                    WHERE username IS NOT NULL
                      AND LOWER(username)=LOWER(?)
                    LIMIT 1
                    """,
                    (username,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def set_referrer_once(
        self,
        telegram_id: int,
        referrer_id: int,
    ) -> bool:
        if telegram_id == referrer_id:
            return False
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                """
                UPDATE users
                SET referrer_id=?
                WHERE telegram_id=? AND referrer_id IS NULL
                  AND EXISTS (SELECT 1 FROM users WHERE telegram_id=?)
                """,
                (referrer_id, telegram_id, referrer_id),
            )
            if cursor.rowcount == 1:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (
                        referrer_id, referred_id, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (referrer_id, telegram_id, to_iso(utcnow())),
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

    async def referral_stats(self, telegram_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT COUNT(*) AS invited,
                           SUM(CASE WHEN rewarded_at IS NOT NULL THEN 1 ELSE 0 END) AS rewarded
                    FROM referrals WHERE referrer_id=?
                    """,
                    (telegram_id,),
                )
            ).fetchone()
        invited = int(row[0] or 0)
        rewarded = min(3, int(row[1] or 0))
        return {"invited": invited, "rewarded": rewarded}

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
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            user = await (
                await db.execute(
                    "SELECT * FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
            if user is None or int(user["trial_used"] or 0):
                await db.rollback()
                return False
            current = from_iso(user["subscription_until"])
            start = max(utcnow(), current) if current else utcnow()
            until = to_iso(start + timedelta(minutes=minutes))
            device_limit = min(5, max(1, int(user["max_devices"] or max_devices)))
            cursor = await db.execute(
                """
                UPDATE users
                SET trial_used=1,
                    subscription_until=?,
                    plan_name='Бесплатный доступ',
                    traffic_limit_gb=0,
                    max_devices=?
                WHERE telegram_id=? AND trial_used=0
                """,
                (until, device_limit, telegram_id),
            )
            referrer_id = user["referrer_id"]
            if cursor.rowcount == 1 and referrer_id:
                rewarded = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded_at IS NOT NULL",
                        (int(referrer_id),),
                    )
                ).fetchone()
                referral = await (
                    await db.execute(
                        "SELECT rewarded_at FROM referrals WHERE referrer_id=? AND referred_id=?",
                        (int(referrer_id), telegram_id),
                    )
                ).fetchone()
                if referral is not None and referral[0] is None and int(rewarded[0]) < 3:
                    now = to_iso(utcnow())
                    await db.execute(
                        "UPDATE referrals SET qualified_at=?, rewarded_at=? WHERE referrer_id=? AND referred_id=? AND rewarded_at IS NULL",
                        (now, now, int(referrer_id), telegram_id),
                    )
                    owner = await (
                        await db.execute(
                            "SELECT subscription_until FROM users WHERE telegram_id=?",
                            (int(referrer_id),),
                        )
                    ).fetchone()
                    if owner is not None:
                        owner_until = from_iso(owner[0])
                        owner_start = max(utcnow(), owner_until) if owner_until else utcnow()
                        await db.execute(
                            """
                            UPDATE users
                            SET subscription_until=?,
                                plan_name=CASE
                                    WHEN subscription_until IS NULL OR subscription_until <= ?
                                    THEN 'Реферальный бонус'
                                    ELSE plan_name
                                END
                            WHERE telegram_id=?
                            """,
                            (
                                to_iso(owner_start + timedelta(days=1)),
                                to_iso(utcnow()),
                                int(referrer_id),
                            ),
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

        bonus_devices = min(
            4,
            max(0, int(user.get("bonus_devices") or 0)),
        )
        effective_devices = min(5, 1 + bonus_devices)

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


    async def change_device_slots(
        self,
        telegram_id: int,
        delta: int,
        max_total_devices: int = 5,
    ) -> dict[str, Any] | None:
        delta = int(delta)
        max_total_devices = min(5, max(1, int(max_total_devices)))

        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    """
                    SELECT bonus_devices, max_devices
                    FROM users
                    WHERE telegram_id=?
                    """,
                    (telegram_id,),
                )
            ).fetchone()
            if row is None:
                await db.rollback()
                return None

            bonus = max(0, int(row["bonus_devices"] or 0))
            new_bonus = bonus + delta
            new_total = 1 + new_bonus

            if new_bonus < 0 or new_total < 1 or new_total > max_total_devices:
                await db.rollback()
                return None

            await db.execute(
                """
                UPDATE users
                SET bonus_devices=?,
                    max_devices=?
                WHERE telegram_id=?
                """,
                (new_bonus, new_total, telegram_id),
            )
            await db.commit()

        return await self.get_user(telegram_id)

    async def grant_extra_device(
        self,
        telegram_id: int,
        max_total_devices: int = 5,
    ) -> dict[str, Any] | None:
        return await self.change_device_slots(
            telegram_id=telegram_id,
            delta=1,
            max_total_devices=max_total_devices,
        )

    async def revoke_extra_device(
        self,
        telegram_id: int,
    ) -> dict[str, Any] | None:
        return await self.change_device_slots(
            telegram_id=telegram_id,
            delta=-1,
            max_total_devices=5,
        )

    async def create_service_promo(
        self,
        *,
        code: str,
        promo_type: str,
        value: int,
        created_by: int,
        max_uses: int | None = None,
        per_user_limit: int = 1,
        expires_at: str | None = None,
        applicable_plans: str = "all",
    ) -> dict[str, Any]:
        normalized = code.strip().upper()
        if not normalized or len(normalized) > 40:
            raise ValueError("invalid promo code")
        if promo_type not in {"discount", "free_days"}:
            raise ValueError("invalid promo type")
        value = int(value)
        if value < 1 or (promo_type == "discount" and value > 100):
            raise ValueError("invalid promo value")
        plans = applicable_plans.strip().lower() or "all"
        if plans != "all":
            allowed = {"7", "30", "90", "180", "365"}
            selected = [part.strip() for part in plans.split(",") if part.strip()]
            if not selected or any(part not in allowed for part in selected):
                raise ValueError("invalid applicable plans")
            plans = ",".join(dict.fromkeys(selected))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT INTO service_promo_codes (
                    code, type, value, max_uses, per_user_limit, expires_at,
                    active, applicable_plans, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    normalized,
                    promo_type,
                    value,
                    max_uses if max_uses and int(max_uses) > 0 else None,
                    max(1, int(per_user_limit)),
                    expires_at,
                    plans,
                    created_by,
                    to_iso(utcnow()),
                ),
            )
            await db.commit()
            row = await (
                await db.execute(
                    "SELECT * FROM service_promo_codes WHERE id=?",
                    (cursor.lastrowid,),
                )
            ).fetchone()
        return dict(row)

    async def list_service_promos(self, limit: int = 30) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    "SELECT * FROM service_promo_codes ORDER BY id DESC LIMIT ?",
                    (max(1, min(int(limit), 100)),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def promo_quote(
        self,
        *,
        code: str,
        telegram_id: int,
        plan_code: str,
        original_price: int,
    ) -> dict[str, Any] | None:
        normalized = code.strip().upper()
        if not normalized:
            return None
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM service_promo_codes WHERE code=? COLLATE NOCASE",
                    (normalized,),
                )
            ).fetchone()
            if row is None or not int(row["active"]):
                return None
            expires = from_iso(row["expires_at"])
            if expires and expires <= utcnow():
                return None
            if row["max_uses"] is not None and int(row["used_count"]) >= int(row["max_uses"]):
                return None
            used = await (
                await db.execute(
                    "SELECT COUNT(*) FROM promo_uses WHERE promo_id=? AND telegram_id=?",
                    (int(row["id"]), telegram_id),
                )
            ).fetchone()
            if int(used[0]) >= int(row["per_user_limit"]):
                return None
            plans = str(row["applicable_plans"] or "all")
            if plans != "all" and plan_code not in plans.split(","):
                return None
        result = dict(row)
        original = max(0, int(original_price))
        if result["type"] == "discount":
            discount = original * int(result["value"]) // 100
            result.update(original_price=original, discount=discount, final_price=original - discount)
        else:
            result.update(original_price=original, discount=0, final_price=original)
        return result

    async def consume_promo(
        self,
        *,
        promo_id: int,
        telegram_id: int,
        payment_id: str | None,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT max_uses, used_count, per_user_limit, active, expires_at FROM service_promo_codes WHERE id=?",
                    (promo_id,),
                )
            ).fetchone()
            if row is None or not int(row[3]):
                await db.rollback()
                return False
            expires = from_iso(row[4])
            if expires and expires <= utcnow():
                await db.rollback()
                return False
            if row[0] is not None and int(row[1]) >= int(row[0]):
                await db.rollback()
                return False
            used = await (
                await db.execute(
                    "SELECT COUNT(*) FROM promo_uses WHERE promo_id=? AND telegram_id=?",
                    (promo_id, telegram_id),
                )
            ).fetchone()
            if int(used[0]) >= int(row[2]):
                await db.rollback()
                return False
            await db.execute(
                "INSERT INTO promo_uses (promo_id, telegram_id, payment_id, used_at) VALUES (?, ?, ?, ?)",
                (promo_id, telegram_id, payment_id, to_iso(utcnow())),
            )
            await db.execute(
                "UPDATE service_promo_codes SET used_count=used_count+1 WHERE id=?",
                (promo_id,),
            )
            await db.commit()
            return True

    async def create_payment_intent(
        self,
        *,
        intent_id: str,
        buyer_id: int,
        target_id: int,
        product_code: str,
        original_amount_rub: int,
        discount_amount_rub: int,
        final_amount_rub: int,
        currency: str,
        currency_amount: int,
        promo_id: int | None = None,
        promo_code: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO payment_intents (
                    intent_id, buyer_telegram_id, target_telegram_id,
                    product_code, original_amount_rub, discount_amount_rub,
                    final_amount_rub, currency, currency_amount,
                    promo_id, promo_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id, buyer_id, target_id, product_code,
                    original_amount_rub, discount_amount_rub, final_amount_rub,
                    currency.upper(), currency_amount, promo_id, promo_code,
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

    async def get_payment_intent(self, intent_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM payment_intents WHERE intent_id=?",
                    (intent_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def mark_payment_intent_paid(self, intent_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "UPDATE payment_intents SET status='paid', paid_at=? WHERE intent_id=? AND status='created'",
                (to_iso(utcnow()), intent_id),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def redeem_free_days_promo(self, code: str, telegram_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            promo = await (
                await db.execute(
                    "SELECT * FROM service_promo_codes WHERE code=? COLLATE NOCASE",
                    (code.strip().upper(),),
                )
            ).fetchone()
            user = await (
                await db.execute(
                    "SELECT subscription_until, max_devices FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
            if promo is None or user is None or promo["type"] != "free_days" or not int(promo["active"]):
                await db.rollback()
                return None
            expires = from_iso(promo["expires_at"])
            if expires and expires <= utcnow():
                await db.rollback()
                return None
            if promo["max_uses"] is not None and int(promo["used_count"]) >= int(promo["max_uses"]):
                await db.rollback()
                return None
            used = await (
                await db.execute(
                    "SELECT COUNT(*) FROM promo_uses WHERE promo_id=? AND telegram_id=?",
                    (int(promo["id"]), telegram_id),
                )
            ).fetchone()
            if int(used[0]) >= int(promo["per_user_limit"]):
                await db.rollback()
                return None
            now = utcnow()
            current = from_iso(user["subscription_until"])
            start = max(now, current) if current else now
            until = start + timedelta(days=int(promo["value"]))
            await db.execute(
                "INSERT INTO promo_uses (promo_id, telegram_id, payment_id, used_at) VALUES (?, ?, NULL, ?)",
                (int(promo["id"]), telegram_id, to_iso(now)),
            )
            await db.execute(
                "UPDATE service_promo_codes SET used_count=used_count+1 WHERE id=?",
                (int(promo["id"]),),
            )
            await db.execute(
                """
                UPDATE users SET subscription_until=?, plan_name=?, max_devices=?
                WHERE telegram_id=?
                """,
                (
                    to_iso(until),
                    f"Промокод +{int(promo['value'])} дней",
                    min(5, max(1, int(user["max_devices"] or 1))),
                    telegram_id,
                ),
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
        target_telegram_id: int | None = None,
        original_amount_rub: int | None = None,
        discount_amount_rub: int = 0,
        promo_id: int | None = None,
        promo_code: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sbp_payments (
                    payment_id, order_id, telegram_id, target_telegram_id,
                    plan_code, amount_rub, original_amount_rub,
                    discount_amount_rub, promo_id, promo_code,
                    status, created_at, paid_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'created', ?, NULL)
                """,
                (
                    payment_id,
                    order_id,
                    telegram_id,
                    target_telegram_id or telegram_id,
                    plan_code,
                    amount_rub,
                    original_amount_rub if original_amount_rub is not None else amount_rub,
                    max(0, int(discount_amount_rub)),
                    promo_id,
                    promo_code,
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

    async def record_star_payment(
        self,
        telegram_payment_charge_id: str,
        buyer_telegram_id: int,
        target_telegram_id: int,
        plan_code: str,
        stars: int,
    ) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO star_payments (
                    telegram_payment_charge_id,
                    buyer_telegram_id,
                    target_telegram_id,
                    plan_code,
                    stars,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    telegram_payment_charge_id,
                    buyer_telegram_id,
                    target_telegram_id,
                    plan_code,
                    int(stars),
                    to_iso(utcnow()),
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def get_admin_role(self, telegram_id: int) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            row = await (
                await db.execute(
                    "SELECT role FROM admin_roles WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
        return str(row[0]) if row else None

    async def set_admin_role(
        self,
        telegram_id: int,
        role: str,
        granted_by: int,
    ) -> None:
        role = role.strip().lower()
        if role not in {"full", "limited"}:
            raise ValueError("role must be full or limited")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO admin_roles (
                    telegram_id, role, granted_by, granted_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    role=excluded.role,
                    granted_by=excluded.granted_by,
                    granted_at=excluded.granted_at
                """,
                (
                    telegram_id,
                    role,
                    granted_by,
                    to_iso(utcnow()),
                ),
            )
            await db.commit()

    async def remove_admin_role(self, telegram_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "DELETE FROM admin_roles WHERE telegram_id=?",
                (telegram_id,),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def list_admin_roles(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT a.telegram_id, a.role, a.granted_by, a.granted_at,
                           u.username, u.first_name
                    FROM admin_roles a
                    LEFT JOIN users u ON u.telegram_id=a.telegram_id
                    ORDER BY
                        CASE a.role WHEN 'full' THEN 0 ELSE 1 END,
                        a.granted_at DESC
                    """
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def admin_overview(self) -> dict[str, int]:
        now = to_iso(utcnow())
        day_ago = to_iso(utcnow() - timedelta(days=1))
        week_ago = to_iso(utcnow() - timedelta(days=7))
        month_ago = to_iso(utcnow() - timedelta(days=30))

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
            new_30d = (await (await db.execute(
                "SELECT COUNT(*) FROM users WHERE created_at >= ?",
                (month_ago,),
            )).fetchone())[0]
            active_paid = (await (await db.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE subscription_until IS NOT NULL
                  AND subscription_until > ?
                  AND plan_name NOT IN ('Пробный', 'Бесплатный доступ')
                """,
                (now,),
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
            star_paid = (await (await db.execute(
                "SELECT COUNT(*) FROM star_payments"
            )).fetchone())[0]
            star_revenue = (await (await db.execute(
                "SELECT COALESCE(SUM(stars), 0) FROM star_payments"
            )).fetchone())[0]

        return {
            "total": int(total),
            "active": int(active),
            "new_24h": int(new_24h),
            "new_7d": int(new_7d),
            "new_30d": int(new_30d),
            "active_paid": int(active_paid),
            "trials": int(trials),
            "sbp_paid": int(sbp_paid),
            "sbp_revenue": int(sbp_revenue),
            "star_paid": int(star_paid),
            "star_revenue": int(star_revenue),
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
