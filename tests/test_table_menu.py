import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from table_menu import bucket_of, compose_table, is_shared_table, table_size  # noqa: E402


def rows(*specs):
    return [
        {"name": name, "price": price, "category": category, "family": name}
        for name, price, category in specs
    ]


class BucketTest(unittest.TestCase):
    def test_hot_pot_by_name_suffix_even_without_category(self):
        # 「鍋$」曾經因為品名後面被接上空白的分類而永遠對不到。
        self.assertEqual(bucket_of("東北酸白菜肉鍋"), "湯鍋")
        self.assertEqual(bucket_of("蟹黃豆腐煲"), "湯鍋")
        self.assertEqual(bucket_of("招牌雞", "經典鍋物"), "湯鍋")

    def test_rice_and_noodles_are_staples_not_hot_pot(self):
        self.assertEqual(bucket_of("玉子鮭魚石鍋飯"), "主食")
        self.assertEqual(bucket_of("鍋燒意麵"), "主食")

    def test_vegetable_terms_that_come_with_meat(self):
        # 明確的蔬菜名一律算青菜，葷素同名的（筍、菇）有肉就算肉。
        self.assertEqual(bucket_of("鵝油高麗菜"), "青菜")
        self.assertEqual(bucket_of("清炒蘆筍"), "青菜")
        self.assertEqual(bucket_of("蘆筍牛肉"), "肉類")
        self.assertEqual(bucket_of("控肉桂竹筍"), "肉類")


class SharedTableTest(unittest.TestCase):
    def test_detection_uses_menu_shape(self):
        self.assertTrue(is_shared_table(135, 1))    # 大肥鵝
        self.assertTrue(is_shared_table(81, 9))     # 森森燒肉
        self.assertFalse(is_shared_table(28, 9))    # 斗六當歸鴨
        self.assertFalse(is_shared_table(8, 7))     # 奔頂牛排
        self.assertFalse(is_shared_table(20, 0))    # 品項太少，看不出是合菜店

    def test_table_size_is_people_plus_one(self):
        self.assertEqual(table_size(4), 5)
        self.assertEqual(table_size(6), 7)
        self.assertEqual(table_size(1), 3)   # 最少三道，不然湊不成一桌
        self.assertEqual(table_size(20), 10)  # 上限，免得推一整本菜單


class ComposeTableTest(unittest.TestCase):
    def menu_rows(self):
        return rows(
            ("清炒時蔬", 250, "季節時蔬"),
            ("鵝油高麗菜", 220, "季節時蔬"),
            ("酸白菜肉鍋", 880, "經典鍋物"),
            ("魷魚螺肉蒜鍋", 1080, "經典鍋物"),
            ("燒臘雙拼盤", 480, "潮粵燒臘"),
            ("川味水煮牛", 680, "明火好味"),
            ("菠蘿蝦球", 450, "明火好味"),
            ("蒜香蒸魚", 520, "海鮮區"),
            ("櫻花蝦炒飯", 300, "主食"),
            ("什錦水果盤", 300, "水果甜品"),
        )

    def test_table_has_vegetable_soup_and_staple(self):
        table = compose_table(self.menu_rows(), people=4, budget=3000)
        buckets = [row["bucket"] for row in table]
        self.assertEqual(len(table), 5)
        for required in ("青菜", "湯鍋", "主食"):
            self.assertIn(required, buckets)

    def test_stays_within_budget(self):
        table = compose_table(self.menu_rows(), people=6, budget=2500)
        self.assertLessEqual(sum(row["price"] for row in table), 2500)

    def test_at_most_two_dishes_from_one_bucket(self):
        table = compose_table(self.menu_rows(), people=6, budget=6000)
        for bucket in {row["bucket"] for row in table}:
            self.assertLessEqual(sum(1 for row in table if row["bucket"] == bucket), 2)

    def test_market_price_items_are_left_out_when_a_budget_is_set(self):
        pool = [*self.menu_rows(), *rows(("時價龍蝦", None, "海鮮區"))]
        table = compose_table(pool, people=4, budget=3000)
        self.assertNotIn("時價龍蝦", [row["name"] for row in table])
        # 沒有預算就無所謂，時價可以出現
        self.assertTrue(compose_table(pool, people=4, budget=None))


if __name__ == "__main__":
    unittest.main()
