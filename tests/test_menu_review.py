"""產品端價格合理性檢查（src/menu_review.py）與辨識流程的整合。"""

import io
import json
import sys
import unittest
from pathlib import Path

from PIL import Image

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import menu_vision  # noqa: E402
from menu_review import REVIEW_OUTLIER_RATIO, apply_price_review, outlier_scores  # noqa: E402


def menu(*prices):
    return {"categories": [{"name": "麵", "items": [
        {"name": f"麵{i}", "price": price} for i, price in enumerate(prices)
    ]}]}


class MenuReviewTest(unittest.TestCase):
    def test_threshold_is_the_calibrated_value(self):
        # 來源：evals/review_policy/calibrate.py，Conformal Risk Control，α＝0.10
        self.assertEqual(REVIEW_OUTLIER_RATIO, 0.10)

    def test_dropped_digit_far_below_peers_is_flagged(self):
        result = menu(55, 45, 65, 5, 60)  # 55 被遮掉一位讀成 5
        self.assertEqual(apply_price_review(result), 1)
        flagged = [i["name"] for i in result["categories"][0]["items"] if "review" in i]
        self.assertEqual(flagged, ["麵3"])

    def test_known_limitation_at_calibrated_threshold(self):
        # 680→68、同類中位數 540：比值 0.126，高於 0.10，不會被標記（α＝0.10 容許的漏標）
        result = menu(680, 520, 480, 68, 560)
        self.assertEqual(apply_price_review(result), 0)
        score = next(iter(sorted(s for s, _ in outlier_scores(result).values())))
        self.assertAlmostEqual(score, 68 / 540, places=3)

    def test_recheck_after_correction_removes_flag_and_keeps_other_flags(self):
        result = menu(55, 45, 65, 5, 60)
        item = result["categories"][0]["items"][3]
        item["review"] = {"flags": ["name_not_in_ocr"], "detail": {}}
        apply_price_review(result)
        self.assertEqual(item["review"]["flags"], ["name_not_in_ocr", "price_outlier"])
        item["price"] = 55
        apply_price_review(result)
        self.assertEqual(item["review"]["flags"], ["name_not_in_ocr"])

    def test_analyze_counts_outliers_as_needing_review(self):
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), "white").save(buffer, "PNG")

        def fake_vision(prompt, **kwargs):
            return json.dumps({"categories": [{"title": "麵", "items": [
                {"dish": "乾麵", "price": 55}, {"dish": "湯麵", "price": 45}, {"dish": "肉羹麵", "price": 5},
                {"dish": "牛肉麵", "price": 65},
            ]}]}, ensure_ascii=False)

        result = menu_vision.analyze_menu_image(buffer.getvalue(), "image/png", vision_func=fake_vision)
        self.assertEqual(result["quality"]["priceOutlierCount"], 1)
        self.assertEqual(result["quality"]["needsReviewCount"], 1)
        corrected = menu_vision.apply_manual_correction(result, "改 3 價格 55")["result"]
        self.assertEqual(corrected["quality"]["needsReviewCount"], 0)


if __name__ == "__main__":
    unittest.main()
