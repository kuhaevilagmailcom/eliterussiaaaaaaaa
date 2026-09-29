from giveaway import render_giveaway_post


def test_giveaway_does_not_publish_private_first_name_without_username():
    giveaway = {
        "text_html": "Тестовый розыгрыш",
        "winners_count": 1,
        "prize_days": 30,
        "status": "finished",
    }
    text = render_giveaway_post(
        giveaway,
        participant_count=1,
        winners=[{"telegram_id": 42, "username": "", "first_name": "Private Name"}],
    )
    assert "Private Name" not in text
    assert "Победитель #1" in text


def test_cancelled_giveaway_has_no_winner_claim():
    giveaway = {
        "text_html": "Тестовый розыгрыш",
        "winners_count": 2,
        "prize_days": 90,
        "status": "cancelled",
    }
    text = render_giveaway_post(giveaway, participant_count=10)
    assert "Розыгрыш отменён" in text
    assert "Победители:" not in text
