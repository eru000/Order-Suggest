"""節點 H：封存組遮蔽實驗的彙整（讀 evals/results/stepB/occlusion-holdout.json）。

analyze.py 以菜名模糊配對找「被遮蔽的那一列」。被遮蔽的列整列沒讀出來時，配對會
接到同名不同杯型或相近菜名的另一列（例如「砂鍋菇菇雞湯（中）」配到「（大）」），
把那一列正確的價格當成「錯數字」。這裡另外標出這類配對假象：配到的品名在標準答案
裡存在、且價格完全相同，就改記為「漏項」。這是事後的計分修正，原始分類保留在
occlusion-holdout.json。

    python -X utf8 evals/holdout/occlusion_summary.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "evals" / "results" / "stepB" / "occlusion-holdout.json"
TRUTHS = ROOT / "菜單照片資料" / "第二項" / "獨立封存"
OUT = ROOT / "evals" / "results" / "holdout" / "occlusion-summary.json"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    data = json.loads(SOURCE.read_text(encoding="utf-8"))["groups"]["B"]
    truths: dict[str, dict[str, float | None]] = {}
    table: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    rows = []
    for record in data["records"]:
        base = record["case"].split("__")[0]
        if base not in truths:
            doc = json.loads((TRUTHS / f"{base}.expected.json").read_text(encoding="utf-8"))
            truths[base] = {item["name"]: item.get("price") for item in doc["items"]}
        outcome = record["outcome"]
        predicted = record.get("predicted") or {}
        artifact = (
            outcome == "wrong_number"
            and predicted.get("name") != record["item"]
            and predicted.get("name") in truths[base]
            and truths[base][predicted["name"]] == predicted.get("price")
        )
        corrected = "missing" if artifact else outcome
        flags = set(record.get("crossFlags") or [])
        for key in (record["treatment"], "all_" + record["treatment"].split("_")[1]):
            table[key]["n"] += 1
            table[key][corrected] += 1
            if corrected == "wrong_number":
                table[key]["wrongCaughtByPlausibility"] += "price_outlier" in flags
                table[key]["wrongCaughtByOcr"] += bool(flags - {"price_outlier"})
                table[key]["wrongCaughtByEither"] += bool(flags)
        rows.append({**record, "matchArtifact": artifact, "correctedOutcome": corrected})
    summary = {key: dict(value) for key, value in sorted(table.items())}
    OUT.write_text(json.dumps({
        "source": str(SOURCE.relative_to(ROOT)),
        "technicalFailures": data["technicalFailures"],
        "matchArtifacts": sum(r["matchArtifact"] for r in rows),
        "summary": summary, "records": rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"技術失敗（不計）：{[f['case'] for f in data['technicalFailures']]}")
    print(f"配對假象改記為漏項：{sum(r['matchArtifact'] for r in rows)}")
    print(f"{'處理':14}{'n':>4}{'棄答':>6}{'漏項':>6}{'錯數字':>7}{'對數字':>7}{'合理性抓到':>8}{'OCR抓到':>8}{'任一':>6}")
    for key, v in summary.items():
        print(f"{key:14}{v['n']:>4}{v.get('abstain', 0):>6}{v.get('missing', 0):>6}"
              f"{v.get('wrong_number', 0):>7}{v.get('right_number', 0):>7}"
              f"{v.get('wrongCaughtByPlausibility', 0):>8}{v.get('wrongCaughtByOcr', 0):>8}"
              f"{v.get('wrongCaughtByEither', 0):>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
