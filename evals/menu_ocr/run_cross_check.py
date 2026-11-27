"""節點 5 開發實驗：VLM（第二項已保存的回應）× OCR 交叉複核，只用開發資料。

不打 API：VLM 端重播第二項 --debug 存下的原始回應，經目前的解析器（menu_vision）
轉成結構化結果；OCR 端在本機執行。每個 VLM 品項依真值判定對錯，比較三種複核政策：

  none      不複核（使用者直接用 VLM 結果）
  null_only 只看「價格不明」的品項（現行確認畫面的提示）
  cross     D 組：VLM 與 OCR 不一致才標記
  all       全部逐項檢查（等同人工核對整份菜單）

執行：python -X utf8 evals/menu_ocr/run_cross_check.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "src"))

import ocr_baseline  # noqa: E402
from cross_check import cross_check  # noqa: E402
from run_eval import _price_equal, match_items  # noqa: E402

STEP2 = ROOT / "evals" / "results" / "2026-11-26-step2"
SETS = {
    "dev": ROOT / "菜單照片資料" / "第二項" / "開發原圖",
    "stress": ROOT / "菜單照片資料" / "第二項" / "開發合成干擾",
}
OUT = ROOT / "evals" / "results" / "step5"


def vlm_result(split: str, case: str) -> dict[str, Any] | None:
    import menu_vision

    runs = sorted((STEP2 / split / f"{case}__r1").glob("attempt-*/*.json"))
    if not runs:
        return None
    row = json.loads(runs[-1].read_text(encoding="utf-8"))["cases"][0]
    response = (row.get("exchanges") or [{}])[0].get("response")
    if not response:
        return None
    parsed, _ = menu_vision._extract_json_document(response)
    return menu_vision.normalize_vision_result(parsed)


def evaluate(split: str, case: str, image: Path, truth: list[dict[str, Any]]) -> dict[str, Any] | None:
    result = vlm_result(split, case)
    if result is None:
        return None
    lines = ocr_baseline.ocr_lines(image.read_bytes())
    checked = cross_check(result, lines)
    predicted = [{"name": item["name"], "price": item.get("price")} for item in checked["items"]]
    matching = match_items(truth, predicted)
    by_pred = {m["predicted"]: m for m in matching["matches"]}
    spurious = {item["name"] for item in matching["spurious"]}

    rows = []
    for item in checked["items"]:
        match = by_pred.get(item["name"])
        if item["name"] in spurious or match is None:
            error = "spurious"
        elif match["expectedPrice"] is None and item.get("price") is None:
            error = None
        elif not _price_equal(match["expectedPrice"], item.get("price")):
            error = "price"
        else:
            error = None
        rows.append({
            "name": item["name"],
            "price": item.get("price"),
            "error": error,
            "nameTypo": bool(match and not match["exactName"] and not match["reordered"]),
            "flags": item["review"]["flags"],
            "detail": item["review"]["detail"],
        })
    missed = [m["name"] for m in matching["missed"]]
    orphan_hits = sum(
        1 for orphan in checked["orphans"]
        if match_items([{"name": n, "price": None} for n in missed],
                       [{"name": orphan["dish"], "price": orphan["price"]}])["matches"]
    )
    return {
        "split": split, "case": case, "items": rows, "missed": missed,
        "orphans": checked["orphans"], "orphanHitsMissed": orphan_hits,
    }


def policy_counts(rows: list[dict[str, Any]], policy: str) -> dict[str, int]:
    def flagged(row: dict[str, Any]) -> bool:
        return {
            "none": False,
            "null_only": row["price"] is None,
            "cross": bool(row["flags"]),
            "all": True,
        }[policy]

    errors = [row for row in rows if row["error"]]
    return {
        "flagged": sum(flagged(row) for row in rows),
        "errorsCaught": sum(flagged(row) for row in errors),
        "errorsResidual": sum(not flagged(row) for row in errors),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    for split, directory in SETS.items():
        for truth_path in sorted(directory.glob("*.expected.json")):
            case = truth_path.name.removesuffix(".expected.json")
            image = directory / f"{case}.png"
            if not image.is_file():
                continue
            truth = json.loads(truth_path.read_text(encoding="utf-8"))["items"]
            truth = [{"name": row["name"], "price": row.get("price")} for row in truth]
            record = evaluate(split, case, image, truth)
            if record:
                cases.append(record)
                print(f"  {split:6} {case:32} items={len(record['items']):3} "
                      f"errors={sum(1 for r in record['items'] if r['error']):2} "
                      f"flagged={sum(1 for r in record['items'] if r['flags']):2} "
                      f"missed={len(record['missed'])} orphanHits={record['orphanHitsMissed']}")

    summary: dict[str, Any] = {}
    for split in ("dev", "stress", "all"):
        rows = [row for c in cases if split in ("all", c["split"]) for row in c["items"]]
        missed = sum(len(c["missed"]) for c in cases if split in ("all", c["split"]))
        orphan_hits = sum(c["orphanHitsMissed"] for c in cases if split in ("all", c["split"]))
        errors = sum(1 for row in rows if row["error"])
        summary[split] = {
            "items": len(rows),
            "errors": errors,
            "errorKinds": {k: sum(1 for r in rows if r["error"] == k) for k in ("price", "spurious")},
            "missedTruthItems": missed,
            "missedCaughtByOrphans": orphan_hits,
            "policies": {p: policy_counts(rows, p) for p in ("none", "null_only", "cross", "all")},
            "flagKinds": {
                flag: sum(1 for r in rows if flag in r["flags"])
                for flag in ("price_ambiguous", "name_not_in_ocr", "ocr_price_mismatch", "price_unverified")
            },
        }

    record = {
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "vlmSource": "evals/results/2026-11-26-step2 第 1 輪 --debug 原始回應，經目前 menu_vision 解析",
        "ocr": ocr_baseline.engine_info(),
        "summary": summary,
        "cases": cases,
    }
    path = OUT / "cross-check-dev.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"已存到 {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
