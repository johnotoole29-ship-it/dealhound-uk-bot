import os
import tempfile
import unittest

import bot


def sample_item(item_id="v1|123|0", price=100.0):
    return {
        "item_id": item_id,
        "title": "Test product",
        "price": f"£{price:,.2f}",
        "price_value": price,
        "shipping": "Free",
        "total": f"£{price:,.2f}",
        "condition": "New",
        "url": "https://www.ebay.co.uk/itm/123?campid=123",
        "image_url": "https://i.ebayimg.com/images/test.jpg",
    }


class PersistentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        bot.DATA_DIR = self.temp_dir.name
        bot.DATABASE_PATH = os.path.join(self.temp_dir.name, "dealhound.db")
        bot.init_database()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_migration_keeps_favorites_and_creates_alerts(self):
        status, favorite_id = bot.save_favorite(1, sample_item())
        self.assertEqual(status, "saved")
        bot.init_database()
        self.assertEqual(len(bot.load_favorites(1)), 1)
        self.assertIsNotNone(favorite_id)
        status, alert_id = bot.save_price_alert(1, 1, sample_item())
        self.assertEqual(status, "saved")
        self.assertIsNotNone(alert_id)

    def test_alert_deduplication_and_reactivation_baseline(self):
        status, alert_id = bot.save_price_alert(1, 1, sample_item(price=100))
        self.assertEqual(status, "saved")
        status, duplicate_id = bot.save_price_alert(1, 1, sample_item(price=90))
        self.assertEqual(status, "existing")
        self.assertEqual(alert_id, duplicate_id)
        self.assertEqual(bot.load_price_alerts(1)[0]["lowest_price"], 100)
        self.assertTrue(bot.delete_price_alert(1, alert_id))
        status, reactivated_id = bot.save_price_alert(1, 1, sample_item(price=90))
        self.assertEqual(status, "saved")
        self.assertEqual(alert_id, reactivated_id)
        self.assertEqual(bot.load_price_alerts(1)[0]["lowest_price"], 90)

    def test_new_low_only_notifies_once(self):
        _, alert_id = bot.save_price_alert(1, 1, sample_item(price=100))
        url = sample_item()["url"]
        self.assertFalse(bot.record_alert_check(alert_id, 105, url))
        self.assertTrue(bot.record_alert_check(alert_id, 90, url))
        self.assertFalse(bot.record_alert_check(alert_id, 90, url))
        self.assertFalse(bot.record_alert_check(alert_id, 95, url))
        self.assertTrue(bot.record_alert_check(alert_id, 80, url))

    def test_alert_removal_is_scoped_to_owner(self):
        _, alert_id = bot.save_price_alert(1, 1, sample_item())
        self.assertFalse(bot.delete_price_alert(2, alert_id))
        self.assertEqual(len(bot.load_price_alerts(1)), 1)
        self.assertTrue(bot.delete_price_alert(1, alert_id))

    def test_clear_only_affects_requesting_user(self):
        bot.save_price_alert(1, 1, sample_item("v1|1|0"))
        bot.save_price_alert(1, 1, sample_item("v1|2|0"))
        bot.save_price_alert(2, 2, sample_item("v1|3|0"))
        self.assertEqual(bot.clear_price_alerts(1), 2)
        self.assertEqual(bot.load_price_alerts(1), [])
        self.assertEqual(len(bot.load_price_alerts(2)), 1)

    def test_per_user_limit(self):
        original_limit = bot.MAX_PRICE_ALERTS_PER_USER
        bot.MAX_PRICE_ALERTS_PER_USER = 2
        try:
            self.assertEqual(
                bot.save_price_alert(1, 1, sample_item("v1|1|0"))[0], "saved"
            )
            self.assertEqual(
                bot.save_price_alert(1, 1, sample_item("v1|2|0"))[0], "saved"
            )
            self.assertEqual(
                bot.save_price_alert(1, 1, sample_item("v1|3|0"))[0], "limit"
            )
        finally:
            bot.MAX_PRICE_ALERTS_PER_USER = original_limit

    def test_invalid_or_non_ebay_alert_is_rejected(self):
        item = sample_item()
        item["url"] = "https://example.com/not-ebay"
        with self.assertRaises(ValueError):
            bot.save_price_alert(1, 1, item)


if __name__ == "__main__":
    unittest.main()
