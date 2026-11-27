"""封存組（節點 H）找到的需求理解缺陷，事後修正的回歸測試。

封存組拆封後才發現、才修的，所以這些修正對封存組而言是「事後」。測試用自己編的
小菜單重現同一類句子，不直接拿封存菜單當測資。
"""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from decision_service import DecisionService  # noqa: E402
from preference_engine import parse_preferences  # noqa: E402
from recommendation import recommend  # noqa: E402
from session_store import SessionStore  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_exclusion_cues(self):
        self.assertEqual(parse_preferences("別放白菜類")["excludes"], ["白菜"])  # 類是量詞
        self.assertEqual(parse_preferences("先排除咖哩，其他一樣")["excludes"], ["咖哩"])
        self.assertEqual(parse_preferences("菇菇這次也先不要")["excludes"], ["菇菇"])

    def test_conversation_words_are_not_ingredients(self):
        for text in ("第一道再換一個，不要重複剛拒絕的", "再換第一杯，同樣預算和排除條件",
                     "今天不要了"):
            self.assertFalse(parse_preferences(text).get("excludes"), text)

    def test_cancelling_an_exclusion_is_not_a_new_exclusion(self):
        delta = parse_preferences("取消排除咖哩")
        self.assertFalse(delta.get("excludes"))

    def test_require_known_price(self):
        for text in ("不設金額上限，可是仍然只選明確標價的", "先給我500元以下且確定有標價的一道",
                     "增加到1000元，但價錢必須看得到", "我要能核實價格的冰品", "只要有標價的"):
            self.assertTrue(parse_preferences(text).get("requireKnownPrice"), text)
        # 問句不是條件
        self.assertFalse(parse_preferences("有標價嗎").get("requireKnownPrice"))


def menu(*rows):
    return {"categories": [{"name": "菜單", "items": [
        {"name": name, "price": price} for name, price in rows
    ]}]}


class RecommendKnownPriceTest(unittest.TestCase):
    def test_unpriced_items_are_dropped_without_a_budget(self):
        catalog = menu(("紅豆牛奶冰", None), ("芒果牛奶冰", None), ("檸檬愛玉", 60))
        prefs = parse_preferences("只選明確標價的")
        names = [row["name"] for row in recommend(catalog, prefs)["items"]]
        self.assertEqual(names, ["檸檬愛玉"])


class DecisionFlowTest(unittest.TestCase):
    def start(self, catalog, text):
        self.service = DecisionService(SessionStore(), {"店": catalog}, "店")
        self.sid = "holdout-fix"
        self.view = self.service.handle(self.sid, action="start", text=text)
        return self.view

    def send(self, **kwargs):
        self.view = self.service.handle(self.sid, revision=self.view["revision"], **kwargs)
        return self.view

    def names(self):
        return [item["name"] for item in self.view.get("items") or []]

    def test_rice_category_is_a_requirement_on_the_first_turn(self):
        catalog = menu(("XO醬乾拌麵", 160), ("老爺式炒飯", 160), ("咖哩豬排飯", 220),
                       ("牛肉麵", 220), ("雞腿飯", 200))
        self.start(catalog, "午餐要飯類，250元內，不吃雞也不要牛，請幫我決定")
        self.assertTrue(self.names())
        self.assertTrue(all("飯" in name for name in self.names()), self.names())

    def test_trailing_and_category_exclusions_apply_in_the_flow(self):
        catalog = menu(("砂鍋土雞湯（小）", 400), ("砂鍋白菜豆腐雞湯（小）", 440),
                       ("砂鍋菇菇雞湯（小）", 520), ("砂鍋蒜頭雞湯（小）", 480))
        self.start(catalog, "我要小份雞湯，800元以內，別放白菜類")
        self.assertNotIn("砂鍋白菜豆腐雞湯（小）", self.names())
        self.send(action="message", text="菇菇這次也先不要")
        self.assertTrue(self.names())
        self.assertFalse(any("白菜" in n or "菇菇" in n for n in self.names()), self.names())

    def test_no_solution_stays_no_solution_after_clearing_the_budget(self):
        catalog = menu(("紅豆牛奶冰", None), ("芒果牛奶冰", None), ("總匯八寶冰", None))
        self.start(catalog, "我要能核實價格的冰品，300元內")
        self.assertEqual(self.view["type"], "no_match")
        self.send(action="message", text="不設金額上限，可是仍然只選明確標價的")
        self.assertEqual(self.view["type"], "no_match")
        self.assertFalse(self.names())

    def test_replacing_twice_still_offers_a_feasible_item(self):
        catalog = menu(("綜合生魚片（小份）", 350), ("綜合魚肚生魚片（小份）", 450),
                       ("和風海鮮沙拉盤（小份）", 350), ("韓式冰卷沙拉盤（小份）", 350),
                       ("象拔蚌兩吃", None))
        self.start(catalog, "先給我500元以下且確定有標價的一道")
        for text in ("第一個換掉，別改其他選項", "第一道再換一個，不要重複剛拒絕的"):
            first = self.view["items"][0]
            self.send(action="replace", item_id=first["id"], text=text)
            self.assertTrue(self.names(), text)
            self.assertNotIn(first["name"], self.names())
            self.assertNotIn("象拔蚌兩吃", self.names())
        self.assertNotIn("避開重複剛拒絕的", self.view.get("constraints") or [])


if __name__ == "__main__":
    unittest.main()
