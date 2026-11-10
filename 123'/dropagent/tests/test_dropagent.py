"""Tester med mockad AliExpress och Shopify. Inget anropas på riktigt och den riktiga databasen rörs inte.

Kör från mappen dropagent:  python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

_tmp = tempfile.mkdtemp(prefix="dropagent-test-")
os.environ["DROPAGENT_DATA"] = _tmp  # måste sättas innan core.db importeras
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util  # noqa: E402
if importlib.util.find_spec("anthropic") is None:  # testerna anropar aldrig Claude
    sys.modules["anthropic"] = types.SimpleNamespace()

from core import aliexpress, autopilot, db, marketing, portfolio, scheduler, settings, shopify, stock  # noqa: E402


def ae_response(stock_units, sku_attr="14:29#White", price="70.00", status="onSelling"):
    return {"aliexpress_ds_product_get_response": {"result": {
        "ae_item_base_info_dto": {"product_status_type": status},
        "ae_item_sku_info_dtos": {"ae_item_sku_info_d_t_o": [
            {"sku_attr": sku_attr, "sku_available_stock": stock_units, "offer_sale_price": price},
            {"sku_attr": "14:193#Black", "sku_available_stock": 500, "offer_sale_price": "75.00"},
        ]}}}}


class Base(unittest.TestCase):
    def setUp(self):
        if os.path.exists(db.DB_PATH):
            os.remove(db.DB_PATH)
        db.init()
        for k, v in {"shopify_domain": "test.myshopify.com", "shopify_token": "shpat_test", "aliexpress_app_key": "1",
                     "aliexpress_app_secret": "s", "aliexpress_access_token": "t"}.items():
            settings.set(k, v)
        self.set_status = mock.patch.object(shopify, "set_status").start()
        self.delete = mock.patch.object(shopify, "delete_product").start()
        self.call = mock.patch.object(aliexpress, "call").start()
        self.addCleanup(mock.patch.stopall)

    def product(self, name="Laptopställ", ae_id="100", sku="14:29#White", **extra):
        return db.insert("products", {"created_at": db.now(), "name": name, "status": "publicerad",
                                      "shopify_id": f"gid://shopify/Product/{ae_id}", "ae_product_id": ae_id,
                                      "ae_sku_attr": sku, "ae_unit_price": 70, **extra})

    def stock_of(self, pid):
        return json.loads(db.one("SELECT stock_json FROM products WHERE id = ?", (pid,))["stock_json"])


