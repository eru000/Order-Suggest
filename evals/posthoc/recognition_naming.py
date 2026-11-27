"""事後敏感度分析：封存組辨識的「完整列」對菜名標註慣例有多敏感（非預先登錄）。

節點 H 發現 C 組沒對到的 51 列中，33 列是菜名寫法慣例不同（標準答案含「（L）」
「（小份）」、VLM 多寫英文名）。這裡把標準答案與兩組預測的菜名都做同樣的正規化
——去掉英文字詞、去掉括號內的杯型／份量標註——再用同一個計分器重算。主要結果仍以
節點 H 的預先登錄數字為準。

    python -X utf8 evals/posthoc/recognition_naming.py
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "src", ROOT / "evals" / "menu_ocr", ROOT / "evals" / "holdout"):
    sys.path.insert(0, str(path))

import recognition as R  # noqa: E402
from run_eval import load_cases, score_case  # noqa: E402

SIZE = re.compile(r"[（(]\s*(?:XL|L|M|S|大|中|小|小份|大份|中份|盅|杯|瓶)\s*[）)]")
ASCII_WORDS = re.compile(r"[A-Za-z][A-Za-z&'.\- ]*[A-Za-z.]|[A-Za-z]")


def normalize(name: str, strip_size: bool = True) -> str:
    if strip_size:
        name = SIZE.sub("", name)
    if re.search(r"[一-鿿]", name):
        # 只去掉括號外的英文；括號內的 M／L 是杯型，留著
        parts = re.split(r"([（(][^（）()]*[）)])", name)
        name = "".join(part if part[:1] in "（(" else ASCII_WORDS.sub("", part) for part in parts)
    return re.sub(r"\s+", "", name).strip()


def normalize_all(items: list[dict]) -> None:
    """同一份清單裡，去掉杯型後會撞名（同一款 M／L 兩杯）就保留杯型，免得把真的
    價格錯誤（L 杯讀成別的價）藏進配對裡。"""
    stripped = [normalize(item["name"]) for item in items]
    collide = {name for name in stripped if stripped.count(name) > 1}
    for item, name in zip(items, stripped, strict=True):
        item["name"] = normalize(item["name"], strip_size=False) if name in collide else name


def norm_result(result: dict) -> dict:
    result = copy.deepcopy(result)
    normalize_all([i for c in result.get("categories", []) for i in c.get("items", [])])
    return result


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    rows = []
    for case in load_cases(cases_dir=R.CASES_DIR):
        name = case["name"]
        norm_case = copy.deepcopy(case)
        normalize_all(norm_case["expected"]["items"])
        _, run = R.sealed_run(name, 1)
        vlm, _ = R.parse_response(run)
        lines = R.ocr_lines(name, Path(case["image_path"]).read_bytes())
        ocr = R.ocr_result(lines)
        c = score_case(norm_case, norm_result(vlm), R.THRESHOLD)
        a = score_case(norm_case, norm_result(ocr), R.THRESHOLD)
        rows.append({"case": name, "expected": len(case["expected"]["items"]),
                     "A": a["exactNamePriceCount"], "C": c["exactNamePriceCount"]})
        print(f"  {name:26} {rows[-1]['expected']:3}  A {rows[-1]['A']:3}  C {rows[-1]['C']:3}")
    a_ci = R.bootstrap([(float(r["A"]), float(r["expected"])) for r in rows])
    c_ci = R.bootstrap([(float(r["C"]), float(r["expected"])) for r in rows])
    summary = {"A": {"count": sum(r["A"] for r in rows), **a_ci},
               "C": {"count": sum(r["C"] for r in rows), **c_ci},
               "truthItems": sum(r["expected"] for r in rows)}
    out = ROOT / "evals" / "results" / "posthoc" / "recognition-naming.json"
    out.write_text(json.dumps({"summary": summary, "cases": rows}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
