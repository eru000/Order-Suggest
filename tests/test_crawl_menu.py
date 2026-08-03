import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for path in (str(ROOT), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

import crawl_menu  # noqa: E402
import back  # noqa: E402
from session_store import SessionStore  # noqa: E402

SESSION_ID = "test_session"


def restaurant(name="測試餐廳", count=8, error=""):
    return crawl_menu.Restaurant(
        name=name,
        menu_items=[crawl_menu.MenuItem(name=f"測試品項{i}", price=f"NT$ {50 + i}") for i in range(count)],
        error=error,
    )


class CrawlerParsingTests(unittest.TestCase):
    def test_extract_json_object_accepts_wrapped_model_output(self):
        parsed = crawl_menu._extract_json_object('結果如下：\n{"menu_items":[{"name":"牛肉麵","price":"120"}]}\n完成')
        self.assertEqual(parsed["menu_items"][0]["name"], "牛肉麵")

    def test_normalize_vision_items_removes_duplicates_and_cleans_price(self):
        items = crawl_menu._normalize_vision_menu_items([
            {"name": "牛肉麵", "price": "$120.00"},
            {"dish": "牛肉麵", "amount": "130"},
            {"dish": "水餃", "amount": "價格未標示"},
            {"name": "x", "price": 10},
        ])
        self.assertEqual([(item.name, item.price) for item in items], [
            ("牛肉麵", "120"),
            ("水餃", "價格未標示"),
        ])
        self.assertFalse(crawl_menu.is_usable_menu_result(items))
        self.assertTrue(crawl_menu.is_usable_menu_result(items * 4))

    def test_public_menu_page_text_extracts_adjacent_item_prices(self):
        items = crawl_menu.extract_menu_items_from_page_text(
            "人氣精選\n原汁牛肉麵\n$160\n • 97% (41)\n介紹文字\n水餃10顆\nNT$ 70\n"
        )
        self.assertEqual([(item.name, item.price) for item in items], [
            ("原汁牛肉麵", "160"),
            ("水餃10顆", "70"),
        ])

    def test_google_result_url_only_accepts_public_menu_domains(self):
        wrapped = "https://www.google.com/url?q=https%3A%2F%2Fwww.ubereats.com%2Ftw%2Fstore%2Fabc&sa=U"
        self.assertEqual(
            crawl_menu._unwrap_google_result_url(wrapped),
            "https://www.ubereats.com/tw/store/abc",
        )
        self.assertEqual(crawl_menu._unwrap_google_result_url("https://example.com/menu"), "")


class CrawlBackendTests(unittest.TestCase):
    def setUp(self):
        self.old_restaurants = dict(back.RESTAURANT_MENUS)
        self.sessions_patch = mock.patch.object(back, "SESSIONS", SessionStore())
        self.sessions_patch.start()

    def tearDown(self):
        self.sessions_patch.stop()
        back.RESTAURANT_MENUS.clear()
        back.RESTAURANT_MENUS.update(self.old_restaurants)
        if back.CRAWLER_LOCK.locked():
            back.CRAWLER_LOCK.release()

    def test_price_normalization(self):
        self.assertEqual(back._normalize_crawled_price("$1,280.00"), 1280.0)
        self.assertEqual(back._normalize_crawled_price("NT$ 95"), 95.0)
        self.assertIsNone(back._normalize_crawled_price("價格未標示"))
        self.assertIsNone(back._normalize_crawled_price("時價"))
        self.assertTrue(back._is_non_food_crawled_item("塑膠袋 Plastic Bag"))
        self.assertTrue(back._is_non_food_crawled_item("甜心卡送禮套組"))
        self.assertFalse(back._is_non_food_crawled_item("麥克鷄塊分享餐"))

    def test_register_crawled_menu_persists_atomically(self):
        result = back._register_crawled_menu(restaurant())
        self.assertEqual(result["itemCount"], 8)
        menu = back.RESTAURANT_MENUS["測試餐廳"]
        first = menu["restaurants"]["測試餐廳"]["categories"]["全部菜色"]["items"][0]
        self.assertEqual(first["price"], 50.0)
        self.assertIn("semantic", first)
        self.assertIn("測試餐廳", back.RESTAURANT_MENUS)

    def test_database_failure_does_not_change_runtime_state(self):
        with mock.patch.object(
            back.RESTAURANT_MENUS.repository, "save", side_effect=OSError("database unavailable")
        ):
            with self.assertRaises(OSError):
                back._register_crawled_menu(restaurant())

        self.assertEqual(back.RESTAURANT_MENUS, self.old_restaurants)

    def test_crawl_endpoint_success_uses_fixed_response_shape(self):
        registered = {
            "restaurantName": "測試餐廳",
            "itemCount": 8,
            "menuItems": [{"dish": "牛肉麵", "price": 120.0}],
        }
        with mock.patch.object(back, "CRAWLER_ENABLED", True), mock.patch.object(
            back, "CRAWLER_AVAILABLE", True
        ), mock.patch.object(back, "_run_crawler", return_value=restaurant()), mock.patch.object(
            back, "_register_crawled_menu", return_value=registered
        ):
            response = asyncio.run(back.crawl_restaurant_menu(
                back.CrawlMenuReq(restaurantName="  測試餐廳  ", sessionId=SESSION_ID)
            ))

        self.assertTrue(response["success"])
        self.assertEqual(set(response), {"success", "message", "restaurantName", "itemCount", "menuItems", "errorCode"})
        self.assertEqual(response["restaurantName"], "測試餐廳")
        self.assertEqual(
            "測試餐廳", back.SESSIONS.get(SESSION_ID).active_restaurant
        )

    def test_crawl_endpoint_reports_captcha_without_mutation(self):
        captcha = restaurant(error="google_verification_required")
        captcha.menu_items = []
        with mock.patch.object(back, "CRAWLER_ENABLED", True), mock.patch.object(
            back, "CRAWLER_AVAILABLE", True
        ), mock.patch.object(back, "_run_crawler", return_value=captcha):
            response = asyncio.run(back.crawl_restaurant_menu(
                back.CrawlMenuReq(restaurantName="測試餐廳", sessionId=SESSION_ID)
            ))
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(payload["errorCode"], "google_verification_required")

    def test_crawl_endpoint_rejects_parallel_request(self):
        back.CRAWLER_LOCK.acquire()
        with mock.patch.object(back, "CRAWLER_ENABLED", True), mock.patch.object(back, "CRAWLER_AVAILABLE", True):
            response = asyncio.run(back.crawl_restaurant_menu(
                back.CrawlMenuReq(restaurantName="測試餐廳", sessionId=SESSION_ID)
            ))
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["errorCode"], "crawler_busy")


if __name__ == "__main__":
    unittest.main()
