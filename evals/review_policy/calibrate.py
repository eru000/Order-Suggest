"""複核政策的校準：以 Conformal Risk Control 決定門檻，不用主觀數字（期末規劃 節點 5／B）。

依據
- Angelopoulos, Bates, Fisch, Lei, Schuster. Conformal Risk Control. ICLR 2024（arXiv:2208.02814）。
  對任何隨 λ 單調不增、上界為 B 的損失，取
      λ̂ = inf{ λ : R̂(λ) ≤ α − (B − α)/n }
  則新資料的期望損失 ≤ α（資料可交換的前提下）。入門見 arXiv:2107.07511 第 4.3 節。
- Geifman & El-Yaniv. Selective Classification for DNNs. NeurIPS 2017（arXiv:1705.08500）：
  使用者先定可接受的風險，再找覆蓋率最高（＝需複核最少）的拒絕規則。

本專題的設定
- 單位：一張菜單影像。損失 L＝該圖「價格錯誤品項」中，既沒被標記、也沒顯示「價格不明」的比例；
  沒有錯誤的圖 L＝0；上界 B＝1。
- 價格錯誤品項：VLM 給了數字但不等於真值，或真值裡沒有這道菜（多出的品項）。
- λ：價格合理性門檻（價格／同分類中位數 < λ 就標記），λ 越大標記越多，L 單調不增。
- 兩種政策：只用合理性檢查；合理性檢查＋OCR 交叉複核標記（不隨 λ 改變）。
- 校準資料：開發組 19 張（7 原圖＋12 合成干擾）＋遮蔽實驗 B 組（預設提示詞）25 張，共 n＝44。
- α：CRC 中由使用者指定的錯誤率；文獻範例以 10% 為例（arXiv:2208.02814、2107.07511），
  這裡以 α＝0.10 為主，並列出 0.05、0.20 的結果，讓選擇的後果透明。
- 在可行的政策中，選「需人工複核品項比例」最低者（Geifman & El-Yaniv 的風險–覆蓋率觀點）。

執行：python -X utf8 evals/review_policy/calibrate.py   （OCR 結果快取在 evals/results/review_policy/ocr_cache）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for path in (ROOT / "src", ROOT / "evals" / "menu_ocr", ROOT / "evals" / "occlusion"):
    sys.path.insert(0, str(path))

import menu_vision  # noqa: E402
from cross_check import cross_check, outlier_scores  # noqa: E402
from run_eval import match_items  # noqa: E402

OUT = ROOT / "evals" / "results" / "review_policy"
CACHE = OUT / "ocr_cache"
DEV_SETS = {
    "dev": ROOT / "菜單照片資料" / "第二項" / "開發原圖",
    "stress": ROOT / "菜單照片資料" / "第二項" / "開發合成干擾",
}
STEP2 = ROOT / "evals" / "results" / "2026-11-26-step2"
OCCLUSION_DIR = ROOT / "evals" / "occlusion" / "dev"
OCCLUSION_RUN = ROOT / "evals" / "results" / "stepB" / "occl-dev-B.json"
OCR_FLAGS = {"name_not_in_ocr", "ocr_price_mismatch", "price_unverified", "price_ambiguous"}
LAMBDAS = [round(0.05 * i, 2) for i in range(0, 13)]  # 0.00～0.60
ALPHAS = (0.05, 0.10, 0.20)


def _ocr_lines(key: str, image: Path) -> list[dict[str, Any]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{key}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    import ocr_baseline

    lines = ocr_baseline.ocr_lines(image.read_bytes())
    path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return lines


def _parse(response: str) -> dict[str, Any]:
    parsed, _ = menu_vision._extract_json_document(response)
    return menu_vision.normalize_vision_result(parsed)


def _image_record(key: str, image: Path, vlm: dict[str, Any], truth: list[dict[str, Any]]) -> dict[str, Any]:
    checked = cross_check(vlm, _ocr_lines(key, image), outlier_ratio=None)
    scores = outlier_scores(vlm)
    items = [item for category in vlm["categories"] for item in category["items"]]
    matching = match_items(truth, [{"name": i["name"], "price": i.get("price")} for i in items])
    by_pred = {m["predicted"]: m for m in matching["matches"]}
    flags_by_id = {id(original): reviewed["review"]["flags"]
                   for original, reviewed in zip(items, checked["items"], strict=True)}
    rows = []
    for item in items:
        match = by_pred.get(item["name"])
        price = item.get("price")
        if match is None:
            error = True  # 真值沒有這道菜
        elif price is None:
            error = match["expectedPrice"] is not None
        else:
            expected = match["expectedPrice"]
            error = expected is None or abs(float(expected) - float(price)) >= 0.01
        score = scores.get(id(item))
        rows.append({
            "name": item["name"], "price": price, "error": error, "priceNull": price is None,
            "outlierScore": None if score is None else round(score[0], 4),
            "ocrFlags": sorted(set(flags_by_id[id(item)]) & OCR_FLAGS),
        })
    return {"key": key, "items": rows}


def collect() -> list[dict[str, Any]]:
    import run_cross_check

    images = []
    for split, directory in DEV_SETS.items():
        for truth_path in sorted(directory.glob("*.expected.json")):
            case = truth_path.name.removesuffix(".expected.json")
            image = directory / f"{case}.png"
            vlm = run_cross_check.vlm_result(split, case)
            if not image.is_file() or vlm is None:
                continue
            truth = [{"name": r["name"], "price": r.get("price")}
                     for r in json.loads(truth_path.read_text(encoding="utf-8"))["items"]]
            images.append(_image_record(f"{split}__{case}", image, vlm, truth))
    run = json.loads(OCCLUSION_RUN.read_text(encoding="utf-8"))
    for row in run["cases"]:
        if row.get("status") != "ok":
            continue
        response = next((e.get("response") for e in row.get("exchanges") or [] if e.get("response")), None)
        if not response:
            continue
        doc = json.loads((OCCLUSION_DIR / f"{row['case']}.expected.json").read_text(encoding="utf-8"))
        truth = [{"name": r["name"], "price": r.get("true_price", r.get("price"))} for r in doc["items"]]
        images.append(_image_record(f"occl__{row['case']}", OCCLUSION_DIR / f"{row['case']}.png",
                                    _parse(response), truth))
    return images


def flagged(item: dict[str, Any], lam: float, use_ocr: bool) -> bool:
    by_outlier = item["outlierScore"] is not None and item["outlierScore"] < lam
    return by_outlier or (use_ocr and bool(item["ocrFlags"]))


def evaluate(images: list[dict[str, Any]], lam: float, use_ocr: bool) -> dict[str, Any]:
    losses, flags, total, errors, missed = [], 0, 0, 0, 0
    for image in images:
        errs = [i for i in image["items"] if i["error"]]
        unsurfaced = [i for i in errs if not i["priceNull"] and not flagged(i, lam, use_ocr)]
        losses.append(len(unsurfaced) / len(errs) if errs else 0.0)
        flags += sum(1 for i in image["items"] if flagged(i, lam, use_ocr))
        total += len(image["items"])
        errors += len(errs)
        missed += len(unsurfaced)
    return {
        "lambda": lam, "useOcr": use_ocr, "risk": sum(losses) / len(losses),
        "flagRate": flags / total, "flagged": flags, "items": total, "errors": errors, "missedErrors": missed,
    }


def main() -> int:
    images = collect()
    n = len(images)
    grid = [evaluate(images, lam, use_ocr) for use_ocr in (False, True) for lam in LAMBDAS]
    decisions = {}
    for alpha in ALPHAS:
        bound = alpha - (1 - alpha) / n  # B＝1
        best = {}
        for use_ocr in (False, True):
            feasible = [g for g in grid if g["useOcr"] == use_ocr and g["risk"] <= bound]
            best["plausibility+ocr" if use_ocr else "plausibility"] = (
                min(feasible, key=lambda g: g["lambda"]) if feasible else None
            )
        candidates = [v for v in best.values() if v]
        chosen = min(candidates, key=lambda g: (g["flagRate"], g["useOcr"])) if candidates else None
        decisions[str(alpha)] = {"riskBound": round(bound, 4), "perPolicy": best, "chosen": chosen}

    OUT.mkdir(parents=True, exist_ok=True)
    record = {"n": n, "lambdas": LAMBDAS, "grid": grid, "decisions": decisions, "images": images}
    (OUT / "calibration-dev.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"校準影像 n＝{n}，品項 {grid[0]['items']}，價格錯誤 {grid[0]['errors']}")
    print(f"{'政策':18}{'λ':>6}{'風險 R̂':>9}{'漏掉錯誤':>9}{'複核比例':>9}")
    for g in grid:
        if g["lambda"] in (0.0, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5):
            name = "合理性＋OCR" if g["useOcr"] else "只用合理性"
            print(f"{name:16}{g['lambda']:>6.2f}{g['risk']:>9.4f}{g['missedErrors']:>6}/{g['errors']:<3}{g['flagRate']:>8.1%}")
    for alpha, d in decisions.items():
        c = d["chosen"]
        desc = "無可行政策" if c is None else (
            f"{'合理性＋OCR' if c['useOcr'] else '只用合理性'}，λ̂＝{c['lambda']}，"
            f"R̂＝{c['risk']:.4f}，複核 {c['flagRate']:.1%}")
        print(f"α＝{alpha}：風險上限 {d['riskBound']} → {desc}")
    print(f"已存到 {(OUT / 'calibration-dev.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
