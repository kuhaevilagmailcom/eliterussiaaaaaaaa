from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import hmac
import json
import logging
import re
import secrets
import time
from html import escape
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote
from uuid import uuid4

from aiohttp import web
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from admin_notifications import notify_purchase_admins
from catalog import (
    EXTRA_DEVICE_PRICE_RUB,
    MAX_DEVICES,
    STAR_RATE_RUB,
    STAR_RATE_XTR,
    PLANS,
    plan_price_rub,
    plan_price_stars,
    plan_total_price_rub,
    plan_savings_rub,
    rub_to_stars,
)
from db import Database, from_iso, utcnow
from payments import RollyPayError, create_payment, get_payment
from legal import (
    AGREEMENT_SECTIONS,
    AGREEMENT_UPDATED,
    PRIVACY_UPDATED,
    agreement_html,
    privacy_html,
)
from vpn import BASE_MGN_SERVERS, VpnProvider, VpnState, prettify_subscription_payload
from vpn_clients import client_registry, get_client


logger = logging.getLogger(__name__)


def _json_error(status: int, message: str) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        403: web.HTTPForbidden,
        404: web.HTTPNotFound,
        409: web.HTTPConflict,
        429: web.HTTPTooManyRequests,
        503: web.HTTPServiceUnavailable,
        500: web.HTTPInternalServerError,
    }.get(status, web.HTTPBadRequest)
    return cls(
        text=json.dumps({"message": message}, ensure_ascii=False),
        content_type="application/json",
    )


def validate_init_data(raw: str, bot_token: str, max_age: int = 3600) -> dict:
    if not isinstance(raw, str) or not raw or len(raw) > 16384:
        raise _json_error(401, "Открой MGN VPN внутри Telegram")

    parsed = parse_qsl(raw, keep_blank_values=True)
    pairs = dict(parsed)
    if len(pairs) != len(parsed):
        raise _json_error(401, "Некорректная сессия Telegram")
    received_hash = pairs.pop("hash", "")
    if not re.fullmatch(r"[0-9a-f]{64}", received_hash):
        raise _json_error(401, "Telegram не передал подпись")

    data_check = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise _json_error(401, "Неверная подпись Telegram")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise _json_error(401, "Некорректная сессия Telegram") from exc

    if not auth_date or int(time.time()) - auth_date > max(60, max_age) or auth_date > int(time.time()) + 30:
        raise _json_error(401, "Сессия устарела. Открой Mini App заново")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise _json_error(401, "Не удалось прочитать Telegram-профиль") from exc

    if not isinstance(user, dict) or type(user.get("id")) is not int or not 0 < user["id"] < 2**63:
        raise _json_error(401, "Telegram-пользователь не найден")
    return user


def _active(user: dict) -> bool:
    until = from_iso(user.get("subscription_until"))
    return bool(until and until > utcnow())


def _remaining_seconds(user: dict) -> int:
    if user.get("plan_name") == "Навсегда":
        return 36500 * 24 * 60 * 60
    until = from_iso(user.get("subscription_until"))
    if not until:
        return 0
    return max(0, int((until - utcnow()).total_seconds()))


