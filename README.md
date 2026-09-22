# MGN VPN

Telegram-бот для продажи и управления VPN-подписками.

## Что уже есть

- профиль пользователя: баланс, подписка, трафик, сервер, лимит устройств;
- пробная подписка;
- кнопка подключения и выдача subscription URL;
- управление устройствами;
- покупка тарифов через Telegram Stars;
- загрузка custom emoji из наборов:
  - `CryptoGIFTPODARKI`
  - `TgAndroidIcons`
  - `progressBarEmoji`
- SQLite без лишней нагрузки: WAL, индексы, короткие запросы;
- два режима VPN:
  - `demo` — интерфейс можно проверить без VPN-панели;
  - `webhook` — подключение к своей панели через простой HTTP API.

## Запуск

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env
python app.py
```

В `.env` обязательно укажите `BOT_TOKEN` и `ADMIN_IDS`.

## Контракт VPN API для режима webhook

Бот ожидает:

### Создать/обновить подписку

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
  "server": "Finland",
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

## Важно

Не коммитьте `.env`, токен Telegram и токены VPN-панели в GitHub.
