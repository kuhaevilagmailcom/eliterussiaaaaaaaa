import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import aiosqlite
import pytest
from aiohttp import ClientSession, web

from config import Config
from db import Database, utcnow, from_iso
from miniapp import MiniAppServer, validate_init_data
from handlers import connection_keyboard
from vpn import H1CloudVpnProvider

TOKEN = '123456:TEST_ONLY'


def signed(user=None, age=0):
    fields = {'auth_date': str(int(time.time()) - age), 'user': json.dumps(user or {'id': 42, 'first_name': 'Test'})}
    check = '\n'.join(f'{k}={v}' for k, v in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.mark.parametrize('raw', ['', 'hash=bad', signed(age=7200), signed(age=-3600), signed({'id': 'abc'}), signed({'id': -1}), signed({'id': True}), signed()+'&user=bad'])
def test_reject_invalid_auth(raw):
    with pytest.raises(web.HTTPUnauthorized):
        validate_init_data(raw, TOKEN)


def test_canonical_config_and_back(monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://attacker.invalid')
    monkeypatch.setenv('VPN_SUB_BASE_URL', 'https://sub.mgnvpn.ru/sub')
    monkeypatch.delenv('ADMIN_IDS', raising=False)
    config = Config.from_env()
    assert config.miniapp_url == 'https://mgnvpn.ru/app'
    assert config.vpn_sub_base_url == 'https://mgnvpn.ru/sub'
    assert config.admin_ids == (8464597898,)
    buttons = connection_keyboard('https://mgnvpn.ru/sub/'+'a'*32).inline_keyboard
    assert len(buttons) == 3
    assert buttons[0][0].url == 'https://mgnvpn.ru/client/happ/'+'a'*32
    assert buttons[2][0].text == 'Назад'
    assert buttons[2][0].icon_custom_emoji_id is None


def test_atomic_payments_and_concurrency(tmp_path):
    async def run():
        db = Database(str(tmp_path/'test.db'))
        await db.init()
        await db.ensure_user(42, None, 'Test')
        await db.create_sbp_payment('p', 'o', 42, '30', 149)
        results = await asyncio.gather(*(db.settle_sbp_payment('p') for _ in range(8)))
        assert results.count(True) == 1
        until = (await db.get_user(42))['subscription_until']
        assert from_iso(until) > utcnow()+timedelta(days=29)
        await db.set_sbp_status('p', 'processing')
        assert (await db.get_sbp_payment('p'))['status'] == 'paid'
        assert not await Database(db.path).settle_sbp_payment('p')
        assert (await db.get_user(42))['subscription_until'] == until
        with pytest.raises(aiosqlite.IntegrityError):
            await db.create_sbp_payment('p', 'o', 42, '30', 149)
        await db.create_sbp_payment('invalid', 'invalid', 42, 'missing-plan', 1)
        with pytest.raises(KeyError):
            await db.settle_sbp_payment('invalid')
        assert (await db.get_sbp_payment('invalid'))['status'] == 'created'
        await db.create_payment_intent(intent_id='intent', buyer_id=42, target_id=42, product_code='7', original_amount_rub=59, discount_amount_rub=0, final_amount_rub=59, currency='XTR', currency_amount=37)
        assert await db.settle_star_payment('charge', 42, 42, '7', 37, 'intent')
        assert not await db.settle_star_payment('charge', 42, 42, '7', 37, 'intent')
        assert not await db.settle_star_payment('another-charge', 42, 42, '7', 37, 'intent')
        assert (await db.get_payment_intent('intent'))['status'] == 'paid'
        await asyncio.gather(*(db.extend_subscription(42, 1, 'Test', 1) for _ in range(5)))
        assert from_iso((await db.get_user(42))['subscription_until']) >= from_iso(until)+timedelta(days=12)
    asyncio.run(run())


def test_http_security_routes_and_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)
    async def run():
        config = replace(Config.from_env(), db_path=str(tmp_path/'http.db'), miniapp_host='127.0.0.1', miniapp_port=0)
        db = Database(config.db_path)
        await db.init()
        user = await db.ensure_user(42, None, 'Test')
        provider = SimpleNamespace(mode_name='h1cloud', service_ready=False, capabilities=H1CloudVpnProvider.capabilities,
                                   fetch_subscription=AsyncMock(return_value=(b'vless://test@example.com:443#test', {})))
        server = MiniAppServer(SimpleNamespace(), config, db, provider)
        await server.start()
        port = server.site._server.sockets[0].getsockname()[1]
        async with ClientSession(base_url=f'http://127.0.0.1:{port}') as client:
            root = await client.get('/')
            app = await client.get('/app')
            assert root.status == app.status == 200
            root_html=await root.text()
            assert 'hero-copy' in root_html
            assert all(f'{price} ₽' in root_html for price in (59,149,349,599,1200))
            assert 'bottomNav' in await app.text()
            assert (await client.get('/api/miniapp/me')).status == 401
            for data in ('[]', 'null', '{', '{"code":12}', '{"code":"'+'x'*65+'"}'):
                response = await client.post('/api/miniapp/promo/redeem', data=data, headers={'X-Telegram-Init-Data': signed()})
                assert response.status == 400
                assert 'message' in await response.json()
                assert 'no-store' in response.headers['Cache-Control']
            oversized = await client.post('/api/miniapp/payment/stars', data='x'*70000, headers={'X-Telegram-Init-Data': signed()})
            assert oversized.status == 413
            token = user['sub_token']
            assert (await client.get('/sub/not-a-token')).status == 404
            assert (await client.get('/sub/'+token)).status == 403
            await db.extend_subscription(42, 7, 'Test', 1)
            server._schedule_subscription_federation_refresh = lambda *args: None
            response = await client.get('/sub/'+token)
            assert response.status == 200
            assert 'no-store' in response.headers['Cache-Control']
            redirect = await client.get('/client/happ/'+token)
            assert redirect.status == 200
            assert 'happ://add/https://mgnvpn.ru/sub/' in await redirect.text()
            assert 'nonce-' in redirect.headers['Content-Security-Policy']
            assert redirect.headers['Referrer-Policy'] == 'no-referrer'
            assert server._external_base_url(None) == 'https://mgnvpn.ru'
            await db.revoke_subscription(42)
            assert (await client.get('/sub/'+token)).status == 403
        await server.close()
    asyncio.run(run())
import logging
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from app import SecretSafeFormatter
from emoji import EmojiBank, EmojiFallbackMiddleware
from handlers import build_router
from payments import get_payment, create_payment, RollyPayError


def test_admin_callbacks_reject_regular_user(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)
    async def run():
        config = replace(Config.from_env(), admin_ids=(1,), db_path=str(tmp_path/'admin.db'))
        db = Database(config.db_path)
        await db.init()
        await db.ensure_user(42, None, 'User')
        router = build_router(config, db, EmojiBank(()), SimpleNamespace())
        checked = []
        for handler in router.callback_query.handlers:
            if handler.callback.__name__.startswith('admin_') or handler.callback.__name__ == 'support_reply':
                callback = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=AsyncMock(), message=None, data='admin:grant:42:365')
                await handler.callback(callback)
                checked.append(handler.callback.__name__)
        assert len(checked) >= 5
        assert (await db.get_user(42))['subscription_until'] is None
    asyncio.run(run())


