from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import httpx

from config import Config


class RollyPayError(RuntimeError):
    pass


def _error_message(response: httpx.Response) -> str:
    try:
        data = response.json()
        return str(
            data.get("detail")
            or data.get("message")
            or data.get("error")
            or "request failed"
        )[:300]
    except ValueError:
        return "request failed"


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

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{config.rollypay_api_base}/api/v1/payments",
            json=payload,
            headers=headers,
        )

    if response.is_error:
        raise RollyPayError(
            f"RollyPay returned HTTP {response.status_code}: {_error_message(response)}"
        )

    try:
        result = response.json()
    except ValueError as exc:
        raise RollyPayError("RollyPay returned invalid JSON") from exc

    if not result.get("payment_id") or not result.get("pay_url"):
        raise RollyPayError("RollyPay response has no payment link")
    return result


async def get_payment(config: Config, payment_id: str) -> dict:
    if not config.rollypay_enabled:
        raise RollyPayError("RollyPay is not configured")

    headers = {
        "X-API-Key": config.rollypay_api_key,
        "X-Nonce": str(uuid4()),
    }

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{config.rollypay_api_base}/api/v1/payments/{payment_id}",
            headers=headers,
        )

    if response.is_error:
        raise RollyPayError(
            f"RollyPay returned HTTP {response.status_code}: {_error_message(response)}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise RollyPayError("RollyPay returned invalid JSON") from exc
