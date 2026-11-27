"""節點 6：提問策略消融實驗（模擬使用者，只用開發菜單，不打 API）。

每位模擬使用者心裡有一道「目標菜」，誠實回答系統的提問：
  - 飯麵／湯乾／主食材／口味／份量：目標菜在該欄有標註且是選項之一就照答，否則按「都可以」
  - 預算：選「不低於目標菜價格」的最小選項；沒有就按「先不設上限」
  - 比較題：目標菜在選項裡就選它，否則跳過
對每份非合菜菜單的每一道候選菜各模擬一次，四種策略完全相同的使用者、相同的推薦器。

指標
  hit@3        最後推薦的卡片中有目標菜
  questions    實際提問數（上限 MAX_QUESTIONS＝2，比較題另計）
  skipped      使用者按「都可以」的次數（這題對他沒有幫助）
  noMatch      最後沒有任何推薦

執行：python -X utf8 evals/questions/run_ablation.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evals" / "recommendation"))

import decision_service  # noqa: E402
from decision_catalog import menu_choices, shared_table_menu  # noqa: E402
from decision_service import QUESTION_STRATEGIES, DecisionService  # noqa: E402
from run_eval import load_menus  # noqa: E402
from session_store import SessionStore  # noqa: E402

DEV_TRUTHS = ROOT / "菜單照片資料" / "第二項" / "開發原圖"
# 節點 H：凍結後以同一協定換成封存組菜單（--split holdout）
HOLDOUT_TRUTHS = ROOT / "菜單照片資料" / "第二項" / "獨立封存"
MAX_TARGETS_PER_MENU = 60


def truth_menus(directory: Path, prefix: str) -> dict[str, dict[str, Any]]:
    menus: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.expected.json")):
        truth = json.loads(path.read_text(encoding="utf-8"))
        name = prefix + path.name.removesuffix(".expected.json")
        menus[name] = {
            "serviceRate": 0,
            "categories": [{"name": "菜單", "items": [
                {"name": row["name"], "price": row.get("price")} for row in truth["items"]
            ]}],
        }
    return menus


def dev_menus() -> dict[str, dict[str, Any]]:
    return {**load_menus(ROOT), **truth_menus(DEV_TRUTHS, "dev_")}


def answer(question: dict[str, Any], target: dict[str, Any]) -> tuple[str, bool]:
    """回傳 (選項值, 是否等於跳過)。"""
    field = question["field"]
    values = [option["value"] for option in question["options"]]
    if field == "comparison":
        return (target["id"], False) if target["id"] in values else ("skip", True)
    if field == "budget":
        price = target.get("price")
        numeric = sorted(float(v) for v in values if v != "skip")
        fitting = [v for v in numeric if price is not None and v >= price]
        if fitting:
            return (f"{fitting[0]:g}", False)
        return ("skip", True)
    value = target.get(field)
    if value in values:
        return (value, False)
    return ("skip", True)


def simulate(name: str, menu: dict[str, Any], target: dict[str, Any], strategy: str) -> dict[str, Any]:
    """strategy＝"none" 時不回答任何問題，直接按「直接推薦」，作為「問了有沒有用」的對照。"""
    service = DecisionService(
        SessionStore(), {name: menu}, name, question_strategy="gini" if strategy == "none" else strategy
    )
    session = f"sim_{strategy}_{target['id'][:12]}"
    view = service.handle(session, action="start", text="不知道吃什麼")
    asked, skipped, fields = 0, 0, []
    for _ in range(6):
        if view.get("type") != "question":
            break
        if strategy == "none":
            view = service.handle(session, action="recommend", revision=view["revision"])
            continue
        question = view["question"]
        value, was_skip = answer(question, target)
        if question["field"] != "comparison":
            asked += 1
        skipped += was_skip
        fields.append(question["field"])
        view = service.handle(session, action="answer", value=value, revision=view["revision"])
    cards = view.get("items") or []
    return {
        "hit": any(card.get("id") == target["id"] for card in cards),
        "questions": asked,
        "skipped": skipped,
        "noMatch": not cards,
        "fields": fields,
    }


def main() -> int:
    split = "holdout" if "--split" in sys.argv and sys.argv[-1] == "holdout" else "dev"
    decision_service._QUESTION_RANDOM.seed(20261127)
    menus = truth_menus(HOLDOUT_TRUTHS, "holdout_") if split == "holdout" else dev_menus()
    records = []
    for name, menu in sorted(menus.items()):
        if shared_table_menu(menu):
            continue  # 合菜店只問人數，策略無從比較
        rows, _ = menu_choices(menu)
        targets = sorted(rows, key=lambda row: row["id"])[:MAX_TARGETS_PER_MENU]
        for strategy in ("none", *QUESTION_STRATEGIES):
            for target in targets:
                result = simulate(name, menu, target, strategy)
                records.append({"menu": name, "strategy": strategy, "target": target["name"], **result})
        print(f"  {name:32} 候選 {len(rows):3}，模擬 {len(targets)} 位使用者")

    summary: dict[str, Any] = {}
    by_strategy = defaultdict(list)
    for record in records:
        by_strategy[record["strategy"]].append(record)
    for strategy, rows in by_strategy.items():
        n = len(rows)
        first = defaultdict(int)
        for row in rows:
            if row["fields"]:
                first[row["fields"][0]] += 1
        summary[strategy] = {
            "users": n,
            "hitAt3": round(sum(r["hit"] for r in rows) / n, 4),
            "meanQuestions": round(sum(r["questions"] for r in rows) / n, 3),
            "skippedPerUser": round(sum(r["skipped"] for r in rows) / n, 3),
            "noMatchRate": round(sum(r["noMatch"] for r in rows) / n, 4),
            "firstQuestion": dict(sorted(first.items(), key=lambda kv: -kv[1])),
        }
    # 成對比較（同一位模擬使用者）：McNemar 精確檢定，只看結果不同的配對
    from math import comb

    def mcnemar(a: str, b: str) -> dict[str, Any]:
        key = lambda r: (r["menu"], r["target"])  # noqa: E731
        hits_a = {key(r): r["hit"] for r in by_strategy[a]}
        hits_b = {key(r): r["hit"] for r in by_strategy[b]}
        only_a = sum(1 for k in hits_a if hits_a[k] and not hits_b[k])
        only_b = sum(1 for k in hits_a if hits_b[k] and not hits_a[k])
        n, k = only_a + only_b, min(only_a, only_b)
        p = min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n) if n else 1.0
        return {"onlyA": only_a, "onlyB": only_b, "pTwoSided": round(p, 4)}

    pairs = {f"{a}_vs_{b}": mcnemar(a, b) for a, b in (
        ("gini", "none"), ("gini", "entropy"), ("gini", "fixed"), ("gini", "random"),
    )}
    out = ROOT / "evals" / "results" / ("holdout" if split == "holdout" else "step6")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"question-ablation-{split}.json"
    path.write_text(json.dumps({
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "maxQuestions": decision_service.MAX_QUESTIONS,
        "summary": summary,
        "pairedTests": pairs,
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{'策略':10}{'使用者':>6}{'hit@3':>8}{'平均提問':>8}{'無效回答':>8}{'無推薦':>8}  第一題分布")
    for strategy in ("none", *QUESTION_STRATEGIES):
        s = summary[strategy]
        print(f"{strategy:10}{s['users']:>6}{s['hitAt3']:>8.1%}{s['meanQuestions']:>8.2f}"
              f"{s['skippedPerUser']:>8.2f}{s['noMatchRate']:>8.1%}  {s['firstQuestion']}")
    for name, test in pairs.items():
        print(f"  {name:18} 只有前者命中 {test['onlyA']:3}，只有後者命中 {test['onlyB']:3}，p＝{test['pTwoSided']}")
    print(f"已存到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