def test_miniapp_render_has_no_out_of_scope_page_reference():
    source = (__import__('pathlib').Path(__file__).parents[1] / 'miniapp' / 'web' / 'app.js').read_text(encoding='utf-8')
    render_block = source.split('function render(){', 1)[1].split('function go(page){', 1)[0]
    go_block = source.split('function go(page){', 1)[1].split('async function load(', 1)[0]
    assert "if(page==='support')" not in render_block
    assert "if(page==='support')loadSupport();" in go_block


def test_miniapp_state_failure_provisions_in_background(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)

    async def run():
        config = replace(Config.from_env(), db_path=str(tmp_path/'fast-start.db'))
        db = Database(config.db_path)
        await db.init()
        user = await db.ensure_user(42, None, 'Test')
        user = await db.extend_subscription(42, 7, 'Test', 1)
        blocker = asyncio.Event()

        async def provision(_user):
            await blocker.wait()

        provider = SimpleNamespace(
            mode_name='h1cloud', service_ready=True,
            get_state=AsyncMock(side_effect=RuntimeError('offline')),
            provision=AsyncMock(side_effect=provision),
        )
        server = MiniAppServer(SimpleNamespace(), config, db, provider)
        state, ok = await server._load_state(user)
        assert not ok
        assert state.devices == []
        assert 42 in server._reconcile_tasks
        await server.close()

    asyncio.run(run())


