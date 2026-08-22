import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import back  # noqa: E402


class ReviewEndpointTests(unittest.TestCase):
    def setUp(self):
        self.restaurant_name = "評價端點測試餐廳"
        self.old_menu = back.RESTAURANT_MENUS.get(self.restaurant_name)
        back.RESTAURANT_MENUS[self.restaurant_name] = {"restaurants": {}}

    def tearDown(self):
        if self.old_menu is None:
            back.RESTAURANT_MENUS.pop(self.restaurant_name, None)
        else:
            back.RESTAURANT_MENUS[self.restaurant_name] = self.old_menu

    def test_cache_endpoint_reads_selected_restaurant(self):
        report = {"success": True, "restaurantName": self.restaurant_name}
        with mock.patch.object(back, "load_review_cache", return_value=report) as load_cache:
            response = back.get_restaurant_review(self.restaurant_name)
        self.assertEqual(response, report)
        load_cache.assert_called_once_with(back.PROJECT_ROOT, self.restaurant_name)

    def test_identify_endpoint_returns_branch_candidates(self):
        candidates = [{
            "officialName": f"{self.restaurant_name} 東海店",
            "address": "台中市",
            "confidence": 92,
        }]
        with mock.patch.object(back, "identify_restaurant_candidates", return_value=candidates):
            response = asyncio.run(back.identify_restaurant_review(
                back.RestaurantReviewIdentifyReq(restaurant_name=self.restaurant_name)
            ))
        self.assertTrue(response["success"])
        self.assertEqual(response["candidates"], candidates)

    def test_refresh_endpoint_forwards_confirmed_identity(self):
        identity = {
            "officialName": f"{self.restaurant_name} 東海店",
            "address": "台中市",
        }
        report = {
            "success": True,
            "restaurantName": self.restaurant_name,
            "restaurantIdentity": identity,
        }
        source_urls = ["https://www.dcard.tw/f/food/p/123"]
        with mock.patch.object(back, "refresh_restaurant_reviews", return_value=report) as refresh:
            response = asyncio.run(back.refresh_restaurant_review(
                back.RestaurantReviewRefreshReq(
                    restaurant_name=self.restaurant_name,
                    restaurant_identity=identity,
                    source_urls=source_urls,
                )
            ))
        self.assertEqual(response, report)
        refresh.assert_called_once_with(
            back.PROJECT_ROOT, self.restaurant_name, identity, source_urls
        )

    def test_refresh_endpoint_accepts_restaurant_not_in_menu_library(self):
        arbitrary_name = "臨時輸入的公開餐廳"
        self.assertNotIn(arbitrary_name, back.RESTAURANT_MENUS)
        report = {"success": True, "restaurantName": arbitrary_name}
        with mock.patch.object(back, "refresh_restaurant_reviews", return_value=report) as refresh:
            response = asyncio.run(back.refresh_restaurant_review(
                back.RestaurantReviewRefreshReq(restaurant_name=arbitrary_name)
            ))

        self.assertEqual(response, report)
        refresh.assert_called_once_with(back.PROJECT_ROOT, arbitrary_name, None, None)


if __name__ == "__main__":
    unittest.main()
