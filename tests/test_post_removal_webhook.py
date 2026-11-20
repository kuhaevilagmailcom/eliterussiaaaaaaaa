import hashlib
import hmac
import json
import time
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import miniapp
from miniapp import MiniAppServer


def signed_headers(secret: str, body: bytes) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Timestamp": timestamp,
        "X-Signature": signature,
    }


def test_paid_post_removal_webhook_verifies_and_deletes(monkeypatch):
    async def run():
        secret = "webhook-secret"
        config = SimpleNamespace(
        rollypay_signing_secret=secret,
        rollypay_test_mode=False,
        post_removal_bot_token="123:test",
    )
        db = SimpleNamespace(
        post_removal_webhook_processed=AsyncMock(return_value=False),
        mark_post_removal_webhook=AsyncMock(),
    )
        server = object.__new__(MiniAppServer)
        server.config = config
        server.db = db
        server._delete_shop_post_message = AsyncMock()
        monkeypatch.setattr(
        miniapp,
        "get_payment",
        AsyncMock(return_value={
            "payment_id": "pay-1", "order_id": "postrm-7-abc",
            "status": "paid", "amount": "100.00", "payment_currency": "RUB",
        }),
    )
        event = {
        "event_type": "payment.paid", "payment_id": "pay-1",
        "order_id": "postrm-7-abc", "status": "paid",
        "amount": "100.00", "currency": "RUB", "test": False,
        "metadata": {
            "action": "post_removal", "chat_id": "-100123",
            "message_ids": [51, 52],
        },
    }
        body = json.dumps(event, separators=(",", ":")).encode()
        app = web.Application()
        app.router.add_post("/webhooks/rollypay", server.rollypay_webhook)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post(
                "/webhooks/rollypay", data=body, headers=signed_headers(secret, body)
            )
            assert response.status == 200
            assert (await response.json())["deleted"] == 2
        finally:
            await client.close()
        assert server._delete_shop_post_message.await_count == 2
        db.mark_post_removal_webhook.assert_awaited_once_with(
            "pay-1", "postrm-7-abc", "paid"
        )
    asyncio.run(run())


def test_post_removal_webhook_rejects_bad_signature():
    async def run():
        server = object.__new__(MiniAppServer)
        server.config = SimpleNamespace(rollypay_signing_secret="right")
        request = SimpleNamespace(
            read=AsyncMock(return_value=b"{}"),
            headers={"X-Timestamp": str(int(time.time())), "X-Signature": "bad"},
        )
        with pytest.raises(web.HTTPForbidden):
            await server.rollypay_webhook(request)
    asyncio.run(run())
