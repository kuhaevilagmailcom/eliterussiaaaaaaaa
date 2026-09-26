from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from html import escape
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, quote
from uuid import uuid4

from aiohttp import web
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from catalog import (
    EXTRA_DEVICE_PRICE_RUB,
    MAX_DEVICES,
    PLANS,
    plan_price_rub,
    plan_price_stars,
    plan_savings_rub,
    rub_to_stars,
)
from db import Database, from_iso, utcnow
from payments import RollyPayError, create_payment, get_payment
from vpn import VpnProvider, VpnState, prettify_subscription_payload
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
    }.get(status, web.HTTPBadRequest)
    return cls(
        text=json.dumps({"message": message}, ensure_ascii=False),
        content_type="application/json",
    )


def validate_init_data(raw: str, bot_token: str, max_age: int = 3600) -> dict:
    if not raw:
        raise _json_error(401, "Открой MGN VPN внутри Telegram")

    pairs = dict(parse_qsl(raw, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
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

    if max_age > 0 and (not auth_date or abs(int(time.time()) - auth_date) > max_age):
        raise _json_error(401, "Сессия устарела. Открой Mini App заново")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise _json_error(401, "Не удалось прочитать Telegram-профиль") from exc

    if not isinstance(user, dict) or not int(user.get("id", 0) or 0):
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
        self._subscription_cache: dict[str, dict] = {}
        self._rate_events: dict[str, list[float]] = {}
        self._subscription_cache_dir = Path(self.config.db_path).with_name(
            "subscription_cache"
        )

    def _subscription_cache_path(self, token: str) -> Path:
        # Version the on-disk cache so a deployment that fixes subscription
        # composition never keeps serving an older NL-only payload.
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return self._subscription_cache_dir / f"v5-{digest}.json"

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
            tmp.replace(path)
        except Exception:
            logger.exception("Could not persist subscription cache")

    def _rate_limit(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
    ) -> None:
        now = time.monotonic()
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
        row = await self.db.ensure_user(
            user_id,
            tg_user.get("username"),
            tg_user.get("first_name") or "Пользователь",
        )
        return user_id, tg_user, row

    async def _username(self) -> str:
        if not self._bot_username:
            me = await self.bot.get_me()
            self._bot_username = me.username or "mgnvpn_bot"
        return self._bot_username

    async def _is_trial_channel_member(
        self,
        user_id: int,
        *,
        retries: int = 1,
    ) -> bool:
        retries = max(1, min(int(retries), 4))
        for attempt in range(retries):
            try:
                member = await self.bot.get_chat_member(
                    chat_id=self.config.trial_channel_username,
                    user_id=user_id,
                )
                status = getattr(
                    getattr(member, "status", ""),
                    "value",
                    getattr(member, "status", ""),
                )
                status = str(status)
                if status in {"member", "administrator", "creator"}:
                    return True
                if status == "restricted" and bool(getattr(member, "is_member", False)):
                    return True
            except Exception as exc:
                logger.warning(
                    "Mini App channel membership check failed for %s: %s",
                    user_id,
                    exc,
                )

            if attempt + 1 < retries:
                await asyncio.sleep(0.6)

        return False

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
            await asyncio.wait_for(
                self.provider.provision(user),
                45.0,
            )
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
            await asyncio.wait_for(self.provider.provision(user), 45.0)
            # The old cached payload may contain only NL/US. Remove it after
            # reconciliation so the next client refresh is rebuilt from all
            # reachable linked nodes.
            self._subscription_cache.pop(token, None)
            try:
                self._subscription_cache_path(token).unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning(
                    "Could not invalidate subscription cache for %s: %s",
                    user_id,
                    exc,
                )
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
        current = self._subscription_refresh_tasks.get(user_id)
        if current and not current.done():
            return
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
                5.0,
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

        # First activation or a missing main client: provisioning is required.
        try:
            state = await asyncio.wait_for(
                self.provider.provision(user),
                15.0,
            )
            if is_h1:
                self._last_reconcile[user_id] = time.monotonic()
            return state, True
        except Exception as exc:
            logger.warning(
                "Mini App VPN state unavailable for %s: %s",
                user_id,
                str(exc).strip() or type(exc).__name__,
            )
            return self._fallback_state(user), False


    def _remember_public_base_url(self, value: str) -> str:
        value = str(value or "").strip().rstrip("/")
        if value.startswith("http://"):
            value = "https://" + value[len("http://"):]
        elif value and not value.startswith("https://"):
            value = "https://" + value.lstrip("/")
        if not value.startswith("https://"):
            return ""
        path = Path(self.config.db_path).with_name("public_base_url.txt")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            current = ""
            try:
                current = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                pass
            if current != value:
                path.write_text(value + "\n", encoding="utf-8")
                logger.info("Public Mini App base URL learned from request host: %s", value)
        except Exception:
            logger.exception("Could not persist public Mini App base URL")
        return value

    def _external_base_url(self, request: web.Request) -> str:
        if self.config.miniapp_url:
            return self._remember_public_base_url(
                self.config.miniapp_url.rstrip("/")
            )

        forwarded_proto = (
            request.headers.get("X-Forwarded-Proto", "")
            .split(",", 1)[0]
            .strip()
        )
        forwarded_host = (
            request.headers.get("X-Forwarded-Host", "")
            .split(",", 1)[0]
            .strip()
        )
        host = forwarded_host or request.headers.get("Host", "").strip()

        if host:
            # The public Bothost/Telegram endpoint is HTTPS even when the
            # reverse proxy talks to aiohttp over plain HTTP internally.
            return self._remember_public_base_url(
                f"https://{host}".rstrip("/")
            )
        return ""

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


    async def _activate_paid(self, buyer_id: int, target_id: int, code: str, event_key: str) -> None:
        plan = PLANS[code]
        target = await self.db.extend_subscription(
            telegram_id=target_id,
            days=int(plan["days"]),
            plan_name=str(plan["name"]),
            max_devices=int(plan["devices"]),
        )
        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(target), 7.0)
            except Exception as exc:
                logger.warning("Mini App provisioning deferred for %s: %s", target_id, exc)
        await self._sync_bot_subscription_menu(target)

    async def landing(self, request: web.Request) -> web.Response:
        return web.Response(
            text=r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="theme-color" content="#050507">
  <meta name="description" content="MGN VPN — быстрый VPN в Telegram. Персональная ссылка, Happ, тарифы и управление устройствами.">
  <title>MGN VPN — свободный интернет</title>
  <style>
    :root{
      --bg:#050507;
      --panel:#0d0d11;
      --panel2:#131318;
      --line:rgba(255,255,255,.09);
      --text:#fff;
      --muted:#a0a0aa;
      --pink:#ff3aa7;
      --pink2:#ff1f91;
      --green:#32df80;
      --max:1320px;
    }
    *{box-sizing:border-box}
    html{scroll-behavior:smooth;background:var(--bg)}
    body{
      margin:0;
      min-height:100%;
      overflow-x:hidden;
      color:var(--text);
      background:
        radial-gradient(900px 520px at 78% 22%,rgba(255,34,143,.13),transparent 62%),
        radial-gradient(700px 420px at 10% 0%,rgba(255,58,167,.05),transparent 70%),
        var(--bg);
      font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Inter","Helvetica Neue",Arial,sans-serif;
      -webkit-font-smoothing:antialiased;
    }
    a{color:inherit;text-decoration:none}
    button{font:inherit}
    .wrap{width:min(calc(100% - 48px),var(--max));margin:auto}
    .topbar{
      position:sticky;top:0;z-index:30;
      background:rgba(5,5,7,.78);
      border-bottom:1px solid rgba(255,255,255,.035);
      backdrop-filter:blur(18px);
    }
    .nav{height:78px;display:flex;align-items:center;justify-content:space-between;gap:28px}
    .logo{width:104px;height:auto;display:block}
    .links{display:flex;gap:34px;color:#b8b8c0;font-size:13px}
    .links a{transition:.18s}
    .links a:hover{color:#fff}
    .nav-actions{display:flex;align-items:center;gap:10px}
    .ghost,.cta{
      min-height:44px;padding:0 18px;border-radius:14px;
      display:inline-flex;align-items:center;justify-content:center;
      font-size:13px;font-weight:750;border:1px solid var(--line);
    }
    .ghost{background:#0b0b0e}
    .cta{
      border-color:transparent;
      background:linear-gradient(135deg,var(--pink),var(--pink2));
      box-shadow:0 8px 28px rgba(255,37,148,.18);
    }

    .hero{
      min-height:690px;
      display:grid;grid-template-columns:minmax(0,1.03fr) minmax(420px,.97fr);
      gap:42px;align-items:center;padding:56px 0 34px;
    }
    .badge{
      width:max-content;display:flex;align-items:center;gap:9px;
      min-height:34px;padding:0 14px;border-radius:999px;
      border:1px solid var(--line);background:#101014;color:#c5c5cc;
      font-size:11px;letter-spacing:.08em;text-transform:uppercase;
    }
    .badge i{width:9px;height:9px;border-radius:50%;background:var(--pink);box-shadow:0 0 14px rgba(255,58,167,.65)}
    h1{
      margin:20px 0 0;
      max-width:760px;
      font-size:clamp(52px,6.1vw,84px);
      line-height:.96;letter-spacing:-.055em;font-weight:880;
    }
    h1 span{color:var(--pink)}
    .lead{max-width:650px;margin:24px 0 0;color:#aaaab3;font-size:18px;line-height:1.42}
    .hero-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:31px}
    .hero-actions a{min-height:56px;padding:0 24px;border-radius:17px;font-weight:800}
    .hero-actions .main{
      min-width:210px;display:inline-flex;align-items:center;justify-content:space-between;gap:22px;
      background:linear-gradient(135deg,#ff54b5,#ff2495);box-shadow:0 14px 40px rgba(255,39,148,.22)
    }
    .hero-actions .secondary{
      display:inline-flex;align-items:center;gap:11px;border:1px solid var(--line);background:#0e0e12
    }
    .play{
      width:33px;height:33px;border-radius:50%;display:grid;place-items:center;
      border:1px solid rgba(255,58,167,.45);color:var(--pink)
    }
    .trust{display:flex;gap:28px;flex-wrap:wrap;margin-top:30px}
    .trust div{display:flex;align-items:center;gap:10px;color:#a9a9b2;font-size:12px}
    .trust span{
      width:36px;height:36px;border:1px solid var(--line);border-radius:12px;background:#0e0e12;
      display:grid;place-items:center;color:#fff;font-size:15px
    }

    .visual{position:relative;min-height:590px;isolation:isolate}
    .orb{
      position:absolute;left:4%;top:4%;width:94%;aspect-ratio:1;border-radius:50%;
      border:1px solid rgba(255,58,167,.18);
      background:
        radial-gradient(circle at 50% 50%,transparent 0 34%,rgba(255,58,167,.055) 35% 36%,transparent 37% 48%,rgba(255,58,167,.04) 49% 50%,transparent 51%),
        radial-gradient(circle at 35% 35%,rgba(255,58,167,.13),transparent 48%);
      box-shadow:inset 0 0 80px rgba(255,58,167,.04);
      z-index:-2;
    }
    .orb:before,.orb:after{
      content:"";position:absolute;inset:8%;border-radius:50%;border:1px solid rgba(255,58,167,.14);
      transform:rotate(26deg) scaleY(.46);
    }
    .orb:after{transform:rotate(-28deg) scaleY(.58)}
    .node{position:absolute;width:8px;height:8px;border-radius:50%;background:var(--pink);box-shadow:0 0 16px rgba(255,58,167,.9)}
    .n1{left:17%;top:30%}.n2{right:14%;top:22%}.n3{right:6%;top:62%}.n4{left:8%;top:69%}

    .phone{
      position:absolute;left:50%;top:46%;width:330px;height:570px;
      transform:translate(-48%,-50%) rotate(8deg);
      border-radius:50px;padding:10px;background:linear-gradient(145deg,#5e5e65,#08080a 18%,#2b2b30 70%,#070708);
      box-shadow:0 42px 80px rgba(0,0,0,.55),0 0 0 1px rgba(255,255,255,.14);
    }
    .phone:before{
      content:"";position:absolute;z-index:5;left:50%;top:17px;transform:translateX(-50%);
      width:96px;height:27px;border-radius:18px;background:#030304;
    }
    .screen{
      height:100%;overflow:hidden;border-radius:41px;background:#070709;
      border:1px solid rgba(255,255,255,.08);padding:38px 16px 16px;position:relative;
    }
    .phone-head{display:flex;align-items:center;justify-content:space-between;font-size:10px;font-weight:700}
    .phone-logo{width:58px}
    .phone-user{margin-top:14px;display:flex;align-items:center;justify-content:space-between}
    .phone-avatar{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#28282e,#111)}
    .phone-user div:last-child{text-align:right}
    .phone-user b{display:block;font-size:12px}
    .phone-user small{color:var(--green);font-size:9px;font-weight:700}
    .mini-plan{
      margin-top:12px;padding:14px;border:1px solid var(--line);border-radius:17px;
      background:linear-gradient(145deg,#17171c,#0e0e12);
    }
    .mini-plan small{color:#858590;font-size:9px}
    .mini-plan h3{margin:2px 0 10px;font-size:25px;letter-spacing:-.04em}
    .mini-stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
    .mini-stats div{padding:8px;border-radius:11px;background:#15151a}
    .mini-stats b{display:block;font-size:11px}.mini-stats small{font-size:8px}
    .mini-ready{
      margin-top:9px;padding:13px;border-radius:17px;color:#fff;
      background:linear-gradient(145deg,#ff42ad,#f51386);
    }
    .mini-ready h3{margin:0;font-size:17px}
    .mini-ready p{margin:3px 0 10px;font-size:8px;color:rgba(255,255,255,.75)}
    .mini-link{height:36px;padding:0 10px;border-radius:10px;background:#121216;display:flex;align-items:center;justify-content:space-between;font-size:9px;font-weight:700}
    .mini-copy{height:34px;margin-top:7px;border-radius:10px;background:#fff;color:#111;display:grid;place-items:center;font-size:10px;font-weight:800}
    .mini-happ{height:32px;margin-top:6px;border:1px solid rgba(255,255,255,.35);border-radius:10px;display:grid;place-items:center;font-size:10px;font-weight:700}
    .mini-title{margin:12px 0 6px;font-size:11px;font-weight:800}
    .mini-grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}
    .mini-grid div{padding:10px;border-radius:13px;border:1px solid var(--line);background:#101014}
    .mini-grid b{display:block;font-size:10px}.mini-grid small{font-size:8px;color:#858590}
    .mini-nav{position:absolute;left:12px;right:12px;bottom:12px;height:48px;border:1px solid var(--line);border-radius:15px;background:#101014;display:grid;grid-template-columns:repeat(3,1fr)}
    .mini-nav div{display:grid;place-items:center;font-size:8px;color:#7f7f89}
    .mini-nav .active{color:var(--pink)}

    .float{
      position:absolute;min-width:160px;padding:14px 16px;border:1px solid rgba(255,255,255,.11);
      border-radius:16px;background:linear-gradient(145deg,rgba(28,28,34,.96),rgba(13,13,17,.94));
      box-shadow:0 14px 35px rgba(0,0,0,.25);backdrop-filter:blur(12px)
    }
    .float b{display:block;font-size:15px}.float small{display:block;margin-top:3px;color:#aaaab2;font-size:10px}
    .float em{font-style:normal;color:var(--pink);margin-right:7px}
    .f1{left:-2%;top:10%}.f2{right:-2%;top:30%}.f3{right:0;bottom:14%}

    .metricbar{
      display:grid;grid-template-columns:repeat(4,1fr);gap:0;
      border:1px solid var(--line);border-radius:22px;background:linear-gradient(180deg,#121217,#0b0b0f);
      overflow:hidden;margin:10px 0 20px;
    }
    .metric{min-height:104px;padding:22px;display:flex;align-items:center;gap:14px}
    .metric+.metric{border-left:1px solid var(--line)}
    .metric i{width:49px;height:49px;border-radius:15px;background:#1c1720;display:grid;place-items:center;color:var(--pink);font-style:normal;font-size:20px}
    .metric b{font-size:16px}.metric small{display:block;margin-top:2px;color:#92929b;font-size:11px;line-height:1.3}

    .section{padding:76px 0}
    .section-head{display:flex;align-items:end;justify-content:space-between;gap:30px;margin-bottom:24px}
    .kicker{color:var(--pink);font-size:11px;letter-spacing:.12em;text-transform:uppercase;font-weight:800}
    h2{margin:6px 0 0;font-size:clamp(32px,4vw,52px);letter-spacing:-.045em}
    .section-head p{max-width:480px;color:#92929c;font-size:14px;line-height:1.55}
    .features{display:grid;grid-template-columns:1.15fr .9fr .95fr;gap:14px}
    .feature{
      min-height:390px;padding:26px;border:1px solid var(--line);border-radius:24px;
      background:linear-gradient(145deg,#111116,#09090c);position:relative;overflow:hidden;
    }
    .feature:after{
      content:"";position:absolute;width:260px;height:260px;border-radius:50%;
      background:radial-gradient(circle,rgba(255,58,167,.17),transparent 68%);right:-70px;top:-70px;
    }
    .feature-icon{width:54px;height:54px;border-radius:16px;background:#1b1720;color:var(--pink);display:grid;place-items:center;font-size:23px}
    .feature h3{margin:52px 0 0;font-size:27px;letter-spacing:-.035em;max-width:360px}
    .feature p{margin:12px 0 0;max-width:390px;color:#93939d;line-height:1.55;font-size:13px}
    .feature-row{position:absolute;left:24px;right:24px;bottom:24px;display:flex;gap:8px}
    .chip{flex:1;min-height:74px;border:1px solid var(--line);border-radius:15px;background:#111115;padding:12px;font-size:11px}
    .chip b{display:block;margin-top:5px}
    .speedlines{
      position:absolute;right:-40px;top:90px;width:280px;height:160px;
      border-radius:50%;border-top:4px solid rgba(255,58,167,.75);transform:rotate(-19deg);
      box-shadow:0 -13px 0 rgba(255,58,167,.28),0 -26px 0 rgba(255,58,167,.12);
    }
    .worlddots{
      position:absolute;right:22px;top:88px;width:190px;height:110px;opacity:.75;
      background-image:radial-gradient(circle,rgba(255,255,255,.28) 1px,transparent 1.6px);
      background-size:9px 9px;
      mask-image:radial-gradient(ellipse at center,#000 42%,transparent 75%);
    }

    .plans{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
    .plan{padding:25px;border:1px solid var(--line);border-radius:22px;background:#0d0d11;position:relative}
    .plan.hot{border-color:rgba(255,58,167,.5);background:linear-gradient(160deg,#17111a,#0d0d11 58%)}
    .plan-tag{position:absolute;right:18px;top:18px;color:var(--pink);font-size:10px;font-weight:800}
    .plan h3{margin:0;font-size:20px}.price{margin-top:18px;font-size:36px;font-weight:850;letter-spacing:-.04em}
    .price small{font-size:13px;color:#8f8f99;font-weight:500}
    .plan ul{padding:0;margin:22px 0 24px;list-style:none;display:grid;gap:10px;color:#aaaab2;font-size:12px}
    .plan li:before{content:"✓";color:var(--pink);margin-right:9px}
    .plan a{height:48px;border-radius:14px;display:grid;place-items:center;border:1px solid var(--line);background:#151519;font-size:13px;font-weight:800}
    .plan.hot a{background:var(--pink);border-color:transparent}

    .faq{display:grid;gap:9px;max-width:900px;margin:auto}
    details{border:1px solid var(--line);border-radius:17px;background:#0d0d11;padding:0 18px}
    summary{min-height:58px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-weight:750;font-size:14px;list-style:none}
    summary::-webkit-details-marker{display:none}
    details p{margin:0 0 18px;color:#9696a0;font-size:13px;line-height:1.55}

    footer{border-top:1px solid var(--line);padding:30px 0 42px;color:#777781;font-size:12px}
    .foot{display:flex;align-items:center;justify-content:space-between;gap:20px}.foot img{width:86px}

    .reveal{opacity:0;transform:translateY(14px);transition:.55s cubic-bezier(.22,.8,.28,1)}
    .reveal.show{opacity:1;transform:none}

    @media(max-width:1080px){
      .links{display:none}
      .hero{grid-template-columns:1fr;min-height:auto;padding-top:58px}
      .hero-copy{text-align:center}.badge{margin:auto}.lead{margin-left:auto;margin-right:auto}
      .hero-actions,.trust{justify-content:center}
      .visual{min-height:620px;margin-top:-15px}
      .metricbar{grid-template-columns:1fr 1fr}
      .metric:nth-child(3){border-left:0;border-top:1px solid var(--line)}
      .metric:nth-child(4){border-top:1px solid var(--line)}
      .features{grid-template-columns:1fr 1fr}.feature:first-child{grid-column:1/-1}
    }
    @media(max-width:700px){
      .wrap{width:min(calc(100% - 28px),var(--max))}
      .nav{height:64px}.logo{width:86px}.nav-actions .ghost{display:none}.cta{min-height:40px;padding:0 14px}
      .hero{padding:38px 0 16px;gap:20px}
      h1{font-size:48px}.lead{font-size:15px}.hero-actions a{min-height:50px}
      .visual{min-height:520px}
      .phone{width:270px;height:467px;border-radius:42px}.screen{border-radius:34px;padding:32px 13px 13px}
      .phone:before{width:80px;height:22px;top:15px}
      .float{min-width:132px;padding:11px}.float b{font-size:12px}.float small{font-size:9px}
      .f1{left:-1%;top:5%}.f2{right:-3%;top:28%}.f3{right:-1%;bottom:7%}
      .metricbar{grid-template-columns:1fr 1fr}.metric{min-height:85px;padding:14px}.metric i{width:40px;height:40px}
      .section{padding:52px 0}.section-head{display:block}.section-head p{margin-top:10px}
      .features,.plans{grid-template-columns:1fr}.feature:first-child{grid-column:auto}.feature{min-height:350px}
      .foot{align-items:flex-start;flex-direction:column}
    }
    @media(max-width:430px){
      h1{font-size:42px}.hero-actions{display:grid}.hero-actions a{width:100%}
      .trust{gap:12px}.trust div{width:calc(50% - 6px)}
      .visual{min-height:490px}.phone{width:244px;height:422px}
      .float{display:none}
      .metricbar{grid-template-columns:1fr}.metric+.metric{border-left:0;border-top:1px solid var(--line)}
      .feature-row{position:static;margin-top:34px;display:grid;grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="topbar">
    <div class="wrap nav">
      <a href="/" aria-label="MGN VPN"><img class="logo" src="/static/assets/mgn-vpn-logo.webp?v=8" alt="MGN VPN"></a>
      <nav class="links">
        <a href="#features">Возможности</a>
        <a href="#plans">Тарифы</a>
        <a href="#servers">Серверы</a>
        <a href="#faq">FAQ</a>
      </nav>
      <div class="nav-actions">
        <a class="ghost" href="/app">Mini App</a>
        <a class="cta" href="https://t.me/mgnvpn_bot/mgnvpn">Подключить</a>
      </div>
    </div>
  </div>

  <main>
    <section class="wrap hero">
      <div class="hero-copy reveal show">
        <div class="badge"><i></i> Свобода без лишних шагов</div>
        <h1>Свободный<br>и безопасный<br>интернет с <span>MGN VPN</span></h1>
        <p class="lead">Подключай VPN через Telegram, добавляй подписку в Happ и управляй тарифом и устройствами в одном минималистичном интерфейсе.</p>
        <div class="hero-actions">
          <a class="main" href="https://t.me/mgnvpn_bot/mgnvpn"><span>Подключить VPN</span><span>→</span></a>
          <a class="secondary" href="/app"><span class="play">▶</span><span>Открыть Mini App</span></a>
        </div>
        <div class="trust">
          <div><span>⌁</span>Персональная ссылка</div>
          <div><span>▣</span>До 5 устройств</div>
          <div><span>◉</span>Подключение через Happ</div>
        </div>
      </div>

      <div class="visual reveal show" aria-label="MGN VPN Mini App">
        <div class="orb"><i class="node n1"></i><i class="node n2"></i><i class="node n3"></i><i class="node n4"></i></div>

        <div class="float f1"><b><em>▦</em>VPN в Telegram</b><small>подключение без ручной настройки</small></div>
        <div class="float f2"><b><em>◎</em>Happ</b><small>добавление подписки в один тап</small></div>
        <div class="float f3"><b><em>▣</em>До 5 устройств</b><small>управление прямо в профиле</small></div>

        <div class="phone">
          <div class="screen">
            <div class="phone-head"><span>9:41</span><img class="phone-logo" src="/static/assets/mgn-vpn-logo.webp?v=8" alt=""><span>•••</span></div>
            <div class="phone-user">
              <div class="phone-avatar"></div>
              <div><b>Пользователь</b><small>● Подписка активна</small></div>
            </div>
            <div class="mini-plan">
              <small>Текущий тариф</small>
              <h3>MGN Plus</h3>
              <div class="mini-stats"><div><small>Осталось</small><b>28 дней</b></div><div><small>Устройства</small><b>2 из 5</b></div></div>
            </div>
            <div class="mini-ready">
              <h3>Ваш VPN готов</h3>
              <p>Персональная ссылка только для вашего аккаунта</p>
              <div class="mini-link"><span>mgnvpn.ru/••••••</span><span>▣</span></div>
              <div class="mini-copy">▣ Скопировать ссылку</div>
              <div class="mini-happ">◇ Добавить в Happ</div>
            </div>
            <div class="mini-title">Быстрый доступ</div>
            <div class="mini-grid"><div><b>Профиль</b><small>2 из 5 устройств</small></div><div><b>Тарифы</b><small>Продлить VPN</small></div></div>
            <div class="mini-nav"><div class="active">⌂<br>Главная</div><div>♕<br>Подписка</div><div>♙<br>Профиль</div></div>
          </div>
        </div>
      </div>
    </section>

    <section class="wrap metricbar reveal">
      <div class="metric"><i>⚡</i><div><b>Быстрое подключение</b><small>Одна ссылка для Happ</small></div></div>
      <div class="metric"><i>▦</i><div><b>5 тарифов</b><small>От 7 дней до 1 года</small></div></div>
      <div class="metric"><i>▣</i><div><b>До 5 устройств</b><small>Добавляй дополнительные слоты</small></div></div>
      <div class="metric"><i>✦</i><div><b>Всё в Telegram</b><small>Покупка, профиль и поддержка</small></div></div>
    </section>

    <section class="wrap section" id="features">
      <div class="section-head reveal">
        <div><span class="kicker">Возможности</span><h2>VPN без перегруженного интерфейса</h2></div>
        <p>Сайт, бот и Mini App работают как один продукт: одна подписка, одна персональная ссылка и понятное управление.</p>
      </div>
      <div class="features">
        <article class="feature reveal">
          <div class="feature-icon">⌾</div>
          <h3>Персональная подписка</h3>
          <p>После активации тарифа MGN VPN формирует персональную ссылку. Её можно скопировать или сразу добавить в Happ.</p>
          <div class="feature-row"><div class="chip">Ссылка<b>mgnvpn.ru/sub/…</b></div><div class="chip">Клиент<b>Happ</b></div><div class="chip">Доступ<b>Только владельцу</b></div></div>
        </article>
        <article class="feature reveal">
          <div class="feature-icon">⚡</div><div class="speedlines"></div>
          <h3>Минимум действий</h3>
          <p>Открыл Mini App → нажал «Добавить в Happ» → подписка передана клиенту через защищённый HTTPS-переход.</p>
          <div class="feature-row"><div class="chip">Подключение<b>1–2 нажатия</b></div><div class="chip">Обновление<b>Автоматически</b></div></div>
        </article>
        <article class="feature reveal" id="servers">
          <div class="feature-icon">◎</div><div class="worlddots"></div>
          <h3>Несколько локаций</h3>
          <p>Серверы собраны в одну подписку — не нужно вручную хранить и менять отдельные конфиги.</p>
          <div class="feature-row"><div class="chip">Единый профиль<b>MGN VPN</b></div><div class="chip">Серверы<b>В одной ссылке</b></div></div>
        </article>
      </div>
    </section>

    <section class="wrap section" id="plans">
      <div class="section-head reveal">
        <div><span class="kicker">Тарифы</span><h2>Выбери срок доступа</h2></div>
        <p>Дополнительные устройства можно подключить отдельно. Максимальный лимит — 5 устройств.</p>
      </div>
      <div class="plans">
        <article class="plan reveal"><h3>1 месяц</h3><div class="price">149 ₽ <small>/ 30 дней</small></div><ul><li>1 устройство включено</li><li>Персональная VPN-ссылка</li><li>Подключение через Happ</li></ul><a href="https://t.me/mgnvpn_bot/mgnvpn">Выбрать</a></article>
        <article class="plan hot reveal"><span class="plan-tag">ПОПУЛЯРНЫЙ</span><h3>3 месяца</h3><div class="price">349 ₽ <small>/ 90 дней</small></div><ul><li>1 устройство включено</li><li>Экономнее помесячной оплаты</li><li>Профиль и управление устройствами</li></ul><a href="https://t.me/mgnvpn_bot/mgnvpn">Подключить</a></article>
        <article class="plan reveal"><h3>1 год</h3><div class="price">1200 ₽ <small>/ 365 дней</small></div><ul><li>1 устройство включено</li><li>Долгий срок без продлений</li><li>Поддержка внутри Telegram</li></ul><a href="https://t.me/mgnvpn_bot/mgnvpn">Выбрать</a></article>
      </div>
    </section>

    <section class="wrap section" id="faq">
      <div class="section-head reveal"><div><span class="kicker">FAQ</span><h2>Коротко о подключении</h2></div></div>
      <div class="faq">
        <details class="reveal"><summary>Как подключить MGN VPN?<span>＋</span></summary><p>Открой Mini App в Telegram, активируй тариф и нажми «Добавить в Happ». Если Happ не установлен, сначала установи приложение, затем повтори подключение.</p></details>
        <details class="reveal"><summary>Где находится моя ссылка?<span>＋</span></summary><p>Персональная ссылка доступна в боте и в Mini App после активации подписки. Она имеет вид mgnvpn.ru/sub/… и предназначена только для владельца аккаунта.</p></details>
        <details class="reveal"><summary>Можно подключить несколько устройств?<span>＋</span></summary><p>Да. В тариф включено одно устройство, а дополнительные слоты можно докупить в профиле. Максимум — 5 устройств.</p></details>
      </div>
    </section>
  </main>

  <footer>
    <div class="wrap foot">
      <img src="/static/assets/mgn-vpn-logo.webp?v=8" alt="MGN VPN">
      <span>© 2026 MGN VPN</span>
      <span>Mini App: <a href="/app">mgnvpn.ru/app</a></span>
    </div>
  </footer>

  <script>
    const io=new IntersectionObserver((entries)=>{
      entries.forEach(entry=>{if(entry.isIntersecting){entry.target.classList.add('show');io.unobserve(entry.target)}})
    },{threshold:.12});
    document.querySelectorAll('.reveal:not(.show)').forEach(el=>io.observe(el));
  </script>
</body>
</html>""",
            content_type="text/html",
            headers={"Cache-Control": "public, max-age=60"},
        )

    async def index(self, request: web.Request) -> web.StreamResponse:
        index = self.web_dir / "index.html"
        if not index.exists():
            raise web.HTTPNotFound(text="Mini App files are missing")
        return web.FileResponse(index, headers={"Cache-Control": "no-store"})

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": "MGN VPN Mini App",
                "build": "h1cloud-v27-dual-federation",
                "vpn_mode": getattr(self.provider, "mode_name", "vpn"),
                "vpn_ready": bool(getattr(self.provider, "service_ready", True)),
            }
        )

    async def subscription(self, request: web.Request) -> web.Response:
        token = str(request.match_info.get("token") or "").strip()
        user = await self.db.get_user_by_sub_token(token)
        if user is None:
            raise web.HTTPNotFound(text="Subscription not found")
        if not _active(user):
            raise web.HTTPForbidden(text="Subscription expired")

        self._schedule_subscription_federation_refresh(user, token)

        cached = self._subscription_cache.get(token)
        now = time.monotonic()
        if cached and now - float(cached["created"]) <= 30.0:
            return web.Response(
                body=cached["body"],
                headers=dict(cached["headers"]),
            )

        persistent_cached = self._read_persistent_subscription_cache(token)
        if (
            persistent_cached
            and time.time() - float(persistent_cached["created_at"]) <= 300.0
        ):
            # A recent successful payload is safer and dramatically faster than
            # rebuilding the federation list for every VPN-client refresh.
            return web.Response(
                body=persistent_cached["body"],
                headers=dict(persistent_cached["headers"]),
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
                8.5,
            )
            if count < 1:
                raise RuntimeError("H1Cloud subscription contains no VLESS nodes")

            title = base64.b64encode("MGN VPN".encode("utf-8")).decode("ascii")
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Cache-Control": "private, no-store, max-age=0",
                "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'inline; filename="MGN-VPN.txt"',
                "Profile-Title": f"base64:{title}",
                "Profile-Update-Interval": "12",
            }
            for key in (
                "subscription-userinfo",
                "support-url",
                "profile-web-page-url",
            ):
                value = upstream_headers.get(key)
                if value:
                    headers[key.title()] = value

            self._subscription_cache[token] = {
                "created": now,
                "body": body,
                "headers": headers,
            }
            self._write_persistent_subscription_cache(token, body, headers)
            logger.info(
                "MGN subscription served for %s with %s node(s)",
                user["telegram_id"],
                count,
            )
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
                return web.Response(
                    body=cached["body"],
                    headers=dict(cached["headers"]),
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
                return web.Response(
                    body=persistent_cached["body"],
                    headers=dict(persistent_cached["headers"]),
                )
            logger.warning(
                "MGN subscription unavailable for %s: %s",
                user["telegram_id"],
                exc,
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
<script>location.href={json.dumps(target)};</script></body></html>""",
            content_type="text/html",
            headers={"Cache-Control": "no-store"},
        )

    async def me(self, request: web.Request) -> web.Response:
        uid, tg_user, row = await self._auth(request)
        state, vpn_ok = await self._load_state(row)
        referral_stats = await self.db.referral_stats(uid)
        username = await self._username()
        trial_available = not bool(row.get("trial_used")) and not _active(row)
        trial_channel_member = (
            await self._is_trial_channel_member(uid, retries=1)
            if trial_available
            else False
        )

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
                    "trial_available": trial_available,
                    "trial_channel_member": trial_channel_member,
                },
                "vpn": {
                    "ready": bool(getattr(self.provider, "service_ready", True)),
                    "ok": vpn_ok,
                    "server": state.server or self.config.vpn_server_name,
                    "subscription_url": subscription_url,
                    "traffic_used_gb": round(float(state.traffic_used_gb or 0), 2),
                    "traffic_limit_gb": round(float(state.traffic_limit_gb or 0), 2),
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
                    }
                    for code, plan in PLANS.items()
                ],
                "payments": {"sbp_enabled": bool(self.config.rollypay_enabled)},
                "capabilities": {
                    "device_list": bool(self.provider.capabilities.supports_device_list),
                    "device_removal": bool(self.provider.capabilities.supports_device_removal),
                    "device_reset": bool(self.provider.capabilities.supports_device_reset),
                },
                "clients": client_registry(subscription_url) if subscription_url else [],
                "shop": {
                    "extra_device_price_rub": int(EXTRA_DEVICE_PRICE_RUB),
                    "extra_device_price_stars": rub_to_stars(EXTRA_DEVICE_PRICE_RUB),
                    "max_devices": MAX_DEVICES,
                },
                "trial_channel_url": self.config.trial_channel_url,
                "bot_url": f"https://t.me/{username}",
            }
        )

    async def activate_trial(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if row.get("trial_used"):
            raise _json_error(409, "Бесплатный день уже использован")
        subscribed = await self._is_trial_channel_member(uid, retries=3)
        if not subscribed:
            raise _json_error(
                403,
                "Подписка на канал пока не найдена. Вернитесь из канала и нажмите «Забрать 1 день» ещё раз.",
            )

        activated = await self.db.activate_trial(
            uid,
            self.config.trial_minutes,
            self.config.trial_max_devices,
        )
        if not activated:
            raise _json_error(409, "Бесплатный день уже использован")

        row = await self.db.get_user(uid)
        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(row), 7.0)
            except Exception as exc:
                logger.warning("Trial provisioning deferred for %s: %s", uid, exc)

        await self._sync_bot_subscription_menu(row)
        return web.json_response(
            {
                "ok": True,
                "subscription_until": row.get("subscription_until"),
                "plan": row.get("plan_name") or "Бесплатный доступ",
            }
        )

    async def stars_invoice(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        data = await request.json()
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        original = plan_price_rub(self.config, code)
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
        )
        payload = f"xtr2|{intent_id}"
        try:
            invoice_url = await self.bot.create_invoice_link(
                title=f"MGN VPN · {plan['name']}",
                description=f"Подписка MGN VPN: {plan['name']}",
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
        })

    async def sbp_create(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        if not self.config.rollypay_enabled:
            raise _json_error(503, "СБП пока не настроена")

        data = await request.json()
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        original = plan_price_rub(self.config, code)
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
        order_id = f"vpn-mini-{uid}-{uuid4().hex[:12]}"
        try:
            payment = await create_payment(
                self.config,
                order_id=order_id,
                amount=Decimal(amount),
                description=f"MGN VPN {plan['name']}",
                user_id=uid,
            )
            payment_id = str(payment["payment_id"])
            pay_url = str(payment["pay_url"])
            await self.db.create_sbp_payment(
                payment_id=payment_id,
                order_id=order_id,
                telegram_id=uid,
                target_telegram_id=uid,
                plan_code=code,
                amount_rub=amount,
                original_amount_rub=original,
                discount_amount_rub=int(quote["discount"]) if quote else 0,
                promo_id=int(quote["id"]) if quote else None,
                promo_code=str(quote["code"]) if quote else None,
            )
        except (RollyPayError, KeyError) as exc:
            logger.warning("Mini App SBP create failed: %s", exc)
            raise _json_error(503, "Не удалось создать платёж")

        return web.json_response(
            {
                "payment_id": payment_id,
                "pay_url": pay_url,
                "amount_rub": amount,
                "original_price": original,
                "discount": int(quote["discount"]) if quote else 0,
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
            and remote_amount == Decimal(int(local["amount_rub"]))
        )
        if not matches:
            raise _json_error(409, "Данные платежа не совпали")

        if status == "paid":
            fresh = await self.db.mark_sbp_paid(payment_id)
            if fresh:
                if local.get("promo_id"):
                    await self.db.consume_promo(
                        promo_id=int(local["promo_id"]),
                        telegram_id=uid,
                        payment_id=payment_id,
                    )
                if str(local["plan_code"]) == "device":
                    updated = await self.db.grant_extra_device(uid, MAX_DEVICES)
                    if updated and getattr(self.provider, "service_ready", True):
                        await self.provider.provision(updated)
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
            await self.db.create_sbp_payment(
                payment_id=payment_id,
                order_id=order_id,
                telegram_id=uid,
                target_telegram_id=uid,
                plan_code="device",
                amount_rub=EXTRA_DEVICE_PRICE_RUB,
                original_amount_rub=EXTRA_DEVICE_PRICE_RUB,
            )
        except (RollyPayError, KeyError) as exc:
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
        uid, _tg_user, _row = await self._auth(request)
        data = await request.json()
        code = str(data.get("code") or "")
        plan_code = str(data.get("plan_code") or "")
        plan = PLANS.get(plan_code)
        original = plan_price_rub(self.config, plan_code) if plan else 0
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
            }
        )

    async def promo_redeem(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        data = await request.json()
        code = str(data.get("code") or "")
        updated = await self.db.redeem_free_days_promo(code, uid)
        if not updated:
            raise _json_error(400, "Промокод недействителен или не даёт бесплатные дни")
        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(updated), 7.0)
            except Exception as exc:
                logger.warning("Promo provisioning deferred for %s: %s", uid, exc)
        await self._sync_bot_subscription_menu(updated)
        return web.json_response({"ok": True, "subscription_until": updated["subscription_until"]})

    async def delete_device(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not _active(row):
            raise _json_error(409, "Подписка не активна")
        if not getattr(self.provider, "service_ready", True):
            raise _json_error(503, "VPN-серверы ещё не подключены")

        if not self.provider.capabilities.supports_device_removal:
            raise _json_error(
                409,
                "H1Cloud не поддерживает отключение одного устройства. Используйте сброс всех устройств.",
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
        uid, tg_user, _row = await self._auth(request)
        self._rate_limit(
            f"support:{uid}",
            limit=3,
            window_seconds=600.0,
        )
        try:
            payload = await request.json()
        except Exception as exc:
            raise _json_error(400, "Некорректный запрос") from exc

        message = str(payload.get("message") or "").strip()
        if not message:
            raise _json_error(400, "Напишите текст обращения")
        if len(message) > 3000:
            raise _json_error(400, "Максимальная длина обращения — 3000 символов")

        ticket = await self.db.create_support_ticket(
            telegram_id=uid,
            username=tg_user.get("username"),
            first_name=tg_user.get("first_name"),
            message=message,
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
        admin_text = (
            f"<b>Новое обращение #{int(ticket['id'])}</b>\n\n"
            f"Пользователь: <b>{username}</b>\n"
            f"ID: <code>{uid}</code>\n"
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
                response = exc

            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=(), payment=()",
            )
            if request.path.startswith("/api/") or request.path.startswith("/sub/"):
                response.headers["Cache-Control"] = "private, no-store, max-age=0"
                response.headers["Pragma"] = "no-cache"
            return response

        app = web.Application(
            client_max_size=64 * 1024,
            middlewares=[security_headers],
        )
        app.router.add_get("/", self.landing)
        app.router.add_get("/app", self.index)
        app.router.add_get("/app/", self.index)
        # Keep old Mini App paths alive for already cached Telegram links.
        app.router.add_get("/miniapp", self.index)
        app.router.add_get("/miniapp/", self.index)
        app.router.add_get("/sub/{token}", self.subscription)
        app.router.add_get("/client/{client}/{token}", self.client_redirect)
        app.router.add_get("/api/miniapp/health", self.health)
        app.router.add_get("/api/miniapp/me", self.me)
        app.router.add_post("/api/miniapp/trial", self.activate_trial)
        app.router.add_post("/api/miniapp/payment/stars", self.stars_invoice)
        app.router.add_post("/api/miniapp/payment/sbp", self.sbp_create)
        app.router.add_get("/api/miniapp/payment/sbp/{payment_id}", self.sbp_check)
        app.router.add_post("/api/miniapp/shop/device", self.buy_extra_device)
        app.router.add_post("/api/miniapp/promo/quote", self.promo_quote)
        app.router.add_post("/api/miniapp/promo/redeem", self.promo_redeem)
        app.router.add_post("/api/miniapp/support", self.create_support_ticket)
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
        tasks = list(self._reconcile_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reconcile_tasks.clear()

        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
