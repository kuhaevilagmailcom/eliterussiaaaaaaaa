# MGN VPN

Telegram-бот для продажи и управления VPN-подписками.

## Что уже есть

- профиль пользователя: подписка, срок, трафик, сервер и устройства;
- одноразовый пробный период;
- кнопка подключения с индивидуальной subscription-ссылкой;
- управление зарегистрированными устройствами;
- тарифы на 30 / 90 / 365 дней;
- оплата через Telegram Stars;
- админ-команды `/stats` и `/grant TELEGRAM_ID DAYS`;
- custom emoji из наборов:
  - `CryptoGIFTPODARKI`;
  - `TgAndroidIcons`;
  - `progressBarEmoji`;
- SQLite с WAL и короткими запросами;
- Dockerfile;
- GitHub Actions с проверкой Python.

## Режимы VPN

### `demo`

Можно проверить бот без VPN-панели.

### `3xui`

Нативная работа с актуальным API 3x-ui:

- создание клиента;
- обновление срока и лимита трафика;
- лимит устройств через HWID;
- чтение использованного трафика;
- просмотр устройств;
- удаление отдельного устройства;
- привязка клиента к нужным inbound;
- Telegram ID сохраняется в клиенте 3x-ui.

Для этого режима нужны:

```env
VPN_MODE=3xui
VPN_SERVER_NAME=MGN VPN

XUI_URL=https://panel.example.com
XUI_TOKEN=...
XUI_INBOUND_IDS=1
XUI_SUBSCRIPTION_TEMPLATE=https://vpn.example.com:2096/subpath/{sub_id}
XUI_VERIFY_SSL=true
```

`XUI_TOKEN` — API token панели. Не кладите его в GitHub.

`XUI_INBOUND_IDS` — ID inbound через запятую, например:

```env
XUI_INBOUND_IDS=1,2,5
```

`XUI_SUBSCRIPTION_TEMPLATE` должен быть публичной HTTPS-ссылкой 3x-ui subscription server и обязательно содержать `{sub_id}`.

### `webhook`

Оставлен универсальный режим для собственной VPN-панели.

## Запуск

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

Создайте `.env` из `.env.example`, затем:

```bash
python app.py
```

Минимально:

```env
BOT_TOKEN=...
ADMIN_IDS=...
VPN_MODE=demo
```

## Docker

```bash
docker build -t mgn-vpn .
docker run --env-file .env -v mgn-vpn-data:/app/data mgn-vpn
```

Если хотите хранить SQLite в volume:

```env
DB_PATH=/app/data/mgn_vpn.sqlite3
```

## Контракт режима webhook

### Создать или обновить подписку

`POST {VPN_API_URL}/subscriptions`

```json
{
  "telegram_id": 123456789,
  "token": "user-token",
  "expires_at": "2026-10-22T12:00:00+00:00",
  "traffic_limit_gb": 100,
  "max_devices": 2
}
```

Ответ:

```json
{
  "subscription_url": "https://example.com/sub/...",
  "server": "MGN VPN",
  "traffic_used_gb": 0,
  "traffic_limit_gb": 100,
  "devices": []
}
```

### Получить состояние

`GET {VPN_API_URL}/subscriptions/{telegram_id}`

### Удалить устройство

`DELETE {VPN_API_URL}/subscriptions/{telegram_id}/devices/{device_id}`

Авторизация: `Authorization: Bearer <VPN_API_TOKEN>`.

## Безопасность

Никогда не коммитьте:

- `.env`;
- Telegram bot token;
- `XUI_TOKEN`;
- логин/пароль панели;
- VPN API token.

Если секрет случайно попал в чат, лог или репозиторий, перевыпустите его перед боевым запуском.
