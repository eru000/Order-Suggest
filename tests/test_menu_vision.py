import json
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import menu_vision  # noqa: E402


class MenuVisionTests(unittest.TestCase):
    def test_normalizes_categories_prices_and_duplicates(self):
        result = menu_vision.normalize_vision_result(
            {
                "restaurant_name": "照片店名",
                "source_type": "menu",
                "confidence": 1.4,
                "categories": [
                    {"name": "主餐", "items": [
                        {"name": "牛肉麵", "price": "NT$ 180"},
                        {"name": "牛肉麵", "price": 999},
                        {"name": "滷肉飯", "price": "時價"},
                    ]},
                ],
            },
            "使用者店名",
        )
        self.assertEqual(result["restaurant_name"], "使用者店名")
        self.assertEqual(result["confidence"], 1.0)
        self.assertEqual(result["categories"][0]["items"], [
            {"name": "牛肉麵", "price": 180.0},
            {"name": "滷肉飯", "price": None},
        ])

    def test_accepts_nested_array_shape_from_model(self):
        """模型無視 schema 回巢狀陣列時不能整批丟掉。

        實測 llama4scout 讀雙欄菜單時，右欄兩個切塊都回這個格式。舊版在
        `isinstance(raw_category, dict)` 就 continue 掉，導致 24 項的菜單
        只剩 14 項，而且 warnings 完全沒有提示。
        """
        result = menu_vision.normalize_vision_result(
            {"categories": [["麵類", [["乾麵", 45], ["大乾麵", "NT$55"], ["時價麵", None]]]]}
        )
        self.assertEqual(result["categories"][0]["name"], "麵類")
        self.assertEqual(result["categories"][0]["items"], [
            {"name": "乾麵", "price": 45.0},
            {"name": "大乾麵", "price": 55.0},
            {"name": "時價麵", "price": None},
        ])

    def test_analyze_encodes_image_and_parses_fenced_json(self):
        captured = {}

        def fake_vision(prompt, image_url, timeout):
            captured.update(prompt=prompt, image_url=image_url, timeout=timeout)
            return "```json\n" + json.dumps({
                "restaurant_name": "測試餐廳",
                "source_type": "dish_display",
                "confidence": 0.88,
                "categories": [{"name": "現場菜色", "items": [{"name": "炒高麗菜", "price": None}]}],
                "warnings": ["價格未顯示"],
            }, ensure_ascii=False) + "\n```"

        result = menu_vision.analyze_menu_image(b"fake-jpeg", "image/jpeg", vision_func=fake_vision)
        self.assertTrue(captured["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(result["restaurant_name"], "測試餐廳")
        self.assertEqual(result["categories"][0]["items"][0]["name"], "炒高麗菜")

    def test_rejects_non_image_and_empty_result(self):
        with self.assertRaisesRegex(ValueError, "僅支援"):
            menu_vision.analyze_menu_image(b"x", "text/plain", vision_func=lambda *args, **kwargs: "{}")
        with self.assertRaisesRegex(ValueError, "沒有辨識到"):
            menu_vision.analyze_menu_image(b"x", "image/png", vision_func=lambda *args, **kwargs: "{}")

    def test_accepts_common_alternate_model_schemas(self):
        dict_categories = menu_vision.normalize_vision_result({
            "categories": {
                "飯類": [{"item_name": "雞腿飯", "amount": "$120"}],
                "飲料": ["紅茶 $30"],
            }
        })
        self.assertEqual(dict_categories["categories"][0]["items"][0]["name"], "雞腿飯")
        self.assertEqual(dict_categories["categories"][1]["items"][0]["price"], 30.0)

        top_level_list = menu_vision.normalize_vision_result([{"dish": "牛肉湯", "cost": 100}])
        self.assertEqual(top_level_list["categories"][0]["items"][0]["price"], 100.0)

    def test_retries_with_simpler_schema_when_first_pass_is_empty(self):
        responses = iter([
            '{"categories":[]}',
            '[{"name":"鍋燒意麵","price":90}]',
        ])

        def fake_vision(*args, **kwargs):
            return next(responses)

        result = menu_vision.analyze_menu_image(b"image", "image/jpeg", "小店", vision_func=fake_vision)
        self.assertEqual(result["restaurant_name"], "小店")
        self.assertEqual(result["categories"][0]["items"][0]["name"], "鍋燒意麵")

    def test_second_pass_verification_replaces_inaccurate_draft(self):
        responses = iter([
            '{"restaurant_name":"麥當勞","categories":[{"name":"主餐","items":[{"name":"大麥克克","price":99}]}]}',
            '{"restaurant_name":"麥當勞","categories":[{"name":"主餐","items":[{"name":"大麥克","price":80},{"name":"麥香雞","price":49}]}]}',
        ])

        result = menu_vision.analyze_menu_image(
            b"image",
            "image/jpeg",
            vision_func=lambda *args, **kwargs: next(responses),
        )
        self.assertEqual(
            [item["name"] for item in result["categories"][0]["items"]],
            ["大麥克", "麥香雞"],
        )

    def test_persisted_document_retains_categories(self):
        document = menu_vision.to_persisted_document({
            "restaurant_name": "店",
            "categories": [{"name": "飲料", "items": [{"name": "紅茶", "price": 30.0}]}],
        })
        self.assertEqual(document["menu_items"][0]["category"], "飲料")
        self.assertEqual(document["schemaVersion"], 2)
        self.assertTrue(document["confirmedAt"])

    def test_exif_applied_and_large_image_is_one_region_by_default(self):
        """切塊預設關閉，所以大圖也只回一塊。EXIF 轉正仍要生效。"""
        image = Image.new("RGB", (2000, 1800), "white")
        exif = Image.Exif()
        exif[274] = 1
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif)
        _, regions, size = menu_vision.prepare_image_regions(buffer.getvalue())
        self.assertEqual(max(size), menu_vision.TARGET_LONG_SIDE)
        self.assertEqual(size[0] / size[1], 2000 / 1800)   # 長寬比不變
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["id"], "full")
        self.assertEqual(regions[0]["box"], [0, 0, size[0], size[1]])

    def test_tiling_still_available_behind_the_flag(self):
        """切塊沒有刪掉，只是預設不走。VISION_TILES=1 要能拿回 2x2 重疊切塊。"""
        image = Image.new("RGB", (2000, 1800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        with mock.patch.dict(os.environ, {"VISION_TILES": "1"}):
            _, regions, size = menu_vision.prepare_image_regions(buffer.getvalue())
        self.assertEqual(len(regions), 4)
        self.assertEqual(regions[0]["box"][0:2], [0, 0])
        self.assertEqual(regions[3]["box"][2:4], list(size))
        # 相鄰兩塊要重疊，招牌橫跨中線時才不會被切斷
        self.assertLess(regions[1]["box"][0], regions[0]["box"][2])

    def test_every_image_is_normalised_to_the_same_long_side(self):
        """小圖放大、大圖縮小，都收斂到 TARGET_LONG_SIDE。

        2600 是量出來的：guoshao_6col（原圖 910px）正規化到 2000 時價格正確率
        54.5%，到 2600 變成 100%。內插沒有增加資訊，變好是因為文字在模型固定
        的圖片 token 預算裡佔到更多 token。
        """
        image = Image.new("RGB", (841, 607), "white")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        _, regions, size = menu_vision.prepare_image_regions(buffer.getvalue())
        self.assertEqual(max(size), menu_vision.TARGET_LONG_SIDE)
        self.assertEqual(len(regions), 1)

        big = Image.new("RGB", (4032, 3024), "white")
        buf2 = io.BytesIO()
        big.save(buf2, "JPEG")
        _, _, big_size = menu_vision.prepare_image_regions(buf2.getvalue())
        self.assertEqual(max(big_size), menu_vision.TARGET_LONG_SIDE)


    def test_dense_image_is_read_in_a_single_call(self):
        """整條流程只打一次 API，而且送的是整張圖。

        這裡曾經釘的是「總覽 → 4 塊並行 → 校對」六次呼叫。四個 eval 案例實測
        那套架構是在用呼叫次數補償解析度不足，換成單次呼叫後召回與精確持平、
        價格正確率反而從 86.1% 升到 96.3%。
        """
        image = Image.new("RGB", (2000, 1800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        calls = []

        def fake_vision(prompt, image_url, model=None, timeout=0, temperature=None):
            calls.append((prompt, image_url, model, temperature))
            return json.dumps({
                "restaurant_name": "犇頂牛排",
                "categories": [{"title": "排餐", "items": [
                    {"dish": "犇頂牛排", "price": 230},
                    {"dish": "菲力牛排", "price": 360},
                ]}],
            }, ensure_ascii=False)

        result = menu_vision.analyze_menu_image(
            buffer.getvalue(), "image/jpeg", "東海愛將", vision_func=fake_vision
        )

        self.assertEqual(len(calls), 1)
        self.assertIsInstance(calls[0][1], str)
        self.assertEqual(calls[0][2], menu_vision.DEFAULT_OCR_MODEL)
        self.assertEqual(calls[0][3], 0)
        self.assertIn("這是整張菜單的完整照片", calls[0][0])
        self.assertEqual(result["detected_restaurant_name"], "犇頂牛排")
        self.assertTrue(result["identityConflict"])
        self.assertEqual(result["quality"]["priceCoverage"], 1.0)
        self.assertEqual(result["conflicts"], [])

    def test_falls_back_to_verify_model_when_ocr_model_is_unavailable(self):
        """主要 OCR 模型掛掉要能降級，而且不能假裝成高信心結果。"""
        image = Image.new("RGB", (900, 800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        seen = []

        def fake_vision(prompt, image_url, model=None, timeout=0, temperature=None):
            seen.append(model)
            if len(seen) == 1:
                raise RuntimeError("model API request failed: HTTP 500")
            return json.dumps({
                "restaurant_name": "斗六門當歸鴨",
                "categories": [{"title": "主食", "items": [{"dish": "鴨肉飯", "price": 40}]}],
            }, ensure_ascii=False)

        with mock.patch.dict(os.environ, {"VISION_MODEL": "broken-model",
                                          "VISION_VERIFY_MODEL": "backup-model"}):
            result = menu_vision.analyze_menu_image(
                buffer.getvalue(), "image/jpeg", vision_func=fake_vision
            )

        self.assertEqual(seen, ["broken-model", "backup-model"])
        self.assertTrue(result["quality"]["modelFallback"])
        self.assertLessEqual(result["quality"]["score"], 0.7)
        self.assertEqual(result["detected_restaurant_name"], "斗六門當歸鴨")


    def test_human_can_correct_ocr_name_and_price_before_confirmation(self):
        result = {
            "categories": [{"name": "飯類", "items": [
                {"name": "豬肝飯", "price": 80},
                {"name": "雞腿飯", "price": None},
            ]}],
            "quality": {"itemCount": 2, "priceCoverage": 0.5},
            "conflicts": [{"candidates": ["豬肝飯", "豬腳飯"], "resolved": False}],
        }
        correction = menu_vision.apply_manual_correction(result, "把豬肝飯改成豬腳飯")
        self.assertIn("豬腳飯", correction["message"])
        self.assertEqual(result["categories"][0]["items"][0]["name"], "豬腳飯")
        self.assertTrue(result["conflicts"][0]["resolved"])
        menu_vision.apply_manual_correction(result, "把雞腿飯價格改成90")
        self.assertEqual(result["categories"][0]["items"][1]["price"], 90)
        self.assertEqual(result["quality"]["priceCoverage"], 1.0)
        self.assertTrue(result["quality"]["humanReviewed"])
        persisted = menu_vision.to_persisted_document(result)
        self.assertEqual(len(persisted["manualCorrections"]), 2)

    def test_human_can_correct_by_preview_number(self):
        result = {"categories": [{"name": "飯類", "items": [
            {"name": "排骨飯", "price": 90}, {"name": "豬肝飯", "price": 80}
        ]}]}
        preview = menu_vision.format_menu_preview(result)
        self.assertIn("2. [飯類] 豬肝飯", preview)
        menu_vision.apply_manual_correction(result, "改 2 菜名 豬腳飯")
        self.assertEqual(result["categories"][0]["items"][1]["name"], "豬腳飯")
        menu_vision.apply_manual_correction(result, "改 2 價格 95")
        self.assertEqual(result["categories"][0]["items"][1]["price"], 95)

    def test_natural_correction_with_plain_change_word_is_detected(self):
        self.assertTrue(menu_vision.looks_like_manual_correction("特製炒醬麵 改特製炸醬麵"))
        result = {"categories": [{"name": "麵類", "items": [{"name": "特製炒醬麵", "price": 80}]}]}
        menu_vision.apply_manual_correction(result, "特製炒醬麵 改特製炸醬麵")
        self.assertEqual(result["categories"][0]["items"][0]["name"], "特製炸醬麵")

    def test_unique_fuzzy_ocr_name_can_be_corrected_without_number(self):
        result = {"categories": [{"name": "麵類", "items": [
            {"name": "特製炒將麵", "price": 80},
            {"name": "牛肉湯麵", "price": 100},
        ]}]}
        correction = menu_vision.apply_manual_correction(result, "把特製炒醬麵改成特製炸醬麵")
        self.assertEqual(result["categories"][0]["items"][0]["name"], "特製炸醬麵")
        self.assertIn("近似品名", correction["message"])

    def test_unknown_manual_correction_does_not_mutate_menu(self):
        result = {"categories": [{"name": "飯類", "items": [{"name": "排骨飯", "price": 90}]}]}
        with self.assertRaisesRegex(ValueError, "找不到品項"):
            menu_vision.apply_manual_correction(result, "把豬肝飯改成豬腳飯")
        self.assertEqual(result["categories"][0]["items"][0]["name"], "排骨飯")


if __name__ == "__main__":
    unittest.main()


class ThinkingModelResponseTests(unittest.TestCase):
    """thinking model 的 content 可能是空的，答案留在 reasoning_content。

    Ornith-397B（vibe／nemotron-3-ultra）實測回一句 {"ok":1} 就燒掉 208 個
    completion token。推理吃光預算時 content 會空，整個切塊白跑。
    """

    def _reply(self, message):
        return json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]})

    def _call(self, message, **kwargs):
        import ollama_fuc

        class FakeResponse(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with mock.patch.object(
            ollama_fuc.request, "urlopen",
            return_value=FakeResponse(self._reply(message).encode("utf-8")),
        ):
            return ollama_fuc._api_chat([{"role": "user", "content": "x"}], "m", **kwargs)

    def test_uses_reasoning_content_when_it_carries_the_json(self):
        text = self._call(
            {"content": "", "reasoning_content": '想一下…最後給 {"categories":[]}'},
            allow_reasoning_fallback=True,
        )
        self.assertIn('{"categories":[]}', text)

    def test_still_raises_when_reasoning_has_no_json(self):
        """沒有 JSON 就要照樣拋錯——呼叫端靠這個例外降級到備用模型。"""
        with self.assertRaisesRegex(RuntimeError, "empty content"):
            self._call(
                {"content": "", "reasoning_content": "我需要再想想這張圖片"},
                allow_reasoning_fallback=True,
            )

    def test_chat_path_never_returns_raw_thinking(self):
        """對話那條路不開這個後備，否則使用者會看到思考過程而不是回覆。"""
        with self.assertRaisesRegex(RuntimeError, "empty content"):
            self._call({"content": "", "reasoning_content": '思考中 {"a":1}'})