class StockTest(Base):
    def test_in_stock_changes_nothing(self):
        pid = self.product()
        self.call.return_value = ae_response(120)
        self.assertEqual(stock.run(), {"checked": 1, "hidden": 0, "restored": 0, "unsure": 0})
        self.set_status.assert_not_called()
        self.assertEqual(self.stock_of(pid)["stock"], 120)
        self.assertTrue(settings.get("stock_last_check"))

    def test_out_of_stock_hides_product(self):
        pid = self.product()
        self.call.return_value = ae_response(0)
        self.assertEqual(stock.run()["hidden"], 1)
        self.set_status.assert_called_once_with("gid://shopify/Product/100", "DRAFT")
        self.assertTrue(self.stock_of(pid)["hidden_at"])
        stock.run()  # nästa kontroll: redan dold, inget nytt anrop
        self.assertEqual(self.set_status.call_count, 1)

    def test_only_our_variant_counts(self):
        self.product()
        self.call.return_value = ae_response(1)  # vår variant nästan slut, en annan har 500
        self.assertEqual(stock.run()["hidden"], 1)

    def test_listing_offline_hides_product(self):
        self.product()
        self.call.return_value = ae_response(300, status="offline")
        self.assertEqual(stock.run()["hidden"], 1)

    def test_comes_back_when_restocked(self):
        pid = self.product()
        self.call.return_value = ae_response(0)
        stock.run()
        self.call.return_value = ae_response(5)  # lite påfyllt men under gränsen: fortfarande dold
        self.assertEqual(stock.run()["restored"], 0)
        self.call.return_value = ae_response(200)
        self.assertEqual(stock.run()["restored"], 1)
        self.set_status.assert_called_with("gid://shopify/Product/100", "ACTIVE")
        self.assertFalse(self.stock_of(pid)["hidden_at"])

    def test_api_down_never_hides(self):
        self.product()
        self.product("Lampa", "200")
        self.call.side_effect = aliexpress.AliExpressError("timeout")
        for _ in range(5):
            self.assertEqual(stock.run()["hidden"], 0)
        self.set_status.assert_not_called()

    def test_missing_variant_hides_after_three_checks(self):
        gone = self.product(sku="14:999#Pink")
        self.product("Lampa", "200")
        self.call.return_value = ae_response(100, sku_attr="14:999#Pink")
        stock.run()  # varianten finns
        self.call.return_value = ae_response(100)  # säljaren har tagit bort varianten
        self.assertEqual(stock.run()["hidden"], 0)
        self.assertEqual(stock.run()["hidden"], 0)
        self.assertEqual(stock.run()["hidden"], 1)
        self.set_status.assert_called_once_with("gid://shopify/Product/100", "DRAFT")
        self.assertTrue(self.stock_of(gone)["hidden_at"])

    def test_never_seen_variant_is_not_hidden(self):
        self.product(sku="handskriven-fel")  # koppling ifylld för hand som inte stämmer
        self.product("Lampa", "200")
        self.call.return_value = ae_response(100)
        for _ in range(6):
            self.assertEqual(stock.run()["hidden"], 0)
        self.assertEqual(db.one("SELECT COUNT(*) c FROM activity WHERE text LIKE '%aldrig hittats%'")["c"], 1)

    def test_delete_mode(self):
        pid = self.product()
        settings.set("stock_out_action", "radera")
        self.call.return_value = ae_response(0)
        stock.run()
        self.delete.assert_called_once_with("gid://shopify/Product/100")
        p = db.one("SELECT * FROM products WHERE id = ?", (pid,))
        self.assertEqual(p["shopify_id"], "")
        self.assertTrue(p["retired_at"])
        self.assertEqual(stock.watched(), [])

    def test_shopify_error_is_retried_next_time(self):
        pid = self.product()
        self.call.return_value = ae_response(0)
        self.set_status.side_effect = shopify.ShopifyError("403")
        self.assertEqual(stock.run()["hidden"], 0)
        self.assertFalse(self.stock_of(pid).get("hidden_at"))
        self.set_status.side_effect = None
        self.assertEqual(stock.run()["hidden"], 1)

    def test_hidden_a_week_frees_the_slot(self):
        pid = self.product()
        old = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
        db.update("products", pid, {"stock_json": json.dumps({"hidden_at": old})})
        self.call.return_value = ae_response(0)
        stock.run()
        self.assertTrue(db.one("SELECT retired_at FROM products WHERE id = ?", (pid,))["retired_at"])
        self.assertEqual(portfolio.active_products(), [])

    def test_price_rise_warns_once(self):
        pid = self.product()
        self.call.return_value = ae_response(100, price="95.00")
        stock.run()
        stock.run()
        self.assertIn("stigit", self.stock_of(pid)["price_warning"])
        self.assertEqual(db.one("SELECT COUNT(*) c FROM activity WHERE text LIKE '%Inköpspriset%'")["c"], 1)

    def test_not_ready_without_account(self):
        settings.set("aliexpress_access_token", "")
        self.product()
        self.assertEqual(stock.run()["checked"], 0)
        self.call.assert_not_called()


