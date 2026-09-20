import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import menu_semantics  # noqa: E402
import menu_vocabulary  # noqa: E402
import recommendation  # noqa: E402
import table_menu  # noqa: E402
from menu_semantics import is_alcohol, role_of  # noqa: E402


class SingleSourceTest(unittest.TestCase):
    """字表只能有一份。

    這裡鎖的不是某個字，是「不要再各寫一份」。之前 MEAT_TERMS 有兩份：
    一份有培根火腿貢丸、另一份有叉燒松阪，於是「松阪豬」在分桶時算肉、
    在素食判斷時不算肉。同一類 bug（時價、分類規則）已經修過兩次。
    """

    def test_modules_do_not_define_their_own_copies(self):
        for module in (menu_semantics, table_menu, recommendation):
            for name in ("MEAT_TERMS", "SEAFOOD_TERMS", "VEGETABLE_TERMS", "ALCOHOL_TERMS"):
                value = getattr(module, name, None)
                if value is None:
                    continue
                self.assertIs(
                    value,
                    getattr(menu_vocabulary, name),
                    f"{module.__name__}.{name} 自己又寫了一份，請改用 menu_vocabulary",
                )

    def test_recommender_has_no_second_classifier(self):
        # 推薦器原本有一份 _classify()，跟 menu_semantics 的角色判斷重複 79 行。
        self.assertFalse(hasattr(recommendation, "_classify"))

    def test_meat_means_meat_on_both_paths(self):
        """同一道菜在兩條路徑上必須是同一種東西。

        「松阪」「叉燒」原本只在分桶那份字表裡，素食判斷那份沒有，
        所以松阪豬會同時是「肉類」和「可以給吃素的人」。
        """
        for name in ("松阪豬肉飯", "叉燒拼盤", "培根蛋炒飯", "鮮蝦炒飯"):
            flags, conflicts = menu_semantics._dietary(name, None)
            self.assertIn("vegetarian", conflicts, f"{name} 應該被判定為葷食")
            self.assertNotIn("vegetarian", flags, name)
            self.assertIn(table_menu.bucket_of(name), {"主食", "肉類", "海鮮"}, name)

    def test_alcohol_is_one_rule_everywhere(self):
        self.assertTrue(is_alcohol("蘇格登15年", "烈酒區"))
        self.assertTrue(is_alcohol("海尼根啤酒500CC", "啤酒區"))
        # sake 不能列：日文的鮭魚也是 sake，酒蒸料理的英文譯名也有。
        self.assertFalse(is_alcohol("酒蒸海鮮石鍋燒 (Sake-Steamed Seafood Stone Pot)"))
        self.assertFalse(is_alcohol("全酒麻油雞鍋"))
        self.assertEqual(role_of("蘇格登15年", "烈酒區"), "drink")


if __name__ == "__main__":
    unittest.main()
