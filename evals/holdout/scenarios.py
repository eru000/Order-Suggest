"""節點 H：封存組互動需求情境的揭示（預先登錄規則 (4)）。

封存的盲跑 ZIP 由 evals/step2/scenarios/run_eval.py --mode holdout-blind 產生，內含完整
輸出與自動評分。這裡只讀 ZIP、彙整，不重跑也不改分數：
- 第二項 11-26 的兩次盲跑（當時的程式）
- 凍結（freeze-final）後的一次盲跑
兩者並列，差異歸因於程式版本。

    python -X utf8 evals/holdout/scenarios.py
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evals" / "step2" / "scenarios"
OUT = ROOT / "evals" / "results" / "holdout"


def load(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        data = json.loads(archive.read("sealed_results.json").decode("utf-8"))
    execution = data["execution"]
    cases = []
    for case in execution["cases"]:
        turns = [
            {"turn": t["turn"], "text": t["expected"]["text"], "passed": t["passed"],
             "errors": t["errors"], "technicalErrors": t["technicalErrors"]}
            for t in case["turns"]
        ]
        cases.append({
            "id": case["id"], "category": case["category"],
            "passed": all(t["passed"] for t in turns), "turns": turns,
        })
    return {
        "archive": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "generatedAt": data.get("generatedAt"),
        "productSourceHashes": data.get("productSourceHashes"),
        "passedCases": execution["passedCases"], "caseCount": execution["caseCount"],
        "passedTurns": execution["passedTurns"], "turnCount": execution["turnCount"],
        "cases": cases,
    }


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    runs = [load(path) for path in sorted(SCENARIOS.glob("holdout_blind_*.zip"))]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scenarios.json").write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    for run in runs:
        print(f"{run['archive']}：情境 {run['passedCases']}/{run['caseCount']}，"
              f"輪 {run['passedTurns']}/{run['turnCount']}")
        for case in run["cases"]:
            failed = [t for t in case["turns"] if not t["passed"]]
            print(f"  {'通過' if case['passed'] else '失敗'} {case['id']:28} {case['category']:10} "
                  + "；".join(f"第{t['turn']}輪 {t['text']} → {'、'.join(t['errors'])}" for t in failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
