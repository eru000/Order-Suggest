"""節點 5：OCR 版面配對與交叉複核的純邏輯測試（不需要安裝 OCR 模型）。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "evals" / "menu_ocr"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import ocr_baseline  # noqa: E402
from cross_check import cross_check  # noqa: E402


def line(text, x0, y0, x1, y1):
    return {"text": text, "raw": text, "box": (x0, y0, x1, y1), "conf": 0.99}


class PairItemsTest(unittest.TestCase):
    def test_name_pairs_with_price_on_same_row(self):
        items = ocr_baseline.pair_items([
            line("魯肉飯", 10, 10, 80, 30), line("35", 200, 12, 230, 30),
            line("排骨飯", 10, 50, 80, 70), line("90", 200, 52, 230, 70),
        ])
        self.assertEqual([(i["dish"], i["price"]) for i in items], [("魯肉飯", "35"), ("排骨飯", "90")])

    def test_price_of_neighbouring_column_is_not_taken(self):
        items = ocr_baseline.pair_items([
            line("乾麵", 10, 10, 60, 30), line("湯麵", 300, 10, 350, 30), line("45", 400, 12, 430, 30),
            line("滷蛋", 10, 50, 60, 70), line("15", 100, 52, 130, 70),
        ])
        self.assertNotIn(("乾麵", "45"), [(i["dish"], i["price"]) for i in items])
        self.assertIn(("湯麵", "45"), [(i["dish"], i["price"]) for i in items])

    def test_size_price_line_becomes_variants(self):
        items = ocr_baseline.pair_items([
            line("蛤仔湯", 10, 10, 90, 30), line("小90中160", 200, 10, 300, 30), line("大240", 310, 10, 360, 30),
        ])
        self.assertEqual(
            [(i["dish"], i["price"]) for i in items],
            [("蛤仔湯（小）", "90"), ("蛤仔湯（中）", "160"), ("蛤仔湯（大）", "240")],
        )

    def test_bracket_note_attaches_to_name_below_and_english_is_dropped(self):
        items = ocr_baseline.pair_items([
            line("（外帶）", 20, 0, 70, 12), line("大魯肉飯", 10, 14, 90, 34), line("55", 200, 15, 230, 33),
            line("麵類Noodles", 10, 60, 150, 80), line("乾麵", 10, 100, 60, 120), line("45", 200, 100, 230, 120),
        ])
        self.assertIn(("（外帶）大魯肉飯", "55"), [(i["dish"], i["price"]) for i in items])
        self.assertNotIn("Noodles", "".join(i["dish"] for i in items))

    def test_ocr_currency_misread_is_still_a_price(self):
        self.assertEqual(ocr_baseline._kind("NTs180"), "price")


class CrossCheckTest(unittest.TestCase):
    def vlm(self, *items):
        return {"categories": [{"name": "菜單", "items": [
            {"name": name, "price": price, "priceInfo": {"status": "fixed" if price is not None else "missing"}}
            for name, price in items
        ]}]}

    def flags(self, result):
        return {item["name"]: item["review"]["flags"] for item in result["items"]}

    def test_flags(self):
        lines = [
            line("魯肉飯", 10, 10, 80, 30), line("35", 200, 12, 230, 30),
            line("排骨飯", 10, 50, 80, 70), line("90", 200, 52, 230, 70),
            line("雞腿飯", 10, 90, 80, 110),
            line("控肉飯", 10, 130, 80, 150), line("60", 200, 132, 230, 150),
        ]
        result = cross_check(self.vlm(("魯肉飯", 35.0), ("排骨飯", 100.0), ("雞腿飯", 95.0), ("鮭魚飯", 120.0)), lines)
        flags = self.flags(result)
        self.assertEqual(flags["魯肉飯"], [])
        self.assertEqual(flags["排骨飯"], ["ocr_price_mismatch"])
        self.assertEqual(flags["雞腿飯"], ["price_unverified"])
        self.assertEqual(flags["鮭魚飯"], ["name_not_in_ocr"])
        self.assertEqual([(o["dish"], o["price"]) for o in result["orphans"]], [("控肉飯", "60")])

    def test_ambiguous_price_is_flagged_and_input_not_modified(self):
        vlm = self.vlm(("燒鵝飯", None))
        vlm["categories"][0]["items"][0]["priceInfo"]["status"] = "unreadable"
        result = cross_check(vlm, [line("燒鵝飯", 10, 10, 80, 30)])
        self.assertIn("price_ambiguous", self.flags(result)["燒鵝飯"])
        self.assertNotIn("review", vlm["categories"][0]["items"][0])



class PriceOutlierTest(unittest.TestCase):
    """遮蔽實驗：被遮掉一位數時 VLM 與 OCR 讀到同一個殘缺數字（680→68），彼此一致；
    只有和同分類價格比較的合理性檢查抓得到。"""

    def menu(self, *prices):
        return {"categories": [{"name": "主菜", "items": [
            {"name": f"菜{i}", "price": price} for i, price in enumerate(prices)
        ]}]}

    def test_truncated_price_is_flagged(self):
        from cross_check import price_outliers
        menu = self.menu(680, 520, 480, 68, 560)
        flagged = price_outliers(menu)
        names = [i["name"] for i in menu["categories"][0]["items"] if id(i) in flagged]
        self.assertEqual(names, ["菜3"])

    def test_zero_price_among_priced_items_is_flagged(self):
        from cross_check import price_outliers
        menu = self.menu(20, 15, 35, 0)
        self.assertEqual(len(price_outliers(menu)), 1)

    def test_normal_cheap_side_dish_is_not_flagged(self):
        from cross_check import price_outliers
        self.assertEqual(price_outliers(self.menu(15, 50, 55, 45, 65)), {})

    def test_needs_enough_peers(self):
        from cross_check import price_outliers
        self.assertEqual(price_outliers(self.menu(500, 5)), {})

if __name__ == "__main__":
    unittest.main()
