"""VLM 菜單辨識三個端點的整合測試。

前四條取自 partner/main 的 tests/test_back_integrations.py（只取 vision 部分，
LINE bot 那幾條需要 line_bot.py，本分支還沒有）。

後三條是本分支補的：
- 對方的測試把 _register_vision_menu 全部 mock 掉，真正寫檔那段沒被驗證過，
  而那個函式在搬過來時被改寫過，所以補一條真的落地。
- 舊的 /api/upload-menu-photo 必須維持可用，前端還在用。
"""

import asyncio
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import back  # noqa: E402


def jpeg_upload(name: str = "menu.jpg") -> UploadFile:
    return UploadFile(
        io.BytesIO(b"jpeg-bytes"),
        filename=name,
        headers=Headers({"content-type": "image/jpeg"}),
    )


class VisionEndpointTests(unittest.TestCase):
    def setUp(self):
        back.PENDING_ANALYSES.clear()

    def tearDown(self):
        back.PENDING_ANALYSES.clear()

    def test_vision_upload_stores_pending_and_does_not_persist(self):
        result = {
            "restaurant_name": "測試餐廳",
            "source_type": "menu",
            "confidence": 0.9,
            "categories": [{"name": "主餐", "items": [{"name": "牛肉麵", "price": 180}]}],
            "warnings": [],
        }
        with mock.patch.object(back, "analyze_menu_image", return_value=result) as analyze, \
                mock.patch.object(back, "_register_vision_menu") as register:
            response = asyncio.run(back.create_menu_from_photo("測試餐廳", jpeg_upload()))

        self.assertEqual(1, response["itemCount"])
        self.assertTrue(response["analysisId"])
        register.assert_not_called()
        self.assertEqual("測試餐廳", analyze.call_args.args[2])

    def test_vision_confirm_persists_pending_analysis(self):
        result = {
            "restaurant_name": "測試餐廳",
            "detected_restaurant_name": "測試餐廳",
            "categories": [{"name": "主餐", "items": [{"name": "牛肉麵", "price": 180}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
            "identityConflict": False,
        }
        analysis_id = back._store_pending_analysis(result)
        with mock.patch.object(
            back,
            "_register_vision_menu",
            return_value={"restaurantName": "測試餐廳", "itemCount": 1, "categories": ["主餐"]},
        ) as register:
            response = back.confirm_menu_from_photo(
                analysis_id, back.VisionConfirmReq(restaurant_name="測試餐廳")
            )

        self.assertTrue(response["success"])
        register.assert_called_once()

    def test_vision_confirm_requires_explicit_conflict_acceptance(self):
        """使用者說的店名跟圖片辨識出來的不同時，不能默默存檔。"""
        result = {
            "restaurant_name": "東海愛將",
            "detected_restaurant_name": "犇頂牛排",
            "categories": [{"name": "排餐", "items": [{"name": "犇頂牛排", "price": 230}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
            "identityConflict": True,
        }
        analysis_id = back._store_pending_analysis(result)

        with self.assertRaises(HTTPException) as caught:
            back.confirm_menu_from_photo(analysis_id, back.VisionConfirmReq(restaurant_name="東海愛將"))

        self.assertEqual(409, caught.exception.status_code)
        # 被擋下來之後結果要留在待確認區，使用者才能接受衝突後再送一次
        self.assertIn(analysis_id, back.PENDING_ANALYSES)

    def test_pending_analysis_can_be_human_corrected(self):
        result = {
            "restaurant_name": "便當店",
            "categories": [{"name": "飯類", "items": [{"name": "豬肝飯", "price": 80}]}],
            "quality": {"score": 0.7, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
        }
        analysis_id = back._store_pending_analysis(result)

        response = back.correct_menu_from_photo(
            analysis_id,
            back.VisionCorrectionReq(instruction="把豬肝飯改成豬腳飯"),
        )

        self.assertTrue(response["success"])
        self.assertEqual("豬腳飯", response["categories"][0]["items"][0]["name"])
        self.assertTrue(response["quality"]["humanReviewed"])
        self.assertIn(analysis_id, back.PENDING_ANALYSES)

    # --- 本分支補的 ---

    def test_register_vision_menu_actually_writes_file_and_switches_restaurant(self):
        """真的跑一次落地，不 mock。對方的測試沒有覆蓋這條路徑。"""
        result = {
            "restaurant_name": "煙霧測試餐廳",
            "categories": [{"name": "主餐", "items": [{"name": "牛肉麵", "price": 180}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
        }
        prev_active = back.ACTIVE_RESTAURANT
        prev_menu = back.menu
        with tempfile.TemporaryDirectory() as tmp:
            try:
                with mock.patch.object(back, "PROJECT_ROOT", tmp):
                    summary = back._register_vision_menu(result)
                    written = Path(tmp) / "menu_煙霧測試餐廳.json"

                    self.assertTrue(written.exists(), "應該寫出菜單檔")
                    saved = json.loads(written.read_text(encoding="utf-8"))
                    self.assertEqual(2, saved["schemaVersion"])
                    self.assertEqual("user_photo_vlm", saved["source"])
                    self.assertEqual("牛肉麵", saved["menu_items"][0]["name"])
                    # 沒有留下 .tmp 殘骸
                    self.assertEqual([], list(Path(tmp).glob("*.tmp")))

                self.assertEqual("煙霧測試餐廳", summary["restaurantName"])
                self.assertEqual(1, summary["itemCount"])
                self.assertEqual("煙霧測試餐廳", back.ACTIVE_RESTAURANT)
            finally:
                back.ACTIVE_RESTAURANT = prev_active
                back.menu = prev_menu
                back.RESTAURANT_MENUS.pop("煙霧測試餐廳", None)

    def test_expired_pending_analysis_is_rejected(self):
        analysis_id = back._store_pending_analysis({"restaurant_name": "過期店", "categories": []})
        back.PENDING_ANALYSES[analysis_id]["expires_at"] = time.time() - 1

        with self.assertRaises(ValueError):
            back._take_pending_analysis(analysis_id)

    def test_legacy_upload_menu_photo_endpoint_still_exists(self):
        """前端已改走 /api/menu/vision，舊端點暫時留著當退路。

        兩條路徑並存是刻意的過渡狀態：舊端點辨識完直接覆寫菜單、沒有確認步驟，
        等新流程實際用過確認沒問題再移除。
        """
        paths = {route.path for route in back.app.routes if hasattr(route, "path")}
        self.assertIn("/api/upload-menu-photo", paths)
        self.assertIn("/api/menu/vision", paths)


if __name__ == "__main__":
    unittest.main()
