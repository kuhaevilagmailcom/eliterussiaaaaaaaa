from __future__ import annotations

import html
from typing import Any


def metric_bar(value: float, maximum: float, width: int = 10) -> str:
    if maximum <= 0:
        return "░" * width
    filled = max(0, min(width, int(round(value / maximum * width))))
    return "█" * filled + "░" * (width - filled)


def render_business_analytics(data: dict[str, Any]) -> str:
    revenue = data["revenue"]
    max_rub = max(
        1,
        int(revenue["day"]["sbp_rub"]),
        int(revenue["week"]["sbp_rub"]),
        int(revenue["month"]["sbp_rub"]),
    )
    lines = ["📈 <b>Бизнес-аналитика MGN VPN</b>", "", "💰 <b>Выручка</b>"]
    for key, label in (("day", "24ч"), ("week", "7д"), ("month", "30д")):
        rub = int(revenue[key]["sbp_rub"])
        stars = int(revenue[key]["stars"])
        count = int(revenue[key]["purchases"])
        lines.append(
            f"<code>{label:>3} {metric_bar(rub, max_rub)} {rub:>5} ₽</code> "
            f"· ⭐ {stars} · {count} покуп."
        )

    total = int(data["new_purchases"]) + int(data["renewals"])
    new_share = int(data["new_purchases"]) / total * 100.0 if total else 0.0
    lines += [
        "",
        "🧾 <b>Продажи</b>",
        f"Новые: <b>{int(data['new_purchases'])}</b> · продления: <b>{int(data['renewals'])}</b>",
        f"<code>{metric_bar(new_share, 100)} {new_share:.0f}% новых</code>",
        f"Средний чек: <b>{float(data['avg_check_rub']):.0f} ₽</b> · "
        f"<b>{float(data['avg_check_stars']):.0f} ⭐</b>",
        f"Retention: <b>{float(data['retention']):.1f}%</b>",
        "",
        "⏳ <b>Истекают подписки</b>",
        f"1 день: <b>{int(data['expiring']['1'])}</b> · "
        f"3 дня: <b>{int(data['expiring']['3'])}</b> · "
        f"7 дней: <b>{int(data['expiring']['7'])}</b>",
        f"Активно пользовались VPN за 7 дней: <b>{int(data['active_vpn_users_7d'])}</b>",
        "",
        "📣 <b>Источники</b>",
    ]

    if data["sources"]:
        for row in data["sources"]:
            source = str(row["source"])
            label = {"anonchat_mgn": "Anon MGN", "pozor_mgn": "Позор MGN"}.get(source, source)
            lines.append(
                f"{html.escape(label)}: <b>{int(row['arrived'])}</b> → "
                f"<b>{int(row['buyers'])}</b> · <b>{float(row['conversion']):.1f}%</b>"
            )
    else:
        lines.append("Пока нет данных.")

    lines += ["", "🎟 <b>Промокоды</b>"]
    if data["promos"]:
        for row in data["promos"][:8]:
            lines.append(
                f"<code>{html.escape(str(row['code']))}</code> · "
                f"<b>{int(row['used_count'] or 0)}</b> использ. · "
                f"<b>{int(row['unique_users'] or 0)}</b> чел."
            )
    else:
        lines.append("Пока нет использований.")

    country_names = {
        "nl": "🇳🇱 NL", "de": "🇩🇪 DE", "fi": "🇫🇮 FI", "pl": "🇵🇱 PL",
        "pk": "🇵🇰 PK", "us": "🇺🇸 US", "us2": "🇺🇸 US2", "lt": "🇱🇹 LT",
    }
    lines += ["", "🌍 <b>Предпочтения по странам</b>"]
    prefs = data["preferred_countries"]
    if prefs:
        top = max(1, max(int(row["users"]) for row in prefs))
        for row in prefs[:8]:
            count = int(row["users"])
            label = country_names.get(str(row["country"]), str(row["country"]).upper())
            lines.append(f"<code>{label:7} {metric_bar(count, top, 8)} {count}</code>")
    else:
        lines.append("Все используют авто-режим.")

    mentions = data.get("support_server_mentions") or {}
    lines += [
        "",
        "🆘 <b>Упоминания серверов в поддержке</b>",
        f"NL {int(mentions.get('nl') or 0)} · DE {int(mentions.get('de') or 0)} · "
        f"FI {int(mentions.get('fi') or 0)} · PL {int(mentions.get('pl') or 0)} · "
        f"PK {int(mentions.get('pk') or 0)} · US {int(mentions.get('us') or 0)}",
    ]
    return "\n".join(lines)
