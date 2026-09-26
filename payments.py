from __future__ import annotations

from decimal import Decimal
from uuid import uuid4
from urllib.parse import quote, urlsplit

import httpx

from config import Config


class RollyPayError(RuntimeError):
    pass


async def _request(config: Config, method: str, path: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(7, connect=3)) as client:
            response = await client.request(method, f"{config.rollypay_api_base}{path}", **kwargs)
    except httpx.RequestError as exc:
        raise RollyPayError("Payment provider unavailable") from exc
    if response.is_error:
        raise RollyPayError(f"Payment provider HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError as exc:
        raise RollyPayError("Payment provider returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RollyPayError("Payment provider returned invalid object")
    return result


async def create_payment(
    config: Config,
    order_id: str,
    amount: Decimal,
    description: str,
    user_id: int,
) -> dict:
    if not config.rollypay_enabled:
        raise RollyPayError("RollyPay is not configured")

    payload = {
        "amount": f"{amount:.2f}",
        "payment_currency": "RUB",
        "order_id": order_id,
        "description": description[:255],
        "customer_id": str(user_id),
        "metadata": {"telegram_user_id": str(user_id)},
        "test": config.rollypay_test_mode,
    }
    if config.rollypay_terminal_id:
        payload["terminal_id"] = config.rollypay_terminal_id

    headers = {
        "X-API-Key": config.rollypay_api_key,
        "X-Nonce": str(uuid4()),
        "Content-Type": "application/json",
    }

    result = await _request(config, "POST", "/api/v1/payments", json=payload, headers=headers)
    pay_url = result.get("pay_url")
    if not isinstance(result.get("payment_id"), str) or not isinstance(pay_url, str):
        raise RollyPayError("Payment provider returned invalid payment link")
    parsed = urlsplit(pay_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RollyPayError("Payment provider returned unsafe payment link")
    return result


async def get_payment(config: Config, payment_id: str) -> dict:
    if not config.rollypay_enabled:
        raise RollyPayError("RollyPay is not configured")

    headers = {
        "X-API-Key": config.rollypay_api_key,
        "X-Nonce": str(uuid4()),
    }

    if not payment_id or len(payment_id) > 200:
        raise RollyPayError("Invalid payment ID")
    return await _request(config, "GET", f"/api/v1/payments/{quote(payment_id, safe='')}", headers=headers)
