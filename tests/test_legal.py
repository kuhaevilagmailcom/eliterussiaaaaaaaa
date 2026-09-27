from legal import (
    AGREEMENT_SECTIONS,
    PRIVACY_SECTIONS,
    agreement_html,
    agreement_telegram,
    privacy_html,
)


def test_agreement_is_complete_and_fits_telegram_message():
    telegram = agreement_telegram()
    assert len(AGREEMENT_SECTIONS) == 9
    assert len(telegram) < 4096
    assert "Возвраты" in telegram
    assert "Данные и приватность" in telegram
    assert "mgnvpn.ru" in telegram
    assert "<script" not in agreement_html().lower()


def test_privacy_policy_covers_data_and_user_rights():
    policy = privacy_html()
    assert len(PRIVACY_SECTIONS) == 7
    assert "Какие данные обрабатываются" in policy
    assert "Права пользователя" in policy
    assert "не продаёт персональные данные" in policy
    assert "<script" not in policy.lower()
