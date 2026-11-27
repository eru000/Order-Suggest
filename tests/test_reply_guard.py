"""LLM 推薦說明的輸出防護（src/reply_guard.py，回應委員5-9）。"""

import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import main  # noqa: E402
from reply_guard import check_reply, is_mentioned  # noqa: E402


def rec(items, **meta):
    return {"items": items, "notes": "", "meta": meta}


def item(name, price):
    return {"name": name, "price": price, "category": "主餐", "reason": "", "type": "main"}


MENU = {"categories": [{"name": "熱炒", "items": [
    {"name": "鵝油高麗菜", "price": 220},
    {"name": "燒臘雙拼盤", "price": 480},
    {"name": "東北酸白菜肉鍋", "price": 880},
    {"name": "凍檸茶", "price": 250},
]}, {"name": "飲料", "items": [{"name": "七喜汽水", "price": 40}]}]}
REC = rec(
    [item("鵝油高麗菜", 220), item("燒臘雙拼盤", 480), item("七喜汽水", 40)],
    people=2, budget=1000, estimatedSubtotal=740, estimatedTotal=814, serviceRate=0.1,
)


class CheckReplyTest(unittest.TestCase):
    def test_faithful_reply_passes(self):
        text = "兩位可以點鵝油高麗菜跟燒臘雙拼盤，再來一瓶七喜，小計七百四十，含服務費約 $814。"
        self.assertTrue(check_reply(text, REC, MENU)["ok"])

    def test_omitted_item_is_rejected(self):
        verdict = check_reply("點燒臘雙拼盤配七喜，小計 $740。", REC, MENU)
        self.assertEqual(verdict["reasons"], ["missing_items"])
        self.assertEqual(verdict["missingItems"], ["鵝油高麗菜"])

    def test_wrong_total_is_rejected(self):
        text = "鵝油高麗菜、燒臘雙拼盤、七喜汽水，總共大概三千五六百。"
        self.assertIn("unsupported_numbers", check_reply(text, REC, MENU)["reasons"])

    def test_derived_amounts_are_supported(self):
        # 服務費 74、剩餘預算 260、每人 370 都算得出來，不算錯
        text = "鵝油高麗菜、燒臘雙拼盤、七喜汽水小計 740，服務費 74，還剩 260，每人約 370。"
        self.assertTrue(check_reply(text, REC, MENU)["ok"])

    def test_substituted_dish_is_rejected(self):
        text = "鵝油高麗菜、燒臘雙拼盤、七喜汽水都不錯，也可以換成東北酸白菜肉鍋。"
        self.assertIn("unrecommended_dishes", check_reply(text, REC, MENU)["reasons"])

    def test_name_variants(self):
        self.assertTrue(is_mentioned("七喜汽水", "再來一瓶七喜"))
        self.assertTrue(is_mentioned("香辣牛肚絲", "牛肚絲當肉類"))
        self.assertTrue(is_mentioned("綜合野菜 (Vegetables Combo)", "再搭綜合野菜"))
        self.assertTrue(
            is_mentioned("Salmon Bibimbap With Egg (玉子鮭魚石鍋飯)", "點玉子鮭魚石鍋飯")
        )
        # 中間一段是別道菜的名字，不算提到（第六項：LLM 把它換成了松阪豬肉飯）
        self.assertFalse(is_mentioned("當歸松阪豬肉湯", "兩人各來一份松阪豬肉飯"))

    def test_step6_logged_replies(self):
        """第六項實際的 10 則 LLM 說明：人工判讀不忠實的 6 則全部擋下。

        忠實的 4 則中，大肥鵝「兩個人 預算1000 要飲料」順帶推薦了凍檸茶等其他飲料，
        被當成換菜擋下（誤擋，改用模板），其餘 3 則放行。規則是看了這 10 則才定的，
        這不是獨立驗證，只確保之後改程式不會讓結果倒退。
        """
        # evals 底下有好幾支 run_eval.py，用路徑載入，免得拿到別支
        spec = importlib.util.spec_from_file_location(
            "recommendation_run_eval", ROOT / "evals" / "recommendation" / "run_eval.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        load_menus = module.load_menus

        data = json.loads(
            (ROOT / "evals/results/step6/llm-live-20261127-120947.json").read_text(encoding="utf-8")
        )
        menus = load_menus(ROOT)
        verdicts = {
            (row["restaurant"], row["text"]): check_reply(
                row["llm"], row["recommendation"], menus[row["restaurant"]]
            )["ok"]
            for row in data["rows"] if row["kind"] == "explain"
        }
        unfaithful = [
            ("大肥鵝", "四個人 預算3000"), ("大肥鵝", "六個人 預算5000 不吃牛"),
            ("斗六當歸鴨", "一個人 預算100"), ("斗六當歸鴨", "兩個人 預算300 不吃鴨"),
            ("奔頂牛排 東海", "兩個人 預算1000"), ("森森燒肉 台中中科店", "四個人 預算2000"),
        ]
        for key in unfaithful:
            self.assertFalse(verdicts[key], key)
        self.assertEqual(sum(verdicts.values()), 3)
        self.assertFalse(verdicts[("大肥鵝", "兩個人 預算1000 要飲料")])
        # 改用的模板本身必須通過同一套檢查，否則降級後仍可能不一致
        for row in data["rows"]:
            if row["kind"] == "explain":
                self.assertTrue(check_reply(
                    row["template"], row["recommendation"], menus[row["restaurant"]]
                )["ok"], row["text"])


class ProductWiringTest(unittest.TestCase):
    def _with_model(self, name, impl, fn):
        with mock.patch("sys.stdout", io.StringIO()):
            import ollama_fuc
            with mock.patch.object(ollama_fuc, name, impl):
                return fn()

    def test_unfaithful_reply_falls_back_to_template(self):
        reply = self._with_model(
            "chat", lambda *a, **k: "點燒臘雙拼盤就好，大概三千。",
            lambda: main.generate_ai_reply(REC, "兩個人", menu=MENU),
        )
        self.assertIn("餐點小計", reply)
        self.assertIn("鵝油高麗菜", reply)

    def test_faithful_reply_is_kept(self):
        text = "鵝油高麗菜、燒臘雙拼盤加一瓶七喜，小計 $740。"
        reply = self._with_model(
            "chat", lambda *a, **k: text, lambda: main.generate_ai_reply(REC, "兩個人", menu=MENU)
        )
        self.assertEqual(reply, text)

    def test_stream_is_checked_before_anything_is_sent(self):
        pieces = self._with_model(
            "chat_stream", lambda *a, **k: iter(["點燒臘", "雙拼盤", "就好"]),
            lambda: list(main.generate_ai_reply_stream(REC, "兩個人", menu=MENU)),
        )
        self.assertEqual(len(pieces), 1)
        self.assertIn("餐點小計", pieces[0])

    def test_open_questions_are_not_guarded(self):
        # 開放問答本來就會談推薦以外的菜
        reply = self._with_model(
            "chat", lambda *a, **k: "有，凍檸茶 $250。",
            lambda: main.generate_ai_reply({}, "有檸檬茶嗎", menu=MENU, decision_context={}),
        )
        self.assertEqual(reply, "有，凍檸茶 $250。")


if __name__ == "__main__":
    unittest.main()
