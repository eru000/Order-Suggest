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
from menu_ingestion_service import MenuIngestionService  # noqa: E402

SESSION_ID = "test_session"


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
            response = asyncio.run(back.create_menu_from_photo("測試餐廳", SESSION_ID, jpeg_upload()))

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
        analysis_id = back._store_pending_analysis(result, SESSION_ID)
        with mock.patch.object(
            back,
            "_register_vision_menu",
            return_value={"restaurantName": "測試餐廳", "itemCount": 1, "categories": ["主餐"]},
        ) as register:
            response = back.confirm_menu_from_photo(
                analysis_id, back.VisionConfirmReq(restaurant_name="測試餐廳", sessionId=SESSION_ID)
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
        analysis_id = back._store_pending_analysis(result, SESSION_ID)

        with self.assertRaises(HTTPException) as caught:
            back.confirm_menu_from_photo(
                analysis_id,
                back.VisionConfirmReq(restaurant_name="東海愛將", sessionId=SESSION_ID),
            )

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
        analysis_id = back._store_pending_analysis(result, SESSION_ID)

        response = back.correct_menu_from_photo(
            analysis_id,
            back.VisionCorrectionReq(instruction="把豬肝飯改成豬腳飯", sessionId=SESSION_ID),
        )

        self.assertTrue(response["success"])
        self.assertEqual("豬腳飯", response["categories"][0]["items"][0]["name"])
        self.assertTrue(response["quality"]["humanReviewed"])
        self.assertIn(analysis_id, back.PENDING_ANALYSES)

    # --- 本分支補的 ---

    def test_register_vision_menu_persists_to_catalog(self):
        """Confirmed menus are versioned in the repository instead of runtime JSON files."""
        result = {
            "restaurant_name": "煙霧測試餐廳",
            "categories": [{"name": "主餐", "items": [{"name": "牛肉麵", "price": 180}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
        }
        try:
            summary = back._register_vision_menu(result)
            persisted = back.RESTAURANT_MENUS["煙霧測試餐廳"]
            item = persisted["restaurants"]["煙霧測試餐廳"]["categories"]["主餐"]["items"][0]
            self.assertEqual("牛肉麵", item["name"])
            self.assertIn("semantic", item)
            self.assertEqual("煙霧測試餐廳", summary["restaurantName"])
            self.assertEqual(1, summary["itemCount"])
        finally:
            back.RESTAURANT_MENUS.pop("煙霧測試餐廳", None)

    def test_photo_without_a_readable_name_still_lands(self):
        """照片上沒印店名時不該逼使用者先想一個——辨識結果本身是好的。

        用獨立的 dict 當 catalog：RESTAURANT_MENUS 是寫穿到專案資料庫的，
        拿它測命名會把測試資料留在正式庫裡，也會被前一次殘留影響編號。
        """
        catalog: dict = {}
        service = MenuIngestionService(catalog)
        summary = service.register_vision({
            "restaurant_name": "",
            "detected_restaurant_name": "",
            "categories": [{"name": "主餐", "items": [{"name": "乾麵", "price": 45}]}],
        })
        self.assertEqual("未命名菜單", summary["restaurantName"])
        self.assertEqual(1, summary["itemCount"])
        self.assertIn("未命名菜單", catalog)

    def test_detected_name_wins_over_the_unnamed_fallback(self):
        catalog: dict = {}
        summary = MenuIngestionService(catalog).register_vision({
            "restaurant_name": "",
            "detected_restaurant_name": "向宏魯肉飯",
            "categories": [{"name": "飯類", "items": [{"name": "魯肉飯", "price": 30}]}],
        })
        self.assertEqual("向宏魯肉飯", summary["restaurantName"])

    def test_second_unnamed_menu_does_not_overwrite_the_first(self):
        """店名是 catalog 的 key，兩張都沒名字時後者不能蓋掉前者。"""
        catalog: dict = {}
        service = MenuIngestionService(catalog)
        first = service.register_vision({
            "categories": [{"name": "主餐", "items": [{"name": "乾麵", "price": 45}]}],
        })
        second = service.register_vision({
            "categories": [{"name": "主餐", "items": [{"name": "陽春麵", "price": 40}]}],
        })
        self.assertEqual("未命名菜單", first["restaurantName"])
        self.assertEqual("未命名菜單 2", second["restaurantName"])
        self.assertEqual({"未命名菜單", "未命名菜單 2"}, set(catalog))

    def test_confirm_accepts_an_empty_restaurant_name(self):
        """端點層也要放行，否則前端留空一樣會拿到 422。"""
        result = {
            "restaurant_name": "",
            "detected_restaurant_name": "",
            "categories": [{"name": "主餐", "items": [{"name": "乾麵", "price": 45}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
            "identityConflict": False,
        }
        analysis_id = back._store_pending_analysis(result, SESSION_ID)
        # 這條走真的 catalog（寫穿到資料庫），所以前後都要把殘留清掉，
        # 否則編號會從「未命名菜單 2」開始，測試互相污染。
        self._drop_unnamed_menus()
        try:
            response = back.confirm_menu_from_photo(
                analysis_id, back.VisionConfirmReq(sessionId=SESSION_ID)
            )
            self.assertTrue(response["success"])
            self.assertEqual("未命名菜單", response["restaurantName"])
        finally:
            self._drop_unnamed_menus()

    @staticmethod
    def _drop_unnamed_menus() -> None:
        for key in [k for k in list(back.RESTAURANT_MENUS) if k.startswith("未命名菜單")]:
            back.RESTAURANT_MENUS.pop(key, None)

    def test_failed_confirm_keeps_the_analysis_retryable(self):
        """存檔失敗不能把辨識結果吃掉——重跑一次要 22 秒與 6 次 API 呼叫。

        _take_pending_analysis 是「取走」，原本只有 409 會放回去，其他失敗
        都讓結果永久消失，使用者只能重新上傳整張照片。
        """
        result = {
            "restaurant_name": "測試餐廳",
            "categories": [{"name": "主餐", "items": [{"name": "牛肉麵", "price": 180}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
            "identityConflict": False,
        }
        analysis_id = back._store_pending_analysis(result, SESSION_ID)

        with mock.patch.object(back, "_register_vision_menu", side_effect=ValueError("寫入失敗")):
            with self.assertRaises(HTTPException) as caught:
                back.confirm_menu_from_photo(
                    analysis_id, back.VisionConfirmReq(sessionId=SESSION_ID)
                )
        self.assertEqual(422, caught.exception.status_code)

        # 結果還在，使用者可以直接重送而不用重新辨識
        recovered = back._take_pending_analysis(analysis_id, SESSION_ID)
        self.assertEqual("測試餐廳", recovered["restaurant_name"])

    def test_conflict_409_also_keeps_the_analysis_retryable(self):
        result = {
            "restaurant_name": "東海愛將",
            "detected_restaurant_name": "犇頂牛排",
            "categories": [{"name": "排餐", "items": [{"name": "犇頂牛排", "price": 230}]}],
            "quality": {"score": 0.9, "priceCoverage": 1, "itemCount": 1},
            "conflicts": [],
            "identityConflict": True,
        }
        analysis_id = back._store_pending_analysis(result, SESSION_ID)
        with self.assertRaises(HTTPException) as caught:
            back.confirm_menu_from_photo(
                analysis_id, back.VisionConfirmReq(restaurant_name="東海愛將", sessionId=SESSION_ID)
            )
        self.assertEqual(409, caught.exception.status_code)

        try:
            response = back.confirm_menu_from_photo(
                analysis_id,
                back.VisionConfirmReq(
                    restaurant_name="東海愛將", accept_conflicts=True, sessionId=SESSION_ID
                ),
            )
            self.assertTrue(response["success"])
        finally:
            back.RESTAURANT_MENUS.pop("東海愛將", None)

    def test_vision_routes_bind_to_their_intended_handlers(self):
        """路由要綁在對的函式上。

        直接呼叫函式的測試看不到這一層：在 @app.post 與路由函式之間插入一個
        helper，裝飾器就會套到 helper 上，所有測試照樣全綠，但實際打 HTTP
        會拿到「Field required: query session_id」這種完全無關的 422。
        """
        expected = {
            "/api/menu/vision": "create_menu_from_photo",
            "/api/menu/vision/{analysis_id}/confirm": "confirm_menu_from_photo",
            "/api/menu/vision/{analysis_id}/correct": "correct_menu_from_photo",
        }
        actual = {
            route.path: route.endpoint.__name__
            for route in back.app.routes
            if getattr(route, "path", None) in expected and hasattr(route, "endpoint")
        }
        self.assertEqual(expected, actual)

    def test_expired_pending_analysis_is_rejected(self):
        analysis_id = back._store_pending_analysis(
            {"restaurant_name": "過期店", "categories": []}, SESSION_ID
        )
        back.PENDING_ANALYSES[analysis_id]["expires_at"] = time.time() - 1

        with self.assertRaises(ValueError):
            back._take_pending_analysis(analysis_id, SESSION_ID)

    def test_pending_analysis_cannot_be_used_by_another_session(self):
        analysis_id = back._store_pending_analysis(
            {"restaurant_name": "測試店", "categories": []}, SESSION_ID
        )
        with self.assertRaises(ValueError):
            back._take_pending_analysis(analysis_id, "other_session")

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
