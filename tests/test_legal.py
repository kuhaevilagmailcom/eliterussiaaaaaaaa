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
    assert "розыгрыш" in policy.lower()
    assert "Telegram ID" in policy
    assert "Telegram-профиль" in policy
    assert "<script" not in policy.lower()


def test_public_offer_and_refund_texts_are_safe_and_informative():
    from legal import OFFER_SECTIONS, REFUNDS_SECTIONS, offer_html, refunds_html

    offer = offer_html()
    refunds = refunds_html()
    assert len(OFFER_SECTIONS) == 6
    assert len(REFUNDS_SECTIONS) == 3
    assert "RollyPay" in offer
    assert "Тарифы и акцепт" in offer
    assert "Юридическое имя" in offer  # Must be filled before merging.
    assert "Telegram" in refunds
    assert "возврат" in refunds.lower()
    assert "<script" not in offer.lower()
    assert "<script" not in refunds.lower()


def test_public_site_provides_rollypay_links_and_telegram_support():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "miniapp" / "web" / "site"
    index = (root / "index.html").read_text(encoding="utf-8")
    offer = (root / "offer.html").read_text(encoding="utf-8")
    privacy = (root / "privacy.html").read_text(encoding="utf-8")
    refund = (root / "refunds.html").read_text(encoding="utf-8")

    assert 'href="/offer"' in index
    assert 'href="/refunds"' in index
    assert 'href="/privacy"' in index
    assert 'data-support-link' in index
    assert "https://rollypay.io/legal/payer-agreement" in index
    assert "https://rollypay.io/privacy/" in offer
    assert "https://rollypay.io/privacy/" in privacy
    assert 'data-support-link' in refund
