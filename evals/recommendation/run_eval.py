"""推薦結果合理性檢查。

為什麼需要這支 script：tests/ 用的是人工編的小菜單，而且推薦品質沒有任何
斷言。實際跑真實菜單才發現，大肥鵝「四個人 預算3000」會推一瓶 $2090 的
威士忌當菜、還有一道「$0」的時價時蔬，而系統自己記錄 budgetViolation=false。
那兩個錯誤在 246 個測試全綠的狀態下存在了很久，因為沒有人在看推薦結果。

跟 evals/menu_ocr/ 不同，這支**不打 API**：recommend() 是純本機計算，所以
可以進 CI，每次改推薦規則、菜單標註規則都跑得起。

菜單直接讀專案根目錄的 menu_*.json，不經資料庫，結果不受本機資料影響。

用法：
    python evals/recommendation/run_eval.py            # 跑全部案例
    python evals/recommendation/run_eval.py --verbose  # 連通過的品項也列出來
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preference_engine import parse_preferences  # noqa: E402
from recommendation import recommend  # noqa: E402
from storage.importer import menu_document_to_runtime  # noqa: E402


def load_menus() -> dict[str, dict]:
    """讀專案根目錄的菜單檔。menu_*.json 有兩種格式（爬蟲的 menu_items、
    辨識的 categories），menu_document_to_runtime 兩種都吃。"""
    menus = {}
    for path in sorted(PROJECT_ROOT.glob("menu*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            name, menu = menu_document_to_runtime(data, path.stem.removeprefix("menu_"))
            menus[name] = menu
        except Exception as exc:  # 壞掉的檔案不該讓整份評測停掉
            print(f"[略過] 讀不到 {path.name}：{exc}")
    return menus

# 品名／分類出現這些字就是酒。沒要酒卻推酒，是使用者一眼看得出來的錯。
ALCOHOL_TERMS = (
    "啤酒", "紅酒", "白酒", "烈酒", "威士忌", "白蘭地", "伏特加", "琴酒",
    "清酒", "梅酒", "高粱", "調酒", "雞尾酒", "沙瓦", "香檳",
    "beer", "wine", "whisky", "whiskey", "vodka", "sake", "cocktail", "champagne",
)
WANTS_ALCOHOL = ("酒", "beer", "wine")


def _is_alcohol(name: str, category: str) -> bool:
    value = f"{category} {name}".casefold()
    return any(term.casefold() in value for term in ALCOHOL_TERMS)


def check(case: dict, menu: dict) -> list[str]:
    """回傳這個案例違反的規則；空清單代表通過。"""
    text = case["text"]
    prefs = parse_preferences(text)
    result = recommend(menu, prefs)
    items = result.get("items") or []
    failures = []

    if not items:
        return ["沒有推薦任何品項"]

    budget = prefs.get("budget")
    subtotal = sum(row["price"] for row in items if row.get("price") is not None)
    if budget is not None and subtotal > budget + 1e-9:
        failures.append(f"小計 {subtotal:g} 超過預算 {budget:g}")

    allow_alcohol = case.get("allowAlcohol") or any(term in text for term in WANTS_ALCOHOL)
    if not allow_alcohol:
        drunk = [r["name"] for r in items if _is_alcohol(r["name"], r.get("category", ""))]
        if drunk:
            failures.append("沒要求卻推了酒：" + "、".join(drunk))

    # 時價品項價格未知，放進有預算的推薦等於當成 0 元。
    if budget is not None:
        unknown = [r["name"] for r in items if r.get("price") is None]
        if unknown:
            failures.append("有預算時推了價格未知的品項：" + "、".join(unknown))

    mains = [r for r in items if r.get("type") == "main"]
    if len(mains) < int(case.get("expectMains", 1)):
        failures.append(f"主餐只有 {len(mains)} 道（至少要 {case.get('expectMains', 1)} 道）")

    drinks_as_food = [
        r["name"] for r in items
        if r.get("type") in {"main", "side"} and _is_alcohol(r["name"], r.get("category", ""))
    ]
    if drinks_as_food:
        failures.append("酒被當成餐點：" + "、".join(drinks_as_food))

    names = [r["name"] for r in items]
    if len(names) != len(set(names)):
        failures.append("同一道菜重複推薦")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="推薦結果合理性檢查（不打 API）")
    parser.add_argument("--verbose", action="store_true", help="連通過的案例也列出推薦品項")
    args = parser.parse_args()

    cases = json.loads((Path(__file__).parent / "cases.json").read_text(encoding="utf-8"))["cases"]
    menus = load_menus()

    failed = 0
    for case in cases:
        name, text = case["restaurant"], case["text"]
        menu = menus.get(name)
        if menu is None:
            print(f"[略過] {name}：專案裡沒有這家的 menu_*.json")
            continue
        failures = check(case, menu)
        if failures:
            failed += 1
            print(f"[失敗] {name}「{text}」")
            for line in failures:
                print(f"        - {line}")
        else:
            print(f"[通過] {name}「{text}」")
        if args.verbose or failures:
            result = recommend(menu, parse_preferences(text))
            for row in result.get("items") or []:
                price = "時價" if row.get("price") is None else f"${row['price']:g}"
                print(f"          {row['name']}　{price}　({row.get('type')})")

    total = len(cases)
    print(f"\n{total - failed}/{total} 個案例通過")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
