import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from menu_price import parse_price, price_value  # noqa: E402


class ParsePriceTests(unittest.TestCase):
    """期中評審（委員5-3）指出：VLM 看不清時若補值，推薦會建立在錯價上。

    舊解析「抓第一段數字」把下列輸入全部變成看似合法的單價。這裡固定新規則：
    只有確定是單一明確價格才給數字。
    """

    def assertPrice(self, raw, status, value=None):
        info = parse_price(raw)
        self.assertEqual(info["status"], status, f"{raw!r} -> {info}")
        self.assertEqual(info["value"], value, f"{raw!r} -> {info}")
        return info

    def test_regressions_from_old_first_number_rule(self):
        # 舊輸出依序為 1.0、9.0、100.0、60.0、150.0（2026-11-26 實際執行確認）
        self.assertPrice("1?0", "unreadable")
        self.assertPrice("$ 9?", "unreadable")
        self.assertPrice("100～190", "range")
        self.assertPrice("大60/小45", "multi")
        self.assertPrice("約150", "range")

    def test_fixed_prices(self):
        self.assertPrice(120, "fixed", 120.0)
        self.assertPrice(45.5, "fixed", 45.5)
        self.assertPrice("120元", "fixed", 120.0)
        self.assertPrice("NT$ 180", "fixed", 180.0)
        self.assertPrice("NT$1,200", "fixed", 1200.0)
        self.assertPrice("＄１２０", "fixed", 120.0)  # 全形
        self.assertPrice("2-3人份 680", "fixed", 680.0)  # 份量不是價格
        self.assertPrice("1/2隻 300", "fixed", 300.0)
        self.assertPrice("起司 +20", "fixed", 20.0)  # 起司不是起價

    def test_zero_keeps_legacy_convention(self):
        # 推薦器另有「price == 0 視為時價」的慣例，這裡不改變數字 0 的行為。
        self.assertPrice(0, "fixed", 0.0)

    def test_market_and_missing(self):
        self.assertPrice("時價", "market")
        self.assertPrice("季節時價", "market")
        for raw in (None, "", "null", "未知", "未提供", "未標示", "-", True, -5, float("nan")):
            self.assertEqual(parse_price(raw)["status"], "missing", repr(raw))
            self.assertIsNone(parse_price(raw)["value"], repr(raw))

    def test_unreadable_markers(self):
        for raw in ("?", "？？", "看不清", "1□0", "模糊"):
            self.assertPrice(raw, "unreadable")

    def test_ranges(self):
        for raw in ("100-190", "100 ~ 190", "NT$100 - NT$190", "100元~190元", "150起", "200以上"):
            self.assertPrice(raw, "range")

    def test_multi_keeps_labelled_variants(self):
        info = self.assertPrice("大60/小45", "multi")
        self.assertEqual(info["variants"], [{"label": "大", "value": 60.0}, {"label": "小", "value": 45.0}])
        info = self.assertPrice("60(大)/45(小)", "multi")
        self.assertEqual(info["variants"], [{"label": "大", "value": 60.0}, {"label": "小", "value": 45.0}])
        info = self.assertPrice("小45 大55", "multi")
        self.assertEqual([v["label"] for v in info["variants"]], ["小", "大"])

    def test_structured_model_output(self):
        info = self.assertPrice({"大": 60, "小": 45}, "multi")
        self.assertEqual(len(info["variants"]), 2)
        self.assertPrice({"price": "NT$90"}, "fixed", 90.0)
        self.assertPrice([60, 45], "multi")
        self.assertPrice([80], "fixed", 80.0)
        self.assertPrice([60, "1?0"], "unreadable")

    def test_raw_text_is_kept_for_human_review(self):
        self.assertEqual(parse_price("1?0")["raw"], "1?0")
        self.assertIsNone(parse_price(None)["raw"])

    def test_price_value_shortcut(self):
        self.assertEqual(price_value("120元"), 120.0)
        self.assertIsNone(price_value("100～190"))


if __name__ == "__main__":
    unittest.main()
