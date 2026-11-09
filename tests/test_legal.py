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


def test_public_site_links_use_exact_telegraph_urls():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "miniapp" / "web" / "site"
    home = (root / "index.html").read_text(encoding="utf-8")
    assert 'href="https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"' in home
    assert 'href="https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"' in home
    assert 'data-support-link' in home
    assert 'id="renew-submit"' in home


def test_legacy_legal_routes_redirect_to_user_provided_documents():
    import asyncio

    import pytest
    from aiohttp import web

    from miniapp import MiniAppServer

    server = object.__new__(MiniAppServer)
    for handler, url in [
        (server.agreement, "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"),
        (server.privacy, "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"),
    ]:
        with pytest.raises(web.HTTPFound) as exc:
            asyncio.run(handler(None))
        assert exc.value.location == url