class SchedulerTest(Base):
    def test_autopilot_plan(self):
        settings.set("anthropic_api_key", "sk-test")
        settings.set("autopilot_enabled", "1")
        settings.set("autopilot_hour", "8")
        settings.set("autopilot_interval_days", "7")
        settings.set("autopilot_last_run", "2026-11-08 20:00:00")
        at = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M")
        self.assertEqual(scheduler.autopilot_plan(at("2026-11-10 07:30")), (False, False))  # för tidigt
        self.assertEqual(scheduler.autopilot_plan(at("2026-11-10 09:00")), (True, False))   # underhåll, ingen sökning
        self.assertEqual(scheduler.autopilot_plan(at("2026-11-15 09:00")), (True, True))    # 7 dagar: sök nya produkter
        settings.set("autopilot_last_upkeep", "2026-11-15 09:00:01")
        self.assertEqual(scheduler.autopilot_plan(at("2026-11-15 12:00")), (False, False))  # redan kört i dag
        settings.set("autopilot_enabled", "0")
        self.assertEqual(scheduler.autopilot_plan(at("2026-11-16 09:00")), (False, False))

    def test_stock_due(self):
        self.assertTrue(scheduler.stock_due())  # aldrig körd
        settings.set("stock_last_check", db.now())
        self.assertFalse(scheduler.stock_due())
        settings.set("stock_last_check", (datetime.now() - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S"))
        self.assertTrue(scheduler.stock_due())


class MarketingTest(Base):
    KIT = {"facebook": "Ordning på skrivbordet!", "instagram": "Snyggt och enkelt.", "tiktok": "Före och efter",
           "pinterest_titel": "Laptopställ", "pinterest_text": "Hopfällbart ställ.", "kort": "Fri frakt", "hashtags": "#hemmakontor"}

    def test_share_links(self):
        p = {"content": self.KIT, "store_url": "https://butik.se/products/stall", "images": ["https://img/1.jpg"]}
        s = marketing.share_links(p)
        self.assertIn("https://butik.se/products/stall?utm_source=facebook", s["facebook"]["text"])
        self.assertTrue(s["facebook"]["share"].startswith("https://www.facebook.com/sharer/sharer.php?u=https%3A%2F%2Fbutik.se"))
        self.assertIn("media=https%3A%2F%2Fimg%2F1.jpg", s["pinterest"]["share"])
        self.assertIn("#hemmakontor", s["instagram"]["text"])
        self.assertEqual(marketing.share_links({"content": self.KIT, "store_url": ""})["facebook"]["share"], "")
        self.assertEqual(marketing.share_links({"content": {"inlagg": []}}), {})  # gammalt format (filmmanus)

    def test_weekly_due(self):
        fresh = self.product("A", "1", content_json=json.dumps({**self.KIT, "created_at": db.now()}))
        old = self.product("B", "2", content_json=json.dumps({**self.KIT, "created_at": "2026-01-01 10:00:00"}))
        legacy = self.product("C", "3", content_json=json.dumps({"inlagg": [], "created_at": db.now()}))
        empty = self.product("D", "4")
        due = marketing.weekly_content_due()
        self.assertNotIn(fresh, due)
        self.assertEqual(sorted(due), sorted([old, legacy, empty]))


class AutopilotTest(Base):
    def test_duplicates(self):
        known = {"hopfällbart laptopställ i aluminium", "dimbar usb-skrivbordslampa"}
        self.assertTrue(autopilot.is_duplicate("Dimbar USB-skrivbordslampa", known))
        self.assertTrue(autopilot.is_duplicate("Laptopställ hopfällbart aluminium", known))
        self.assertFalse(autopilot.is_duplicate("Kabelhållare i silikon", known))
        self.assertFalse(autopilot.is_duplicate("Skrivbordslampa med klämma", known))

    def test_run_without_scout_skips_product_search(self):
        with mock.patch.object(autopilot, "upkeep") as up, mock.patch.object(autopilot, "find_products") as find:
            autopilot.run(scout=False)
            find.assert_not_called()
            up.assert_called_once()
            autopilot.run()
            find.assert_called_once()


class PagesTest(Base):
    def test_pages_render(self):
        import app as webapp
        webapp.app.testing = True
        pid = self.product(listing_json=json.dumps({"titel": "Laptopställ", "pris_sek": 249, "beskrivning_html": "<p>Bra</p>"}),
                           content_json=json.dumps({**MarketingTest.KIT, "created_at": db.now()}),
                           store_url="https://butik.se/products/stall", image_urls="https://img/1.jpg")
        self.call.return_value = ae_response(80)
        stock.run()
        self.product("Slut produkt", "300", stock_json=json.dumps({"hidden_at": db.now(), "hidden_reason": "0 kvar", "checked_at": db.now()}))
        with mock.patch.object(marketing, "channel_status", return_value={}), \
                mock.patch.object(shopify, "list_orders", return_value=[]):
            c = webapp.app.test_client()
            for url in ("/", "/autopilot", "/synlighet", "/produkter", f"/produkter/{pid}", "/ordrar", "/installningar", "/trender", "/kundtjanst"):
                r = c.get(url)
                self.assertEqual(r.status_code, 200, url)
            html = c.get("/autopilot").get_data(as_text=True)
            self.assertIn("80 st", html)
            self.assertIn("Slut – dold sedan", html)
            html = c.get("/synlighet").get_data(as_text=True)
            self.assertIn("Dela på Facebook", html)
            self.assertIn("Det här sköts automatiskt", c.get("/").get_data(as_text=True))
            self.assertIn("stock_out_action", c.get("/installningar").get_data(as_text=True))
            self.assertEqual(c.post("/lager/kolla").status_code, 400)  # utan CSRF-token


if __name__ == "__main__":
    unittest.main()
