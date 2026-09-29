from __future__ import annotations

import secrets
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from catalog import (
    DEVICE_PRODUCT_CODE,
    EXTRA_DEVICE_PRICE_RUB,
    MAX_DEVICES,
    PLANS,
    rub_to_stars,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        path = Path(self.path)
        if path.parent != Path("."):
            path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    bot_started_at TEXT,
                    trial_used INTEGER NOT NULL DEFAULT 0,
                    subscription_until TEXT,
                    plan_name TEXT NOT NULL DEFAULT '',
                    traffic_limit_gb INTEGER NOT NULL DEFAULT 0,
                    max_devices INTEGER NOT NULL DEFAULT 1,
                    sub_token TEXT NOT NULL UNIQUE,
                    referrer_id INTEGER,
                    last_menu_message_id INTEGER,
                    bonus_devices INTEGER NOT NULL DEFAULT 0,
                    vpn_client_id TEXT UNIQUE,
                    attribution_source TEXT,
                    attribution_at TEXT,
                    channel_verified_at TEXT
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
                    pay_url TEXT,
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

                CREATE TABLE IF NOT EXISTS admin_subscription_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    granted_by INTEGER NOT NULL,
                    days INTEGER NOT NULL CHECK(days > 0),
                    action TEXT NOT NULL DEFAULT 'grant'
                        CHECK(action IN ('grant', 'add')),
                    granted_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_admin_subscription_grants_user
                ON admin_subscription_grants(telegram_id, granted_at);

                CREATE TABLE IF NOT EXISTS subscription_expiry_notifications (
                    telegram_id INTEGER NOT NULL,
                    subscription_until TEXT NOT NULL,
                    days_before INTEGER NOT NULL CHECK(days_before IN (1, 2, 3)),
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, subscription_until, days_before)
                );

                CREATE TABLE IF NOT EXISTS traffic_daily (
                    telegram_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    used_gb REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, day)
                );

                CREATE INDEX IF NOT EXISTS idx_traffic_daily_user_day
                ON traffic_daily(telegram_id, day);

                CREATE TABLE IF NOT EXISTS giveaways (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL,
                    text_html TEXT NOT NULL,
                    text_plain TEXT NOT NULL DEFAULT '',
                    photo_file_id TEXT,
                    winners_count INTEGER NOT NULL,
                    prize_days INTEGER NOT NULL,
                    end_mode TEXT NOT NULL CHECK(end_mode IN ('time', 'participants')),
                    ends_at TEXT,
                    participant_limit INTEGER,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active', 'finishing', 'finished', 'cancelled')),
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    admin_notified_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_giveaways_status_end
                ON giveaways(status, ends_at);

                CREATE TABLE IF NOT EXISTS giveaway_posts (
                    giveaway_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    finalized_at TEXT,
                    PRIMARY KEY (giveaway_id, chat_id),
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
                );

                CREATE TABLE IF NOT EXISTS giveaway_participants (
                    giveaway_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    joined_at TEXT NOT NULL,
                    PRIMARY KEY (giveaway_id, telegram_id),
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
                );

                CREATE INDEX IF NOT EXISTS idx_giveaway_participants_joined
                ON giveaway_participants(giveaway_id, joined_at);

                CREATE TABLE IF NOT EXISTS giveaway_winners (
                    giveaway_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    position INTEGER NOT NULL,
                    granted_at TEXT,
                    notified_at TEXT,
                    PRIMARY KEY (giveaway_id, telegram_id),
                    UNIQUE (giveaway_id, position),
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
                );

                CREATE TABLE IF NOT EXISTS giveaway_rerolls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    giveaway_id INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    old_telegram_id INTEGER NOT NULL,
                    old_username TEXT,
                    new_telegram_id INTEGER NOT NULL,
                    new_username TEXT,
                    rerolled_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(giveaway_id) REFERENCES giveaways(id)
                );

                CREATE INDEX IF NOT EXISTS idx_giveaway_rerolls_giveaway
                ON giveaway_rerolls(giveaway_id, id);

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

                CREATE TABLE IF NOT EXISTS support_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open', 'answered', 'closed')),
                    created_at TEXT NOT NULL,
                    answered_at TEXT,
                    answered_by INTEGER,
                    answer_text TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_support_tickets_status_created
                ON support_tickets(status, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_support_tickets_user_created
                ON support_tickets(telegram_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS support_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender_type TEXT NOT NULL CHECK(sender_type IN ('user', 'admin')),
                    sender_telegram_id INTEGER NOT NULL,
                    message_type TEXT NOT NULL CHECK(message_type IN ('text', 'photo', 'video')),
                    text TEXT,
                    file_id TEXT,
                    file_unique_id TEXT,
                    caption TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(ticket_id) REFERENCES support_tickets(id)
                );

                CREATE INDEX IF NOT EXISTS idx_support_messages_ticket_created
                ON support_messages(ticket_id, created_at, id);

                CREATE TABLE IF NOT EXISTS support_sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL CHECK(mode IN ('new', 'user_reply', 'admin_reply', 'admin_search', 'admin_days')),
                    ticket_id INTEGER,
                    payload TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS interaction_sessions (
                    telegram_id INTEGER PRIMARY KEY,
                    mode TEXT NOT NULL,
                    ticket_id INTEGER,
                    payload TEXT,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT
                );

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
                    expires_at TEXT,
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
                await db.execute("UPDATE users SET bonus_devices=MIN(4, MAX(0, max_devices-1))")
            if "vpn_client_id" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN vpn_client_id TEXT")
            if "attribution_source" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN attribution_source TEXT")
            if "attribution_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN attribution_at TEXT")
            if "channel_verified_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN channel_verified_at TEXT")
                # Users who existed before the mandatory channel gate are
                # grandfathered. Only accounts created after this migration
                # must complete the check.
                await db.execute(
                    "UPDATE users SET channel_verified_at=created_at "
                    "WHERE channel_verified_at IS NULL"
                )
            if "bot_started_at" not in columns:
                await db.execute("ALTER TABLE users ADD COLUMN bot_started_at TEXT")
                # Existing accounts predate this marker and must never become
                # eligible for a fresh referral merely because of the migration.
                await db.execute(
                    "UPDATE users SET bot_started_at=created_at "
                    "WHERE bot_started_at IS NULL"
                )
            await db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_vpn_client_id "
                "ON users(vpn_client_id) WHERE vpn_client_id IS NOT NULL"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_attribution_source "
                "ON users(attribution_source)"
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
                ("pay_url", "TEXT"),
                ("reconciliation_checked_at", "TEXT"),
                ("access_reversed_at", "TEXT"),
            ):
                if name not in sbp_columns:
                    await db.execute(
                        f"ALTER TABLE sbp_payments ADD COLUMN {name} {declaration}"
                    )

            intent_columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(payment_intents)")
                ).fetchall()
            }
            if "expires_at" not in intent_columns:
                await db.execute("ALTER TABLE payment_intents ADD COLUMN expires_at TEXT")
            await db.execute(
                "UPDATE payment_intents SET expires_at=datetime(created_at, '+20 minutes') "
                "WHERE expires_at IS NULL"
            )
            await db.execute(
                "UPDATE payment_intents SET status='expired' "
                "WHERE status='created' AND expires_at<=?",
                (to_iso(utcnow()),),
            )
            await db.execute(
                "INSERT OR IGNORE INTO interaction_sessions "
                "(telegram_id, mode, ticket_id, payload, updated_at) "
                "SELECT telegram_id, mode, ticket_id, payload, updated_at FROM support_sessions"
            )

            giveaway_columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(giveaways)")
                ).fetchall()
            }
            if "admin_notified_at" not in giveaway_columns:
                await db.execute(
                    "ALTER TABLE giveaways ADD COLUMN admin_notified_at TEXT"
                )

            support_columns = {
                row[1]
                for row in await (
                    await db.execute("PRAGMA table_info(support_tickets)")
                ).fetchall()
            }
            for name, declaration in (
                ("updated_at", "TEXT"),
                ("closed_at", "TEXT"),
                ("closed_by", "INTEGER"),
                ("deleted_at", "TEXT"),
                ("deleted_by", "INTEGER"),
            ):
                if name not in support_columns:
                    await db.execute(
                        f"ALTER TABLE support_tickets ADD COLUMN {name} {declaration}"
                    )
            await db.execute(
                "UPDATE support_tickets SET updated_at=COALESCE(updated_at, answered_at, created_at)"
            )
            # Preserve legacy support data while making it available through
            # the new threaded model. INSERT OR IGNORE keeps migration idempotent.
            legacy_rows = await (
                await db.execute(
                    "SELECT id, telegram_id, message, created_at, answer_text, answered_by, answered_at "
                    "FROM support_tickets"
                )
            ).fetchall()
            for row in legacy_rows:
                existing = await (
                    await db.execute(
                        "SELECT 1 FROM support_messages WHERE ticket_id=? LIMIT 1",
                        (row[0],),
                    )
                ).fetchone()
                if existing:
                    continue
                if row[2]:
                    await db.execute(
                        "INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_type, text, created_at) "
                        "VALUES (?, 'user', ?, 'text', ?, ?)",
                        (row[0], row[1], row[2], row[3]),
                    )
                if row[4]:
                    await db.execute(
                        "INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_type, text, created_at) "
                        "VALUES (?, 'admin', ?, 'text', ?, ?)",
                        (row[0], row[5] or 0, row[4], row[6] or row[3]),
                    )

            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_referrer_id "
                "ON users(referrer_id)"
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_username_nocase "
                "ON users(username COLLATE NOCASE)"
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
        vpn_client_id = secrets.token_hex(16)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            existed = await (
                await db.execute(
                    "SELECT 1 FROM users WHERE telegram_id=?",
                    (telegram_id,),
                )
            ).fetchone()
            await db.execute(
                """
                INSERT INTO users (
                    telegram_id, username, first_name, created_at, sub_token, vpn_client_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name
                WHERE users.username IS NOT excluded.username OR users.first_name IS NOT excluded.first_name
                """,
                (telegram_id, username, first_name or "", now, token, vpn_client_id),
            )
            await db.commit()
        result = await self.get_user(telegram_id)
        result["_is_new"] = existed is None
        return result

    async def ping(self) -> bool:
        """Cheap SQLite readiness check used by the non-liveness diagnostics endpoint."""
        try:
            async with aiosqlite.connect(self.path, timeout=3.0) as db:
                await (await db.execute("SELECT 1")).fetchone()
            return True
        except Exception:
            return False

    async def claim_first_bot_start(self, telegram_id: int) -> bool:
        """Atomically mark the first private /start without confusing channel callbacks for starts."""
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET bot_started_at=?
                WHERE telegram_id=? AND bot_started_at IS NULL
                """,
                (to_iso(utcnow()), int(telegram_id)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def set_attribution_source_once(
        self,
        telegram_id: int,
        source: str,
    ) -> bool:
        source = str(source or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", source):
            raise ValueError("invalid attribution source")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                UPDATE users
                SET attribution_source=?, attribution_at=?
                WHERE telegram_id=?
                  AND (attribution_source IS NULL OR attribution_source='')
                """,
                (source, to_iso(utcnow()), int(telegram_id)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def mark_channel_verified(self, telegram_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET channel_verified_at=? WHERE telegram_id=?",
                (to_iso(utcnow()), int(telegram_id)),
            )
            await db.commit()
        return await self.get_user(int(telegram_id))

    async def attribution_stats(self, source: str) -> dict[str, Any]:
        source = str(source or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,64}", source):
            raise ValueError("invalid attribution source")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            arrived = int(
                (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM users WHERE attribution_source=?",
                            (source,),
                        )
                    ).fetchone()
                )[0]
            )
            buyers = int(
                (
                    await (
                        await db.execute(
                            """
                            SELECT COUNT(*)
                            FROM users u
                            WHERE u.attribution_source=?
                              AND (
                                EXISTS (
                                    SELECT 1
                                    FROM sbp_payments s
                                    WHERE s.telegram_id=u.telegram_id
                                      AND s.status='paid'
                                )
                                OR EXISTS (
                                    SELECT 1
                                    FROM star_payments sp
                                    WHERE sp.buyer_telegram_id=u.telegram_id
                                )
                              )
                            """,
                            (source,),
                        )
                    ).fetchone()
                )[0]
            )
        conversion = (buyers / arrived * 100.0) if arrived else 0.0
        return {
            "source": source,
            "arrived": arrived,
            "buyers": buyers,
            "conversion": conversion,
        }

    async def list_attribution_stats(
        self,
        *,
        prefix: str = "utm_",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return campaign totals for dynamic Telegram start parameters."""
        prefix = str(prefix or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,63}", prefix):
            raise ValueError("invalid attribution prefix")
        limit = max(1, min(int(limit), 100))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT u.attribution_source AS source,
                           COUNT(*) AS arrived,
                           SUM(CASE WHEN
                               EXISTS (
                                   SELECT 1 FROM sbp_payments s
                                   WHERE s.telegram_id=u.telegram_id AND s.status='paid'
                               ) OR EXISTS (
                                   SELECT 1 FROM star_payments sp
                                   WHERE sp.buyer_telegram_id=u.telegram_id
                               ) THEN 1 ELSE 0 END) AS buyers
                    FROM users u
                    WHERE u.attribution_source LIKE ?
                    GROUP BY u.attribution_source
                    ORDER BY arrived DESC, source
                    LIMIT ?
                    """,
                    (f"{prefix}%", limit),
                )
            ).fetchall()
        result = []
        for row in rows:
            arrived = int(row["arrived"] or 0)
            buyers = int(row["buyers"] or 0)
            result.append({
                "source": str(row["source"]),
                "arrived": arrived,
                "buyers": buyers,
                "conversion": buyers / arrived * 100.0 if arrived else 0.0,
            })
        return result

    async def get_user(self, telegram_id: int) -> dict[str, Any]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token):
            return None
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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

    async def list_users_page(self, page: int = 0, page_size: int = 12) -> tuple[list[dict[str, Any]], int]:
        page_size = max(1, min(int(page_size), 20))
        page = max(0, int(page))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            total = int((await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0])
            rows = await (
                await db.execute(
                    "SELECT * FROM users ORDER BY created_at DESC, telegram_id DESC LIMIT ? OFFSET ?",
                    (page_size, page * page_size),
                )
            ).fetchall()
        return [dict(row) for row in rows], total

    async def get_last_payment(self, telegram_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT method, amount, currency, created_at FROM (
                        SELECT 'СБП' AS method, amount_rub AS amount, 'RUB' AS currency, created_at
                        FROM sbp_payments WHERE target_telegram_id=? OR telegram_id=?
                        UNION ALL
                        SELECT 'Telegram Stars', stars, 'XTR', created_at
                        FROM star_payments WHERE target_telegram_id=? OR buyer_telegram_id=?
                    ) ORDER BY created_at DESC LIMIT 1
                    """,
                    (telegram_id, telegram_id, telegram_id, telegram_id),
                )
            ).fetchone()
        return dict(row) if row else None

    async def adjust_subscription_days(self, telegram_id: int, days_delta: int) -> dict[str, Any]:
        if not isinstance(days_delta, int) or days_delta == 0 or abs(days_delta) > 3650:
            raise ValueError("invalid days delta")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT subscription_until, plan_name FROM users WHERE telegram_id=?", (telegram_id,)
            )).fetchone()
            if row is None:
                raise KeyError(telegram_id)
            current = from_iso(row["subscription_until"])
            if days_delta > 0:
                start = max(utcnow(), current) if current else utcnow()
                until = start + timedelta(days=days_delta)
            else:
                start = current if current and current > utcnow() else utcnow()
                until = max(utcnow(), start + timedelta(days=days_delta))
            active = until > utcnow()
            await db.execute(
                "UPDATE users SET subscription_until=?, plan_name=CASE WHEN ? "
                "THEN COALESCE(NULLIF(plan_name, ''), 'Ручная подписка') ELSE '' END WHERE telegram_id=?",
                (to_iso(until) if active else None, int(active), telegram_id),
            )
            await db.commit()
        return await self.get_user(telegram_id)

    async def set_device_limit(self, telegram_id: int, limit: int) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_DEVICES:
            raise ValueError("device limit must be 1..5")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                "UPDATE users SET max_devices=?, bonus_devices=? WHERE telegram_id=?",
                (limit, limit - 1, telegram_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(telegram_id)
            await db.commit()
        return await self.get_user(telegram_id)

    async def set_referrer_once(
        self,
        telegram_id: int,
        referrer_id: int,
    ) -> bool:
        if telegram_id == referrer_id:
            return False
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
                inserted = await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (
                        referrer_id, referred_id, created_at, qualified_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (referrer_id, telegram_id, to_iso(utcnow()), to_iso(utcnow())),
                )
                rewarded = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded_at IS NOT NULL",
                        (referrer_id,),
                    )
                ).fetchone()
                if inserted.rowcount == 1 and int(rewarded[0]) < 3:
                    now = utcnow()
                    reward = await db.execute(
                        "UPDATE referrals SET rewarded_at=? WHERE referrer_id=? AND referred_id=? AND rewarded_at IS NULL",
                        (to_iso(now), referrer_id, telegram_id),
                    )
                    if reward.rowcount == 1:
                        owner = await (
                            await db.execute(
                                "SELECT subscription_until FROM users WHERE telegram_id=?",
                                (referrer_id,),
                            )
                        ).fetchone()
                        owner_until = from_iso(owner[0]) if owner else None
                        start = max(now, owner_until) if owner_until else now
                        await db.execute(
                            """
                            UPDATE users SET subscription_until=?,
                                plan_name=CASE WHEN subscription_until IS NULL OR subscription_until<=?
                                    THEN 'Реферальный бонус' ELSE plan_name END
                            WHERE telegram_id=?
                            """,
                            (to_iso(start + timedelta(days=1)), to_iso(now), referrer_id),
                        )
            await db.commit()
            return cursor.rowcount == 1

    async def referral_count(self, telegram_id: int) -> int:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM users WHERE referrer_id=?",
                    (telegram_id,),
                )
            ).fetchone()
        return int(row[0])

    async def referral_stats(self, telegram_id: int) -> dict[str, int]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                "UPDATE users SET last_menu_message_id=? WHERE telegram_id=?",
                (message_id, telegram_id),
            )
            await db.commit()

    async def extend_subscription(
        self,
        telegram_id: int,
        days: int,
        plan_name: str,
        max_devices: int,
    ) -> dict[str, Any]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            )).fetchone()
            if row is None:
                raise KeyError(telegram_id)
            current = from_iso(row["subscription_until"])
            until = max(utcnow(), current or utcnow()) + timedelta(days=days)
            await db.execute(
                "UPDATE users SET subscription_until=?, plan_name=?, traffic_limit_gb=0 WHERE telegram_id=?",
                (to_iso(until), plan_name, telegram_id),
            )
            await db.commit()
        return await self.get_user(telegram_id)

    async def grant_subscription_by_admin(
        self,
        telegram_id: int,
        days: int,
        plan_name: str,
        granted_by: int,
        *,
        action: str = "grant",
    ) -> dict[str, Any]:
        """Extend access and record its administrative origin atomically."""

        days = int(days)
        if days < 1 or action not in {"grant", "add"}:
            raise ValueError("Invalid administrative subscription grant")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT subscription_until FROM users WHERE telegram_id=?",
                (int(telegram_id),),
            )).fetchone()
            if row is None:
                await db.rollback()
                raise KeyError(telegram_id)
            current = from_iso(row["subscription_until"])
            until = max(utcnow(), current or utcnow()) + timedelta(days=days)
            now = to_iso(utcnow())
            await db.execute(
                "UPDATE users SET subscription_until=?, plan_name=?, traffic_limit_gb=0 "
                "WHERE telegram_id=?",
                (to_iso(until), plan_name, int(telegram_id)),
            )
            await db.execute(
                "INSERT INTO admin_subscription_grants "
                "(telegram_id, granted_by, days, action, granted_at) VALUES (?, ?, ?, ?, ?)",
                (int(telegram_id), int(granted_by), days, action, now),
            )
            await db.commit()
        return await self.get_user(int(telegram_id))

    async def change_device_slots(
        self,
        telegram_id: int,
        delta: int,
        max_total_devices: int = 5,
    ) -> dict[str, Any] | None:
        delta = int(delta)
        max_total_devices = min(5, max(1, int(max_total_devices)))

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
            allowed = set(PLANS)
            selected = [part.strip() for part in plans.split(",") if part.strip()]
            if not selected or any(part not in allowed for part in selected):
                raise ValueError("invalid applicable plans")
            plans = ",".join(dict.fromkeys(selected))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        created_at = utcnow()
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                INSERT INTO payment_intents (
                    intent_id, buyer_telegram_id, target_telegram_id,
                    product_code, original_amount_rub, discount_amount_rub,
                    final_amount_rub, currency, currency_amount,
                    promo_id, promo_code, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id, buyer_id, target_id, product_code,
                    original_amount_rub, discount_amount_rub, final_amount_rub,
                    currency.upper(), currency_amount, promo_id, promo_code,
                    to_iso(created_at), to_iso(created_at + timedelta(minutes=20)),
                ),
            )
            await db.commit()

    async def get_payment_intent(self, intent_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM payment_intents WHERE intent_id=?",
                    (intent_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def mark_payment_intent_paid(self, intent_id: str) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                "UPDATE payment_intents SET status='paid', paid_at=? "
                "WHERE intent_id=? AND status='created' AND expires_at>?",
                (to_iso(utcnow()), intent_id, to_iso(utcnow())),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def redeem_free_days_promo(self, code: str, telegram_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                INSERT INTO sbp_payments (
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

    async def create_sbp_order(
        self, *, order_id: str, telegram_id: int, plan_code: str,
        amount_rub: int, target_telegram_id: int | None = None,
        original_amount_rub: int | None = None, discount_amount_rub: int = 0,
        promo_id: int | None = None, promo_code: str | None = None,
    ) -> str:
        local_id = f"creating:{order_id}"
        await self.create_sbp_payment(
            payment_id=local_id, order_id=order_id, telegram_id=telegram_id,
            target_telegram_id=target_telegram_id, plan_code=plan_code,
            amount_rub=amount_rub, original_amount_rub=original_amount_rub,
            discount_amount_rub=discount_amount_rub, promo_id=promo_id,
            promo_code=promo_code,
        )
        await self.set_sbp_status(local_id, "creating")
        return local_id

    async def attach_sbp_provider_payment(
        self, local_id: str, provider_payment_id: str, pay_url: str,
    ) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                "UPDATE sbp_payments SET payment_id=?, pay_url=?, status='awaiting_payment' "
                "WHERE payment_id=? AND status='creating'",
                (provider_payment_id, pay_url, local_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Local payment order is not attachable")
            await db.commit()

    async def get_sbp_payment(self, payment_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    "SELECT * FROM sbp_payments WHERE payment_id=?",
                    (payment_id,),
                )
            ).fetchone()
        return dict(row) if row else None

    async def list_sbp_for_reconciliation(self, limit: int = 100) -> list[dict[str, Any]]:
        """Prioritize unsettled payments, then rotate through paid history forever."""
        limit = max(10, min(int(limit), 500))
        pending_limit = max(1, int(limit * 0.75))
        paid_limit = max(1, limit - pending_limit)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            pending = await (
                await db.execute(
                    """
                    SELECT * FROM sbp_payments
                    WHERE status IN ('created','creating','awaiting_payment','processing')
                    ORDER BY
                        COALESCE(reconciliation_checked_at, '1970-01-01T00:00:00+00:00'),
                        created_at
                    LIMIT ?
                    """,
                    (pending_limit,),
                )
            ).fetchall()
            paid = await (
                await db.execute(
                    """
                    SELECT * FROM sbp_payments
                    WHERE status='paid'
                    ORDER BY
                        COALESCE(reconciliation_checked_at, '1970-01-01T00:00:00+00:00'),
                        COALESCE(paid_at, created_at)
                    LIMIT ?
                    """,
                    (paid_limit,),
                )
            ).fetchall()
        return [dict(row) for row in (*pending, *paid)]

    async def mark_sbp_reconciled(self, payment_id: str) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                "UPDATE sbp_payments SET reconciliation_checked_at=? WHERE payment_id=?",
                (to_iso(utcnow()), payment_id),
            )
            await db.commit()

    async def reverse_sbp_access(self, payment_id: str, status: str) -> dict[str, Any] | None:
        """Reverse exactly the entitlement created by an SBP payment, once."""
        normalized = str(status or "").lower()
        if normalized not in {"refunded", "chargeback"}:
            raise ValueError("Only refunded/chargeback can reverse access")

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            payment = await (
                await db.execute(
                    "SELECT * FROM sbp_payments WHERE payment_id=?",
                    (payment_id,),
                )
            ).fetchone()
            if payment is None:
                await db.rollback()
                return None

            if payment["access_reversed_at"]:
                await db.execute(
                    "UPDATE sbp_payments SET status=?, reconciliation_checked_at=? WHERE payment_id=?",
                    (normalized, to_iso(utcnow()), payment_id),
                )
                await db.commit()
                return {"changed": False, "target_telegram_id": int(payment["target_telegram_id"] or payment["telegram_id"])}

            target_id = int(payment["target_telegram_id"] or payment["telegram_id"])
            user = await (
                await db.execute(
                    "SELECT * FROM users WHERE telegram_id=?",
                    (target_id,),
                )
            ).fetchone()
            if user is None:
                await db.rollback()
                raise ValueError("Payment recipient does not exist")

            code = str(payment["plan_code"])
            if code == DEVICE_PRODUCT_CODE:
                bonus = max(0, int(user["bonus_devices"] or 0) - 1)
                max_devices = max(BASE_DEVICES, min(MAX_DEVICES, BASE_DEVICES + bonus))
                await db.execute(
                    "UPDATE users SET bonus_devices=?, max_devices=? WHERE telegram_id=?",
                    (bonus, max_devices, target_id),
                )
            else:
                if code not in PLANS:
                    await db.rollback()
                    raise ValueError("Unknown refunded payment product")
                days = int(PLANS[code]["days"])
                current = from_iso(user["subscription_until"])
                if current:
                    until = current - timedelta(days=days)
                    if until <= utcnow():
                        until_value = None
                        plan_name = ""
                    else:
                        until_value = to_iso(until)
                        plan_name = str(user["plan_name"] or "")
                    await db.execute(
                        "UPDATE users SET subscription_until=?, plan_name=? WHERE telegram_id=?",
                        (until_value, plan_name, target_id),
                    )

            reversed_at = to_iso(utcnow())
            await db.execute(
                """
                UPDATE sbp_payments
                SET status=?, access_reversed_at=?, reconciliation_checked_at=?
                WHERE payment_id=?
                """,
                (normalized, reversed_at, reversed_at, payment_id),
            )
            await db.commit()
            return {"changed": True, "target_telegram_id": target_id, "plan_code": code}

    async def set_sbp_status(self, payment_id: str, status: str) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            normalized = status[:32]
            if normalized in {"refunded", "chargeback"}:
                await db.execute(
                    "UPDATE sbp_payments SET status=? WHERE payment_id=?",
                    (normalized, payment_id),
                )
            else:
                await db.execute(
                    "UPDATE sbp_payments SET status=? WHERE payment_id=? AND status!='paid'",
                    (normalized, payment_id),
                )
            await db.commit()

    async def mark_sbp_paid(self, payment_id: str) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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

    async def _apply_product(self, db, target_id: int, code: str) -> None:
        row = await (await db.execute(
            "SELECT * FROM users WHERE telegram_id=?", (target_id,)
        )).fetchone()
        if row is None:
            raise ValueError("Payment recipient does not exist")
        user = dict(row)
        if code == "device":
            if int(user["max_devices"]) >= MAX_DEVICES:
                raise ValueError("Device limit reached; payment requires support")
            await db.execute(
                "UPDATE users SET bonus_devices=bonus_devices+1, max_devices=max_devices+1 WHERE telegram_id=?",
                (target_id,),
            )
        else:
            plan = PLANS[code]
            current = from_iso(user["subscription_until"])
            until = max(utcnow(), current or utcnow()) + timedelta(days=plan["days"])
            await db.execute(
                "UPDATE users SET subscription_until=?, plan_name=?, traffic_limit_gb=0 WHERE telegram_id=?",
                (to_iso(until), plan["name"], target_id),
            )

    async def _consume_paid_promo(
        self, db, *, promo_id: int | None, buyer_id: int, payment_id: str,
        product_code: str, original_amount: int, discount_amount: int,
        final_amount: int,
    ) -> None:
        if promo_id is None:
            return
        promo = await (await db.execute(
            "SELECT * FROM service_promo_codes WHERE id=?", (promo_id,)
        )).fetchone()
        if promo is None or promo["type"] != "discount" or not int(promo["active"]):
            raise ValueError("Promo is no longer available")
        expires = from_iso(promo["expires_at"])
        if expires and expires <= utcnow():
            raise ValueError("Promo has expired")
        if promo["max_uses"] is not None and int(promo["used_count"]) >= int(promo["max_uses"]):
            raise ValueError("Promo usage limit reached")
        plans = str(promo["applicable_plans"] or "all")
        if plans != "all" and product_code not in plans.split(","):
            raise ValueError("Promo is not valid for this product")
        used = await (await db.execute(
            "SELECT COUNT(*) FROM promo_uses WHERE promo_id=? AND telegram_id=?",
            (promo_id, buyer_id),
        )).fetchone()
        if int(used[0]) >= int(promo["per_user_limit"]):
            raise ValueError("Promo user limit reached")

        expected_original = (
            EXTRA_DEVICE_PRICE_RUB if product_code == DEVICE_PRODUCT_CODE
            else int(PLANS[product_code]["price_rub"])
        )
        expected_discount = expected_original * int(promo["value"]) // 100
        expected_final = expected_original - expected_discount
        if (
            int(original_amount) != expected_original
            or int(discount_amount) != expected_discount
            or int(final_amount) != expected_final
        ):
            raise ValueError("Promo payment amounts are invalid")

        await db.execute(
            "INSERT INTO promo_uses (promo_id, telegram_id, payment_id, used_at) VALUES (?, ?, ?, ?)",
            (promo_id, buyer_id, payment_id, to_iso(utcnow())),
        )
        await db.execute(
            "UPDATE service_promo_codes SET used_count=used_count+1 WHERE id=?", (promo_id,)
        )

    @staticmethod
    def _product_price(product_code: str) -> int:
        if product_code == DEVICE_PRODUCT_CODE:
            return EXTRA_DEVICE_PRICE_RUB
        if product_code not in PLANS:
            raise ValueError("Unknown payment product")
        return int(PLANS[product_code]["price_rub"])

    async def redeem_full_discount(
        self, *, promo_id: int, buyer_id: int, target_id: int, product_code: str,
    ) -> dict[str, Any]:
        original = self._product_price(product_code)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            promo = await (await db.execute(
                "SELECT value FROM service_promo_codes WHERE id=?", (promo_id,)
            )).fetchone()
            if promo is None or int(promo["value"]) != 100:
                raise ValueError("Promo is not a full discount")
            redemption_id = f"free:{secrets.token_hex(16)}"
            await self._consume_paid_promo(
                db,
                promo_id=promo_id,
                buyer_id=buyer_id,
                payment_id=redemption_id,
                product_code=product_code,
                original_amount=original,
                discount_amount=original,
                final_amount=0,
            )
            await self._apply_product(db, target_id, product_code)
            await db.commit()
        return await self.get_user(target_id)

    async def settle_sbp_payment(self, payment_id: str) -> bool:
        """Commit verified payment and access together; replay is a no-op."""
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT * FROM sbp_payments WHERE payment_id=?", (payment_id,)
            )).fetchone()
            if row is None or row["status"] == "paid":
                return False
            original_amount = int(row["original_amount_rub"] or row["amount_rub"])
            if original_amount != self._product_price(str(row["plan_code"])):
                raise ValueError("Payment price is outdated")
            if row["promo_id"] is None and (
                int(row["discount_amount_rub"] or 0) != 0
                or int(row["amount_rub"]) != original_amount
            ):
                raise ValueError("Payment amounts are invalid")
            await self._consume_paid_promo(
                db,
                promo_id=row["promo_id"],
                buyer_id=int(row["telegram_id"]),
                payment_id=payment_id,
                product_code=str(row["plan_code"]),
                original_amount=original_amount,
                discount_amount=int(row["discount_amount_rub"] or 0),
                final_amount=int(row["amount_rub"]),
            )
            await self._apply_product(db, row["target_telegram_id"] or row["telegram_id"], row["plan_code"])
            await db.execute(
                "UPDATE sbp_payments SET status='paid', paid_at=? WHERE payment_id=?",
                (to_iso(utcnow()), payment_id),
            )
            await db.commit()
            return True

    async def settle_star_payment(
        self, telegram_payment_charge_id: str, buyer_telegram_id: int,
        target_telegram_id: int, plan_code: str, stars: int,
        intent_id: str | None = None,
    ) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            existing = await (await db.execute(
                "SELECT 1 FROM star_payments WHERE telegram_payment_charge_id=?",
                (telegram_payment_charge_id,),
            )).fetchone()
            if existing:
                return False
            intent = None
            if intent_id:
                intent = await (await db.execute(
                    "SELECT * FROM payment_intents WHERE intent_id=?", (intent_id,)
                )).fetchone()
                if intent is None or (
                    intent["buyer_telegram_id"] != buyer_telegram_id
                    or intent["target_telegram_id"] != target_telegram_id
                    or intent["product_code"] != plan_code
                    or intent["currency"] != "XTR"
                    or intent["currency_amount"] != stars
                ):
                    raise ValueError("Invalid payment intent")
                expires_at = from_iso(intent["expires_at"])
                if intent["status"] == "paid":
                    return False
                if intent["status"] != "created" or not expires_at or expires_at <= utcnow():
                    raise ValueError("Payment intent expired")
                expected_stars = rub_to_stars(int(intent["final_amount_rub"]))
                if (
                    int(intent["original_amount_rub"]) != self._product_price(plan_code)
                    or expected_stars != stars
                ):
                    raise ValueError("Payment intent price is invalid")
                await self._consume_paid_promo(
                    db,
                    promo_id=intent["promo_id"],
                    buyer_id=buyer_telegram_id,
                    payment_id=telegram_payment_charge_id,
                    product_code=plan_code,
                    original_amount=int(intent["original_amount_rub"]),
                    discount_amount=int(intent["discount_amount_rub"]),
                    final_amount=int(intent["final_amount_rub"]),
                )
            await self._apply_product(db, target_telegram_id, plan_code)
            await db.execute(
                "INSERT INTO star_payments VALUES (?, ?, ?, ?, ?, ?)",
                (telegram_payment_charge_id, buyer_telegram_id, target_telegram_id,
                 plan_code, stars, to_iso(utcnow())),
            )
            if intent:
                await db.execute(
                    "UPDATE payment_intents SET status='paid', paid_at=? WHERE intent_id=?",
                    (to_iso(utcnow()), intent_id),
                )
            await db.commit()
            return True

    async def get_admin_role(self, telegram_id: int) -> str | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                "DELETE FROM admin_roles WHERE telegram_id=?",
                (telegram_id,),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def list_admin_roles(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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

    async def create_support_ticket(
        self,
        *,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        message: str,
    ) -> dict[str, Any]:
        return await self.create_support_thread(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            message_type="text",
            text=message,
        )

    async def list_support_tickets(
        self,
        *,
        page: int = 0,
        page_size: int = 10,
        status: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), 20))
        params: list[Any] = []
        clauses = ["deleted_at IS NULL"]
        if status == "closed":
            clauses.append("closed_at IS NOT NULL")
        elif status in {"open", "answered"}:
            clauses.extend(["closed_at IS NULL", "status=?"])
            params.append(status)
        where = "WHERE " + " AND ".join(clauses)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            total = int((await (await db.execute(
                f"SELECT COUNT(*) FROM support_tickets {where}", tuple(params)
            )).fetchone())[0])
            rows = await (
                await db.execute(
                    f"""
                    SELECT *
                    FROM support_tickets
                    {where}
                    ORDER BY
                        CASE status WHEN 'open' THEN 0 WHEN 'answered' THEN 1 ELSE 2 END,
                        created_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    (*params, page_size, page * page_size),
                )
            ).fetchall()
        return [self._support_dict(row) for row in rows], total

    async def answer_support_ticket(
        self,
        *,
        ticket_id: int,
        answered_by: int,
        answer_text: str,
    ) -> dict[str, Any] | None:
        return await self.add_support_message(
            ticket_id=ticket_id,
            sender_type="admin",
            sender_telegram_id=answered_by,
            message_type="text",
            text=answer_text,
            is_admin=True,
        )

    async def set_support_session(
        self, telegram_id: int, mode: str, ticket_id: int | None = None, payload: str | None = None
    ) -> None:
        if mode not in {
            "new", "user_reply", "admin_reply", "admin_search", "admin_days", "gift",
            "ad_text", "ad_photo", "ad_url", "ad_button", "ad_channel", "ad_confirm",
            "giveaway_text", "giveaway_photo", "giveaway_winners", "giveaway_days",
            "giveaway_end_value", "giveaway_channels", "giveaway_confirm",
        }:
            raise ValueError("invalid support session")
        now = utcnow()
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                INSERT INTO interaction_sessions (
                    telegram_id, mode, ticket_id, payload, updated_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET mode=excluded.mode,
                    ticket_id=excluded.ticket_id, payload=excluded.payload,
                    updated_at=excluded.updated_at, expires_at=excluded.expires_at
                """,
                (telegram_id, mode, ticket_id, payload, to_iso(now), to_iso(now + timedelta(hours=1))),
            )
            await db.commit()

    @staticmethod
    def _support_dict(row: Any) -> dict[str, Any]:
        result = dict(row)
        if result.get("closed_at"):
            result["status"] = "closed"
        return result

    async def get_support_session(self, telegram_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                "SELECT * FROM interaction_sessions "
                "WHERE telegram_id=? AND (expires_at IS NULL OR expires_at>?)",
                (telegram_id, to_iso(utcnow())),
            )).fetchone()
        return dict(row) if row else None

    async def clear_support_session(self, telegram_id: int) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute("DELETE FROM interaction_sessions WHERE telegram_id=?", (telegram_id,))
            await db.execute("DELETE FROM support_sessions WHERE telegram_id=?", (telegram_id,))
            await db.commit()

    async def create_support_thread(
        self, *, telegram_id: int, username: str | None, first_name: str | None,
        message_type: str, text: str | None = None, file_id: str | None = None,
        file_unique_id: str | None = None, caption: str | None = None,
    ) -> dict[str, Any]:
        created = to_iso(utcnow())
        preview = (text or caption or {"photo": "Фото", "video": "Видео"}.get(message_type, "Обращение")).strip()
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "INSERT INTO support_tickets (telegram_id, username, first_name, message, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (telegram_id, username, first_name or "", preview[:3000], created, created),
            )
            ticket_id = int(cursor.lastrowid)
            await self._insert_support_message(
                db, ticket_id, "user", telegram_id, message_type, text, file_id,
                file_unique_id, caption, created,
            )
            await db.commit()
        return await self.get_support_ticket(ticket_id)

    async def _insert_support_message(
        self, db, ticket_id: int, sender_type: str, sender_id: int, message_type: str,
        text: str | None, file_id: str | None, file_unique_id: str | None,
        caption: str | None, created_at: str,
    ) -> None:
        if sender_type not in {"user", "admin"} or message_type not in {"text", "photo", "video"}:
            raise ValueError("unsupported support message")
        clean_text = str(text or "").strip() or None
        clean_caption = str(caption or "").strip() or None
        if clean_text and len(clean_text) > 3000:
            raise ValueError("support text too long")
        if clean_caption and len(clean_caption) > 1024:
            raise ValueError("support caption too long")
        if message_type == "text" and not clean_text:
            raise ValueError("support text required")
        if message_type != "text" and (not file_id or len(file_id) > 512 or not file_unique_id or len(file_unique_id) > 256):
            raise ValueError("support media metadata required")
        await db.execute(
            """
            INSERT INTO support_messages (
                ticket_id, sender_type, sender_telegram_id, message_type,
                text, file_id, file_unique_id, caption, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ticket_id, sender_type, sender_id, message_type, clean_text,
             file_id, file_unique_id, clean_caption, created_at),
        )

    async def add_support_message(
        self, *, ticket_id: int, sender_type: str, sender_telegram_id: int,
        message_type: str, text: str | None = None, file_id: str | None = None,
        file_unique_id: str | None = None, caption: str | None = None,
        owner_id: int | None = None, is_admin: bool = False,
    ) -> dict[str, Any] | None:
        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            ticket = await (await db.execute(
                "SELECT * FROM support_tickets WHERE id=? AND deleted_at IS NULL", (ticket_id,)
            )).fetchone()
            if ticket is None or (not is_admin and int(ticket["telegram_id"]) != int(owner_id or 0)):
                await db.rollback()
                return None
            if ticket["status"] == "closed" or ticket["closed_at"]:
                await db.rollback()
                raise ValueError("ticket closed")
            await self._insert_support_message(
                db, ticket_id, sender_type, sender_telegram_id, message_type,
                text, file_id, file_unique_id, caption, now,
            )
            status = "answered" if sender_type == "admin" else "open"
            await db.execute(
                "UPDATE support_tickets SET status=?, updated_at=? WHERE id=?",
                (status, now, ticket_id),
            )
            await db.commit()
        return await self.get_support_ticket(ticket_id, include_deleted=is_admin)

    async def get_support_ticket(
        self, ticket_id: int, *, owner_id: int | None = None,
        is_admin: bool = False, include_deleted: bool = False,
    ) -> dict[str, Any] | None:
        clauses = ["id=?"]
        params: list[Any] = [int(ticket_id)]
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if not is_admin:
            if owner_id is None:
                # Compatibility for trusted internal callers.
                pass
            else:
                clauses.append("telegram_id=?")
                params.append(int(owner_id))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute(
                f"SELECT * FROM support_tickets WHERE {' AND '.join(clauses)}", tuple(params)
            )).fetchone()
        return self._support_dict(row) if row else None

    async def list_support_messages(
        self, ticket_id: int, *, owner_id: int | None = None, is_admin: bool = False,
    ) -> list[dict[str, Any]]:
        ticket = await self.get_support_ticket(ticket_id, owner_id=owner_id, is_admin=is_admin)
        if not ticket:
            return []
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM support_messages WHERE ticket_id=? ORDER BY created_at, id", (ticket_id,)
            )).fetchall()
        return [dict(row) for row in rows]

    async def list_user_support_tickets(self, telegram_id: int, page: int = 0, page_size: int = 10) -> tuple[list[dict[str, Any]], int]:
        page, page_size = max(0, int(page)), max(1, min(int(page_size), 20))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            total = int((await (await db.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE telegram_id=? AND deleted_at IS NULL", (telegram_id,)
            )).fetchone())[0])
            rows = await (await db.execute(
                "SELECT * FROM support_tickets WHERE telegram_id=? AND deleted_at IS NULL "
                "ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                (telegram_id, page_size, page * page_size),
            )).fetchall()
        return [self._support_dict(row) for row in rows], total

    async def recent_support_ticket_count(self, telegram_id: int, minutes: int = 10) -> int:
        since = to_iso(utcnow() - timedelta(minutes=max(1, int(minutes))))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM support_tickets "
                "WHERE telegram_id=? AND created_at>=? AND deleted_at IS NULL",
                (telegram_id, since),
            )).fetchone()
        return int(row[0])

    async def set_support_status(
        self, ticket_id: int, status: str, actor_id: int, *, is_admin: bool,
    ) -> dict[str, Any] | None:
        if status not in {"open", "closed"}:
            raise ValueError("invalid status")
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            ticket = await (await db.execute(
                "SELECT * FROM support_tickets WHERE id=? AND deleted_at IS NULL", (ticket_id,)
            )).fetchone()
            if ticket is None or (not is_admin and int(ticket["telegram_id"]) != actor_id):
                return None
            if not is_admin and status != "closed":
                return None
            now = to_iso(utcnow())
            if status == "closed":
                await db.execute(
                    "UPDATE support_tickets SET updated_at=?, closed_at=?, closed_by=? WHERE id=?",
                    (now, now, actor_id, ticket_id),
                )
            else:
                await db.execute(
                    "UPDATE support_tickets SET status='open', updated_at=?, closed_at=NULL, closed_by=NULL WHERE id=?",
                    (now, ticket_id),
                )
            await db.commit()
        return await self.get_support_ticket(ticket_id, owner_id=actor_id, is_admin=is_admin)

    async def soft_delete_support_ticket(self, ticket_id: int, admin_id: int) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                "UPDATE support_tickets SET deleted_at=?, deleted_by=? WHERE id=? AND deleted_at IS NULL",
                (to_iso(utcnow()), admin_id, ticket_id),
            )
            if cursor.rowcount == 1:
                # Do not leave users/admins stuck in a composer for a ticket
                # that no longer exists.
                await db.execute(
                    "DELETE FROM interaction_sessions WHERE ticket_id=?",
                    (ticket_id,),
                )
                await db.execute(
                    "DELETE FROM support_sessions WHERE ticket_id=?",
                    (ticket_id,),
                )
            await db.commit()
        return cursor.rowcount == 1

    async def admin_overview(self) -> dict[str, int]:
        now = to_iso(utcnow())
        day_ago = to_iso(utcnow() - timedelta(days=1))
        week_ago = to_iso(utcnow() - timedelta(days=7))
        month_ago = to_iso(utcnow() - timedelta(days=30))

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
            self_paid_sql = """
                EXISTS (
                    SELECT 1 FROM sbp_payments s
                    WHERE s.status='paid'
                      AND s.telegram_id=u.telegram_id
                      AND COALESCE(s.target_telegram_id, s.telegram_id)=u.telegram_id
                      AND s.plan_code!='device'
                )
                OR EXISTS (
                    SELECT 1 FROM star_payments sp
                    WHERE sp.buyer_telegram_id=u.telegram_id
                      AND sp.target_telegram_id=u.telegram_id
                      AND sp.plan_code!='device'
                )
            """
            paid_total = (await (await db.execute(
                f"SELECT COUNT(*) FROM users u WHERE {self_paid_sql}"
            )).fetchone())[0]
            active_paid = (await (await db.execute(
                f"""
                SELECT COUNT(*) FROM users u
                WHERE u.subscription_until IS NOT NULL
                  AND u.subscription_until > ?
                  AND ({self_paid_sql})
                """,
                (now,),
            )).fetchone())[0]
            active_without_self_payment = max(0, int(active) - int(active_paid))
            admin_granted_total = (await (await db.execute(
                """
                SELECT COUNT(DISTINCT g.telegram_id)
                FROM admin_subscription_grants g
                WHERE NOT EXISTS (
                    SELECT 1 FROM sbp_payments s
                    WHERE s.status='paid' AND s.telegram_id=g.telegram_id
                      AND COALESCE(s.target_telegram_id, s.telegram_id)=g.telegram_id
                      AND s.plan_code!='device'
                ) AND NOT EXISTS (
                    SELECT 1 FROM star_payments sp
                    WHERE sp.buyer_telegram_id=g.telegram_id
                      AND sp.target_telegram_id=g.telegram_id
                      AND sp.plan_code!='device'
                )
                """,
            )).fetchone())[0]
            active_admin_granted = (await (await db.execute(
                f"""
                SELECT COUNT(*) FROM users u
                WHERE u.subscription_until IS NOT NULL
                  AND u.subscription_until > ?
                  AND EXISTS (
                      SELECT 1 FROM admin_subscription_grants g
                      WHERE g.telegram_id=u.telegram_id
                  )
                  AND NOT ({self_paid_sql})
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
            "paid_total": int(paid_total),
            "active_paid": int(active_paid),
            "active_without_self_payment": active_without_self_payment,
            "admin_granted_total": int(admin_granted_total),
            "active_admin_granted": int(active_admin_granted),
            "trials": int(trials),
            "sbp_paid": int(sbp_paid),
            "sbp_revenue": int(sbp_revenue),
            "star_paid": int(star_paid),
            "star_revenue": int(star_revenue),
        }

    async def business_analytics(self) -> dict[str, Any]:
        """Business KPIs for the Telegram admin dashboard."""
        now = utcnow()
        day_ago = to_iso(now - timedelta(days=1))
        week_ago = to_iso(now - timedelta(days=7))
        month_ago = to_iso(now - timedelta(days=30))
        now_iso = to_iso(now)
        in_1d = to_iso(now + timedelta(days=1))
        in_3d = to_iso(now + timedelta(days=3))
        in_7d = to_iso(now + timedelta(days=7))

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    WITH payments AS (
                        SELECT
                            'sbp' AS method,
                            COALESCE(target_telegram_id, telegram_id) AS target_id,
                            amount_rub AS rub,
                            0 AS stars,
                            COALESCE(paid_at, created_at) AS paid_at
                        FROM sbp_payments
                        WHERE status='paid' AND plan_code!='device'
                        UNION ALL
                        SELECT
                            'stars' AS method,
                            target_telegram_id AS target_id,
                            0 AS rub,
                            stars AS stars,
                            created_at AS paid_at
                        FROM star_payments
                        WHERE plan_code!='device'
                    ),
                    ranked AS (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY target_id
                                ORDER BY paid_at, method
                            ) AS purchase_no
                        FROM payments
                    )
                    SELECT
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN rub ELSE 0 END), 0) AS rub_day,
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN rub ELSE 0 END), 0) AS rub_week,
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN rub ELSE 0 END), 0) AS rub_month,
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN stars ELSE 0 END), 0) AS stars_day,
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN stars ELSE 0 END), 0) AS stars_week,
                        COALESCE(SUM(CASE WHEN paid_at>=? THEN stars ELSE 0 END), 0) AS stars_month,
                        COALESCE(SUM(CASE WHEN paid_at>=? AND purchase_no=1 THEN 1 ELSE 0 END), 0) AS new_month,
                        COALESCE(SUM(CASE WHEN paid_at>=? AND purchase_no>1 THEN 1 ELSE 0 END), 0) AS renew_month,
                        COALESCE(AVG(CASE WHEN paid_at>=? AND method='sbp' THEN rub END), 0) AS avg_rub_month,
                        COALESCE(AVG(CASE WHEN paid_at>=? AND method='stars' THEN stars END), 0) AS avg_stars_month
                    FROM ranked
                    """,
                    (
                        day_ago, week_ago, month_ago,
                        day_ago, week_ago, month_ago,
                        month_ago, month_ago, month_ago, month_ago,
                    ),
                )
            ).fetchone()

            expiry = await (
                await db.execute(
                    """
                    SELECT
                        SUM(CASE WHEN subscription_until>? AND subscription_until<=? THEN 1 ELSE 0 END) AS d1,
                        SUM(CASE WHEN subscription_until>? AND subscription_until<=? THEN 1 ELSE 0 END) AS d3,
                        SUM(CASE WHEN subscription_until>? AND subscription_until<=? THEN 1 ELSE 0 END) AS d7
                    FROM users
                    WHERE subscription_until IS NOT NULL
                    """,
                    (now_iso, in_1d, now_iso, in_3d, now_iso, in_7d),
                )
            ).fetchone()

            promo_rows = await (
                await db.execute(
                    """
                    SELECT
                        p.code,
                        p.type,
                        p.value,
                        p.used_count,
                        COUNT(DISTINCT pu.telegram_id) AS users
                    FROM service_promo_codes p
                    LEFT JOIN promo_uses pu ON pu.promo_id=p.id
                    GROUP BY p.id
                    ORDER BY p.used_count DESC, p.id DESC
                    LIMIT 8
                    """
                )
            ).fetchall()

            referral = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS invited,
                        SUM(CASE WHEN qualified_at IS NOT NULL THEN 1 ELSE 0 END) AS qualified,
                        SUM(CASE WHEN rewarded_at IS NOT NULL THEN 1 ELSE 0 END) AS rewarded
                    FROM referrals
                    """
                )
            ).fetchone()

        return {
            "rub_day": int(row["rub_day"] or 0),
            "rub_week": int(row["rub_week"] or 0),
            "rub_month": int(row["rub_month"] or 0),
            "stars_day": int(row["stars_day"] or 0),
            "stars_week": int(row["stars_week"] or 0),
            "stars_month": int(row["stars_month"] or 0),
            "new_month": int(row["new_month"] or 0),
            "renew_month": int(row["renew_month"] or 0),
            "avg_rub_month": float(row["avg_rub_month"] or 0),
            "avg_stars_month": float(row["avg_stars_month"] or 0),
            "expires_1d": int(expiry["d1"] or 0),
            "expires_3d": int(expiry["d3"] or 0),
            "expires_7d": int(expiry["d7"] or 0),
            "referrals_invited": int(referral["invited"] or 0),
            "referrals_qualified": int(referral["qualified"] or 0),
            "referrals_rewarded": int(referral["rewarded"] or 0),
            "promos": [dict(item) for item in promo_rows],
        }

    async def list_due_expiry_notifications(
        self,
        *,
        limit: int = 200,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return unsent 3/2/1-day reminders for currently active subscriptions."""
        current = (now or utcnow()).astimezone(timezone.utc)
        horizon = current + timedelta(days=3)
        limit = max(1, min(int(limit), 1000))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT u.telegram_id, u.first_name, u.subscription_until,
                           n.days_before AS sent_days_before
                    FROM users u
                    LEFT JOIN subscription_expiry_notifications n
                      ON n.telegram_id=u.telegram_id
                     AND n.subscription_until=u.subscription_until
                    WHERE u.subscription_until IS NOT NULL
                      AND u.subscription_until > ?
                      AND u.subscription_until <= ?
                    ORDER BY u.subscription_until, u.telegram_id
                    LIMIT ?
                    """,
                    (to_iso(current), to_iso(horizon), limit * 3),
                )
            ).fetchall()

        grouped: dict[tuple[int, str], dict[str, Any]] = {}
        for row in rows:
            key = (int(row["telegram_id"]), str(row["subscription_until"]))
            item = grouped.setdefault(
                key,
                {
                    "telegram_id": key[0],
                    "first_name": str(row["first_name"] or ""),
                    "subscription_until": key[1],
                    "sent_days": set(),
                },
            )
            if row["sent_days_before"] is not None:
                item["sent_days"].add(int(row["sent_days_before"]))

        due: list[dict[str, Any]] = []
        for item in grouped.values():
            expires = from_iso(item["subscription_until"])
            if not expires:
                continue
            seconds = (expires - current).total_seconds()
            if seconds <= 0:
                continue
            days_before = max(1, min(3, int((seconds + 86399) // 86400)))
            if days_before in item.pop("sent_days"):
                continue
            item["days_before"] = days_before
            due.append(item)
            if len(due) >= limit:
                break
        return due

    async def claim_expiry_notification(
        self,
        telegram_id: int,
        subscription_until: str,
        days_before: int,
    ) -> bool:
        """Atomically reserve a reminder so parallel loops cannot send duplicates."""
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO subscription_expiry_notifications
                    (telegram_id, subscription_until, days_before, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (telegram_id, subscription_until, days_before, to_iso(utcnow())),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def release_expiry_notification(
        self,
        telegram_id: int,
        subscription_until: str,
        days_before: int,
    ) -> None:
        """Release a failed delivery claim so a later pass can retry it."""
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                DELETE FROM subscription_expiry_notifications
                WHERE telegram_id=? AND subscription_until=? AND days_before=?
                """,
                (telegram_id, subscription_until, days_before),
            )
            await db.commit()

    async def list_active_users_for_vpn_sync(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Return active subscriptions in stable batches for provider repair."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT * FROM users
                    WHERE subscription_until IS NOT NULL
                      AND subscription_until > ?
                    ORDER BY telegram_id
                    LIMIT ? OFFSET ?
                    """,
                    (to_iso(utcnow()), limit, offset),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 20))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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

    async def create_giveaway(
        self,
        *,
        created_by: int,
        text_html: str,
        text_plain: str,
        photo_file_id: str | None,
        winners_count: int,
        prize_days: int,
        end_mode: str,
        ends_at: str | None = None,
        participant_limit: int | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        winners_count = int(winners_count)
        prize_days = int(prize_days)
        if not 1 <= winners_count <= 10:
            raise ValueError("winners_count must be 1..10")
        if not 1 <= prize_days <= 3650:
            raise ValueError("prize_days must be 1..3650")
        if end_mode not in {"time", "participants"}:
            raise ValueError("invalid giveaway end mode")
        if not str(text_html or "").strip() or len(str(text_plain or "")) > 650:
            raise ValueError("invalid giveaway text")

        normalized_ends_at: str | None = None
        normalized_limit: int | None = None
        if end_mode == "time":
            dt = from_iso(ends_at)
            if not dt or dt <= utcnow():
                raise ValueError("giveaway end time must be in the future")
            normalized_ends_at = to_iso(dt)
        else:
            normalized_limit = int(participant_limit or 0)
            if normalized_limit < winners_count or normalized_limit > 100000:
                raise ValueError("participant limit is invalid")

        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                INSERT INTO giveaways (
                    created_by, text_html, text_plain, photo_file_id,
                    winners_count, prize_days, end_mode, ends_at,
                    participant_limit, status, created_at, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(created_by),
                    str(text_html).strip(),
                    str(text_plain or "").strip(),
                    str(photo_file_id or "").strip() or None,
                    winners_count,
                    prize_days,
                    end_mode,
                    normalized_ends_at,
                    normalized_limit,
                    "active" if activate else "cancelled",
                    now,
                    now,
                ),
            )
            giveaway_id = int(cursor.lastrowid)
            await db.commit()
        result = await self.get_giveaway(giveaway_id)
        if result is None:
            raise RuntimeError("giveaway was not created")
        return result

    async def activate_giveaway(self, giveaway_id: int) -> bool:
        """Activate a staged giveaway only after at least one channel post exists."""
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                UPDATE giveaways
                SET status='active', started_at=?
                WHERE id=? AND status='cancelled'
                  AND EXISTS (
                      SELECT 1 FROM giveaway_posts gp
                      WHERE gp.giveaway_id=giveaways.id
                  )
                """,
                (to_iso(utcnow()), int(giveaway_id)),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def add_giveaway_post(
        self,
        giveaway_id: int,
        chat_id: int | str,
        message_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO giveaway_posts
                    (giveaway_id, chat_id, message_id, finalized_at)
                VALUES (?, ?, ?, NULL)
                """,
                (int(giveaway_id), str(chat_id), int(message_id)),
            )
            await db.commit()

    async def get_giveaway(self, giveaway_id: int) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            row = await (
                await db.execute(
                    """
                    SELECT g.*,
                           (SELECT COUNT(*) FROM giveaway_participants p
                            WHERE p.giveaway_id=g.id) AS participant_count
                    FROM giveaways g
                    WHERE g.id=?
                    """,
                    (int(giveaway_id),),
                )
            ).fetchone()
        return dict(row) if row else None

    async def list_giveaways(self, limit: int = 12) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT g.*,
                           (SELECT COUNT(*) FROM giveaway_participants p
                            WHERE p.giveaway_id=g.id) AS participant_count
                    FROM giveaways g
                    ORDER BY g.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_giveaway_posts(self, giveaway_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_posts
                    WHERE giveaway_id=?
                    ORDER BY chat_id
                    """,
                    (int(giveaway_id),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_giveaway_participants(self, giveaway_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_participants
                    WHERE giveaway_id=?
                    ORDER BY joined_at, telegram_id
                    """,
                    (int(giveaway_id),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_giveaway_participants_page(
        self,
        giveaway_id: int,
        *,
        page: int = 0,
        page_size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), 30))
        offset = page * page_size
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            total = int(
                (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id=?",
                            (int(giveaway_id),),
                        )
                    ).fetchone()
                )[0]
            )
            rows = await (
                await db.execute(
                    """
                    SELECT p.*,
                           CASE WHEN w.telegram_id IS NULL THEN 0 ELSE 1 END AS is_winner
                    FROM giveaway_participants p
                    LEFT JOIN giveaway_winners w
                      ON w.giveaway_id=p.giveaway_id
                     AND w.telegram_id=p.telegram_id
                    WHERE p.giveaway_id=?
                    ORDER BY p.joined_at, p.telegram_id
                    LIMIT ? OFFSET ?
                    """,
                    (int(giveaway_id), page_size, offset),
                )
            ).fetchall()
        return [dict(row) for row in rows], total

    async def add_giveaway_participant(
        self,
        *,
        giveaway_id: int,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
    ) -> dict[str, Any]:
        now_dt = utcnow()
        now = to_iso(now_dt)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            giveaway = await (
                await db.execute(
                    "SELECT * FROM giveaways WHERE id=?",
                    (int(giveaway_id),),
                )
            ).fetchone()
            if giveaway is None:
                await db.rollback()
                return {"state": "missing", "count": 0, "due": False}
            if str(giveaway["status"]) != "active":
                count = int(
                    (
                        await (
                            await db.execute(
                                "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id=?",
                                (int(giveaway_id),),
                            )
                        ).fetchone()
                    )[0]
                )
                await db.rollback()
                return {"state": "ended", "count": count, "due": True}

            if giveaway["end_mode"] == "time":
                ends = from_iso(giveaway["ends_at"])
                if not ends or ends <= now_dt:
                    count = int(
                        (
                            await (
                                await db.execute(
                                    "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id=?",
                                    (int(giveaway_id),),
                                )
                            ).fetchone()
                        )[0]
                    )
                    await db.rollback()
                    return {"state": "ended", "count": count, "due": True}

            existing = await (
                await db.execute(
                    """
                    SELECT 1 FROM giveaway_participants
                    WHERE giveaway_id=? AND telegram_id=?
                    """,
                    (int(giveaway_id), int(telegram_id)),
                )
            ).fetchone()

            if existing is None:
                await db.execute(
                    """
                    INSERT INTO giveaway_participants (
                        giveaway_id, telegram_id, username, first_name, joined_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(giveaway_id),
                        int(telegram_id),
                        str(username or "").strip() or None,
                        str(first_name or "").strip(),
                        now,
                    ),
                )
                state = "joined"
            else:
                await db.execute(
                    """
                    UPDATE giveaway_participants
                    SET username=?, first_name=?
                    WHERE giveaway_id=? AND telegram_id=?
                    """,
                    (
                        str(username or "").strip() or None,
                        str(first_name or "").strip(),
                        int(giveaway_id),
                        int(telegram_id),
                    ),
                )
                state = "already"

            count = int(
                (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM giveaway_participants WHERE giveaway_id=?",
                            (int(giveaway_id),),
                        )
                    ).fetchone()
                )[0]
            )
            due = (
                giveaway["end_mode"] == "participants"
                and count >= int(giveaway["participant_limit"] or 0)
            )
            await db.commit()
        return {
            "state": state,
            "count": count,
            "due": bool(due),
            "participant_limit": giveaway["participant_limit"],
        }

    async def giveaway_is_due(self, giveaway_id: int) -> bool:
        item = await self.get_giveaway(giveaway_id)
        if not item:
            return False
        status = str(item.get("status") or "")
        if status in {"finishing", "finished"}:
            return True
        if status != "active":
            return False
        if item.get("end_mode") == "time":
            ends = from_iso(item.get("ends_at"))
            return bool(ends and ends <= utcnow())
        return int(item.get("participant_count") or 0) >= int(item.get("participant_limit") or 0)

    async def mark_giveaway_finishing(self, giveaway_id: int) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                UPDATE giveaways
                SET status='finishing'
                WHERE id=? AND status='active'
                """,
                (int(giveaway_id),),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def save_giveaway_winners(
        self,
        giveaway_id: int,
        winners: list[dict[str, Any]],
    ) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            existing = int(
                (
                    await (
                        await db.execute(
                            "SELECT COUNT(*) FROM giveaway_winners WHERE giveaway_id=?",
                            (int(giveaway_id),),
                        )
                    ).fetchone()
                )[0]
            )
            if existing:
                await db.rollback()
                return
            for position, item in enumerate(winners, start=1):
                await db.execute(
                    """
                    INSERT INTO giveaway_winners (
                        giveaway_id, telegram_id, username, first_name, position
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        int(giveaway_id),
                        int(item["telegram_id"]),
                        str(item.get("username") or "").strip() or None,
                        str(item.get("first_name") or "").strip(),
                        position,
                    ),
                )
            await db.commit()

    async def get_giveaway_winners(self, giveaway_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_winners
                    WHERE giveaway_id=?
                    ORDER BY position
                    """,
                    (int(giveaway_id),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_giveaway_rerolls(self, giveaway_id: int) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            rows = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_rerolls
                    WHERE giveaway_id=?
                    ORDER BY id
                    """,
                    (int(giveaway_id),),
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def replace_giveaway_winner(
        self,
        giveaway_id: int,
        *,
        old_telegram_id: int,
        new_telegram_id: int,
        rerolled_by: int,
    ) -> dict[str, Any]:
        """Atomically revoke the old giveaway prize and install a new winner."""
        giveaway_id = int(giveaway_id)
        old_telegram_id = int(old_telegram_id)
        new_telegram_id = int(new_telegram_id)
        now_dt = utcnow()
        now = to_iso(now_dt)

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")

            giveaway = await (
                await db.execute(
                    "SELECT status, prize_days FROM giveaways WHERE id=?",
                    (giveaway_id,),
                )
            ).fetchone()
            if giveaway is None:
                await db.rollback()
                raise KeyError(giveaway_id)
            if str(giveaway["status"]) != "finished":
                await db.rollback()
                raise ValueError("giveaway is not finished")

            old_winner = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_winners
                    WHERE giveaway_id=? AND telegram_id=?
                    """,
                    (giveaway_id, old_telegram_id),
                )
            ).fetchone()
            if old_winner is None:
                await db.rollback()
                raise ValueError("selected user is not a current winner")

            new_participant = await (
                await db.execute(
                    """
                    SELECT *
                    FROM giveaway_participants
                    WHERE giveaway_id=? AND telegram_id=?
                    """,
                    (giveaway_id, new_telegram_id),
                )
            ).fetchone()
            if new_participant is None:
                await db.rollback()
                raise ValueError("replacement is not a participant")

            duplicate = await (
                await db.execute(
                    """
                    SELECT 1 FROM giveaway_winners
                    WHERE giveaway_id=? AND telegram_id=?
                    """,
                    (giveaway_id, new_telegram_id),
                )
            ).fetchone()
            if duplicate is not None:
                await db.rollback()
                raise ValueError("replacement is already a winner")

            days = int(giveaway["prize_days"])
            if old_winner["granted_at"]:
                old_user = await (
                    await db.execute(
                        """
                        SELECT subscription_until, plan_name
                        FROM users
                        WHERE telegram_id=?
                        """,
                        (old_telegram_id,),
                    )
                ).fetchone()
                if old_user is not None:
                    current = from_iso(old_user["subscription_until"])
                    if current:
                        reversed_until = current - timedelta(days=days)
                        still_active = reversed_until > now_dt
                        plan_name = str(old_user["plan_name"] or "")
                        if not still_active:
                            plan_name = ""
                        await db.execute(
                            """
                            UPDATE users
                            SET subscription_until=?, plan_name=?
                            WHERE telegram_id=?
                            """,
                            (
                                to_iso(reversed_until) if still_active else None,
                                plan_name,
                                old_telegram_id,
                            ),
                        )

            position = int(old_winner["position"])
            await db.execute(
                """
                INSERT INTO giveaway_rerolls (
                    giveaway_id, position,
                    old_telegram_id, old_username,
                    new_telegram_id, new_username,
                    rerolled_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    giveaway_id,
                    position,
                    old_telegram_id,
                    str(old_winner["username"] or "").strip() or None,
                    new_telegram_id,
                    str(new_participant["username"] or "").strip() or None,
                    int(rerolled_by),
                    now,
                ),
            )

            await db.execute(
                """
                DELETE FROM giveaway_winners
                WHERE giveaway_id=? AND telegram_id=?
                """,
                (giveaway_id, old_telegram_id),
            )
            await db.execute(
                """
                INSERT INTO giveaway_winners (
                    giveaway_id, telegram_id, username, first_name,
                    position, granted_at, notified_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    giveaway_id,
                    new_telegram_id,
                    str(new_participant["username"] or "").strip() or None,
                    str(new_participant["first_name"] or "").strip(),
                    position,
                ),
            )
            # Make channel result refresh crash-safe. If the process dies after
            # this transaction, reconciliation will edit the old result later.
            await db.execute(
                """
                UPDATE giveaway_posts
                SET finalized_at=NULL
                WHERE giveaway_id=?
                """,
                (giveaway_id,),
            )
            await db.commit()

        return {
            "giveaway_id": giveaway_id,
            "position": position,
            "old_telegram_id": old_telegram_id,
            "old_username": str(old_winner["username"] or "").strip(),
            "new_telegram_id": new_telegram_id,
            "new_username": str(new_participant["username"] or "").strip(),
            "new_first_name": str(new_participant["first_name"] or "").strip(),
            "prize_days": days,
        }

    async def grant_giveaway_prizes(self, giveaway_id: int) -> list[int]:
        """Grant every selected prize exactly once in one SQLite transaction."""
        now_dt = utcnow()
        now = to_iso(now_dt)
        granted_ids: list[int] = []
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("BEGIN IMMEDIATE")
            giveaway = await (
                await db.execute(
                    "SELECT prize_days, created_by FROM giveaways WHERE id=?",
                    (int(giveaway_id),),
                )
            ).fetchone()
            if giveaway is None:
                await db.rollback()
                return []
            days = int(giveaway["prize_days"])
            creator = int(giveaway["created_by"])
            winners = await (
                await db.execute(
                    """
                    SELECT telegram_id
                    FROM giveaway_winners
                    WHERE giveaway_id=? AND granted_at IS NULL
                    ORDER BY position
                    """,
                    (int(giveaway_id),),
                )
            ).fetchall()

            for winner in winners:
                telegram_id = int(winner["telegram_id"])
                user = await (
                    await db.execute(
                        """
                        SELECT subscription_until, plan_name
                        FROM users
                        WHERE telegram_id=?
                        """,
                        (telegram_id,),
                    )
                ).fetchone()
                if user is None:
                    continue
                current = from_iso(user["subscription_until"])
                active = bool(current and current > now_dt)
                until = max(now_dt, current or now_dt) + timedelta(days=days)
                plan_name = (
                    str(user["plan_name"] or "")
                    if active and str(user["plan_name"] or "").strip()
                    else "Розыгрыш MGN VPN"
                )
                await db.execute(
                    """
                    UPDATE users
                    SET subscription_until=?, plan_name=?, traffic_limit_gb=0
                    WHERE telegram_id=?
                    """,
                    (to_iso(until), plan_name, telegram_id),
                )
                await db.execute(
                    """
                    INSERT INTO admin_subscription_grants
                        (telegram_id, granted_by, days, action, granted_at)
                    VALUES (?, ?, ?, 'grant', ?)
                    """,
                    (telegram_id, creator, days, now),
                )
                await db.execute(
                    """
                    UPDATE giveaway_winners
                    SET granted_at=?
                    WHERE giveaway_id=? AND telegram_id=? AND granted_at IS NULL
                    """,
                    (now, int(giveaway_id), telegram_id),
                )
                granted_ids.append(telegram_id)
            await db.commit()
        return granted_ids

    async def mark_giveaway_finished(self, giveaway_id: int) -> None:
        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                UPDATE giveaways
                SET status='finished', finished_at=COALESCE(finished_at, ?)
                WHERE id=? AND status IN ('active', 'finishing', 'finished')
                """,
                (now, int(giveaway_id)),
            )
            await db.commit()

    async def mark_giveaway_admin_notified(self, giveaway_id: int) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                UPDATE giveaways
                SET admin_notified_at=COALESCE(admin_notified_at, ?)
                WHERE id=?
                """,
                (to_iso(utcnow()), int(giveaway_id)),
            )
            await db.commit()

    async def mark_giveaway_post_finalized(
        self,
        giveaway_id: int,
        chat_id: int | str,
    ) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                UPDATE giveaway_posts
                SET finalized_at=COALESCE(finalized_at, ?)
                WHERE giveaway_id=? AND chat_id=?
                """,
                (to_iso(utcnow()), int(giveaway_id), str(chat_id)),
            )
            await db.commit()

    async def mark_giveaway_winner_notified(
        self,
        giveaway_id: int,
        telegram_id: int,
    ) -> None:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute(
                """
                UPDATE giveaway_winners
                SET notified_at=COALESCE(notified_at, ?)
                WHERE giveaway_id=? AND telegram_id=?
                """,
                (to_iso(utcnow()), int(giveaway_id), int(telegram_id)),
            )
            await db.commit()

    async def list_giveaways_needing_work(self, limit: int = 100) -> list[int]:
        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT DISTINCT g.id
                    FROM giveaways g
                    WHERE
                        g.status='finishing'
                        OR (
                            g.status='active'
                            AND (
                                (g.end_mode='time' AND g.ends_at IS NOT NULL AND g.ends_at<=?)
                                OR (
                                    g.end_mode='participants'
                                    AND (
                                        SELECT COUNT(*)
                                        FROM giveaway_participants p
                                        WHERE p.giveaway_id=g.id
                                    ) >= COALESCE(g.participant_limit, 0)
                                )
                            )
                        )
                        OR (
                            g.status='finished'
                            AND (
                                EXISTS (
                                    SELECT 1 FROM giveaway_posts gp
                                    WHERE gp.giveaway_id=g.id AND gp.finalized_at IS NULL
                                )
                                OR EXISTS (
                                    SELECT 1 FROM giveaway_winners gw
                                    WHERE gw.giveaway_id=g.id
                                      AND (gw.granted_at IS NULL OR gw.notified_at IS NULL)
                                )
                                OR g.admin_notified_at IS NULL
                            )
                        )
                    ORDER BY g.id
                    LIMIT ?
                    """,
                    (now, max(1, min(int(limit), 500))),
                )
            ).fetchall()
        return [int(row[0]) for row in rows]

    async def cancel_giveaway(self, giveaway_id: int) -> bool:
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            cursor = await db.execute(
                """
                UPDATE giveaways
                SET status='cancelled', finished_at=?
                WHERE id=? AND status='active'
                """,
                (to_iso(utcnow()), int(giveaway_id)),
            )
            await db.commit()
        return cursor.rowcount == 1

    async def delete_giveaway(self, giveaway_id: int) -> bool:
        """Permanently remove giveaway metadata without touching granted subscriptions."""
        giveaway_id = int(giveaway_id)
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await (
                await db.execute(
                    "SELECT 1 FROM giveaways WHERE id=?",
                    (giveaway_id,),
                )
            ).fetchone()
            if exists is None:
                await db.rollback()
                return False

            await db.execute(
                "DELETE FROM giveaway_rerolls WHERE giveaway_id=?",
                (giveaway_id,),
            )
            await db.execute(
                "DELETE FROM giveaway_winners WHERE giveaway_id=?",
                (giveaway_id,),
            )
            await db.execute(
                "DELETE FROM giveaway_participants WHERE giveaway_id=?",
                (giveaway_id,),
            )
            await db.execute(
                "DELETE FROM giveaway_posts WHERE giveaway_id=?",
                (giveaway_id,),
            )
            await db.execute(
                "DELETE FROM giveaways WHERE id=?",
                (giveaway_id,),
            )
            await db.commit()
        return True

    async def record_traffic_sample(
        self,
        telegram_id: int,
        used_gb: float,
        *,
        day: str,
    ) -> None:
        """Keep one lightweight cumulative traffic sample per user/day."""
        try:
            current_day = datetime.fromisoformat(str(day)).date()
            used = max(0.0, float(used_gb or 0))
        except (TypeError, ValueError):
            return

        previous_day = (current_day - timedelta(days=1)).isoformat()
        cutoff = (current_day - timedelta(days=45)).isoformat()
        now = to_iso(utcnow())

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            row = await (
                await db.execute(
                    """
                    SELECT day, used_gb
                    FROM traffic_daily
                    WHERE telegram_id=?
                    ORDER BY day DESC
                    LIMIT 1
                    """,
                    (telegram_id,),
                )
            ).fetchone()

            # When collection starts (or resumes after a long gap), create a
            # zero-delta baseline for yesterday instead of attributing old
            # cumulative traffic to the current day.
            if row is None or str(row[0]) < previous_day:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO traffic_daily
                        (telegram_id, day, used_gb, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (telegram_id, previous_day, used, now),
                )

            await db.execute(
                """
                INSERT INTO traffic_daily (telegram_id, day, used_gb, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_id, day) DO UPDATE SET
                    used_gb=excluded.used_gb,
                    updated_at=excluded.updated_at
                """,
                (telegram_id, current_day.isoformat(), used, now),
            )
            await db.execute(
                "DELETE FROM traffic_daily WHERE telegram_id=? AND day<?",
                (telegram_id, cutoff),
            )
            await db.commit()

    async def traffic_usage_history(
        self,
        telegram_id: int,
        *,
        end_day: str,
        days: int = 30,
    ) -> dict[str, Any]:
        """Return observed daily deltas and compact 1/7/30-day summaries."""
        days = max(1, min(int(days), 30))
        try:
            end = datetime.fromisoformat(str(end_day)).date()
        except (TypeError, ValueError):
            end = utcnow().date()
        start = end - timedelta(days=days - 1)
        baseline = start - timedelta(days=1)

        async with aiosqlite.connect(self.path, timeout=15.0) as db:
            rows = await (
                await db.execute(
                    """
                    SELECT day, used_gb
                    FROM traffic_daily
                    WHERE telegram_id=? AND day>=? AND day<=?
                    ORDER BY day
                    """,
                    (telegram_id, baseline.isoformat(), end.isoformat()),
                )
            ).fetchall()

        cumulative = {str(day): max(0.0, float(value or 0)) for day, value in rows}
        usage: dict[str, float] = {}
        previous: float | None = cumulative.get(baseline.isoformat())
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            current = cumulative.get(key)
            if current is None:
                usage[key] = 0.0
            elif previous is None:
                usage[key] = 0.0
                previous = current
            else:
                usage[key] = max(0.0, current - previous) if current >= previous else current
                previous = current
            cursor += timedelta(days=1)

        ordered = [
            {"date": key, "gb": round(value, 3)}
            for key, value in sorted(usage.items())
        ]
        values = [float(item["gb"]) for item in ordered]
        return {
            "today_gb": round(values[-1] if values else 0.0, 3),
            "week_gb": round(sum(values[-7:]), 3),
            "month_gb": round(sum(values[-30:]), 3),
            "days": ordered,
        }

    async def stats(self) -> tuple[int, int]:
        now = to_iso(utcnow())
        async with aiosqlite.connect(self.path, timeout=15.0) as db:
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
