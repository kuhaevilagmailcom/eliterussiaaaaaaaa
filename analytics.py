from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

import aiosqlite

from catalog import DEVICE_PRODUCT_CODE, PLANS
from db import Database, from_iso, to_iso, utcnow


async def load_business_analytics(db: Database) -> dict[str, Any]:
    """Read-only business dashboard data for Telegram admin screens."""
    now = utcnow()
    windows = {
        "day": now - timedelta(days=1),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
    }
    plan_codes = tuple(str(code) for code in PLANS)
    placeholders = ",".join("?" for _ in plan_codes)

    async with aiosqlite.connect(db.path, timeout=15.0) as conn:
        conn.row_factory = aiosqlite.Row

        total = int((await (await conn.execute("SELECT COUNT(*) FROM users")).fetchone())[0])
        active = int((await (await conn.execute(
            "SELECT COUNT(*) FROM users WHERE subscription_until IS NOT NULL AND subscription_until>?",
            (to_iso(now),),
        )).fetchone())[0])

        revenue: dict[str, dict[str, int]] = {}
        purchases: dict[str, dict[str, int]] = {}
        average: dict[str, float] = {}

        paid_events: list[dict[str, Any]] = []
        sbp_rows = await (await conn.execute(
            f"""
            SELECT COALESCE(target_telegram_id, telegram_id) AS target_id,
                   telegram_id AS buyer_id,
                   plan_code,
                   amount_rub AS amount,
                   'RUB' AS currency,
                   COALESCE(paid_at, created_at) AS paid_at
            FROM sbp_payments
            WHERE status='paid' AND plan_code IN ({placeholders})
            """,
            plan_codes,
        )).fetchall()
        star_rows = await (await conn.execute(
            f"""
            SELECT target_telegram_id AS target_id,
                   buyer_telegram_id AS buyer_id,
                   plan_code,
                   stars AS amount,
                   'XTR' AS currency,
                   created_at AS paid_at
            FROM star_payments
            WHERE plan_code IN ({placeholders})
            """,
            plan_codes,
        )).fetchall()

        for row in [*sbp_rows, *star_rows]:
            paid_events.append(dict(row))
        paid_events.sort(
            key=lambda item: from_iso(item.get("paid_at")) or now
        )

        seen_targets: set[int] = set()
        classified: list[dict[str, Any]] = []
        for item in paid_events:
            target = int(item["target_id"])
            item = dict(item)
            item["kind"] = "renewal" if target in seen_targets else "new"
            seen_targets.add(target)
            classified.append(item)

        for key, since in windows.items():
            rub = stars = new_count = renewal_count = 0
            rub_checks: list[int] = []
            star_checks: list[int] = []
            for item in classified:
                paid_at = from_iso(item.get("paid_at"))
                if not paid_at or paid_at < since:
                    continue
                amount = int(item.get("amount") or 0)
                if item["currency"] == "RUB":
                    rub += amount
                    rub_checks.append(amount)
                else:
                    stars += amount
                    star_checks.append(amount)
                if item["kind"] == "new":
                    new_count += 1
                else:
                    renewal_count += 1
            revenue[key] = {"rub": rub, "stars": stars}
            purchases[key] = {"new": new_count, "renewal": renewal_count}
            if key == "month":
                average = {
                    "rub": round(sum(rub_checks) / len(rub_checks), 1) if rub_checks else 0.0,
                    "stars": round(sum(star_checks) / len(star_checks), 1) if star_checks else 0.0,
                }

        by_target: dict[int, list] = defaultdict(list)
        for item in classified:
            dt = from_iso(item.get("paid_at"))
            if dt:
                by_target[int(item["target_id"])].append(dt)
        eligible = retained = 0
        for dates in by_target.values():
            dates.sort()
            first = dates[0]
            if first <= now - timedelta(days=30):
                eligible += 1
                if any(first < value <= first + timedelta(days=30) for value in dates[1:]):
                    retained += 1
        retention_30d = retained / eligible * 100.0 if eligible else 0.0

        expiring: dict[str, int] = {}
        for days in (1, 3, 7):
            expiring[str(days)] = int((await (await conn.execute(
                """
                SELECT COUNT(*) FROM users
                WHERE subscription_until>? AND subscription_until<=?
                """,
                (to_iso(now), to_iso(now + timedelta(days=days))),
            )).fetchone())[0])

        observed_usage: dict[str, int] = {}
        for label, days in (("day", 1), ("week", 7), ("month", 30)):
            observed_usage[label] = int((await (await conn.execute(
                """
                SELECT COUNT(DISTINCT telegram_id)
                FROM traffic_daily
                WHERE updated_at>=? AND used_gb>0
                """,
                (to_iso(now - timedelta(days=days)),),
            )).fetchone())[0])

        preference_rows = await (await conn.execute(
            """
            SELECT COALESCE(NULLIF(preferred_country,''), 'auto') AS country,
                   COUNT(*) AS count
            FROM users
            WHERE subscription_until IS NOT NULL AND subscription_until>?
            GROUP BY COALESCE(NULLIF(preferred_country,''), 'auto')
            ORDER BY count DESC, country
            """,
            (to_iso(now),),
        )).fetchall()
        country_preferences = [
            {"country": str(row["country"]), "count": int(row["count"])}
            for row in preference_rows
        ]

        source_rows = await (await conn.execute(
            """
            SELECT u.attribution_source AS source,
                   COUNT(*) AS arrived,
                   SUM(CASE WHEN
                       EXISTS (
                           SELECT 1 FROM sbp_payments s
                           WHERE s.telegram_id=u.telegram_id
                             AND s.status='paid'
                             AND s.plan_code!='device'
                       ) OR EXISTS (
                           SELECT 1 FROM star_payments sp
                           WHERE sp.buyer_telegram_id=u.telegram_id
                             AND sp.plan_code!='device'
                       ) THEN 1 ELSE 0 END) AS buyers
            FROM users u
            WHERE u.attribution_source IS NOT NULL
            GROUP BY u.attribution_source
            ORDER BY arrived DESC, source
            LIMIT 30
            """
        )).fetchall()
        sources = []
        for row in source_rows:
            arrived = int(row["arrived"] or 0)
            buyers = int(row["buyers"] or 0)
            sources.append({
                "source": str(row["source"]),
                "arrived": arrived,
                "buyers": buyers,
                "conversion": round(buyers / arrived * 100.0, 1) if arrived else 0.0,
            })

        promo_rows = await (await conn.execute(
            """
            SELECT p.id, p.code, p.type, p.value, p.active, p.max_uses,
                   p.used_count,
                   COUNT(DISTINCT pu.telegram_id) AS buyers
            FROM service_promo_codes p
            LEFT JOIN promo_uses pu ON pu.promo_id=p.id
            GROUP BY p.id
            ORDER BY p.id DESC
            LIMIT 30
            """
        )).fetchall()
        promos: list[dict[str, Any]] = []
        for row in promo_rows:
            promo_id = int(row["id"])
            sbp_attempts = int((await (await conn.execute(
                "SELECT COUNT(*) FROM sbp_payments WHERE promo_id=?",
                (promo_id,),
            )).fetchone())[0])
            intent_attempts = int((await (await conn.execute(
                "SELECT COUNT(*) FROM payment_intents WHERE promo_id=?",
                (promo_id,),
            )).fetchone())[0])
            attempts = sbp_attempts + intent_attempts
            uses = int(row["used_count"] or 0)
            promos.append({
                "id": promo_id,
                "code": str(row["code"]),
                "type": str(row["type"]),
                "value": int(row["value"]),
                "active": bool(row["active"]),
                "uses": uses,
                "buyers": int(row["buyers"] or 0),
                "attempts": attempts,
                "conversion": round(uses / attempts * 100.0, 1) if attempts else 0.0,
            })

        support_rows = await (await conn.execute(
            """
            SELECT COALESCE(NULLIF(server_code,''), 'unknown') AS server_code,
                   COUNT(*) AS count
            FROM support_tickets
            WHERE deleted_at IS NULL
            GROUP BY COALESCE(NULLIF(server_code,''), 'unknown')
            ORDER BY count DESC, server_code
            LIMIT 20
            """
        )).fetchall()
        support_servers = [
            {"server_code": str(row["server_code"]), "count": int(row["count"])}
            for row in support_rows
        ]

    return {
        "total_users": total,
        "active_subscriptions": active,
        "revenue": revenue,
        "purchases": purchases,
        "average_check_30d": average,
        "retention_30d": round(retention_30d, 1),
        "retention_sample": eligible,
        "expiring": expiring,
        "observed_usage": observed_usage,
        "country_preferences": country_preferences,
        "sources": sources,
        "promos": promos,
        "support_servers": support_servers,
    }
