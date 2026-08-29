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


def item(name, price, spice, *, allergens=None, known=True, diet=None, diet_conflicts=None):
    return {
        "name": name,
        "price": price,
        "semantic": {
            "schemaVersion": 2,
            "role": "main",
            "spice": {"min": spice, "max": spice, "known": spice is not None},
            "allergens": {"known": known, "values": allergens or []},
            "dietaryFlags": diet or [],
            "dietaryConflicts": diet_conflicts or [],
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

    def test_allergy_excludes_matches_and_demotes_unknown(self):
        """確定含過敏原的排除；成分不明的保留但標記並排在後面。

        舊行為是「不明也一併排除」，而 deterministic 標註的 known 永遠是
        False，等於使用者一提過敏就拿到空清單。
        """
        # 3 人才會取到第二道主餐，否則看不出不確定品項有沒有被保留。
        prefs = {"allergens": ["peanut"], "needDrink": False, "people": 3}
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
        names = [row["name"] for row in result["items"]]
        self.assertNotIn("花生麵", names)
        self.assertEqual(names, ["安全麵", "資料不明麵"])
        self.assertFalse(result["items"][0]["uncertain"])
        self.assertTrue(result["items"][1]["uncertain"])
        self.assertIn("資料不明麵", result["notes"])

    def test_dietary_excludes_conflicts_and_keeps_unknown(self):
        prefs = {"dietaryRestrictions": ["vegetarian"], "needDrink": False, "people": 3}
        result = recommend(
            menu(
                [
                    item("鴨肉飯", 80, 0, diet_conflicts=["vegetarian"]),
                    item("素食飯", 70, 0, diet=["vegetarian"]),
                    item("成分不明飯", 60, 0),
                ]
            ),
            prefs,
        )
        names = [row["name"] for row in result["items"]]
        self.assertNotIn("鴨肉飯", names)
        self.assertEqual(names, ["素食飯", "成分不明飯"])
        self.assertTrue(result["items"][1]["uncertain"])

    def test_deterministic_annotation_reads_diet_from_name(self):
        """沒有明寫語意時，品名要能撐起素食判斷——這是實際菜單的常態。"""
        prefs = {"dietaryRestrictions": ["vegetarian"], "needDrink": False}
        result = recommend(
            {
                "categories": [
                    {
                        "name": "全部",
                        "items": [
                            {"name": "鍋燒雞絲", "price": 55},
                            {"name": "素肉燥飯", "price": 40},
                            {"name": "燙青菜", "price": 30},
                        ],
                    }
                ]
            },
            prefs,
        )
        names = [row["name"] for row in result["items"]]
        self.assertNotIn("鍋燒雞絲", names)
        self.assertIn("素肉燥飯", names)
        self.assertIn("燙青菜", names)

    def test_positive_cue_becomes_likes_and_changes_ranking(self):
        """正面偏好要能影響排序。

        recommendation.py 一直有讀 prefs["likes"] 加分，但沒有人產生它，
        所以「我要當歸的」以前跟「有什麼推薦？」拿到一模一樣的清單。
        """
        parsed = parse_preferences("我要當歸的")
        self.assertEqual(parsed["likes"], ["當歸"])

        catalog = {
            "categories": [
                {
                    "name": "全部",
                    "items": [
                        {"name": "什錦炒飯", "price": 65},
                        {"name": "當歸麵", "price": 65},
                    ],
                }
            ]
        }
        liked = recommend(catalog, parsed, top_k=1)
        plain = recommend(catalog, parse_preferences("有什麼推薦？"), top_k=1)
        self.assertEqual(liked["items"][0]["name"], "當歸麵")
        self.assertNotEqual(liked["items"][0]["name"], plain["items"][0]["name"])

    def test_negation_is_not_read_as_a_like(self):
        """「不要」含「要」、「不吃」含「吃」——單字觸發詞會把否定讀成喜歡。

        後三個是「否定詞 + 正面觸發詞」：「不想吃」含「想吃」、「不喜歡」含
        「喜歡」，光靠多字詞擋不住，要再加否定的 lookbehind。
        """
        for text in ("我不要辣", "我不吃麵", "不要飲料", "我不要當歸",
                     "我不想吃麵", "我不喜歡蓮子", "我不愛吃辣"):
            self.assertIsNone(parse_preferences(text).get("likes"), text)

    def test_negated_cues_become_excludes(self):
        self.assertEqual(parse_preferences("我不想吃麵").get("excludes"), ["麵"])
        self.assertEqual(parse_preferences("我不喜歡蓮子").get("excludes"), ["蓮子"])

    def test_repeated_cue_does_not_leak_into_the_term(self):
        """「我不吃麵、不吃粥」切出來的第二段會夾帶「不吃」。

        留著的話 AI 會把「不吃粥」當成一道菜名唸出來。
        """
        self.assertEqual(parse_preferences("我不吃麵、不吃粥").get("excludes"), ["麵", "粥"])

    def test_spice_phrasings_and_their_negations(self):
        """「想吃辣」以前完全沒被收，「不愛吃辣」則被「愛吃辣」搶去判成中辣。"""
        for text in ("我想吃辣的", "我愛吃辣", "想吃辣一點"):
            self.assertEqual(parse_preferences(text).get("spiceLevel"), "中辣", text)
        for text in ("我不想吃辣", "我不愛吃辣", "我不要辣", "不敢吃辣"):
            self.assertEqual(parse_preferences(text).get("spiceLevel"), "不辣", text)

    def test_disliking_a_term_drops_it_from_likes(self):
        base: dict = {}
        merge_preference_delta(base, parse_preferences("我要當歸的"))
        merge_preference_delta(base, parse_preferences("也喜歡蓮子"))
        self.assertEqual(base["likes"], ["當歸", "蓮子"])
        merge_preference_delta(base, parse_preferences("不要當歸"))
        self.assertEqual(base["likes"], ["蓮子"])
        self.assertIn("當歸", base["excludes"])


if __name__ == "__main__":
    unittest.main()