def test_admin_users_screen_fits_telegram_caption_and_search_does_not_collide(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)

    async def run():
        config = replace(Config.from_env(), admin_ids=(1,), db_path=str(tmp_path/'users.db'))
        db = Database(config.db_path)
        await db.init()
        for user_id in range(1, 16):
            await db.ensure_user(user_id, f'user_{user_id}', 'Очень длинное имя пользователя')
        bot = SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=77)))
        message = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=1))
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1, username='owner', first_name='Owner'),
            answer=AsyncMock(), message=message, data='admin:users',
        )
        router = build_router(config, db, EmojiBank(()), SimpleNamespace())
        users_handler = next(item for item in router.callback_query.handlers if item.callback.__name__ == 'admin_users_callback')
        search_handler = next(item for item in router.callback_query.handlers if item.callback.__name__ == 'admin_user_search')
        assert (await users_handler.check(SimpleNamespace(data='admin:users')))[0]
        assert (await users_handler.check(SimpleNamespace(data='admin:users:2')))[0]
        assert not (await users_handler.check(SimpleNamespace(data='admin:usersearch')))[0]
        assert (await search_handler.check(SimpleNamespace(data='admin:usersearch')))[0]
        await users_handler.callback(callback)
        caption = bot.send_photo.await_args.kwargs['caption']
        assert len(caption) <= 1000
        assert 'Пользователи' in caption
        assert 'user_1' in caption

    asyncio.run(run())


def test_admin_grant_notifies_recipient_with_devices_and_connect_button(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)

    async def run():
        config = replace(Config.from_env(), admin_ids=(1,), db_path=str(tmp_path/'grant.db'))
        db = Database(config.db_path)
        await db.init()
        await db.ensure_user(1, 'owner', 'Owner')
        await db.ensure_user(42, 'recipient', 'Recipient')
        bot = SimpleNamespace(
            send_message=AsyncMock(),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        )
        message = SimpleNamespace(bot=bot, chat=SimpleNamespace(id=1))
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=1, username='owner', first_name='Owner'),
            answer=AsyncMock(), message=message, data='admin:grant:42:30',
        )
        provider = SimpleNamespace(service_ready=False, provision=AsyncMock())
        router = build_router(config, db, EmojiBank(()), provider)
        handler = next(item for item in router.callback_query.handlers if item.callback.__name__ == 'admin_grant_callback')
        await handler.callback(callback)
        notice = bot.send_message.await_args.kwargs
        assert notice['chat_id'] == 42
        assert 'на 30 дней' in notice['text']
        assert 'до 1' in notice['text']
        assert notice['reply_markup'].inline_keyboard[0][0].callback_data == 'menu:connect'

    asyncio.run(run())


def test_reply_keyboard_navigation_replaces_screen_and_removes_button_message(tmp_path, monkeypatch):
    monkeypatch.setenv('BOT_TOKEN', TOKEN)

    async def run():
        config = replace(Config.from_env(), db_path=str(tmp_path/'reply-navigation.db'))
        db = Database(config.db_path)
        await db.init()
        await db.ensure_user(42, 'user', 'User')
        await db.set_last_menu_message(42, 77)
        bot = SimpleNamespace(
            delete_message=AsyncMock(),
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=88)),
        )
        actor = SimpleNamespace(id=42, username='user', first_name='User')
        message = SimpleNamespace(
            from_user=actor,
            bot=bot,
            chat=SimpleNamespace(id=42),
            message_id=55,
            text='Профиль',
        )
        router = build_router(config, db, EmojiBank(()), SimpleNamespace(service_ready=False))
        handler = next(item for item in router.message.handlers if item.callback.__name__ == 'profile')
        await handler.callback(message)
        deleted_ids = [call.kwargs['message_id'] for call in bot.delete_message.await_args_list]
        assert deleted_ids == [55, 77]
        assert bot.send_photo.await_count == 1
        assert (await db.get_user(42))['last_menu_message_id'] == 88

    asyncio.run(run())


