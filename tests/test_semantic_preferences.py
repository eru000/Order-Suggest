import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preference_engine import merge_preference_delta, parse_preferences  # noqa: E402
from recommendation import recommend  # noqa: E402


def item(name, price, spice, *, allergens=None, known=True):
    return {
        "name": name,
        "price": price,
        "semantic": {
            "schemaVersion": 1,
            "role": "main",
            "spice": {"min": spice, "max": spice, "known": spice is not None},
            "allergens": {"known": known, "values": allergens or []},
            "dietaryFlags": [],
            "confidence": 1,
            "source": "test",
        },
    }


def menu(items):
    return {"restaurants": {"測試餐廳": {"categories": {"主餐": {"items": items}}}}}


class StructuredPreferenceTest(unittest.TestCase):
    def test_spice_is_a_range_not_a_boolean(self):
        mild = parse_preferences("不要太辣")["spiceProfile"]
        spicy = parse_preferences("要辣")["spiceProfile"]
        self.assertEqual((mild["maximum"], mild["target"], mild["strict"]), (2, 1, False))
        self.assertEqual(spicy["target"], 3)

    def test_later_turn_can_remove_accumulated_dislike(self):
        state = {"excludes": ["牛肉", "豬肉"]}
        merge_preference_delta(state, parse_preferences("牛肉可以吃了"))
        self.assertEqual(state["excludes"], ["豬肉"])

    def test_target_spice_affects_ranking(self):
        result = recommend(
            menu([item("清湯麵", 80, 0), item("香辣麵", 100, 3)]),
            parse_preferences("要辣"),
            top_k=1,
        )
        self.assertEqual(result["items"][0]["name"], "香辣麵")

    def test_shadow_mode_observes_semantic_result_but_serves_legacy(self):
        with mock.patch.dict("os.environ", {"SEMANTIC_SHADOW_MODE": "true"}):
            result = recommend(
                menu([item("清湯麵", 80, 0), item("香辣麵", 100, 3)]),
                parse_preferences("要辣"),
                top_k=1,
            )
        self.assertEqual(result["items"][0]["name"], "清湯麵")

    def test_allergy_filters_match_and_unknown_metadata(self):
        prefs = {"allergens": ["peanut"], "needDrink": False}
        result = recommend(
            menu(
                [
                    item("花生麵", 80, 0, allergens=["peanut"]),
                    item("安全麵", 90, 0, allergens=[], known=True),
                    item("資料不明麵", 70, 0, known=False),
                ]
            ),
            prefs,
        )
        self.assertEqual([row["name"] for row in result["items"]], ["安全麵"])


if __name__ == "__main__":
    unittest.main()
