# MGN VPN

Telegram-бот для продажи и управления VPN-подписками.

## Готово

- голубое постоянное меню: Подключить VPN, Профиль, Информация, Купить VPN, Устройства, Друзья, Поддержка;
- автоудаление старого меню и сообщений навигации;
- custom emoji из наборов:
  - CryptoGIFTPODARKI
  - TgAndroidIcons
  - progressBarEmoji
- одноразовый пробный доступ на 1 устройство после проверки подписки на Telegram-канал;
- платные подписки до 5 устройств;
- персональная ссылка подключения;
- список устройств и отключение отдельных устройств;
- реферальная ссылка вида `?start=ref_ID` и счётчик приглашённых;
- Telegram Stars;
- СБП через RollyPay по той же схеме, что в LIMYZINOV SHOP: создание платежа, ссылка на оплату и ручная проверка статуса;
- SQLite WAL, защита от повторного зачисления оплаты;
- режимы `demo`, `3xui`, `webhook`;
- BotHost entrypoint: `main.py`.

## BotHost

Главный файл:

```
main.py
```

Основные переменные находятся в `.env`.

Для пробной подписки бот проверяет подписку на канал:

```env
TRIAL_CHANNEL_USERNAME=@mgnvpnn
TRIAL_CHANNEL_URL=https://t.me/mgnvpnn
```

Чтобы проверка подписки работала надёжно, добавьте бота администратором канала.

Для СБП нужны те же значения RollyPay, что используются в LIMYZINOV SHOP:

```env
ROLLYPAY_API_BASE=https://api.rollypay.io
ROLLYPAY_TERMINAL_ID=
ROLLYPAY_API_KEY=
ROLLYPAY_SIGNING_SECRET=
ROLLYPAY_TEST_MODE=false
```

Без `ROLLYPAY_API_KEY` кнопка СБП остаётся в интерфейсе, но создание платежа заблокировано.

## 3x-ui

Для реального VPN вместо demo:

```env
VPN_MODE=3xui
XUI_URL=https://panel.example.com
XUI_TOKEN=...
XUI_INBOUND_IDS=1
XUI_SUBSCRIPTION_TEMPLATE=https://vpn.example.com/sub/{sub_id}
XUI_VERIFY_SSL=true
```

В 3x-ui лимит трафика создаётся без ограничения. Пользователю в интерфейсе показывается только лимит устройств.

## Админ

```
/admin
/stats
/user TELEGRAM_ID
/grant TELEGRAM_ID DAYS
```
