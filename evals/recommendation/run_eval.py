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

from menu_semantics import annotate_item, is_alcohol  # noqa: E402
from preference_engine import parse_preferences  # noqa: E402
from recommendation import _flatten_menu, recommend  # noqa: E402
from storage.importer import menu_document_to_runtime  # noqa: E402
from table_menu import table_size  # noqa: E402


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

WANTS_ALCOHOL = ("酒", "beer", "wine")
WANTS_DRINK = ("要飲料", "加飲料", "配飲料", "想喝")


def _priced_drinks(menu: dict) -> list[str]:
    """菜單上「有標價、而且不是酒」的飲料。

    沒標價的飲料在有預算時本來就不該推（時價不是 0 元），而沒點名要酒的時候
    推一杯威士忌也不算滿足「要飲料」。所以只有這份清單非空時，
    「要飲料卻沒給飲料」才算錯——森森燒肉的十項飲料全都沒標價，不該被判失敗。
    """
    names = []
    for item in _flatten_menu(menu):
        annotated = annotate_item(item, str(item.get("category") or ""))
        role = str((annotated.get("semantic") or {}).get("role") or "")
        name = str(annotated.get("name") or "")
        category = str(annotated.get("category") or "")
        if role == "drink" and annotated.get("price") is not None and not _is_alcohol(name, category):
            names.append(name)
    return names


def _is_alcohol(name: str, category: str) -> bool:
    # 故意用產品程式碼的同一支函式：字表只有一份，evals 才不會跟著長歪。
    # 這支 eval 檢查的是「推薦結果合不合理」，不是「字表抄對了沒」。
    return is_alcohol(name, category)


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

    if case.get("sharedTable"):
        # 合菜店：一桌人點一桌菜。道數照台灣的點法抓人數 + 1，而且一桌不能
        # 沒有青菜或湯鍋——原本的單點規則推出來的是一桌火鍋加壽司。
        people = int(prefs.get("people") or 1)
        food = [r for r in items if r.get("type") not in {"drink", "dessert"}]
        buckets = {r.get("type") for r in food}
        # 預算已經花掉七成以上就不算道數不足——是錢不夠，不是規則挑得不好。
        budget_spent = budget is not None and subtotal >= budget * 0.7
        if len(food) < min(table_size(people), 5) and not budget_spent:
            failures.append(f"一桌只有 {len(food)} 道（{people} 個人）")
        if not buckets & {"青菜"}:
            failures.append("一桌沒有青菜")
        if not buckets & {"湯鍋", "主食"}:
            failures.append("一桌沒有湯鍋也沒有主食")
        if budget is not None and subtotal < budget * 0.5:
            failures.append(f"小計 {subtotal:g} 只用掉預算 {budget:g} 的一半不到")
        for bucket in buckets:
            if sum(1 for r in food if r.get("type") == bucket) > 2:
                failures.append(f"同一類超過兩道：{bucket}")
    else:
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

    # 使用者說了要飲料就該給飲料。合菜分支曾經把整個食物預算花在菜上，
    # 剩下的錢買不起最便宜的汽水，結果「要飲料」與不講的輸出一模一樣，
    # 而上面那些規則一條都抓不到。
    if any(term in text for term in WANTS_DRINK):
        available = _priced_drinks(menu)
        if available and not any(r.get("type") == "drink" for r in items):
            failures.append(
                "要求飲料卻沒推飲料（菜單上有 %d 項標價飲料，例如 %s）"
                % (len(available), "、".join(available[:3]))
            )

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
