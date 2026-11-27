"""由公開合菜套餐推導一桌菜的組成政策（期末規劃節點 C，回應委員5-4「規則缺乏依據」）。

輸入 evals/table_rules/set_menus.json（三家連鎖餐廳官網的 18 組多人套餐），輸出：

1. 道數：主菜＋湯（不含小菜、白飯）對人數做最小平方法線性迴歸。
2. 必備類別：每類在幾組套餐裡出現；湯依人數分開算。
3. 類別比例：每組套餐各類的期望道數（每欄 picks × 該類選項比例）÷ 道數，
   取平均作為 Sainte-Laguë 分配權重，取最大值作為同類上限的比例。
4. 產品分類器 table_menu.bucket_of 對套餐菜名的正確率（店家欄名當標準答案）。

只做本機計算、不打 API。結果寫到 evals/results/table_rules/derived.json，
tests/test_table_policy_evidence.py 會檢查 table_menu.py 的常數與這裡算出的一致。

    .venv\\Scripts\\python.exe evals\\table_rules\\derive_policy.py
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "evals" / "table_rules" / "set_menus.json"
OUT = ROOT / "evals" / "results" / "table_rules" / "derived.json"
sys.path.insert(0, str(ROOT / "src"))

PROTEIN = ("肉類", "海鮮", "蛋豆腐")  # 國健署「豆魚蛋肉類」
SIDE = "小菜"


def load(path: Path = DATA) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sets(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for restaurant in data["restaurants"]:
        for item in restaurant["sets"]:
            out.append({**item, "restaurant": restaurant["id"]})
    return out


def slot_groups(slot: dict[str, Any]) -> dict[str, float]:
    """這一欄各類的期望道數：picks × 該類選項所佔比例。"""
    if slot.get("group"):
        return {slot["group"]: float(slot["picks"])}
    groups = slot["optionGroups"]
    share: dict[str, float] = defaultdict(float)
    for option in slot["options"]:
        share[groups[option]] += slot["picks"] / len(slot["options"])
    return dict(share)


def composition(item: dict[str, Any]) -> dict[str, float]:
    counts: dict[str, float] = defaultdict(float)
    for slot in item["slots"]:
        for group, value in slot_groups(slot).items():
            counts[group] += value
    return dict(counts)


def check_counts(item: dict[str, Any]) -> list[str]:
    errors = []
    dishes = sum(
        slot["picks"] for slot in item["slots"]
        if slot.get("counted", True) and not slot.get("soup")
    )
    soups = sum(slot["picks"] for slot in item["slots"] if slot.get("soup"))
    if dishes != item["titleDishes"]:
        errors.append(
            f"{item['restaurant']} {item['title']}: 欄位 {dishes} ≠ 標題 {item['titleDishes']} 菜"
        )
    if soups != item["titleSoups"]:
        errors.append(
            f"{item['restaurant']} {item['title']}: 湯 {soups} ≠ 標題 {item['titleSoups']} 湯"
        )
    return errors


def main_dishes(item: dict[str, Any]) -> float:
    """主要定義：主菜＋湯，不含小菜（各店對小菜算不算「菜」不一致）。"""
    comp = composition(item)
    return sum(value for group, value in comp.items() if group != SIDE)


def title_dishes(item: dict[str, Any]) -> int:
    return int(item["titleDishes"] + item["titleSoups"])


def ols(points: list[tuple[float, float]]) -> dict[str, float]:
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    sxy = sum((x - mx) * (y - my) for x, y in points)
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    ss_tot = sum((y - my) ** 2 for _, y in points)
    residuals = [y - (intercept + slope * x) for x, y in points]
    return {
        "n": n,
        "intercept": intercept,
        "slope": slope,
        "r2": 1 - ss_res / ss_tot,
        "rmse": math.sqrt(ss_res / n),
        "maxAbsResidual": max(abs(r) for r in residuals),
        "peopleRange": [min(x for x, _ in points), max(x for x, _ in points)],
    }


def derive(data: dict[str, Any] | None = None) -> dict[str, Any]:
    data = data or load()
    items = sets(data)
    errors = [error for item in items for error in check_counts(item)]

    main_points = [(float(item["people"]), main_dishes(item)) for item in items]
    title_points = [(float(item["people"]), float(title_dishes(item))) for item in items]
    per_restaurant = {
        rid: ols([(float(i["people"]), main_dishes(i)) for i in items if i["restaurant"] == rid])
        for rid in sorted({item["restaurant"] for item in items})
    }

    def has(item: dict[str, Any], groups: tuple[str, ...]) -> bool:
        comp = composition(item)
        return sum(comp.get(group, 0.0) for group in groups) > 0

    coverage = {
        "青菜": sum(has(i, ("青菜",)) for i in items),
        "豆魚蛋肉": sum(has(i, PROTEIN) for i in items),
        "主食(每人白飯)": sum(bool(i.get("rice")) for i in items),
        "湯鍋(2人)": [sum(has(i, ("湯鍋",)) for i in items if i["people"] == 2),
                    sum(1 for i in items if i["people"] == 2)],
        "湯鍋(3人以上)": [sum(has(i, ("湯鍋",)) for i in items if i["people"] >= 3),
                      sum(1 for i in items if i["people"] >= 3)],
        "total": len(items),
    }

    share_rows: dict[str, list[float]] = defaultdict(list)
    for item in items:
        comp = composition(item)
        total = main_dishes(item)
        for group in sorted({g for i in items for g in composition(i)} - {SIDE}):
            share_rows[group].append(comp.get(group, 0.0) / total)
    shares = {
        group: {
            "mean": sum(values) / len(values),
            "max": max(values),
            "maxCountAtSize": None,
        }
        for group, values in share_rows.items()
    }
    # 每類在同一桌最多出現幾道（期望值），對照道數
    for group in shares:
        worst = max(items, key=lambda i: composition(i).get(group, 0.0) / main_dishes(i))
        shares[group]["maxCountAtSize"] = [
            composition(worst).get(group, 0.0), main_dishes(worst),
            worst["restaurant"], worst["people"],
        ]

    classifier = classifier_accuracy(items)
    return {
        "source": str(DATA.relative_to(ROOT)),
        "retrieved": data.get("retrieved"),
        "countErrors": errors,
        "sets": len(items),
        "dishCountMain": ols(main_points),
        "dishCountTitle": ols(title_points),
        "dishCountPerRestaurant": per_restaurant,
        "points": [
            {"restaurant": i["restaurant"], "people": i["people"], "main": main_dishes(i),
             "title": title_dishes(i), "variant": i.get("variant")}
            for i in items
        ],
        "coverage": coverage,
        "shares": shares,
        "classifier": classifier,
    }


def classifier_accuracy(items: list[dict[str, Any]]) -> dict[str, Any]:
    from table_menu import bucket_of

    labelled: dict[str, str] = {}
    for item in items:
        for slot in item["slots"]:
            for option in slot["options"]:
                group = slot.get("group") or slot["optionGroups"][option]
                if group != SIDE:
                    labelled[option] = group
    wrong = []
    for name, group in sorted(labelled.items()):
        predicted = bucket_of(name)
        if predicted != group:
            wrong.append({"name": name, "label": group, "predicted": predicted})
    return {
        "dishes": len(labelled),
        "correct": len(labelled) - len(wrong),
        "accuracy": (len(labelled) - len(wrong)) / len(labelled),
        "wrong": wrong,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    result = derive()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    m = result["dishCountMain"]
    t = result["dishCountTitle"]
    print(f"套餐 {result['sets']} 組；欄位檢查錯誤 {len(result['countErrors'])}")
    for error in result["countErrors"]:
        print("  ", error)
    for label, fit in (("主菜+湯", m), ("照標題", t)):
        print(f"道數({label}) = {fit['intercept']:.3f} + {fit['slope']:.3f}×人數  "
              f"R²={fit['r2']:.3f} RMSE={fit['rmse']:.2f}")
    for rid, fit in result["dishCountPerRestaurant"].items():
        print(f"  {rid}: {fit['intercept']:.2f} + {fit['slope']:.2f}×人數 R²={fit['r2']:.3f}")
    print("必備類別出現次數:", result["coverage"])
    for group, value in sorted(result["shares"].items(), key=lambda kv: -kv[1]["mean"]):
        print(f"  {group}: 平均比例 {value['mean']:.3f} 最大 {value['max']:.3f} "
              f"{value['maxCountAtSize']}")
    c = result["classifier"]
    print(f"分類器正確率 {c['correct']}/{c['dishes']} = {c['accuracy']:.3f}")
    for row in c["wrong"]:
        print(f"  {row['name']}: 標 {row['label']} 判 {row['predicted']}")
    print("寫入", OUT.relative_to(ROOT))
    return 1 if result["countErrors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
