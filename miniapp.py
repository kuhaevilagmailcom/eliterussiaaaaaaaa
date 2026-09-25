from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl
from uuid import uuid4

from aiohttp import web
from aiogram.types import LabeledPrice

from catalog import (
    DIAMOND_REWARDS,
    EXTRA_DEVICE_COST,
    PLANS,
    REFERRAL_FIRST_PAID_REWARD,
    REFERRAL_TRIAL_REWARD,
    plan_price_rub,
    plan_price_stars,
)
from db import Database, from_iso, utcnow
from payments import RollyPayError, create_payment, get_payment
from vpn import VpnProvider, VpnState


logger = logging.getLogger(__name__)


def _json_error(status: int, message: str) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        403: web.HTTPForbidden,
        404: web.HTTPNotFound,
        409: web.HTTPConflict,
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

    def _fallback_state(self, user: dict) -> VpnState:
        return VpnState(
            subscription_url="",
            server=self.config.vpn_server_name,
            traffic_used_gb=0.0,
            traffic_limit_gb=0.0,
            devices=[],
        )

    async def _load_state(self, user: dict) -> tuple[VpnState, bool]:
        if not _active(user):
            return self._fallback_state(user), True
        if not getattr(self.provider, "service_ready", True):
            return self._fallback_state(user), False
        try:
            return await asyncio.wait_for(self.provider.get_state(user), 4.0), True
        except Exception:
            try:
                return await asyncio.wait_for(self.provider.provision(user), 6.0), True
            except Exception as exc:
                logger.warning("Mini App VPN state unavailable for %s: %s", user["telegram_id"], exc)
                return self._fallback_state(user), False

    async def _activate_paid(self, buyer_id: int, target_id: int, code: str, event_key: str) -> None:
        plan = PLANS[code]
        target = await self.db.extend_subscription(
            telegram_id=target_id,
            days=int(plan["days"]),
            plan_name=str(plan["name"]),
            max_devices=int(plan["devices"]),
        )
        reward = int(DIAMOND_REWARDS.get(code, 0))
        if reward:
            await self.db.add_diamonds(
                telegram_id=buyer_id,
                amount=reward,
                reason=f"Покупка VPN: {plan['name']}",
                event_key=f"payment-reward:{event_key}",
            )
        referrer_id = target.get("referrer_id")
        if referrer_id:
            await self.db.add_diamonds(
                telegram_id=int(referrer_id),
                amount=REFERRAL_FIRST_PAID_REWARD,
                reason="Друг впервые купил VPN",
                event_key=f"referral-first-paid:{target_id}",
            )
        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(target), 7.0)
            except Exception as exc:
                logger.warning("Mini App provisioning deferred for %s: %s", target_id, exc)

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
                "vpn_mode": getattr(self.provider, "mode_name", "vpn"),
                "vpn_ready": bool(getattr(self.provider, "service_ready", True)),
            }
        )

    async def me(self, request: web.Request) -> web.Response:
        uid, tg_user, row = await self._auth(request)
        state, vpn_ok = await self._load_state(row)
        referrals = await self.db.referral_count(uid)
        diamonds = await self.db.diamond_balance(uid)
        username = await self._username()

        until = from_iso(row.get("subscription_until"))
        until_text = ""
        if until:
            until_text = until.astimezone(self.config.display_tz).isoformat()

        return web.json_response(
            {
                "user": {
                    "id": uid,
                    "first_name": tg_user.get("first_name") or row.get("first_name") or "Пользователь",
                    "username": tg_user.get("username") or row.get("username") or "",
                    "photo_url": tg_user.get("photo_url") or "",
                    "diamonds": diamonds,
                    "referrals": referrals,
                    "referral_url": f"https://t.me/{username}?start=ref_{uid}",
                },
                "subscription": {
                    "active": _active(row),
                    "plan": row.get("plan_name") or "",
                    "until": until_text,
                    "remaining_seconds": _remaining_seconds(row),
                    "max_devices": int(row.get("max_devices") or 1),
                    "trial_used": bool(row.get("trial_used")),
                    "trial_available": not bool(row.get("trial_used")) and not _active(row),
                },
                "vpn": {
                    "ready": bool(getattr(self.provider, "service_ready", True)),
                    "ok": vpn_ok,
                    "server": state.server or self.config.vpn_server_name,
                    "subscription_url": state.subscription_url or "",
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
                        "diamonds": int(DIAMOND_REWARDS.get(code, 0)),
                    }
                    for code, plan in PLANS.items()
                ],
                "payments": {"sbp_enabled": bool(self.config.rollypay_enabled)},
                "shop": {
                    "extra_device_cost": int(EXTRA_DEVICE_COST),
                    "max_devices": 10,
                },
                "trial_channel_url": self.config.trial_channel_url,
                "bot_url": f"https://t.me/{username}",
            }
        )

    async def activate_trial(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if row.get("trial_used"):
            raise _json_error(409, "Пробный период уже использован")
        if _active(row):
            raise _json_error(409, "Подписка уже активна")

        try:
            member = await self.bot.get_chat_member(
                chat_id=self.config.trial_channel_username,
                user_id=uid,
            )
        except Exception as exc:
            logger.warning("Mini App trial membership check failed: %s", exc)
            raise _json_error(503, "Не удалось проверить подписку на канал")

        status = getattr(getattr(member, "status", ""), "value", getattr(member, "status", ""))
        subscribed = str(status) in {"member", "administrator", "creator"} or (
            str(status) == "restricted" and bool(getattr(member, "is_member", False))
        )
        if not subscribed:
            raise _json_error(403, "Сначала подпишись на канал MGN VPN")

        activated = await self.db.activate_trial(
            uid,
            self.config.trial_minutes,
            self.config.trial_max_devices,
        )
        if not activated:
            raise _json_error(409, "Пробный период уже использован")

        row = await self.db.get_user(uid)
        referrer_id = row.get("referrer_id")
        if referrer_id:
            await self.db.add_diamonds(
                telegram_id=int(referrer_id),
                amount=REFERRAL_TRIAL_REWARD,
                reason="Друг активировал пробную подписку",
                event_key=f"referral-trial:{uid}",
            )

        if getattr(self.provider, "service_ready", True):
            try:
                await asyncio.wait_for(self.provider.provision(row), 7.0)
            except Exception as exc:
                logger.warning("Trial provisioning deferred for %s: %s", uid, exc)

        return web.json_response({"ok": True})

    async def stars_invoice(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        data = await request.json()
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        stars = plan_price_stars(self.config, code)
        payload = f"xtr|{code}|{uid}|{uid}|{uuid4().hex[:12]}"
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

        return web.json_response({"invoice_url": invoice_url, "stars": stars})

    async def sbp_create(self, request: web.Request) -> web.Response:
        uid, _tg_user, _row = await self._auth(request)
        if not self.config.rollypay_enabled:
            raise _json_error(503, "СБП пока не настроена")

        data = await request.json()
        code = str(data.get("plan_code") or "")
        plan = PLANS.get(code)
        if not plan:
            raise _json_error(400, "Тариф не найден")

        amount = plan_price_rub(self.config, code)
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
            )
        except (RollyPayError, KeyError) as exc:
            logger.warning("Mini App SBP create failed: %s", exc)
            raise _json_error(503, "Не удалось создать платёж")

        return web.json_response(
            {"payment_id": payment_id, "pay_url": pay_url, "amount_rub": amount}
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
        current_limit = int(row.get("max_devices") or 1)
        if current_limit >= 10:
            raise _json_error(409, "Уже доступно максимальное количество устройств")

        balance = int(row.get("diamonds") or 0)
        if balance < int(EXTRA_DEVICE_COST):
            missing = int(EXTRA_DEVICE_COST) - balance
            raise _json_error(409, f"Не хватает {missing} алмазов")

        updated = await self.db.purchase_extra_device(
            telegram_id=uid,
            cost=int(EXTRA_DEVICE_COST),
            event_key=f"mini-device:{uid}:{uuid4().hex}",
            max_total_devices=10,
        )
        if not updated:
            raise _json_error(409, "Не удалось добавить устройство")

        if getattr(self.provider, "service_ready", True) and _active(updated):
            try:
                await asyncio.wait_for(self.provider.provision(updated), 7.0)
            except Exception as exc:
                logger.warning(
                    "Mini App device limit provisioning deferred for %s: %s",
                    uid,
                    exc,
                )

        return web.json_response(
            {
                "ok": True,
                "max_devices": int(updated.get("max_devices") or current_limit + 1),
                "diamonds": int(updated.get("diamonds") or 0),
            }
        )

    async def delete_device(self, request: web.Request) -> web.Response:
        uid, _tg_user, row = await self._auth(request)
        if not _active(row):
            raise _json_error(409, "Подписка не активна")
        if not getattr(self.provider, "service_ready", True):
            raise _json_error(503, "VPN-серверы ещё не подключены")

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

    async def start(self) -> None:
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/miniapp", self.index)
        app.router.add_get("/miniapp/", self.index)
        app.router.add_get("/api/miniapp/health", self.health)
        app.router.add_get("/api/miniapp/me", self.me)
        app.router.add_post("/api/miniapp/trial", self.activate_trial)
        app.router.add_post("/api/miniapp/payment/stars", self.stars_invoice)
        app.router.add_post("/api/miniapp/payment/sbp", self.sbp_create)
        app.router.add_get("/api/miniapp/payment/sbp/{payment_id}", self.sbp_check)
        app.router.add_post("/api/miniapp/shop/device", self.buy_extra_device)
        app.router.add_delete("/api/miniapp/devices/{device_id}", self.delete_device)
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
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            self.site = None