def test_secret_logging_redacts_urls_and_values():
    formatter = SecretSafeFormatter(SimpleNamespace(bot_token='private-value', h1_api_token='secret-provider'))
    record = logging.LogRecord('test', logging.WARNING, '', 1, 'private-value secret-provider https://mgnvpn.ru/sub/private-token /client/happ/another-token', (), None)
    text = formatter.format(record)
    assert all(value not in text for value in ('private-value', 'secret-provider', 'private-token', 'another-token'))


def test_emoji_fallback_retries_once():
    async def run():
        method = SendMessage(chat_id=42, text='<tg-emoji emoji-id="123">x</tg-emoji>', reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Test', callback_data='home', icon_custom_emoji_id='123')]]))
        request = AsyncMock(side_effect=[TelegramBadRequest(method=method, message='CUSTOM_EMOJI_INVALID'), 'ok'])
        assert await EmojiFallbackMiddleware()(request, None, method) == 'ok'
        fallback = request.call_args.args[1]
        assert fallback.text == 'x'
        assert fallback.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id is None
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['timeout', 'list', 'bad_json', 'http_error', 'unsafe_link'])
def test_payment_provider_errors_are_safe(kind, monkeypatch):
    def respond(request):
        if kind == 'timeout':
            raise httpx.ConnectTimeout('contains-secret-url', request=request)
        if kind == 'list':
            return httpx.Response(200, json=[])
        if kind == 'bad_json':
            return httpx.Response(200, text='not-json')
        if kind == 'http_error':
            return httpx.Response(500, text='contains-secret')
        return httpx.Response(200, json={'payment_id':'p','pay_url':'javascript:alert(1)'})
    factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: factory(transport=httpx.MockTransport(respond), **kw))
    config = SimpleNamespace(rollypay_enabled=True, rollypay_api_base='https://payments.invalid', rollypay_api_key='test', rollypay_test_mode=False, rollypay_terminal_id='')
    async def run():
        with pytest.raises(RollyPayError) as error:
            await create_payment(config,'order',Decimal(59),'Test',42)
        assert 'contains-secret' not in str(error.value)
    asyncio.run(run())

@pytest.mark.parametrize('override', [{'amount':'1'}, {'currency':'USD'}, {'order_id':'other'}, {'payment_id':'other'}, {'amount':'sNaN'}])
def test_sbp_rejects_provider_mismatch(tmp_path, monkeypatch, override):
    from aiohttp.test_utils import make_mocked_request
    import miniapp
    monkeypatch.setenv('BOT_TOKEN', TOKEN)
    async def run():
        db = Database(str(tmp_path/'payment.db')); await db.init()
        await db.ensure_user(42,None,'Test')
        await db.create_sbp_payment('p','o',42,'30',149)
        server = MiniAppServer(None, replace(Config.from_env(),db_path=db.path),db,SimpleNamespace(service_ready=False))
        server._auth=AsyncMock(return_value=(42,{},{}))
        remote={'payment_id':'p','order_id':'o','currency':'RUB','amount':'149','status':'paid',**override}
        monkeypatch.setattr(miniapp,'get_payment',AsyncMock(return_value=remote))
        with pytest.raises(web.HTTPConflict):
            await server.sbp_check(make_mocked_request('GET','/',match_info={'payment_id':'p'}))
        assert (await db.get_user(42))['subscription_until'] is None
        assert (await db.get_sbp_payment('p'))['status']=='created'
    asyncio.run(run())


def test_support_validation_and_device_payment_limits(tmp_path):
    async def run():
        db=Database(str(tmp_path/'limits.db')); await db.init()
        await db.ensure_user(42,None,'Test')
        for message in ['', 'x'*3001]:
            with pytest.raises(ValueError):
                await db.create_support_ticket(telegram_id=42,username=None,first_name='Test',message=message)
        ticket=await db.create_support_ticket(telegram_id=42,username=None,first_name='Test',message='x'*3000)
        assert len(ticket['message'])==3000
        for i in range(4):
            assert await db.settle_star_payment('device-'+str(i),42,42,'device',63)
        assert (await db.get_user(42))['max_devices']==5
        with pytest.raises(ValueError):
            await db.settle_star_payment('device-excess',42,42,'device',63)
        assert (await db.get_user(42))['max_devices']==5
    asyncio.run(run())
