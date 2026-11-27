"""節點 H：封存組辨識的一次性評估（凍結 freeze-final 之後執行）。

依 log.txt「節點 F　凍結」預先登錄的判定規則：
(1) 主要指標「完整列正確率」（菜名與價格都對），以菜單為單位 bootstrap 95% 區間
    （10,000 次，種子 20261127）。主要結果只用第 1 輪（學校 API 有伺服器端快取，
    重複輪不是獨立樣本），另報 3 輪是否一致。C 的區間下界高於 A 的點估計才寫
    「VLM 優於 OCR 基線」。
(2) 複核：CRC 損失（每張圖的價格錯誤中，既沒被標記、也沒顯示價格不明的比例）在
    封存組的平均 ≤ α＝0.10 記為成立。列出 none／null_only／cross／plausibility／all。

組別
- A：OCR＋版面配對（evals/menu_ocr/ocr_baseline.py），本機執行。
- C：第二項 11-26 封存的 VLM 原始回應（ornith-35b，整圖、長邊 3600、預設提示詞），
  以凍結的 menu_vision 解析。封存當時與凍結版本的 menu_vision 差別只在複核標記
  （apply_price_review），不影響品項與價格，所以不需重打 API。
- D：C 的品項不變，加上複核標記（價格合理性 λ＝0.10；對照組再加 OCR 交叉複核）。

執行前會驗證封存 ZIP、輸入影像、標準答案的 SHA-256 與公開收據一致。

    python -X utf8 evals/holdout/recognition.py
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import sys
import zipfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for path in (ROOT / "src", ROOT / "evals" / "menu_ocr"):
    sys.path.insert(0, str(path))

import menu_vision  # noqa: E402
from cross_check import cross_check, outlier_scores  # noqa: E402
from menu_review import REVIEW_OUTLIER_RATIO  # noqa: E402
from run_eval import load_cases, match_items, score_case  # noqa: E402

STEP2 = ROOT / "evals" / "results" / "2026-11-26-step2"
CASES_DIR = ROOT / "菜單照片資料" / "第二項" / "獨立封存"
OUT = ROOT / "evals" / "results" / "holdout"
CACHE = OUT / "ocr_cache"
THRESHOLD = 0.72
ALPHA = 0.10
BOOTSTRAP = 10_000
SEED = 20261127
OCR_FLAGS = {"name_not_in_ocr", "ocr_price_mismatch", "price_unverified", "price_ambiguous"}
POLICIES = ("none", "null_only", "cross", "plausibility", "plausibility+cross", "all")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sealed_run(case: str, round_no: int) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt = json.loads((STEP2 / "receipts" / f"holdout__{case}__r{round_no}.json").read_text("utf-8"))
    attempt = [a for a in receipt["attempts"] if a.get("technicalStatus") == "ok"][-1]
    artifact = ROOT / attempt["artifactPath"].replace("\\", "/")
    data = artifact.read_bytes()
    if sha256(data) != attempt["artifactSha256"]:
        raise RuntimeError(f"{artifact.name} 的 SHA-256 與收據不符")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".json"))
        run = json.loads(archive.read(name).decode("utf-8"))
    return receipt, run


def parse_response(run: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    row = run["cases"][0]
    response = next((e.get("response") for e in row.get("exchanges") or [] if e.get("response")), None)
    if not response:
        return None, None
    parsed, _ = menu_vision._extract_json_document(response)
    return menu_vision.normalize_vision_result(parsed), response


def ocr_lines(case: str, image: bytes) -> list[dict[str, Any]]:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{case}.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    import ocr_baseline

    lines = ocr_baseline.ocr_lines(image)
    path.write_text(json.dumps(lines, ensure_ascii=False), encoding="utf-8")
    return lines


def ocr_result(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """與 ocr_baseline.analyze 相同，但用快取的 OCR 行。"""
    import ocr_baseline

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in ocr_baseline.pair_items(lines):
        grouped.setdefault(item["category"] or "菜單", []).append(
            {"dish": item["dish"], "price": item["price"]}
        )
    return menu_vision.normalize_vision_result(
        {"categories": [{"title": t, "items": rows} for t, rows in grouped.items()]}
    )


def review_rows(vlm: dict[str, Any], lines: list[dict[str, Any]], truth: list[dict[str, Any]]):
    """每個 VLM 品項：是否價格錯誤、合理性分數、OCR 標記（與 calibrate.py 同一定義）。"""
    checked = cross_check(vlm, lines, outlier_ratio=None)
    scores = outlier_scores(vlm)
    items = [item for category in vlm["categories"] for item in category["items"]]
    matching = match_items(truth, [{"name": i["name"], "price": i.get("price")} for i in items])
    by_pred = {m["predicted"]: m for m in matching["matches"]}
    flags = {id(o): r["review"]["flags"] for o, r in zip(items, checked["items"], strict=True)}
    rows = []
    for item in items:
        match = by_pred.get(item["name"])
        price = item.get("price")
        spurious = match is None
        if match is None:
            error = True
        elif price is None:
            error = match["expectedPrice"] is not None
        else:
            expected = match["expectedPrice"]
            error = expected is None or abs(float(expected) - float(price)) >= 0.01
        score = scores.get(id(item))
        rows.append({
            "name": item["name"], "price": price, "error": error, "spurious": spurious,
            "priceNull": price is None,
            "outlierScore": None if score is None else round(score[0], 4),
            "ocrFlags": sorted(set(flags[id(item)]) & OCR_FLAGS),
        })
    return rows


def flagged(row: dict[str, Any], policy: str) -> bool:
    plaus = row["outlierScore"] is not None and row["outlierScore"] < REVIEW_OUTLIER_RATIO
    cross = bool(row["ocrFlags"])
    return {
        "none": False, "null_only": row["priceNull"], "cross": cross, "plausibility": plaus,
        "plausibility+cross": plaus or cross, "all": True,
    }[policy]


def policy_summary(
    images: list[dict[str, Any]], policy: str, price_only: bool = False,
) -> dict[str, Any]:
    """price_only＝True 是事後敏感度分析：只把「配對到真值、價格不對」算錯誤，
    不把真值沒有的品項（多為菜名寫法不同）算進去。預先登錄的是 False。"""
    losses, flag_count, total, errors, caught, residual = [], 0, 0, 0, 0, 0
    for image in images:
        rows = image["review"]
        errs = [r for r in rows if r["error"] and not (price_only and r["spurious"])]
        # CRC 損失：價格錯誤中既沒被標記、也沒顯示價格不明（null）的比例
        unsurfaced = [r for r in errs if not r["priceNull"] and not flagged(r, policy)]
        losses.append(len(unsurfaced) / len(errs) if errs else 0.0)
        flag_count += sum(flagged(r, policy) for r in rows)
        total += len(rows)
        errors += len(errs)
        caught += sum(flagged(r, policy) for r in errs)
        residual += len(unsurfaced)
    return {
        "policy": policy, "risk": sum(losses) / len(losses), "losses": losses,
        "flagRate": flag_count / total if total else 0.0, "flagged": flag_count, "items": total,
        "errors": errors, "errorsCaught": caught, "unsurfacedErrors": residual,
    }


def bootstrap(per_menu: list[tuple[float, float]], seed: int = SEED) -> dict[str, float]:
    """per_menu＝[(分子, 分母)]，以菜單為單位重抽，統計量為合併比例。"""
    rng = random.Random(seed)
    n = len(per_menu)
    point = sum(a for a, _ in per_menu) / sum(b for _, b in per_menu)
    stats = []
    for _ in range(BOOTSTRAP):
        sample = [per_menu[rng.randrange(n)] for _ in range(n)]
        denom = sum(b for _, b in sample)
        stats.append(sum(a for a, _ in sample) / denom if denom else 0.0)
    stats.sort()
    return {"point": point, "low": stats[int(0.025 * BOOTSTRAP)], "high": stats[int(0.975 * BOOTSTRAP) - 1]}


def bootstrap_mean(values: list[float], seed: int = SEED) -> dict[str, float]:
    return bootstrap([(v, 1.0) for v in values], seed)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT.mkdir(parents=True, exist_ok=True)
    cases = load_cases(cases_dir=CASES_DIR)
    records, images = [], []
    for case in cases:
        name = case["name"]
        image = Path(case["image_path"]).read_bytes()
        truth_bytes = Path(case["expected_path"]).read_bytes()
        truth = [{"name": r["name"], "price": r.get("price")} for r in case["expected"]["items"]]
        rounds = {}
        for round_no in (1, 2, 3):
            receipt, run = sealed_run(name, round_no)
            if receipt["inputSha256"] != sha256(image):
                raise RuntimeError(f"{name} 的影像與封存時不同")
            if receipt["truthSha256"] != sha256(truth_bytes):
                raise RuntimeError(f"{name} 的標準答案與封存時不同")
            result, response = parse_response(run)
            rounds[round_no] = {
                "responseSha256": sha256(response.encode("utf-8")) if response else None,
                "elapsedSec": receipt["attempts"][-1].get("elapsedSec"),
                "score": score_case(case, result, THRESHOLD) if result else None,
                "result": result,
            }
        lines = ocr_lines(name, image)
        a_score = score_case(case, ocr_result(lines), THRESHOLD)
        c = rounds[1]
        review = review_rows(c["result"], lines, truth) if c["result"] else []
        images.append({"case": name, "review": review})
        records.append({
            "case": name,
            "expected": len(truth),
            "A": {k: a_score[k] for k in ("matchedCount", "exactNamePriceCount", "exactNamePriceRate",
                                          "recall", "precision", "predictedCount",
                                          "priceCorrectCount", "pricedExpectedTotal")},
            "C": {
                round_no: None if r["score"] is None else {
                    k: r["score"][k] for k in ("matchedCount", "exactNamePriceCount",
                                               "exactNamePriceRate", "recall", "precision",
                                               "predictedCount", "priceCorrectCount",
                                               "pricedExpectedTotal")
                } | {"responseSha256": r["responseSha256"], "elapsedSec": r["elapsedSec"]}
                for round_no, r in rounds.items()
            },
            "review": review,
        })
        a = records[-1]["A"]
        c1 = records[-1]["C"][1] or {}
        print(f"  {name:26} 真值 {len(truth):3}  A 完整列 {a['exactNamePriceCount']:3}  "
              f"C 完整列 {c1.get('exactNamePriceCount', '-'):>3}")

    def per_menu(group: str) -> list[tuple[float, float]]:
        out = []
        for r in records:
            score = r["A"] if group == "A" else r["C"][1]
            out.append((float(score["exactNamePriceCount"] if score else 0), float(r["expected"])))
        return out

    a_ci, c_ci = bootstrap(per_menu("A")), bootstrap(per_menu("C"))
    diff_rng = random.Random(SEED)
    diffs = []
    pairs = list(zip(per_menu("A"), per_menu("C"), strict=True))
    for _ in range(BOOTSTRAP):
        sample = [pairs[diff_rng.randrange(len(pairs))] for _ in pairs]
        denom = sum(a[1] for a, _ in sample)
        diffs.append((sum(c[0] for _, c in sample) - sum(a[0] for a, _ in sample)) / denom)
    diffs.sort()
    verdict_recognition = (
        "VLM 優於 OCR 基線" if c_ci["low"] > a_ci["point"] else "差異不顯著"
    )
    policies = {p: policy_summary(images, p) for p in POLICIES}
    plaus = policies["plausibility"]
    crc_ci = bootstrap_mean(plaus["losses"])
    consistency = {
        r["case"]: {
            "identicalResponses": len({r["C"][k]["responseSha256"] for k in (1, 2, 3) if r["C"][k]}) == 1,
            "completeRows": [r["C"][k]["exactNamePriceCount"] if r["C"][k] else None for k in (1, 2, 3)],
        }
        for r in records
    }
    def total(group: str, key: str) -> int:
        return sum(int((r["A"] if group == "A" else r["C"][1] or {}).get(key) or 0) for r in records)

    secondary = {
        group: {
            "matched": total(group, "matchedCount"),
            "priceCorrect": total(group, "priceCorrectCount"),
            "pricedTruth": total(group, "pricedExpectedTotal"),
            "priceRecall": bootstrap([
                (float((r["A"] if group == "A" else r["C"][1])["priceCorrectCount"]),
                 float((r["A"] if group == "A" else r["C"][1])["pricedExpectedTotal"]))
                for r in records
                if (r["A"] if group == "A" else r["C"][1])["pricedExpectedTotal"]
            ]),
        }
        for group in ("A", "C")
    }
    post_hoc = {p: policy_summary(images, p, price_only=True) for p in POLICIES}
    summary = {
        "frozenAt": "freeze-final",
        "secondaryPriceRecall": secondary,
        "postHocPriceOnlyReview": {
            k: {kk: vv for kk, vv in v.items() if kk != "losses"} for k, v in post_hoc.items()
        },
        "menus": len(records),
        "truthItems": sum(r["expected"] for r in records),
        "completeRows": {
            "A_ocr": {"count": sum(int(a) for a, _ in per_menu("A")), **a_ci},
            "C_vlm": {"count": sum(int(c) for c, _ in per_menu("C")), **c_ci},
            "C_minus_A": {"low": diffs[int(0.025 * BOOTSTRAP)], "high": diffs[int(0.975 * BOOTSTRAP) - 1]},
            "preRegisteredVerdict": verdict_recognition,
        },
        "review": {
            "alpha": ALPHA, "lambda": REVIEW_OUTLIER_RATIO,
            "plausibilityRisk": {"mean": plaus["risk"], "low": crc_ci["low"], "high": crc_ci["high"]},
            "preRegisteredVerdict": "保證在封存組成立" if plaus["risk"] <= ALPHA else "不成立",
            "policies": {k: {kk: vv for kk, vv in v.items() if kk != "losses"} for k, v in policies.items()},
        },
        "roundConsistency": consistency,
    }
    record = {"summary": summary, "cases": records}
    (OUT / "recognition.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
