from handlers import admin_inline_button, blue_inline_button


def test_admin_buttons_keep_regular_unicode_emoji():
    button = admin_inline_button(
        "👥 Пользователи",
        callback_data="admin:users",
    )
    assert button.text == "👥 Пользователи"
    assert button.icon_custom_emoji_id is None


def test_admin_pagination_arrow_is_not_stripped():
    button = blue_inline_button(
        "Дальше ➡️",
        callback_data="admin:users:1",
    )
    assert button.text == "Дальше ➡️"
    assert button.icon_custom_emoji_id is None


def test_non_premium_button_keeps_regular_emoji():
    button = blue_inline_button(
        "❌ Отмена",
        callback_data="support:cancel",
        premium_icon=False,
    )
    assert button.text == "❌ Отмена"