class MiniAppServer:
    def __init__(
        self,
        bot,
        config,
        db: Database,
        provider: VpnProvider,
        *,
        web_dir: str | Path | None = None,
    ) -> None:
        self.bot = bot
        self.config = config
        self.db = db
        self.provider = provider
        self.web_dir = Path(web_dir or "miniapp/web").resolve()
        self.runner: web.AppRunner | None = None
        self.site: web.TCPSite | None = None
        self._bot_username = ""
        self._last_reconcile: dict[int, float] = {}
        self._reconcile_tasks: dict[int, asyncio.Task] = {}
        self._subscription_refresh_tasks: dict[int, asyncio.Task] = {}
        self._subscription_refresh_last: dict[int, float] = {}
        self._h1_semaphore = asyncio.Semaphore(4)
        self._subscription_cache: dict[str, dict] = {}
        self._subscription_cache_invalidated: set[str] = set()
        self._rate_events: dict[str, list[float]] = {}
        self._last_rate_cleanup = 0.0
        self._subscription_cache_dir = Path(self.config.db_path).with_name(
            "subscription_cache"
        )

    def _subscription_cache_path(self, token: str) -> Path:
        # Version the on-disk cache so a deployment that fixes subscription
        # composition never keeps serving an older NL-only payload.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self._subscription_cache_dir / f"v9-{digest}.json"

    async def invalidate_subscription_cache(self, token: str) -> None:
        """Drop every cached form of a user's subscription after H1 sync."""
        token = str(token or "").strip()
        if not token:
            return
        self._subscription_cache.pop(token, None)
        self._subscription_cache_invalidated.add(token)
        await asyncio.to_thread(
            self._subscription_cache_path(token).unlink,
            missing_ok=True,
        )

    def _read_persistent_subscription_cache(self, token: str) -> dict | None:
        path = self._subscription_cache_path(token)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            body = base64.b64decode(str(raw.get("body_b64") or ""), validate=True)
            headers = raw.get("headers")
            created_at = float(raw.get("created_at") or 0)
            if not body or not isinstance(headers, dict) or created_at <= 0:
                return None
            return {
                "created_at": created_at,
                "body": body,
                "headers": {str(k): str(v) for k, v in headers.items()},
            }
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("Could not read persistent subscription cache: %s", exc)
            return None

    def _write_persistent_subscription_cache(
        self,
        token: str,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        try:
            self._subscription_cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._subscription_cache_dir.chmod(0o700)
            except OSError:
                pass
            path = self._subscription_cache_path(token)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "created_at": time.time(),
                        "body_b64": base64.b64encode(body).decode("ascii"),
                        "headers": headers,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            tmp.replace(path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            logger.exception("Could not persist subscription cache")

    def _client_identity(self, request: web.Request) -> str:
        """Use forwarded identity only when the immediate peer is trusted."""
        remote = str(request.remote or "").strip()
        if remote not in self.config.trusted_proxy_ips:
            return remote or "unknown"
        forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            return remote or "unknown"

    def _rate_limit(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
    ) -> None:
        now = time.monotonic()
        if now - self._last_rate_cleanup > 60:
            self._rate_events = {k: v for k, v in self._rate_events.items() if v and v[-1] > now - 600}
            self._last_rate_cleanup = now
        window_start = now - float(window_seconds)
        events = [
            value
            for value in self._rate_events.get(key, [])
            if value >= window_start
        ]
        if len(events) >= int(limit):
            self._rate_events[key] = events
            raise _json_error(429, "Слишком много запросов. Попробуйте немного позже")
        events.append(now)
        self._rate_events[key] = events

    def _telegram_user(self, request: web.Request) -> dict:
        raw = request.headers.get("X-Telegram-Init-Data", "")
        return validate_init_data(
            raw,
            self.config.bot_token,
            self.config.miniapp_initdata_max_age,
        )

    async def _auth(self, request: web.Request) -> tuple[int, dict, dict]:
        tg_user = self._telegram_user(request)
        user_id = int(tg_user["id"])
        self._rate_limit(
            f"miniapp:{user_id}",
            limit=120,
            window_seconds=60.0,
        )
        if request.method != "GET":
            self._rate_limit(f"mutation:{user_id}:{request.path}", limit=8, window_seconds=60)
        elif "/payment/" in request.path:
            self._rate_limit(f"payment-check:{user_id}", limit=20, window_seconds=60)
        row = await self.db.ensure_user(
            user_id,
            tg_user.get("username"),
            tg_user.get("first_name") or "Пользователь",
        )
        return user_id, tg_user, row

    async def _admin_role(self, user_id: int) -> str | None:
        if int(user_id) in set(self.config.admin_ids):
            return "owner"
        role = await self.db.get_admin_role(int(user_id))
        return role if role in {"full", "limited"} else None

    async def _admin_auth(self, request: web.Request) -> tuple[int, dict, dict, str]:
        user_id, tg_user, row = await self._auth(request)
        role = await self._admin_role(user_id)
        if not role:
            logger.warning("Mini App admin access denied for user %s", user_id)
            raise _json_error(403, "Доступ запрещён")
        self._rate_limit(f"miniapp-admin:{user_id}", limit=60, window_seconds=60.0)
        return user_id, tg_user, row, role

    async def _username(self) -> str:
        if not self._bot_username:
            me = await self.bot.get_me()
            self._bot_username = me.username or "mgnvpn_bot"
        return self._bot_username

    async def _sync_bot_subscription_menu(self, user: dict) -> None:
        message_id = user.get("last_menu_message_id")
        if not message_id:
            return

        until = from_iso(user.get("subscription_until"))
        until_text = "—"
        if until:
            until_text = until.astimezone(self.config.display_tz).strftime("%d.%m.%Y %H:%M")

        active = _active(user)
        caption = (
            "🔐 <b>MGN VPN</b>\n\n"
            f"Статус: <b>{'Активна' if active else 'Не активна'}</b>\n"
            f"Тариф: <b>{user.get('plan_name') or '—'}</b>\n"
            f"До: <b>{until_text}</b>\n"
            f"Устройства: <b>до {int(user.get('max_devices') or 1)}</b>"
        )
        try:
            await self.bot.edit_message_caption(
                chat_id=int(user["telegram_id"]),
                message_id=int(message_id),
                caption=caption,
            )
        except Exception as exc:
            logger.debug(
                "Could not sync bot menu caption for %s: %s",
                user.get("telegram_id"),
                exc,
            )

    def _fallback_state(self, user: dict) -> VpnState:
        return VpnState(
            subscription_url="",
            server=self.config.vpn_server_name,
            traffic_used_gb=0.0,
            traffic_limit_gb=0.0,
            devices=[],
        )

    async def _reconcile_h1(self, user: dict) -> None:
        user_id = int(user["telegram_id"])
        try:
            async with self._h1_semaphore:
                await asyncio.wait_for(self.provider.provision(user), 45.0)
            self._last_reconcile[user_id] = time.monotonic()
        except Exception as exc:
            logger.warning(
                "Mini App H1 background reconciliation failed for %s: %s",
                user_id,
                str(exc).strip() or type(exc).__name__,
            )
        finally:
            self._reconcile_tasks.pop(user_id, None)

    def _schedule_h1_reconcile(self, user: dict) -> None:
        user_id = int(user["telegram_id"])
        current = self._reconcile_tasks.get(user_id)
        if current and not current.done():
            return
        self._reconcile_tasks[user_id] = asyncio.create_task(
            self._reconcile_h1(dict(user))
        )

    async def _refresh_subscription_federation(
        self,
        user: dict,
        token: str,
    ) -> None:
        user_id = int(user["telegram_id"])
        try:
            async with self._h1_semaphore:
                await asyncio.wait_for(self.provider.provision(user), 45.0)
            # Не удаляем последний рабочий payload после federation refresh.
            # Следующий обычный refresh сам соберёт новую версию; старый полный
            # список остаётся страховкой, если одна из стран временно недоступна.
            logger.info(
                "H1Cloud background federation refresh completed for %s",
                user_id,
            )
        except Exception as exc:
            logger.warning(
                "H1Cloud background federation refresh failed for %s: %s",
                user_id,
                str(exc).strip() or type(exc).__name__,
            )
        finally:
            self._subscription_refresh_tasks.pop(user_id, None)

    def _schedule_subscription_federation_refresh(
        self,
        user: dict,
        token: str,
    ) -> None:
        if getattr(self.provider, "mode_name", "") != "h1cloud":
            return
        user_id = int(user["telegram_id"])
        now = time.monotonic()
        if now - self._subscription_refresh_last.get(user_id, 0.0) < 60.0:
            return
        current = self._subscription_refresh_tasks.get(user_id)
        if current and not current.done():
            return
        self._subscription_refresh_last[user_id] = now
        self._subscription_refresh_tasks[user_id] = asyncio.create_task(
            self._refresh_subscription_federation(dict(user), token)
        )

    async def _load_state(self, user: dict) -> tuple[VpnState, bool]:
        if not _active(user):
            return self._fallback_state(user), True
        if not getattr(self.provider, "service_ready", True):
            return self._fallback_state(user), False

        user_id = int(user["telegram_id"])
        is_h1 = getattr(self.provider, "mode_name", "") == "h1cloud"

        # Fast path: the main panel already has the canonical client/sub_url.
        # Never make opening the Mini App wait for every remote country.
        try:
            state = await asyncio.wait_for(
                self.provider.get_state(user),
                2.5,
            )
            if (
                is_h1
                and time.monotonic() - self._last_reconcile.get(user_id, 0.0)
                >= 60.0
            ):
                self._schedule_h1_reconcile(user)
            return state, True
        except Exception as state_exc:
            if not is_h1:
                logger.warning(
                    "Mini App VPN state read failed for %s: %s",
                    user_id,
                    str(state_exc).strip() or type(state_exc).__name__,
                )

        # Provisioning can take tens of seconds when a linked VPN node is
        # unavailable. Keep Mini App startup bounded and repair H1 state in the
        # background; the stable public subscription URL remains available.
        if is_h1:
            self._schedule_h1_reconcile(user)
        else:
            logger.warning("Mini App VPN state unavailable for %s", user_id)
        return self._fallback_state(user), False


    def _external_base_url(self, request: web.Request) -> str:
        return "https://mgnvpn.ru"

    async def _json_body(self, request: web.Request) -> dict:
        try:
            data = await request.json()
        except (ValueError, UnicodeError):
            raise _json_error(400, "Некорректный JSON")
        if not isinstance(data, dict):
            raise _json_error(400, "Ожидается объект JSON")
        for key, limit in (("plan_code", 16), ("code", 64), ("promo_code", 64), ("message", 3000)):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > limit):
                raise _json_error(400, "Некорректное поле запроса")
        return data

    def _public_subscription_url(
        self,
        user: dict,
        state: VpnState | None = None,
        *,
        request: web.Request | None = None,
    ) -> str:
        base_url = str(self.config.vpn_sub_base_url or "").strip().rstrip("/")
        if (
            getattr(self.provider, "mode_name", "") == "h1cloud"
            and base_url
            and user.get("sub_token")
            and _active(user)
        ):
            token = quote(str(user["sub_token"]), safe="")
            return f"{base_url}/{token}"
        return (state.subscription_url if state else "") or ""


    async def _subscription_profile_headers(
        self,
        user: dict,
        upstream_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Build stable Happ metadata for the public MGN subscription."""

        normalized = {
            str(key).strip().lower(): str(value).strip()
            for key, value in (upstream_headers or {}).items()
            if str(value).strip()
        }

        title = base64.b64encode("MGN VPN".encode("utf-8")).decode("ascii")
        try:
            username = (await self._username()).lstrip("@")
        except Exception as exc:
            logger.warning(
                "Could not resolve bot username for subscription metadata: %s",
                type(exc).__name__,
            )
            username = "mgnvpn_bot"

        renew_url = f"https://t.me/{username}?start=renew"
        support_url = f"https://t.me/{username}?start=support"
        announce_text = (
            "Если VPN не работает — нажмите 🔄. "
            f"Поддержка и продление подписки — в боте @{username}."
        )
        announce = base64.b64encode(announce_text.encode("utf-8")).decode("ascii")

        userinfo = normalized.get("subscription-userinfo", "")
        until = from_iso(user.get("subscription_until"))
        expire = int(until.timestamp()) if until else 0
        try:
            limit_gb = max(0.0, float(user.get("traffic_limit_gb") or 0))
        except (TypeError, ValueError):
            limit_gb = 0.0
        total_bytes = int(limit_gb * (1024 ** 3))

        # Traffic counters may come from H1, but expiry/limit are authoritative
        # in our DB. Never let cached/upstream Happ metadata keep an old expiry.
        if userinfo:
            if re.search(r"(?:^|;)\s*expire=\d+", userinfo, re.I):
                userinfo = re.sub(
                    r"((?:^|;)\s*expire=)\d+",
                    lambda match: f"{match.group(1)}{max(0, expire)}",
                    userinfo,
                    flags=re.I,
                )
            else:
                userinfo = userinfo.rstrip(" ;") + f"; expire={max(0, expire)}"

            if re.search(r"(?:^|;)\s*total=\d+", userinfo, re.I):
                userinfo = re.sub(
                    r"((?:^|;)\s*total=)\d+",
                    lambda match: f"{match.group(1)}{total_bytes}",
                    userinfo,
                    flags=re.I,
                )
            else:
                userinfo = userinfo.rstrip(" ;") + f"; total={total_bytes}"
        else:
            userinfo = (
                f"upload=0; download=0; total={total_bytes}; expire={max(0, expire)}"
            )

        headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "Cache-Control": "private, no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="MGN-VPN.txt"',
            "Profile-Title": f"base64:{title}",
            "Profile-Update-Interval": "1",
            "Subscription-Userinfo": userinfo,
            "Support-Url": support_url,
            "Profile-Web-Page-Url": "https://mgnvpn.ru",
            "Announce": f"base64:{announce}",
            # Happ advanced expiry card. With Provider ID configured this
            # shows its localized "Renew" action during the last 3 days.
            "Sub-Expire": "1",
            "Notification-Subs-Expire": "1",
            "Sub-Expire-Button-Link": renew_url,
        }
        provider_id = str(getattr(self.config, "happ_provider_id", "") or "").strip()
        if provider_id:
            headers["Providerid"] = provider_id
            # Let Happ measure delay on the user's own network as well. The
            # backend already removes unreachable endpoints before this.
            headers["Subscription-Ping-Onopen-Enabled"] = "1"
        return headers


    async def _activate_paid(self, buyer_id: int, target_id: int, code: str, event_key: str) -> None:
        target = await self.db.get_user(target_id)
        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(target), 7.0)
            except Exception as exc:
                logger.warning("Mini App provisioning deferred for %s: %s", target_id, exc)
        await self._sync_bot_subscription_menu(target)

    async def landing(self, request: web.Request) -> web.StreamResponse:
        index = self.web_dir / "site" / "index.html"
        if not index.exists():
            raise web.HTTPNotFound(text="MGN VPN site files are missing")
        html = await asyncio.to_thread(index.read_text, encoding="utf-8")
        for code in PLANS:
            html = html.replace(f'<strong data-plan-price="{code}"></strong>',
                                f'<strong data-plan-price="{code}">{plan_price_rub(self.config, code)} ₽</strong>')
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "public, max-age=60"})

    async def agreement(self, request: web.Request) -> web.StreamResponse:
        page = self.web_dir / "site" / "agreement.html"
        if not page.exists():
            raise web.HTTPNotFound(text="MGN VPN agreement is missing")
        html = await asyncio.to_thread(page.read_text, encoding="utf-8")
        html = html.replace("{{AGREEMENT_HTML}}", agreement_html())
        html = html.replace("{{AGREEMENT_UPDATED}}", AGREEMENT_UPDATED)
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "public, max-age=60"})

    async def privacy(self, request: web.Request) -> web.StreamResponse:
        page = self.web_dir / "site" / "privacy.html"
        if not page.exists():
            raise web.HTTPNotFound(text="MGN VPN privacy policy is missing")
        html = await asyncio.to_thread(page.read_text, encoding="utf-8")
        html = html.replace("{{PRIVACY_HTML}}", privacy_html())
        html = html.replace("{{PRIVACY_UPDATED}}", PRIVACY_UPDATED)
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "public, max-age=60"})

    async def index(self, request: web.Request) -> web.StreamResponse:
        index = self.web_dir / "index.html"
        if not index.exists():
            raise web.HTTPNotFound(text="Mini App files are missing")
        return web.FileResponse(index, headers={"Cache-Control": "no-store"})

    async def health(self, request: web.Request) -> web.Response:
        """Process liveness only. Do not restart a healthy bot during a provider outage."""
        return web.json_response(
            {
                "ok": True,
                "service": "MGN VPN Mini App",
                "build": "mgn-vpn",
                "provider_mode": str(getattr(self.provider, "mode_name", "vpn")),
                "provider_configured": bool(getattr(self.provider, "service_ready", True)),
                "readiness_url": "/api/miniapp/ready",
            }
        )

    async def readiness(self, request: web.Request) -> web.Response:
        """Real dependency readiness for diagnostics; Docker liveness does not use it."""
        db_ok = await self.db.ping()

        provider_ok = False
        provider_detail: dict[str, Any] = {}
        if getattr(self.provider, "service_ready", True):
            try:
                raw = await asyncio.wait_for(self.provider.health(), timeout=2.5)
                provider_detail = dict(raw or {}) if isinstance(raw, dict) else {}
                if "ok" in provider_detail:
                    provider_ok = bool(provider_detail.get("ok"))
                else:
                    status = str(provider_detail.get("status") or "").strip().lower()
                    provider_ok = status in {"ok", "healthy", "ready", "up"} or not status
            except Exception as exc:
                provider_detail = {"error": type(exc).__name__}
                provider_ok = False

        payload = {
            "ok": bool(db_ok and provider_ok),
            "database_ready": bool(db_ok),
            "vpn_ready": bool(provider_ok),
            "provider_mode": str(getattr(self.provider, "mode_name", "vpn")),
            "sbp_configured": bool(self.config.rollypay_enabled),
        }
        if provider_detail:
            payload["provider"] = provider_detail
        return web.json_response(payload, status=200 if payload["ok"] else 503)

    async def public_catalog(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "plans": [
                    {
                        "code": code,
                        "name": str(plan["name"]),
                        "days": int(plan["days"]),
                        "price_rub": plan_price_rub(self.config, code),
                        "price_stars": plan_price_stars(self.config, code),
                        "savings_rub": plan_savings_rub(code),
                        "popular": bool(plan.get("popular")),
                        "devices": int(plan.get("devices") or 1),
                    }
                    for code, plan in PLANS.items()
                ],
                "base_devices": 1,
                "max_devices": MAX_DEVICES,
                "extra_device_price_rub": EXTRA_DEVICE_PRICE_RUB,
                "extra_device_price_stars": rub_to_stars(EXTRA_DEVICE_PRICE_RUB),
            },
            headers={"Cache-Control": "public, max-age=300"},
        )


    async def public_payment_create(self, request: web.Request) -> web.Response:
        self._rate_limit(
            f"public-payment-create:{self._client_identity(request)}",
            limit=8,
            window_seconds=60,
        )
        if not self.config.rollypay_enabled:
            raise _json_error(503, "СБП пока не настроена")

        data = await self._json_body(request)
        raw_user_id = str(data.get("user_id") or "").strip()
        if not raw_user_id.isdigit():
            raise _json_error(400, "Введите корректный Telegram ID")
        user_id = int(raw_user_id)
        if not 0 < user_id < 2**63:
            raise _json_error(400, "Введите корректный Telegram ID")

        try:
            await self.db.get_user(user_id)
        except KeyError:
            raise _json_error(404, "Пользователь с таким ID не найден")

        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        amount = plan_price_rub(self.config, code)
        if amount <= 0:
            raise _json_error(400, "Некорректная стоимость тарифа")

        order_id = f"vpn-web-{user_id}-{uuid4().hex[:12]}"
        local_id = await self.db.create_sbp_order(
            order_id=order_id,
            telegram_id=user_id,
            target_telegram_id=user_id,
            plan_code=code,
            amount_rub=amount,
            original_amount_rub=amount,
        )
        try:
            payment = await create_payment(
                self.config,
                order_id=order_id,
                amount=Decimal(amount),
                description=f"MGN VPN {plan['name']}",
                user_id=user_id,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await self.db.attach_sbp_provider_payment(local_id, payment_id, pay_url)
        except (RollyPayError, KeyError, ValueError) as exc:
            await self.db.set_sbp_status(local_id, "create_failed")
            logger.warning("Public SBP create failed: %s", type(exc).__name__)
            raise _json_error(503, "Не удалось создать платёж")

        return web.json_response(
            {
                "payment_id": payment_id,
                "pay_url": pay_url,
                "amount_rub": amount,
                "plan_name": str(plan["name"]),
            }
        )

    async def public_payment_check(self, request: web.Request) -> web.Response:
        self._rate_limit(
            f"public-payment-check:{self._client_identity(request)}",
            limit=60,
            window_seconds=60,
        )
        payment_id = str(request.match_info.get("payment_id") or "").strip()
        if not payment_id or len(payment_id) > 200:
            raise _json_error(400, "Некорректный платёж")

        local = await self.db.get_sbp_payment(payment_id)
        if not local:
            raise _json_error(404, "Платёж не найден")

        if str(local.get("status") or "").lower() == "paid":
            return web.json_response({"status": "paid"})

        try:
            remote = await get_payment(self.config, payment_id)
        except RollyPayError:
            raise _json_error(503, "Не удалось проверить платёж")

        status = str(remote.get("status") or "").lower()
        try:
            remote_amount = Decimal(str(remote.get("amount")))
        except (InvalidOperation, ValueError):
            remote_amount = Decimal("-1")

        matches = (
            str(remote.get("payment_id") or "") == payment_id
            and str(remote.get("order_id") or "") == str(local["order_id"])
            and str(
                remote.get("currency") or remote.get("payment_currency") or ""
            ).upper() == "RUB"
            and remote_amount.is_finite()
            and remote_amount == Decimal(int(local["amount_rub"]))
        )
        if not matches:
            raise _json_error(409, "Данные платежа не совпали")

        if status == "paid":
            try:
                fresh = await self.db.settle_sbp_payment(payment_id)
            except ValueError:
                raise _json_error(
                    409,
                    "Оплата получена и требует проверки поддержки",
                )
            target_id = int(
                local.get("target_telegram_id") or local["telegram_id"]
            )
            if fresh:
                updated = await self.db.get_user(target_id)
                await notify_purchase_admins(
                    self.bot,
                    self.config,
                    self.db,
                    buyer_id=int(local["telegram_id"]),
                    target_id=target_id,
                    code=str(local["plan_code"]),
                    method="СБП",
                    amount=int(local["amount_rub"]),
                    payment_id=payment_id,
                )
                if getattr(self.provider, "service_ready", True):
                    try:
                        await asyncio.wait_for(
                            self.provider.provision(updated),
                            timeout=8.0,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Public payment provision deferred for %s: %s",
                            target_id,
                            type(exc).__name__,
                        )
                await self._sync_bot_subscription_menu(updated)
            return web.json_response({"status": "paid"})

        await self.db.set_sbp_status(payment_id, status or "processing")
        return web.json_response({"status": status or "processing"})

    async def subscription(self, request: web.Request) -> web.Response:
        token = str(request.match_info.get("token") or "").strip()
        token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]
        self._rate_limit(
            f"subscription:{token_key}:{self._client_identity(request)}",
            limit=120,
            window_seconds=60,
        )
        user = await self.db.get_user_by_sub_token(token)
        if user is None:
            raise web.HTTPNotFound(text="Subscription not found")
        if not _active(user):
            raise web.HTTPForbidden(text="Subscription expired")

        cached = self._subscription_cache.get(token)
        now = time.monotonic()
        if cached and now - float(cached["created"]) <= 30.0:
            self._schedule_subscription_federation_refresh(user, token)
            headers = await self._subscription_profile_headers(
                user,
                dict(cached.get("headers") or {}),
            )
            return web.Response(
                body=cached["body"],
                headers=headers,
            )

        persistent_cached = await asyncio.to_thread(self._read_persistent_subscription_cache, token)
        if (
            persistent_cached
            and token not in self._subscription_cache_invalidated
            and time.time() - float(persistent_cached["created_at"]) <= 60.0
        ):
            # Serve first, refresh federation in the background. Starting a
            # provision task before a cold fetch races the same H1 client and
            # was a common source of first-import 503 responses.
            self._schedule_subscription_federation_refresh(user, token)
            headers = await self._subscription_profile_headers(
                user,
                dict(persistent_cached.get("headers") or {}),
            )
            return web.Response(
                body=persistent_cached["body"],
                headers=headers,
            )

        async def load_payload() -> tuple[bytes, dict[str, str], int]:
            body, upstream_headers = await self.provider.fetch_subscription(user)
            rendered, count = prettify_subscription_payload(body)
            return rendered, upstream_headers, count

        try:
            # fetch_subscription performs a bounded main-panel self-heal. A
            # second full federation provision made clients wait 30+ seconds.
            body, upstream_headers, count = await asyncio.wait_for(
                load_payload(),
                14.0,
            )
            if count < 1:
                raise RuntimeError("H1Cloud subscription contains no VLESS nodes")

            # Никогда не заменяем более полный последний subscription урезанным
            # только потому, что одна H1-нода сейчас недоступна/медленная.
            richer_cached = None
            richer_count = count
            candidates: list[tuple[dict, float]] = []
            if cached:
                candidates.append(
                    (cached, max(0.0, now - float(cached.get("created") or 0.0)))
                )
            if persistent_cached:
                candidates.append(
                    (
                        persistent_cached,
                        max(
                            0.0,
                            time.time()
                            - float(persistent_cached.get("created_at") or 0.0),
                        ),
                    )
                )

            # A richer snapshot is only a short outage cushion. After 10 min a
            # smaller fresh subscription wins, allowing intentionally removed
            # servers to disappear instead of living in cache forever.
            for candidate, candidate_age in candidates:
                if candidate_age > 600.0:
                    continue
                candidate_body = candidate.get("body")
                if not isinstance(candidate_body, (bytes, bytearray)) or not candidate_body:
                    continue
                try:
                    _normalized, candidate_count = prettify_subscription_payload(
                        bytes(candidate_body)
                    )
                except Exception:
                    continue
                if candidate_count > richer_count:
                    richer_count = candidate_count
                    richer_cached = candidate

            if richer_cached is not None:
                logger.warning(
                    "MGN subscription refresh for %s returned only %s node(s); "
                    "preserving richer cached payload with %s node(s) for a bounded grace period",
                    user["telegram_id"],
                    count,
                    richer_count,
                )
                # Body can be the richer cached snapshot, but Happ metadata must
                # always be rebuilt from the current DB/upstream response.
                headers = await self._subscription_profile_headers(
                    user,
                    upstream_headers,
                )
                self._schedule_subscription_federation_refresh(user, token)
                return web.Response(
                    body=bytes(richer_cached["body"]),
                    headers=headers,
                )

            headers = await self._subscription_profile_headers(
                user,
                upstream_headers,
            )

            userinfo = str(headers.get("Subscription-Userinfo") or "")
            upload_match = re.search(r"(?:^|;)\s*upload=(\d+)", userinfo, re.I)
            download_match = re.search(r"(?:^|;)\s*download=(\d+)", userinfo, re.I)
            if upload_match or download_match:
                used_bytes = int(upload_match.group(1)) if upload_match else 0
                used_bytes += int(download_match.group(1)) if download_match else 0
                local_day = utcnow().astimezone(self.config.display_tz).date().isoformat()
                await self.db.record_traffic_sample(
                    int(user["telegram_id"]),
                    used_bytes / float(1024 ** 3),
                    day=local_day,
                )

            if len(self._subscription_cache) >= 512:
                self._subscription_cache.pop(next(iter(self._subscription_cache)))
            self._subscription_cache[token] = {
                "created": now,
                "body": body,
                "headers": headers,
            }
            await asyncio.to_thread(self._write_persistent_subscription_cache, token, body, headers)
            self._subscription_cache_invalidated.discard(token)
            logger.info(
                "MGN subscription served for %s with %s node(s)",
                user["telegram_id"],
                count,
            )
            self._schedule_subscription_federation_refresh(user, token)
            return web.Response(body=body, headers=headers)
        except Exception as exc:
            # Existing configurations remain useful during a short H1 outage.
            # Serve a recently cached copy rather than turning the profile empty.
            if cached and now - float(cached["created"]) <= 600.0:
                logger.warning(
                    "MGN subscription upstream unavailable for %s; serving stale memory cache: %s",
                    user["telegram_id"],
                    exc,
                )
                headers = await self._subscription_profile_headers(
                    user,
                    dict(cached.get("headers") or {}),
                )
                return web.Response(
                    body=cached["body"],
                    headers=headers,
                )
            if (
                persistent_cached
                and time.time() - float(persistent_cached["created_at"]) <= 86400.0
            ):
                logger.warning(
                    "MGN subscription upstream unavailable for %s; serving persistent cache: %s",
                    user["telegram_id"],
                    exc,
                )
                headers = await self._subscription_profile_headers(
                    user,
                    dict(persistent_cached.get("headers") or {}),
                )
                return web.Response(
                    body=persistent_cached["body"],
                    headers=headers,
                )
            logger.warning(
                "MGN subscription unavailable for user %s (%s)",
                user["telegram_id"],
                type(exc).__name__,
            )
            raise web.HTTPServiceUnavailable(
                text="MGN VPN subscription is temporarily unavailable"
            )

    async def client_redirect(self, request: web.Request) -> web.Response:
        client = get_client(str(request.match_info.get("client") or ""))
        token = str(request.match_info.get("token") or "").strip()
        user = await self.db.get_user_by_sub_token(token)
        if client is None or user is None or not _active(user):
            raise web.HTTPNotFound(text="Client link not found")
        base = str(self.config.vpn_sub_base_url or "").strip().rstrip("/")
        subscription_url = f"{base}/{quote(token, safe='')}"
        target = client.import_url(subscription_url)
        if not target:
            raise web.HTTPFound(client.download_url)
        nonce = secrets.token_urlsafe(24)
        safe_target = escape(target, quote=True)
        safe_download = escape(client.download_url, quote=True)
        safe_name = escape(client.name)
        return web.Response(
            text=f"""<!doctype html>
<html lang=\"ru\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>MGN VPN — {safe_name}</title></head>
<body style=\"margin:0;background:#09090b;color:#fff;font:16px system-ui;display:grid;place-items:center;min-height:100vh;text-align:center\">
<main style=\"max-width:420px;padding:28px\"><h1>Открываем {safe_name}</h1>
<p style=\"color:#a1a1aa\">Подписка MGN VPN будет добавлена автоматически.</p>
<a href=\"{safe_target}\" style=\"display:block;padding:16px;border-radius:14px;background:#ff3dbb;color:#fff;text-decoration:none;font-weight:700\">Открыть {safe_name}</a>
<a href=\"{safe_download}\" style=\"display:block;margin-top:18px;color:#a1a1aa\">Установить приложение</a></main>
<script nonce="{nonce}">location.href={json.dumps(target)};</script></body></html>""",
            content_type="text/html",
            headers={"Cache-Control": "private, no-store", "Content-Security-Policy":
                     f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"},
        )

    async def me(self, request: web.Request) -> web.Response:
        uid, tg_user, row = await self._auth(request)
        admin_role = await self._admin_role(uid)
        state, vpn_ok = await self._load_state(row)
        local_day = utcnow().astimezone(self.config.display_tz).date().isoformat()
        if _active(row) and vpn_ok:
            await self.db.record_traffic_sample(
                uid,
                float(state.traffic_used_gb or 0),
                day=local_day,
            )
        traffic_history = await self.db.traffic_usage_history(
            uid,
            end_day=local_day,
            days=30,
        )
        referral_stats = await self.db.referral_stats(uid)
        username = await self._username()
        until = from_iso(row.get("subscription_until"))
        until_text = ""
        if until:
            until_text = until.astimezone(self.config.display_tz).isoformat()
        subscription_url = self._public_subscription_url(row, state, request=request)

        return web.json_response(
            {
                "user": {
                    "id": uid,
                    "first_name": tg_user.get("first_name") or row.get("first_name") or "Пользователь",
                    "username": tg_user.get("username") or row.get("username") or "",
                    "photo_url": tg_user.get("photo_url") or "",
                    "referrals": referral_stats["invited"],
                    "referral_rewards": referral_stats["rewarded"],
                    "referral_url": f"https://t.me/{username}?start=ref_{uid}",
                },
                "subscription": {
                    "active": _active(row),
                    "plan": row.get("plan_name") or "",
                    "until": until_text,
                    "remaining_seconds": _remaining_seconds(row),
                    "max_devices": int(row.get("max_devices") or 1),
                    "trial_used": bool(row.get("trial_used")),
                },
                "vpn": {
                    "ready": bool(getattr(self.provider, "service_ready", True)),
                    "ok": vpn_ok,
                    "server": state.server or self.config.vpn_server_name,
                    "subscription_url": subscription_url,
                    "traffic_used_gb": round(float(state.traffic_used_gb or 0), 2),
                    "traffic_limit_gb": round(float(state.traffic_limit_gb or 0), 2),
                    "traffic_history": traffic_history,
                    "devices": state.devices,
                },
                "plans": [
                    {
                        "code": code,
                        "name": plan["name"],
                        "days": plan["days"],
                        "devices": plan["devices"],
                        "rub": plan_price_rub(self.config, code),
                        "stars": plan_price_stars(self.config, code),
                        "savings": plan_savings_rub(code),
                        "popular": bool(plan.get("popular")),
                    }
                    for code, plan in PLANS.items()
                ],
                "payments": {"sbp_enabled": bool(self.config.rollypay_enabled)},
                "capabilities": {
                    "device_list": bool(self.provider.capabilities.supports_device_list),
                    "device_removal": bool(self.provider.capabilities.supports_device_removal),
                    "device_reset": bool(self.provider.capabilities.supports_device_reset),
                },
                "admin": {
                    "enabled": bool(admin_role),
                    "role": admin_role or "",
                },
                "clients": client_registry(subscription_url) if subscription_url else [],
                "shop": {
                    "extra_device_price_rub": int(EXTRA_DEVICE_PRICE_RUB),
                    "extra_device_price_stars": rub_to_stars(EXTRA_DEVICE_PRICE_RUB),
                    "max_devices": MAX_DEVICES,
                    "star_rate_rub": STAR_RATE_RUB,
                    "star_rate_xtr": STAR_RATE_XTR,
                },
                "bot_url": f"https://t.me/{username}",
                "agreement": {
                    "updated": AGREEMENT_UPDATED,
                    "sections": [
                        {"heading": heading, "paragraphs": list(paragraphs)}
                        for heading, paragraphs in AGREEMENT_SECTIONS
                    ],
                },
            }
        )

    async def admin_overview(self, request: web.Request) -> web.Response:
        _uid, _tg_user, _row, role = await self._admin_auth(request)
        overview, analytics, timeseries, plans = await asyncio.gather(
            self.db.admin_overview(),
            self.db.business_analytics(),
            self.db.admin_timeseries(14),
            self.db.admin_plan_breakdown(),
        )
        return web.json_response({
            "role": role,
            "overview": overview,
            "analytics": analytics,
            "timeseries": timeseries,
            "plans": plans,
            "generated_at": utcnow().isoformat(),
        })

    async def admin_users(self, request: web.Request) -> web.Response:
        await self._admin_auth(request)
        query = str(request.query.get("q") or "").strip()
        if len(query) > 64:
            raise _json_error(400, "Слишком длинный запрос")
        status = str(request.query.get("status") or "all")
        if status not in {"all", "active", "paid", "granted"}:
            raise _json_error(400, "Неизвестный фильтр")
        try:
            page = int(request.query.get("page") or 0)
        except ValueError as exc:
            raise _json_error(400, "Некорректная страница") from exc
        page = max(0, min(page, 100000))
        users, total = await self.db.admin_users_page(
            query=query,
            status=status,
            page=page,
            page_size=20,
        )
        return web.json_response({
            "users": users,
            "total": total,
            "page": page,
            "page_size": 20,
            "pages": max(1, (total + 19) // 20),
        })

    async def admin_payments(self, request: web.Request) -> web.Response:
        await self._admin_auth(request)
        overview, analytics, payments = await asyncio.gather(
            self.db.admin_overview(),
            self.db.business_analytics(),
            self.db.admin_recent_payments(40),
        )
        return web.json_response({
            "summary": {
                "rub_total": overview["sbp_revenue"],
                "stars_total": overview["star_revenue"],
                "rub_month": analytics["rub_month"],
                "stars_month": analytics["stars_month"],
            },
            "payments": payments,
        })

    async def admin_servers(self, request: web.Request) -> web.Response:
        await self._admin_auth(request)
        sample_users = await self.db.list_active_users_for_vpn_sync(limit=1)
        sample_user = sample_users[0] if sample_users else None
        try:
            report = await asyncio.wait_for(
                self.provider.server_diagnostics(sample_user),
                timeout=18.0,
            )
        except asyncio.TimeoutError as exc:
            raise _json_error(503, "Диагностика серверов не успела завершиться") from exc
        except Exception as exc:
            logger.exception("Mini App admin server diagnostics failed: %s", type(exc).__name__)
            raise _json_error(503, "Не удалось проверить серверы") from exc
        by_name = {
            str(item.get("name") or "").strip(): item
            for item in list(report.get("servers") or [])
        }
        servers = []
        for _server_id, name in BASE_MGN_SERVERS:
            item = by_name.get(name)
            latency = item.get("latency_ms") if item else None
            servers.append({
                "name": name,
                "available": bool(item and item.get("available")),
                "latency_ms": int(latency) if isinstance(latency, (int, float)) else None,
            })
        return web.json_response({"servers": servers, "generated_at": utcnow().isoformat()})

    async def stars_invoice(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        data = await self._json_body(request)
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        try:
            device_count = int(data.get("device_count") or (row.get("max_devices") if _active(row) else 1) or 1)
        except (TypeError, ValueError):
            raise _json_error(400, "Некорректное количество устройств")
        if device_count < 1 or device_count > MAX_DEVICES:
            raise _json_error(400, "Можно выбрать от 1 до 5 устройств")
        previous_device_count = max(1, min(MAX_DEVICES, int(row.get("max_devices") or 1)))

        original = plan_total_price_rub(self.config, code, device_count)
        promo_code = str(data.get("promo_code") or "").strip()
        quote = None
        if promo_code:
            quote = await self.db.promo_quote(
                code=promo_code,
                telegram_id=uid,
                plan_code=code,
                original_price=original,
            )
            if not quote or quote["type"] != "discount":
                raise _json_error(400, "Промокод не подходит для этой покупки")
        final = int(quote["final_price"]) if quote else original
        if final == 0:
            if not quote:
                raise _json_error(400, "Некорректная нулевая стоимость")
            try:
                await self.db.redeem_full_discount(
                    promo_id=int(quote["id"]), buyer_id=uid,
                    target_id=uid, product_code=code,
                    device_count=device_count,
                )
            except ValueError:
                raise _json_error(409, "Промокод уже использован или больше не действует")
            await self._activate_paid(uid, uid, code, f"promo:{quote['id']}")
            return web.json_response({"granted": True, "final_price": 0})
        stars = rub_to_stars(final)
        intent_id = uuid4().hex
        await self.db.create_payment_intent(
            intent_id=intent_id,
            buyer_id=uid,
            target_id=uid,
            product_code=code,
            original_amount_rub=original,
            discount_amount_rub=int(quote["discount"]) if quote else 0,
            final_amount_rub=final,
            currency="XTR",
            currency_amount=stars,
            promo_id=int(quote["id"]) if quote else None,
            promo_code=str(quote["code"]) if quote else None,
            device_count=device_count,
            previous_device_count=previous_device_count,
        )
        payload = f"xtr2|{intent_id}"
        try:
            invoice_url = await self.bot.create_invoice_link(
                title=f"MGN VPN · {plan['name']}",
                description=f"Подписка MGN VPN: {plan['name']} · {device_count} устр.",
                payload=payload,
                currency="XTR",
                prices=[LabeledPrice(label=f"MGN VPN · {plan['name']}", amount=stars)],
            )
        except Exception as exc:
            logger.exception("Mini App Stars invoice failed: %s", exc)
            raise _json_error(503, "Не удалось создать оплату Stars")

        return web.json_response({
            "invoice_url": invoice_url,
            "stars": stars,
            "original_price": original,
            "discount": int(quote["discount"]) if quote else 0,
            "final_price": final,
            "device_count": device_count,
        })

    async def sbp_create(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not self.config.rollypay_enabled:
            raise _json_error(503, "СБП пока не настроена")

        data = await self._json_body(request)
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        try:
            device_count = int(data.get("device_count") or (row.get("max_devices") if _active(row) else 1) or 1)
        except (TypeError, ValueError):
            raise _json_error(400, "Некорректное количество устройств")
        if device_count < 1 or device_count > MAX_DEVICES:
            raise _json_error(400, "Можно выбрать от 1 до 5 устройств")
        previous_device_count = max(1, min(MAX_DEVICES, int(row.get("max_devices") or 1)))

        original = plan_total_price_rub(self.config, code, device_count)
        promo_code = str(data.get("promo_code") or "").strip()
        quote = None
        if promo_code:
            quote = await self.db.promo_quote(
                code=promo_code,
                telegram_id=uid,
                plan_code=code,
                original_price=original,
            )
            if not quote or quote["type"] != "discount":
                raise _json_error(400, "Промокод не подходит для этой покупки")
        amount = int(quote["final_price"]) if quote else original
        if amount == 0:
            if not quote:
                raise _json_error(400, "Некорректная нулевая стоимость")
            try:
                await self.db.redeem_full_discount(
                    promo_id=int(quote["id"]), buyer_id=uid,
                    target_id=uid, product_code=code,
                    device_count=device_count,
                )
            except ValueError:
                raise _json_error(409, "Промокод уже использован или больше не действует")
            await self._activate_paid(uid, uid, code, f"promo:{quote['id']}")
            return web.json_response({"granted": True, "amount_rub": 0})
        order_id = f"vpn-mini-{uid}-{uuid4().hex[:12]}"
        local_id = await self.db.create_sbp_order(
            order_id=order_id, telegram_id=uid, target_telegram_id=uid,
            plan_code=code, amount_rub=amount, original_amount_rub=original,
            discount_amount_rub=int(quote["discount"]) if quote else 0,
            promo_id=int(quote["id"]) if quote else None,
            promo_code=str(quote["code"]) if quote else None,
            device_count=device_count,
            previous_device_count=previous_device_count,
        )
        try:
            payment = await create_payment(
                self.config,
                order_id=order_id,
                amount=Decimal(amount),
                description=f"MGN VPN {plan['name']} · {device_count} устр.",
                user_id=uid,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await self.db.attach_sbp_provider_payment(local_id, payment_id, pay_url)
        except (RollyPayError, KeyError, ValueError) as exc:
            await self.db.set_sbp_status(local_id, "create_failed")
            logger.warning("Mini App SBP create failed: %s", exc)
            raise _json_error(503, "Не удалось создать платёж")

        return web.json_response(
            {
                "payment_id": payment_id,
                "pay_url": pay_url,
                "amount_rub": amount,
                "original_price": original,
                "discount": int(quote["discount"]) if quote else 0,
                "device_count": device_count,
            }
        )

    async def sbp_check(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        payment_id = str(request.match_info.get("payment_id") or "")
        local = await self.db.get_sbp_payment(payment_id)
        if not local or int(local["telegram_id"]) != uid:
            raise _json_error(404, "Платёж не найден")

        if str(local.get("status") or "").lower() == "paid":
            return web.json_response({"status": "paid"})

        try:
            remote = await get_payment(self.config, payment_id)
        except RollyPayError:
            raise _json_error(503, "Не удалось проверить платёж")

        status = str(remote.get("status") or "").lower()
        remote_order = str(remote.get("order_id") or "")
        remote_payment = str(remote.get("payment_id") or "")
        remote_currency = str(
            remote.get("currency") or remote.get("payment_currency") or ""
        ).upper()
        try:
            remote_amount = Decimal(str(remote.get("amount")))
        except (InvalidOperation, ValueError):
            remote_amount = Decimal("-1")

        matches = (
            remote_payment == payment_id
            and remote_order == str(local["order_id"])
            and remote_currency == "RUB"
            and remote_amount.is_finite()
            and remote_amount == Decimal(int(local["amount_rub"]))
        )
        if not matches:
            raise _json_error(409, "Данные платежа не совпали")

        if status == "paid":
            try:
                fresh = await self.db.settle_sbp_payment(payment_id)
            except ValueError:
                raise _json_error(409, "Оплата получена и требует проверки поддержки")
            if fresh:
                await notify_purchase_admins(
                    self.bot,
                    self.config,
                    self.db,
                    buyer_id=uid,
                    target_id=int(local.get("target_telegram_id") or uid),
                    code=str(local["plan_code"]),
                    method="СБП",
                    amount=int(local["amount_rub"]),
                    payment_id=payment_id,
                )
                if str(local["plan_code"]) == "device":
                    updated = await self.db.get_user(uid)
                    if updated and getattr(self.provider, "service_ready", True):
                        await asyncio.wait_for(self.provider.provision(updated), 7.0)
                    if updated:
                        await self._sync_bot_subscription_menu(updated)
                else:
                    await self._activate_paid(
                        buyer_id=uid,
                        target_id=int(local.get("target_telegram_id") or uid),
                        code=str(local["plan_code"]),
                        event_key=f"sbp:{payment_id}",
                    )
            return web.json_response({"status": "paid"})

        await self.db.set_sbp_status(payment_id, status or "processing")
        return web.json_response({"status": status or "processing"})

    async def buy_extra_device(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not _active(row):
            raise _json_error(409, "Сначала активируйте подписку")
        if int(row.get("max_devices") or 1) >= MAX_DEVICES:
            raise _json_error(409, "Уже доступно максимальное количество устройств")
        if not self.config.rollypay_enabled:
            raise _json_error(503, "СБП пока не настроена")

        order_id = f"device-mini-{uid}-{uuid4().hex[:12]}"
        local_id = await self.db.create_sbp_order(
            order_id=order_id, telegram_id=uid, target_telegram_id=uid,
            plan_code="device", amount_rub=EXTRA_DEVICE_PRICE_RUB,
            original_amount_rub=EXTRA_DEVICE_PRICE_RUB,
        )
        try:
            payment = await create_payment(
                self.config,
                order_id=order_id,
                amount=Decimal(EXTRA_DEVICE_PRICE_RUB),
                description="MGN VPN +1 устройство",
                user_id=uid,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await self.db.attach_sbp_provider_payment(local_id, payment_id, pay_url)
        except (RollyPayError, KeyError, ValueError) as exc:
            await self.db.set_sbp_status(local_id, "create_failed")
            logger.warning("Mini App device payment failed: %s", exc)
            raise _json_error(503, "Не удалось создать платёж")
        return web.json_response(
            {
                "payment_id": payment_id,
                "pay_url": pay_url,
                "amount_rub": EXTRA_DEVICE_PRICE_RUB,
            }
        )

    async def promo_quote(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        data = await self._json_body(request)
        code = str(data.get("code") or "")
        plan_code = str(data.get("plan_code") or "")
        plan = PLANS.get(plan_code)
        try:
            device_count = int(data.get("device_count") or (row.get("max_devices") if _active(row) else 1) or 1)
        except (TypeError, ValueError):
            raise _json_error(400, "Некорректное количество устройств")
        if device_count < 1 or device_count > MAX_DEVICES:
            raise _json_error(400, "Можно выбрать от 1 до 5 устройств")
        original = plan_total_price_rub(self.config, plan_code, device_count) if plan else 0
        quote = await self.db.promo_quote(
            code=code,
            telegram_id=uid,
            plan_code=plan_code,
            original_price=original,
        )
        if not quote:
            raise _json_error(400, "Промокод недействителен или уже использован")
        return web.json_response(
            {
                "code": quote["code"],
                "type": quote["type"],
                "value": int(quote["value"]),
                "original_price": int(quote["original_price"]),
                "discount": int(quote["discount"]),
                "final_price": int(quote["final_price"]),
                "device_count": device_count,
            }
        )

    async def promo_redeem(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        data = await self._json_body(request)
        code = str(data.get("code") or "")
        updated = await self.db.redeem_free_days_promo(code, uid)
        if updated:
            if getattr(self.provider, "service_ready", True):
                try:
                    await asyncio.wait_for(self.provider.provision(updated), 7.0)
                except Exception as exc:
                    logger.warning("Promo provisioning deferred for %s: %s", uid, exc)
            await self._sync_bot_subscription_menu(updated)
            return web.json_response({
                "ok": True,
                "type": "free_days",
                "subscription_until": updated["subscription_until"],
            })

        # The bonuses page is also the entry point for discount codes. Validate
        # the code against every plan without consuming it; the client stores
        # it and applies it automatically when the user opens checkout.
        for plan_code in PLANS:
            quote = await self.db.promo_quote(
                code=code,
                telegram_id=uid,
                plan_code=plan_code,
                original_price=plan_total_price_rub(self.config, plan_code, 1),
            )
            if quote and quote["type"] == "discount":
                return web.json_response({
                    "ok": True,
                    "type": "discount",
                    "code": str(quote["code"]),
                    "value": int(quote["value"]),
                })

        raise _json_error(400, "Промокод недействителен или уже использован")

    async def delete_device(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not _active(row):
            raise _json_error(409, "Подписка не активна")
        if not getattr(self.provider, "service_ready", True):
            raise _json_error(503, "VPN-серверы ещё не подключены")

        if not self.provider.capabilities.supports_device_removal:
            raise _json_error(
                409,
                "VPN-система не поддерживает отключение одного устройства. Используйте сброс всех устройств.",
            )

        device_id = str(request.match_info.get("device_id") or "")
        if not device_id:
            raise _json_error(400, "Устройство не найдено")

        try:
            await asyncio.wait_for(self.provider.delete_device(row, device_id), 7.0)
            state, ok = await self._load_state(row)
        except Exception as exc:
            logger.warning("Mini App device delete failed for %s: %s", uid, exc)
            raise _json_error(503, "Не удалось отключить устройство")

        return web.json_response({"ok": True, "vpn_ok": ok, "devices": state.devices})

    async def create_support_ticket(self, request: web.Request) -> web.Response:
        uid, tg_user, row = await self._auth(request)
        self._rate_limit(
            f"support:{uid}",
            limit=3,
            window_seconds=600.0,
        )
        payload = await self._json_body(request)

        message = str(payload.get("message") or "").strip()
        if not message:
            raise _json_error(400, "Напишите текст обращения")
        if len(message) > 3000:
            raise _json_error(400, "Максимальная длина обращения — 3000 символов")

        ticket = await self.db.create_support_thread(
            telegram_id=uid,
            username=tg_user.get("username"),
            first_name=tg_user.get("first_name"),
            message_type="text",
            text=message,
        )

        username = (
            f"@{escape(str(ticket.get('username')))}"
            if ticket.get("username")
            else "без username"
        )
        created = from_iso(ticket.get("created_at"))
        created_text = (
            created.astimezone(self.config.display_tz).strftime("%d.%m.%Y %H:%M:%S")
            if created
            else "—"
        )
        subscription_text = "🟢 активна" if _active(row) else "🔴 нет активной"
        admin_text = (
            f"<b>Новое обращение #{int(ticket['id'])}</b>\n\n"
            f"Пользователь: <b>{username}</b>\n"
            f"Telegram ID: <code>{uid}</code>\n"
            f"Подписка: <b>{subscription_text}</b>\n"
            f"Дата: <b>{created_text}</b>\n\n"
            f"<b>Сообщение:</b>\n{escape(message)}"
        )
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Ответить",
                        callback_data=f"support:reply:{int(ticket['id'])}",
                        style="danger",
                    )
                ]
            ]
        )

        recipients = set(int(value) for value in self.config.admin_ids)
        try:
            recipients.update(
                int(item["telegram_id"])
                for item in await self.db.list_admin_roles()
            )
        except Exception:
            logger.exception("Could not load Mini App support recipients")

        for admin_id in recipients:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=admin_text,
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                logger.warning(
                    "Could not notify admin %s about support ticket %s: %s",
                    admin_id,
                    ticket.get("id"),
                    exc,
                )

        return web.json_response(
            {
                "ok": True,
                "ticket": {
                    "id": int(ticket["id"]),
                    "status": str(ticket.get("status") or "open"),
                    "created_at": str(ticket.get("created_at") or ""),
                },
            }
        )

    @staticmethod
    def _support_ticket_payload(ticket: dict, messages: list[dict] | None = None) -> dict:
        result = {
            "id": int(ticket["id"]),
            "status": str(ticket.get("status") or "open"),
            "preview": str(ticket.get("message") or "")[:160],
            "created_at": str(ticket.get("created_at") or ""),
            "updated_at": str(ticket.get("updated_at") or ticket.get("created_at") or ""),
        }
        if messages is not None:
            result["messages"] = [
                {
                    "id": int(item["id"]),
                    "sender_type": str(item["sender_type"]),
                    "message_type": str(item["message_type"]),
                    "text": str(item.get("text") or item.get("caption") or ""),
                    "created_at": str(item.get("created_at") or ""),
                }
                for item in messages
            ]
        return result

    async def list_support_threads(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        raw_page = request.query.get("page", "0")
        page = int(raw_page) if raw_page.isdigit() else 0
        tickets, total = await self.db.list_user_support_tickets(uid, page, 10)
        return web.json_response(
            {
                "tickets": [self._support_ticket_payload(ticket) for ticket in tickets],
                "page": page,
                "total": total,
            }
        )

    async def get_support_thread(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        raw_id = request.match_info.get("ticket_id", "")
        if not raw_id.isdigit():
            raise _json_error(404, "Обращение не найдено")
        ticket_id = int(raw_id)
        ticket = await self.db.get_support_ticket(ticket_id, owner_id=uid)
        if not ticket:
            raise _json_error(404, "Обращение не найдено")
        messages = await self.db.list_support_messages(ticket_id, owner_id=uid)
        return web.json_response({"ticket": self._support_ticket_payload(ticket, messages)})

    async def add_support_thread_message(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        raw_id = request.match_info.get("ticket_id", "")
        if not raw_id.isdigit():
            raise _json_error(404, "Обращение не найдено")
        payload = await self._json_body(request)
        message = str(payload.get("message") or "").strip()
        if not message or len(message) > 3000:
            raise _json_error(400, "Введите сообщение до 3000 символов")
        ticket_id = int(raw_id)
        try:
            ticket = await self.db.add_support_message(
                ticket_id=ticket_id,
                sender_type="user",
                sender_telegram_id=uid,
                message_type="text",
                text=message,
                owner_id=uid,
            )
        except ValueError:
            raise _json_error(409, "Обращение закрыто")
        if not ticket:
            raise _json_error(404, "Обращение не найдено")
        recipients = set(int(value) for value in self.config.admin_ids)
        recipients.update(int(item["telegram_id"]) for item in await self.db.list_admin_roles())
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть", callback_data=f"support:view:{ticket_id}")
        ]])
        for admin_id in recipients:
            try:
                await self.bot.send_message(
                    chat_id=admin_id,
                    text=f"<b>Новое сообщение в обращении #{ticket_id}</b>\n\n{escape(message)}",
                    reply_markup=markup,
                )
            except Exception as exc:
                logger.warning("Could not notify admin %s about support message: %s", admin_id, type(exc).__name__)
        return web.json_response({"ok": True, "ticket": self._support_ticket_payload(ticket)})

    async def close_support_thread(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        raw_id = request.match_info.get("ticket_id", "")
        if not raw_id.isdigit():
            raise _json_error(404, "Обращение не найдено")
        ticket = await self.db.set_support_status(int(raw_id), "closed", uid, is_admin=False)
        if not ticket:
            raise _json_error(404, "Обращение не найдено")
        return web.json_response({"ok": True, "ticket": self._support_ticket_payload(ticket)})

    async def reset_devices(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not _active(row):
            raise _json_error(409, "Подписка не активна")
        if not getattr(self.provider, "service_ready", True):
            raise _json_error(503, "VPN-серверы ещё не подключены")
        if not self.provider.capabilities.supports_device_reset:
            raise _json_error(409, "Сброс устройств недоступен для этого VPN-провайдера")

        try:
            state = await asyncio.wait_for(self.provider.reset_devices(row), 30.0)
            token = str(row["sub_token"])
            await self.invalidate_subscription_cache(token)
        except Exception as exc:
            logger.warning("Mini App device reset failed for %s: %s", uid, exc)
            raise _json_error(503, "Не удалось сбросить устройства")

        return web.json_response({"ok": True, "devices": state.devices})

    async def start(self) -> None:
        @web.middleware
        async def security_headers(request: web.Request, handler):
            try:
                response = await handler(request)
            except web.HTTPException as exc:
                response = web.Response(body=exc.body, status=exc.status, headers=exc.headers)
                if request.path.startswith("/api/") and exc.content_type != "application/json":
                    response = web.json_response({"message": "Некорректный запрос"}, status=exc.status)
            except Exception as exc:
                # Do not include request paths, payloads or provider exception URLs.
                logger.error("HTTP handler failed: %s", type(exc).__name__)
                response = web.json_response({"message": "Сервис временно недоступен"}, status=500)

            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            response.headers.setdefault("Content-Security-Policy",
                "default-src 'self'; script-src 'self' https://telegram.org https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
                "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'")
            if request.path.startswith(("/api/", "/sub/", "/client/")):
                response.headers["Cache-Control"] = "private, no-store, max-age=0"
                response.headers["Pragma"] = "no-cache"
            return response

        app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[security_headers],
        )
        app.router.add_get("/", self.landing)
        app.router.add_get("/agreement", self.agreement)
        app.router.add_get("/agreement/", self.agreement)
        app.router.add_get("/privacy", self.privacy)
        app.router.add_get("/privacy/", self.privacy)
        app.router.add_get("/app", self.index)
        app.router.add_get("/app/", self.index)
        # Keep old Mini App paths alive for already cached Telegram links.
        app.router.add_get("/miniapp", self.index)
        app.router.add_get("/miniapp/", self.index)
        app.router.add_get("/sub/{token}", self.subscription)
        app.router.add_get("/client/{client}/{token}", self.client_redirect)
        app.router.add_get("/api/miniapp/health", self.health)
        app.router.add_get("/api/miniapp/ready", self.readiness)
        app.router.add_get("/api/public/catalog", self.public_catalog)
        app.router.add_post("/api/public/payment", self.public_payment_create)
        app.router.add_get(
            "/api/public/payment/{payment_id}",
            self.public_payment_check,
        )
        app.router.add_get("/api/miniapp/me", self.me)
        app.router.add_get("/api/miniapp/admin/overview", self.admin_overview)
        app.router.add_get("/api/miniapp/admin/users", self.admin_users)
        app.router.add_get("/api/miniapp/admin/payments", self.admin_payments)
        app.router.add_get("/api/miniapp/admin/servers", self.admin_servers)
        app.router.add_post("/api/miniapp/payment/stars", self.stars_invoice)
        app.router.add_post("/api/miniapp/payment/sbp", self.sbp_create)
        app.router.add_get("/api/miniapp/payment/sbp/{payment_id}", self.sbp_check)
        app.router.add_post("/api/miniapp/shop/device", self.buy_extra_device)
        app.router.add_post("/api/miniapp/promo/quote", self.promo_quote)
        app.router.add_post("/api/miniapp/promo/redeem", self.promo_redeem)
        app.router.add_post("/api/miniapp/support", self.create_support_ticket)
        app.router.add_get("/api/miniapp/support", self.list_support_threads)
        app.router.add_get("/api/miniapp/support/{ticket_id}", self.get_support_thread)
        app.router.add_post("/api/miniapp/support/{ticket_id}/messages", self.add_support_thread_message)
        app.router.add_post("/api/miniapp/support/{ticket_id}/close", self.close_support_thread)
        app.router.add_delete("/api/miniapp/devices/{device_id}", self.delete_device)
        app.router.add_post("/api/miniapp/devices/reset", self.reset_devices)
        app.router.add_static("/static/", str(self.web_dir), show_index=False)

        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(
            self.runner,
            self.config.miniapp_host,
            self.config.miniapp_port,
        )
        await self.site.start()
        logger.info(
            "MGN VPN Mini App listening on %s:%s",
            self.config.miniapp_host,
            self.config.miniapp_port,
        )

    async def close(self) -> None:
        tasks = list(self._reconcile_tasks.values()) + list(self._subscription_refresh_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconcile_tasks.clear()
        self._subscription_refresh_tasks.clear()

        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
