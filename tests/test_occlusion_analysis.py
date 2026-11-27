"""節點 B：遮蔽實驗結果分類的測試（不需要 API 與 OCR 模型）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "evals" / "menu_ocr", ROOT / "evals" / "occlusion"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import analyze  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_outcomes(self):
        self.assertEqual(analyze.classify(480, None), "missing")
        self.assertEqual(analyze.classify(480, {"price": None}), "abstain")
        self.assertEqual(analyze.classify(480, {"price": 480.0}), "right_number")
        self.assertEqual(analyze.classify(480, {"price": 48.0}), "wrong_number")


class AnalyzeRunTest(unittest.TestCase):
    def test_reparses_debug_response_and_classifies_each_occluded_item(self):
        with tempfile.TemporaryDirectory() as directory:
            case_dir = Path(directory)
            truth = {"items": [
                {"name": "排骨飯", "price": 90},
                {"name": "雞腿飯", "price": None, "true_price": 120,
                 "occlusion": {"type": "mask", "level": "full"}},
                {"name": "燒鵝飯", "price": None, "true_price": 150,
                 "occlusion": {"type": "mask", "level": "full"}},
                {"name": "鴨肉飯", "price": None, "true_price": 70,
                 "occlusion": {"type": "mask", "level": "full"}},
            ]}
            (case_dir / "menu__mask_full.expected.json").write_text(
                json.dumps(truth, ensure_ascii=False), encoding="utf-8"
            )
            response = json.dumps({"categories": [{"title": "飯", "items": [
                {"dish": "排骨飯", "price": 90}, {"dish": "雞腿飯", "price": 12},
                {"dish": "燒鵝飯", "price": "1?0"},
            ]}]}, ensure_ascii=False)
            run = {"cases": [{"case": "menu__mask_full", "exchanges": [{"response": response}]}]}
            records = analyze.analyze_run(run, case_dir, with_cross_check=False)
        outcome = {r["item"]: r["outcome"] for r in records}
        self.assertEqual(outcome, {"雞腿飯": "wrong_number", "燒鵝飯": "abstain", "鴨肉飯": "missing"})
        summary = analyze.summarize(records)["mask_full"]
        self.assertEqual((summary["n"], summary["wrong_number"], summary["abstain"]), (3, 1, 1))


if __name__ == "__main__":
    unittest.main()
