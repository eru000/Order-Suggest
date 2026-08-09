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

    def test_verify_pass_cannot_silently_delete_items(self):
        """最終校對漏掉的品項要補回來，不能靜默消失。

        實測 tile-4 把「大肉羹麵 65」讀得清清楚楚，最終校對的回傳卻沒有
        它。漏掉的品項是看不見的失敗，所以補回並標記，讓人工複核裁決。
        """
        tiles = [
            {"name": "大肉羹麵", "price": 65.0, "category": "麵類", "sources": [{"block": "tile-4"}]},
            {"name": "魯肉湯飯", "price": 45.0, "category": "麵類", "sources": [{"block": "tile-2"}]},
        ]
        verified = [
            # 錯字被更正：不該被當成「刪掉一項又新增一項」。
            {"name": "魯肉湯麵", "price": 45.0, "category": "麵類"},
            # 切塊都沒讀到，只有校對生出來的。
            {"name": "招牌炒飯", "price": 80.0, "category": "飯類"},
        ]
        final, restored, invented = menu_vision._reconcile_verified(verified, tiles)

        self.assertEqual(restored, ["大肉羹麵"])
        self.assertEqual(invented, ["招牌炒飯"])
        names = [item["name"] for item in final]
        self.assertIn("大肉羹麵", names)
        self.assertIn("魯肉湯麵", names)
        self.assertNotIn("魯肉湯飯", names, "更正過的品名不該同時留下舊的那一個")
        self.assertEqual(len(names), 3)

    def test_short_name_typo_fix_is_not_counted_as_delete_plus_add(self):
        """三個字的品名改一個字，相似度只剩 0.667——低於一般門檻。

        實測校對把「肉蓗飯」修成「肉羹飯」，結果錯字版被當成「被刪掉」而
        補回、更正版被當成「憑空新增」，同一道菜留下兩筆。價格一致時放寬
        門檻才能認出這是同一項。
        """
        tiles = [{"name": "肉蓗飯", "price": 55.0, "category": "飯類", "sources": []}]
        verified = [{"name": "肉羹飯", "price": 55.0, "category": "飯類"}]
        final, restored, invented = menu_vision._reconcile_verified(verified, tiles)
        self.assertEqual(restored, [])
        self.assertEqual(invented, [])
        self.assertEqual([item["name"] for item in final], ["肉羹飯"])

    def test_same_price_shortcut_does_not_merge_genuinely_different_dishes(self):
        """放寬不能寬到把不同的菜黏在一起。

        這份菜單上肉羹飯與肉羹麵都是 55 元，是兩道真的不同的菜。
        """
        tiles = [
            {"name": "肉羹飯", "price": 55.0, "category": "飯類", "sources": []},
            {"name": "肉羹麵", "price": 55.0, "category": "麵類", "sources": []},
        ]
        verified = [
            {"name": "肉羹飯", "price": 55.0, "category": "飯類"},
            {"name": "肉羹麵", "price": 55.0, "category": "麵類"},
        ]
        final, restored, invented = menu_vision._reconcile_verified(verified, tiles)
        self.assertEqual(restored, [])
        self.assertEqual(invented, [])
        self.assertEqual({item["name"] for item in final}, {"肉羹飯", "肉羹麵"})

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

    def test_exif_orientation_and_overlapping_two_by_two_tiles(self):
        image = Image.new("RGB", (2000, 1800), "white")
        exif = Image.Exif()
        exif[274] = 1
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif)
        _, regions, size = menu_vision.prepare_image_regions(buffer.getvalue())
        self.assertEqual(size, (2000, 1800))
        self.assertEqual(len(regions), 4)
        self.assertEqual(regions[0]["box"], [0, 0, 1120, 1008])
        self.assertEqual(regions[1]["box"], [880, 0, 2000, 1008])
        self.assertEqual(regions[1]["box"][0] - regions[0]["box"][2], -240)

    def test_low_resolution_messaging_copy_is_recovered_before_tiling(self):
        image = Image.new("RGB", (841, 607), "white")
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        _, regions, size = menu_vision.prepare_image_regions(buffer.getvalue())
        self.assertEqual(size[0], 2000)
        self.assertEqual(len(regions), 4)

    def test_merge_requires_matching_price_and_reports_conflict(self):
        results = [
            {"id": "tile-1", "box": [0, 0, 100, 100], "categories": [{"name": "牛排", "items": [{"name": "犇頂牛排", "price": 230}]}]},
            {"id": "tile-2", "box": [80, 0, 180, 100], "categories": [{"name": "牛排", "items": [{"name": "犇頂牛排", "price": 330}]}]},
        ]
        merged, conflicts = menu_vision.merge_region_items(results)
        self.assertEqual(len(merged), 2)
        self.assertEqual(conflicts[0]["type"], "price")

    def test_dense_image_routes_overview_tiles_and_verifier(self):
        image = Image.new("RGB", (2000, 1800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        calls = []

        def fake_vision(prompt, image_url, model=None, timeout=0, temperature=None):
            calls.append((prompt, image_url, model, temperature))
            if "版面與身分" in prompt:
                return '{"restaurant_name":"犇頂牛排","source_type":"menu","menu_type":"牛排館"}'
            if "最終校對員" in prompt:
                return '{"restaurant_name":"犇頂牛排","categories":[{"name":"排餐","items":[{"name":"犇頂牛排","price":230},{"name":"菲力牛排","price":360},{"name":"香煎中卷","price":280}]}]}'
            return '{"categories":[{"name":"排餐","items":[{"name":"犇頂牛排","price":230},{"name":"菲力牛排","price":360}]}]}'

        # 這條釘的是完整流程（總覽 → 切塊 → 校對）。VISION_FAST 會少掉兩次呼叫，
        # 而載入 .env 的其他測試會把它帶進同一個行程，所以這裡明確關掉。
        with mock.patch.dict(os.environ, {"VISION_FAST": "0"}):
            result = menu_vision.analyze_menu_image(
                buffer.getvalue(), "image/jpeg", "東海愛將", vision_func=fake_vision
            )
        self.assertEqual(len(calls), 6)
        self.assertEqual(calls[0][2], "mistral-small-4")
        self.assertTrue(all(call[3] == 0 for call in calls))
        self.assertIsInstance(calls[-1][1], list)
        self.assertEqual(len(calls[-1][1]), 4)
        self.assertEqual(result["detected_restaurant_name"], "犇頂牛排")
        self.assertTrue(result["identityConflict"])
        self.assertEqual(result["quality"]["priceCoverage"], 1.0)

    def test_fast_mode_skips_only_the_overview_and_keeps_the_verifier(self):
        """校對那關會補回切塊漏掉的品項，快速模式也不能省——省了會掉一半菜單。"""
        image = Image.new("RGB", (2000, 1800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        calls = []

        def fake_vision(prompt, image_url, model=None, timeout=0, temperature=None):
            calls.append(prompt)
            if "最終校對員" in prompt:
                return json.dumps(
                    {
                        "restaurant_name": "斗六門當歸鴨",
                        "categories": [{"name": "主食", "items": [
                            {"name": "鴨肉飯", "price": 40},
                            {"name": "鴨腿飯", "price": 80},
                        ]}],
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "restaurant_name": "斗六門當歸鴨" if "tile-1" in prompt else "",
                    "categories": [{"name": "主食", "items": [{"name": "鴨肉飯", "price": 40}]}],
                },
                ensure_ascii=False,
            )

        with mock.patch.dict(os.environ, {"VISION_FAST": "1"}):
            result = menu_vision.analyze_menu_image(
                buffer.getvalue(), "image/jpeg", vision_func=fake_vision
            )

        self.assertEqual(len(calls), 5, "四張切塊加一次校對，只少掉總覽")
        self.assertTrue(all("版面與身分" not in prompt for prompt in calls))
        self.assertIn("最終校對員", calls[-1])
        self.assertEqual(result["detected_restaurant_name"], "斗六門當歸鴨")
        names = [item["name"] for category in result["categories"] for item in category["items"]]
        self.assertIn("鴨腿飯", names, "校對補回來的品項必須留在結果裡")

    def test_fast_mode_prefers_the_longest_tile_name(self):
        """招牌橫跨中線時每塊只看到殘名，挑最長的——殘缺的一定比完整的短。"""
        image = Image.new("RGB", (2000, 1800), "white")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")

        def fake_vision(prompt, image_url, model=None, timeout=0, temperature=None):
            name = "斗六門當歸鴨" if "tile-2" in prompt else "當歸鴨"
            return json.dumps(
                {
                    "restaurant_name": name,
                    "categories": [{"name": "主食", "items": [{"name": "鴨肉飯", "price": 40}]}],
                },
                ensure_ascii=False,
            )

        with mock.patch.dict(os.environ, {"VISION_FAST": "1"}):
            result = menu_vision.analyze_menu_image(
                buffer.getvalue(), "image/jpeg", vision_func=fake_vision
            )

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
