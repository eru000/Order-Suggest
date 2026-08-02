"""extract_prefs_from_text() 的關鍵字解析行為。

這層是 LLM 抽取的 fallback：USE_LLM_EXTRACTION 關掉時它是唯一在跑的邏輯，
LLM 逾時或回傳壞 JSON 時也會退回這裡。所以它必須自己就是對的。

釘住的三件事，都是修過的真實 bug：
- 否定句：「不吃辣」曾因為包含子字串「吃辣」而被判成要吃辣，剛好相反
- 預算單位：「800元」曾因為取錯 capture group（拿到「元」）而整個抓不到
- 並列詞：「牛肉跟豬肉」曾被當成單一關鍵字，比對不到任何菜名
"""

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

os.environ["USE_LLM_EXTRACTION"] = "false"

from main import extract_prefs_from_text  # noqa: E402


def prefs(text: str):
    """呼叫抽取並吃掉函式內的 debug print。"""
    with contextlib.redirect_stdout(io.StringIO()):
        result = extract_prefs_from_text(text)
    result.pop("weights", None)
    return result


class SpiceLevelTest(unittest.TestCase):
    def assert_spice(self, text: str, expected):
        self.assertEqual(prefs(text).get("spiceLevel"), expected, f"輸入：{text}")

    def test_negation_is_not_read_as_wanting_spice(self):
        # 「不吃辣」「不敢吃辣」都包含子字串「吃辣」
        for text in ["我不吃辣", "不吃辣", "不敢吃辣", "不能吃辣", "怕辣", "不要辣", "免辣"]:
            self.assert_spice(text, "不辣")

    def test_negated_explicit_level_downgrades(self):
        # 「不吃大辣」命中關鍵字「大辣」，但整句是否定的
        for text in ["不吃大辣", "不要大辣", "不要太辣", "不會太辣"]:
            self.assert_spice(text, "小辣")

    def test_mild_phrasings(self):
        for text in ["微辣", "微微辣", "小小辣就好", "一點點辣", "可以吃一點辣", "辣度普通"]:
            self.assert_spice(text, "小辣")

    def test_explicit_levels(self):
        self.assert_spice("中辣", "中辣")
        self.assert_spice("大辣", "大辣")
        self.assert_spice("超辣", "大辣")
        self.assert_spice("很辣", "大辣")

    def test_vague_positive_cues(self):
        for text in ["要辣", "辣一點", "重口味", "嗜辣"]:
            self.assert_spice(text, "中辣")

    def test_no_spice_mentioned(self):
        for text in ["隨便", "預算300", "想吃日式"]:
            self.assert_spice(text, None)


class BudgetTest(unittest.TestCase):
    def assert_budget(self, text: str, expected):
        self.assertEqual(prefs(text).get("budget"), expected, f"輸入：{text}")

    def test_amount_with_unit(self):
        # 這組原本全部回 None：正規表達式的 group(2) 是單位而不是數字
        self.assert_budget("800元", 800.0)
        self.assert_budget("800 元", 800.0)
        self.assert_budget("300塊", 300.0)

    def test_amount_with_leading_cue(self):
        self.assert_budget("預算800", 800.0)
        self.assert_budget("不超過500", 500.0)

    def test_chinese_numerals(self):
        self.assert_budget("兩百塊以內", 200.0)
        self.assert_budget("預算三百五", 350.0)
        self.assert_budget("一千二以內", 1200.0)

    def test_trailing_cue_without_unit(self):
        self.assert_budget("800以內", 800.0)

    def test_no_budget_mentioned(self):
        self.assert_budget("不吃辣", None)


class ExcludesTest(unittest.TestCase):
    def assert_excludes(self, text: str, expected):
        self.assertEqual(prefs(text).get("excludes", []), expected, f"輸入：{text}")

    def test_conjunctions_are_split(self):
        for text in ["不要牛肉跟豬肉", "不要牛肉和豬肉", "不要牛肉、豬肉"]:
            self.assert_excludes(text, ["牛肉", "豬肉"])

    def test_spice_words_do_not_leak_into_excludes(self):
        # 辣度由 spiceLevel 表達；「太辣」留在 excludes 只會比對不到任何菜名
        self.assert_excludes("不要太辣", [])
        self.assert_excludes("不吃辣", [])

    def test_ingredient_containing_spice_char_is_kept(self):
        self.assertIn("辣椒", prefs("不要辣椒").get("excludes", []))

    def test_fullwidth_space_ends_the_phrase(self):
        # 中文輸入法常打出全形空白。漏掉它的話後面整句都會被吸進忌口，
        # 「要飲料」會變成假忌口，反而把飲料濾掉。
        result = prefs("不吃辣　不要牛肉跟豬肉　要飲料")
        self.assertEqual(result.get("excludes"), ["牛肉", "豬肉"])
        self.assertEqual(result.get("needDrink"), True)

    def test_fullwidth_punctuation_ends_the_phrase(self):
        self.assertEqual(prefs("不要花生，其他都可以").get("excludes"), ["花生"])


class CombinedTest(unittest.TestCase):
    def test_realistic_sentence(self):
        result = prefs("四個人 800 元 想吃日式 不要生食")
        self.assertEqual(result.get("budget"), 800.0)
        self.assertEqual(result.get("people"), 4)
        self.assertEqual(result.get("cuisine"), "日式")
        self.assertEqual(result.get("excludes"), ["生食"])

    def test_budget_and_exclusion_together(self):
        result = prefs("預算300不要花生")
        self.assertEqual(result.get("budget"), 300.0)
        self.assertEqual(result.get("excludes"), ["花生"])


if __name__ == "__main__":
    unittest.main()
