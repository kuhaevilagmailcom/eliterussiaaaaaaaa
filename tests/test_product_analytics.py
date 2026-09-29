import asyncio

from analytics import load_business_analytics
from db import Database


def test_product_settings_and_analytics_survive_database_roundtrip(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "product.sqlite3"))
        await db.init()
        await db.ensure_user(1001, "product_user", "Product")

        maintenance = await db.maintenance_state()
        assert maintenance["enabled"] is False

        maintenance = await db.set_maintenance(
            True,
            updated_by=1001,
            message="Тестовые работы",
        )
        assert maintenance["enabled"] is True
        assert maintenance["message"] == "Тестовые работы"

        user = await db.set_preferred_country(1001, "de")
        assert user["preferred_country"] == "de"

        analytics = await load_business_analytics(db)
        assert analytics["total_users"] == 1
        assert analytics["active_subscriptions"] == 0
        assert analytics["revenue"]["day"]["rub"] == 0
        assert analytics["expiring"]["7"] == 0

    asyncio.run(scenario())


def test_support_ticket_stores_selected_server_context(tmp_path):
    async def scenario():
        db = Database(str(tmp_path / "support.sqlite3"))
        await db.init()
        await db.ensure_user(2001, "support_user", "Support")
        ticket = await db.create_support_ticket(
            telegram_id=2001,
            username="support_user",
            first_name="Support",
            message="Не работает VPN",
            server_code="de",
        )
        assert ticket["server_code"] == "de"

    asyncio.run(scenario())
