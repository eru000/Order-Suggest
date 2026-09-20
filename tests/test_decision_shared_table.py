import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from decision_service import DecisionService  # noqa: E402
from session_store import SessionStore  # noqa: E402


def shared_menu():
    """熱炒店的樣子：品項多、幾乎沒有一人一道的主餐。"""
    categories = {
        "季節時蔬": [("鵝油高麗菜", 220), ("清炒花椰菜", 250), ("蒜炒地瓜葉", 180)],
        "經典鍋物": [("酸白菜肉鍋", 880), ("魷魚螺肉蒜鍋", 1080), ("麻油雞鍋", 1280)],
        "明火好味": [(f"熱炒{index}號", 300 + index * 20) for index in range(20)],
        "海鮮區": [("菠蘿蝦球", 450), ("清蒸鱈魚", 520), ("三杯中卷", 380)],
        "潮粵燒臘": [("燒臘雙拼盤", 480), ("蜜汁叉燒", 380)],
        "水果甜品": [("什錦水果盤", 300)],
    }
    return {
        "categories": [
            {"name": name, "items": [{"name": dish, "price": price} for dish, price in items]}
            for name, items in categories.items()
        ]
    }


class SharedTableDecisionTest(unittest.TestCase):
    def setUp(self):
        self.sessions = SessionStore()
        self.menus = {"熱炒店": shared_menu()}
        self.service = DecisionService(self.sessions, self.menus, "熱炒店")
        self.sid = "shared_table_session"
        self.view = None

    def step(self, action="message", **kwargs):
        self.view = self.service.handle(
            self.sid, action=action, revision=self.view["revision"] if self.view else None, **kwargs
        )
        return self.view

    def test_asks_people_instead_of_dish_type(self):
        self.step("start", text="不知道吃什麼")
        self.assertEqual(self.view["type"], "question")
        # 合菜店問「飯還是麵」沒有意義，該問的是幾個人。
        self.assertEqual(self.view["question"]["field"], "people")

    def test_table_covers_vegetable_and_soup_and_scales_with_people(self):
        self.step("start", text="不知道吃什麼")
        self.step("answer", value="4")
        names = [item["name"] for item in self.view["items"]]
        self.assertEqual(len(names), 5)  # 人數 + 1
        self.assertTrue(self.view["sharedTable"])
        self.assertIn("4 個人這樣點", self.view["message"])
        self.assertTrue(any(name in {"鵝油高麗菜", "清炒花椰菜", "蒜炒地瓜葉"} for name in names))
        self.assertTrue(any("鍋" in name for name in names))

    def test_budget_is_for_the_whole_table(self):
        self.step("start", text="六個人 預算3000")
        if self.view["type"] == "question":
            self.step("answer", value="6")
        total = sum(item["total"] for item in self.view["items"] if item["total"] is not None)
        self.assertLessEqual(total, 3000)
        self.assertIn("整桌 $ 3000 內", self.view["constraints"])

    def test_one_dish_can_still_be_swapped_out(self):
        self.step("start", text="不知道吃什麼")
        self.step("answer", value="4")
        dropped = self.view["items"][0]
        self.step("replace", item_id=dropped["id"], reason="another")
        names = [item["name"] for item in self.view["items"]]
        self.assertNotIn(dropped["name"], names)
        self.assertEqual(len(names), 5)


if __name__ == "__main__":
    unittest.main()
