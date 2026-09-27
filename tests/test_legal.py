from legal import AGREEMENT_SECTIONS, agreement_html, agreement_telegram


def test_agreement_is_complete_and_fits_telegram_message():
    telegram = agreement_telegram()
    assert len(AGREEMENT_SECTIONS) == 9
    assert len(telegram) < 4096
    assert "Возвраты" in telegram
    assert "Данные и приватность" in telegram
    assert "mgnvpn.ru" in telegram
    assert "<script" not in agreement_html().lower()
