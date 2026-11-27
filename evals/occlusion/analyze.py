"""節點 B：分析受控遮蔽實驗（開發資料）。

讀 run_eval 在 evals/occlusion/<split> 上的執行紀錄（B 組＝預設提示詞，C 組＝
VISION_PRICE_TEXT=1），對每個「被處理過的價格」分類：

  abstain       模型回報未知（price 為 null）
  wrong_number  模型給了數字，但不是原價 → full 等級下就是「補值幻覺」
  right_number  模型給了原價（full 等級下＝猜中或遮蔽不夠徹底）
  missing       整個品項沒被讀出來

D 組＝C 組結果再跑 OCR 交叉複核（cross_check），看 wrong_number 有沒有被標記；
確認畫面本來就會把 price 為 null 的品項顯示成「價格不明」，所以 abstain 視為已提示。

執行：python -X utf8 evals/occlusion/analyze.py --split dev --b <label或路徑> --c <label或路徑>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "evals" / "menu_ocr"))
sys.path.insert(0, str(ROOT / "src"))

from run_eval import load_run, match_items  # noqa: E402

OUTCOMES = ("abstain", "wrong_number", "right_number", "missing")


def classify(true_price: float, predicted: dict[str, Any] | None) -> str:
    if predicted is None:
        return "missing"
    price = predicted.get("price")
    if price is None:
        return "abstain"
    try:
        return "right_number" if abs(float(price) - float(true_price)) < 0.01 else "wrong_number"
    except (TypeError, ValueError):
        return "wrong_number"


def occluded_items(case_dir: Path, case: str) -> list[dict[str, Any]]:
    truth = json.loads((case_dir / f"{case}.expected.json").read_text(encoding="utf-8"))
    return [row for row in truth["items"] if row.get("occlusion")]


def predicted_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    """取回結構化品項（含 priceInfo）。

    --debug 執行會保存模型原始回應，以目前的解析器重新解析（與產品相同路徑）；
    沒有原始回應時退回紀錄裡攤平的 raw.items（沒有 priceInfo）。
    """
    response = next(
        (e.get("response") for e in row.get("exchanges") or [] if isinstance(e, dict) and e.get("response")),
        None,
    )
    if response:
        import menu_vision

        parsed, _ = menu_vision._extract_json_document(response)
        result = menu_vision.normalize_vision_result(parsed)
        return [item for category in result["categories"] for item in category["items"]]
    raw = row.get("raw") or {}
    return list(raw.get("items") or []) if isinstance(raw, dict) else []


def analyze_run(run: dict[str, Any], case_dir: Path, with_cross_check: bool) -> list[dict[str, Any]]:
    records = []
    for row in run["cases"]:
        case = row["case"]
        treatment = case.split("__", 1)[1]
        targets = occluded_items(case_dir, case)
        items = predicted_items(row)
        flags_by_name: dict[str, list[str]] = {}
        if with_cross_check and items:
            import ocr_baseline
            from cross_check import cross_check

            lines = ocr_baseline.ocr_lines((case_dir / f"{case}.png").read_bytes())
            checked = cross_check({"categories": [{"name": "菜單", "items": items}]}, lines)
            flags_by_name = {item["name"]: item["review"]["flags"] for item in checked["items"]}
        matching = match_items(
            [{"name": t["name"], "price": t["true_price"]} for t in targets],
            [{"name": i.get("name"), "price": i.get("price")} for i in items],
        )
        by_expected = {m["expected"]: m for m in matching["matches"]}
        for target in targets:
            match = by_expected.get(target["name"])
            predicted = {"name": match["predicted"], "price": match["predictedPrice"]} if match else None
            outcome = classify(target["true_price"], predicted)
            flags = flags_by_name.get(match["predicted"], []) if match else []
            records.append({
                "case": case,
                "treatment": treatment,
                "level": target["occlusion"]["level"],
                "item": target["name"],
                "truePrice": target["true_price"],
                "predicted": predicted,
                "outcome": outcome,
                "crossFlags": flags,
                # 使用者會看到提示：價格不明（null）或交叉複核有標記
                "surfaced": outcome in {"abstain", "missing"} or bool(flags),
            })
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    table: dict[str, Any] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["treatment"]].append(record)
        groups[f"all_{record['level']}"].append(record)
    for key, rows in sorted(groups.items()):
        counts = {outcome: sum(1 for r in rows if r["outcome"] == outcome) for outcome in OUTCOMES}
        wrong = [r for r in rows if r["outcome"] == "wrong_number"]
        table[key] = {
            "n": len(rows),
            **counts,
            "wrongFlaggedByCrossCheck": sum(1 for r in wrong if r["crossFlags"]),
        }
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--b", required=True, help="B 組（預設提示詞）的 run label 或路徑")
    parser.add_argument("--c", required=True, help="C 組（VISION_PRICE_TEXT=1）的 run label 或路徑")
    args = parser.parse_args()
    case_dir = HERE / args.split
    result: dict[str, Any] = {"split": args.split, "groups": {}}
    for group, label, cross in (("B", args.b, True), ("C", args.c, True)):
        run = load_run(label)
        records = analyze_run(run, case_dir, cross)
        result["groups"][group] = {
            "label": run.get("label"),
            "config": {k: run["config"].get(k) for k in ("visionModel", "visionPriceText", "matchThreshold")},
            "summary": summarize(records),
            "records": records,
        }
    out = ROOT / "evals" / "results" / "stepB"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"occlusion-{args.split}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'組別/處理':22}{'n':>4}{'棄答':>6}{'錯數字':>7}{'對數字':>7}{'漏項':>6}{'錯數字被標記':>10}")
    for group, data in result["groups"].items():
        for key, row in data["summary"].items():
            print(f"{group}/{key:20}{row['n']:>4}{row['abstain']:>6}{row['wrong_number']:>7}"
                  f"{row['right_number']:>7}{row['missing']:>6}{row['wrongFlaggedByCrossCheck']:>10}")
    print(f"已存到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
