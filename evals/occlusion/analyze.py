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


def merge_runs(runs: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """同一組的多次執行（例如重跑逾時的案例）依案例合併；成功的結果優先。

    技術失敗（API 逾時等）不算模型的表現，另外列出，不進結果分類。
    """
    rows: dict[str, dict[str, Any]] = {}
    for run in runs:
        for row in run["cases"]:
            if row.get("status") == "ok" or row["case"] not in rows:
                rows[row["case"]] = row
    ok = [row for row in rows.values() if row.get("status") == "ok"]
    failed = [
        {"case": row["case"], "status": row.get("status"), "error": str(row.get("error"))[:200]}
        for row in rows.values() if row.get("status") != "ok"
    ]
    return {"cases": ok}, failed


_OCR_CACHE: dict[Path, list[dict[str, Any]]] = {}


def _ocr_cached(path: Path) -> list[dict[str, Any]]:
    """B、C 兩組用同一批影像，OCR 只跑一次。"""
    import ocr_baseline

    if path not in _OCR_CACHE:
        _OCR_CACHE[path] = ocr_baseline.ocr_lines(path.read_bytes())
    return _OCR_CACHE[path]


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

            lines = _ocr_cached(case_dir / f"{case}.png")
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
            status = None
            if match:
                status = next(
                    ((i.get("priceInfo") or {}).get("status") for i in items if i.get("name") == match["predicted"]),
                    None,
                )
            records.append({
                "case": case,
                "priceStatus": status,
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
            "abstainAsQuestionMark": sum(
                1 for r in rows if r["outcome"] == "abstain" and r.get("priceStatus") == "unreadable"
            ),
            "wrongFlaggedByCrossCheck": sum(1 for r in wrong if r["crossFlags"]),
            "wrongFlagKinds": {
                flag: sum(1 for r in wrong if flag in r["crossFlags"])
                for flag in sorted({f for r in wrong for f in r["crossFlags"]})
            },
        }
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--b", required=True, nargs="+", help="B 組（預設提示詞）的 run label 或路徑，可多個")
    parser.add_argument("--c", required=True, nargs="+", help="C 組（VISION_PRICE_TEXT=1）的 run，可多個（含重跑）")
    args = parser.parse_args()
    case_dir = HERE / args.split
    result: dict[str, Any] = {"split": args.split, "groups": {}}
    for group, labels, cross in (("B", args.b, True), ("C", args.c, True)):
        runs = [load_run(label) for label in labels]
        merged, failed = merge_runs(runs)
        records = analyze_run(merged, case_dir, cross)
        result["groups"][group] = {
            "labels": [run.get("label") for run in runs],
            "config": {k: runs[0]["config"].get(k) for k in ("visionModel", "visionPriceText", "matchThreshold")},
            "technicalFailures": failed,
            "summary": summarize(records),
            "records": records,
        }
    out = ROOT / "evals" / "results" / "stepB"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"occlusion-{args.split}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    for group, data in result["groups"].items():
        if data["technicalFailures"]:
            print(f"{group} 組技術失敗（不計入結果）：", [f["case"] for f in data["technicalFailures"]])
    print(f"{'組別/處理':22}{'n':>4}{'棄答':>6}{'錯數字':>7}{'對數字':>7}{'漏項':>6}{'錯數字被標記':>10}")
    for group, data in result["groups"].items():
        for key, row in data["summary"].items():
            print(f"{group}/{key:20}{row['n']:>4}{row['abstain']:>6}{row['wrong_number']:>7}"
                  f"{row['right_number']:>7}{row['missing']:>6}{row['wrongFlaggedByCrossCheck']:>10}")
    print(f"已存到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
