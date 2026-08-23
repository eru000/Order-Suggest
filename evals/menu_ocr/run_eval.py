"""菜單 OCR 準確度評測。

為什麼需要這支 script：tests/ 裡的 129 個測試把 vision 呼叫全部 mock 掉了，
所以測試全綠**不代表**辨識是對的。換模型、調切塊策略、改 prompt 時，唯一
能判斷「到底有沒有變好」的方法，就是拿標好答案的照片實跑一次比分數。

這支 script 會真的打 API（每張照片約 5-6 次 vision 呼叫），會消耗配額，
所以刻意不進 CI，需要時手動跑。

用法：
    # 先確認案例格式正確，不打 API
    python evals/menu_ocr/run_eval.py --dry-run

    # 跑全部案例，用 .env 目前的設定
    python evals/menu_ocr/run_eval.py --label baseline

    # 換模型再跑一次，然後比較
    python evals/menu_ocr/run_eval.py --model qwen2.5-vl --label qwen
    python evals/menu_ocr/run_eval.py --compare baseline qwen
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
RUNS_DIR = EVAL_DIR / "runs"
PROJECT_ROOT = EVAL_DIR.parent.parent
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# menu_vision 匯入時會連帶載入 ollama_fuc，.env 也在那時候被讀進來。
import menu_vision  # noqa: E402
from menu_vision import _name_key, _similarity, analyze_menu_image  # noqa: E402

IMAGE_SUFFIXES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".heif": "image/heif",
}

# 跟 menu_vision._attach_sources 用同一個門檻，評分標準才跟程式內部一致。
DEFAULT_MATCH_THRESHOLD = 0.72


# ──────────────────────────────────────────────────
#  案例載入
# ──────────────────────────────────────────────────


class CaseError(Exception):
    """案例檔本身有問題（缺圖、JSON 壞掉、欄位不對）。"""


def _expected_path(image_path: Path) -> Path:
    # 不能用 with_suffix()——檔名含點的話（menu.v2.jpg）它會把 .v2 當成副檔名蓋掉。
    return image_path.parent / f"{image_path.stem}.expected.json"


def load_cases(only: list[str] | None = None) -> list[dict[str, Any]]:
    """掃 cases/ 目錄，把圖片與同名的 .expected.json 配成對。"""
    if not CASES_DIR.is_dir():
        raise CaseError(f"找不到案例目錄：{CASES_DIR}")

    cases: list[dict[str, Any]] = []
    for image_path in sorted(CASES_DIR.iterdir()):
        if image_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        name = image_path.stem
        if only and name not in only:
            continue

        expected_path = _expected_path(image_path)
        if not expected_path.exists():
            raise CaseError(f"「{name}」缺少答案檔：{expected_path.name}")
        try:
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseError(f"「{name}」的答案檔不是合法 JSON：{exc}") from exc

        items = expected.get("items")
        if not isinstance(items, list) or not items:
            raise CaseError(f"「{name}」的答案檔缺少非空的 items 陣列")
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise CaseError(f"「{name}」第 {index} 個 item 缺少 name")

        cases.append({
            "name": name,
            "image_path": image_path,
            "mime": IMAGE_SUFFIXES[image_path.suffix.lower()],
            "expected": expected,
        })

    if only:
        missing = set(only) - {case["name"] for case in cases}
        if missing:
            raise CaseError(f"找不到指定的案例：{'、'.join(sorted(missing))}")
    if not cases:
        raise CaseError(
            f"{CASES_DIR} 裡沒有任何案例。放一張 menu.jpg 和一份 menu.expected.json 就能開始。"
        )
    return cases


def estimate_api_calls(image_path: Path, fast: bool) -> int:
    """算這張圖會打幾次 API。純本機計算。

    辨識已改成單次呼叫（見 menu_vision.analyze_menu_image 的說明），所以除非
    用 VISION_TILES=1 把切塊開回來，否則永遠是 1。
    """
    from PIL import Image, ImageOps

    if not menu_vision._tiling_enabled():
        return 1
    with Image.open(image_path) as opened:
        width, height = ImageOps.exif_transpose(opened).size
    if max(width, height) >= menu_vision.MIN_UPSCALE_LONG_SIDE:
        scale = menu_vision.TARGET_LONG_SIDE / max(width, height)
        width, height = round(width * scale), round(height * scale)
    tiles = 1 if max(width, height) <= menu_vision.TILE_THRESHOLD else 4
    overview = 0 if fast else 1
    return overview + tiles + 1


# ──────────────────────────────────────────────────
#  比對與計分
# ──────────────────────────────────────────────────


def _flatten_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for category in result.get("categories", []):
        if not isinstance(category, dict):
            continue
        for item in category.get("items", []):
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                rows.append({
                    "name": str(item["name"]).strip(),
                    "price": item.get("price"),
                    "category": str(category.get("name") or ""),
                })
    return rows


def _match_score(left: str, right: str) -> float:
    """品名相似度。

    模型很常把限定詞挪到後面：「（外帶）大魯肉飯」→「大魯肉飯(外帶)」。
    那是同一道菜、同樣的字，只是順序不同，用 SequenceMatcher 只有 0.67，
    會被同時算成「漏抓一項」加「幻覺一項」，雙重扣分。

    字元多重集合完全相同才走這條捷徑。只要有一個字不一樣就退回原本的
    比法，所以「大肉羹飯」與「大肉羹麵」仍然不會被誤判成同一項。
    """
    if sorted(_name_key(left)) == sorted(_name_key(right)):
        return 1.0
    return _similarity(left, right)


def match_items(
    expected: list[dict[str, Any]],
    predicted: list[dict[str, Any]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    """把答案與辨識結果配對。

    先算出所有配對的相似度再由高往低貪婪配，比「照順序找最佳」穩定——
    否則答案的排列順序會影響分數，同一份資料換個順序就換個結果。
    """
    pairs = [
        (_match_score(exp["name"], pred["name"]), exp_index, pred_index)
        for exp_index, exp in enumerate(expected)
        for pred_index, pred in enumerate(predicted)
    ]
    pairs.sort(key=lambda row: -row[0])

    used_expected: set[int] = set()
    used_predicted: set[int] = set()
    matches: list[dict[str, Any]] = []
    for score, exp_index, pred_index in pairs:
        if score < threshold:
            break
        if exp_index in used_expected or pred_index in used_predicted:
            continue
        used_expected.add(exp_index)
        used_predicted.add(pred_index)
        exp, pred = expected[exp_index], predicted[pred_index]
        exact = _name_key(exp["name"]) == _name_key(pred["name"])
        matches.append({
            "expected": exp["name"],
            "predicted": pred["name"],
            "similarity": round(score, 3),
            "exactName": exact,
            # 字全對但順序不同。算命中，但要單獨列出來——推薦引擎是用
            # 品名做子字串比對的，順序不同在下游仍然可能出事。
            "reordered": bool(not exact and score >= 1.0),
            "expectedPrice": exp.get("price"),
            "predictedPrice": pred.get("price"),
        })

    missed = [expected[i] for i in range(len(expected)) if i not in used_expected]
    spurious = [predicted[i] for i in range(len(predicted)) if i not in used_predicted]
    return {"matches": matches, "missed": missed, "spurious": spurious}


def _price_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(float(left) - float(right)) < 0.01
    except (TypeError, ValueError):
        return False


def score_case(case: dict[str, Any], result: dict[str, Any], threshold: float) -> dict[str, Any]:
    expected_items = case["expected"]["items"]
    predicted_items = _flatten_result(result)
    matching = match_items(expected_items, predicted_items, threshold)
    matches = matching["matches"]

    matched_count = len(matches)
    recall = matched_count / len(expected_items) if expected_items else 0.0
    precision = matched_count / len(predicted_items) if predicted_items else 0.0
    exact_names = sum(1 for m in matches if m["exactName"])

    # 只在答案有標價格的品項上算價格分數。答案沒標價（菜單上就沒印）的，
    # 反過來檢查模型是不是自己編了一個數字出來。
    priced = [m for m in matches if m["expectedPrice"] is not None]
    price_correct = sum(1 for m in priced if _price_equal(m["expectedPrice"], m["predictedPrice"]))
    unpriced = [m for m in matches if m["expectedPrice"] is None]
    price_invented = sum(1 for m in unpriced if m["predictedPrice"] is not None)

    expected_name = str(case["expected"].get("restaurant_name") or "").strip()
    detected_name = str(result.get("detected_restaurant_name") or "").strip()
    name_ok: bool | None = None
    if expected_name:
        name_ok = _similarity(expected_name, detected_name) >= threshold

    return {
        "expectedCount": len(expected_items),
        "predictedCount": len(predicted_items),
        "matchedCount": matched_count,
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "exactNameRate": round(exact_names / matched_count, 3) if matched_count else 0.0,
        "pricedExpected": len(priced),
        "priceAccuracy": round(price_correct / len(priced), 3) if priced else None,
        "priceInvented": price_invented,
        "restaurantNameExpected": expected_name,
        "restaurantNameDetected": detected_name,
        "restaurantNameOk": name_ok,
        "qualityScoreReported": result.get("quality", {}).get("score"),
        "missed": [item["name"] for item in matching["missed"]],
        "spurious": [item["name"] for item in matching["spurious"]],
        "reordered": [
            {"expected": m["expected"], "predicted": m["predicted"]}
            for m in matches
            if m["reordered"]
        ],
        "nearMisses": [
            {"expected": m["expected"], "predicted": m["predicted"], "similarity": m["similarity"]}
            for m in matches
            if not m["exactName"] and not m["reordered"]
        ],
        "priceErrors": [
            {"item": m["expected"], "expected": m["expectedPrice"], "got": m["predictedPrice"]}
            for m in priced
            if not _price_equal(m["expectedPrice"], m["predictedPrice"])
        ],
    }


# ──────────────────────────────────────────────────
#  執行
# ──────────────────────────────────────────────────


def _logging_vision(sink: list[dict[str, Any]]) -> Any:
    """包一層 vision_chat，把每一次呼叫的原始往返記下來。

    沒有這個就分不出「切塊根本沒讀到」和「切塊讀到了但最終校對把它刪掉」——
    兩者的最終結果一模一樣，修法卻完全相反。
    """
    from ollama_fuc import vision_chat as _vision_chat

    def logged(prompt: str, image_url: Any, model: Any = None,
               timeout: float = 180.0, temperature: Any = None) -> str:
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "model": model,
            "imageCount": 1 if isinstance(image_url, str) else len(list(image_url)),
            "promptHead": prompt[:120],
            # 用 prompt 的雜湊當 replay 的鍵，不能用呼叫順序——切塊是並行跑的，
            # 順序每次都不一樣。
            "promptSha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        try:
            response = _vision_chat(prompt, image_url, model=model,
                                    timeout=timeout, temperature=temperature)
        except Exception as exc:
            entry.update({"error": f"{type(exc).__name__}: {exc}"[:300],
                          "durationSec": round(time.perf_counter() - started, 1)})
            sink.append(entry)
            raise
        parsed = menu_vision._extract_json_value(response)
        normalized = menu_vision.normalize_vision_result(parsed)
        entry.update({
            "durationSec": round(time.perf_counter() - started, 1),
            "responseChars": len(response),
            # JSON 解析失敗而且回應很長 = 幾乎確定是被 max_tokens 截斷。
            "jsonParsed": bool(parsed),
            "endsAbruptly": not response.rstrip().endswith(("}", "]", "```")),
            "itemCount": sum(len(c["items"]) for c in normalized["categories"]),
            "items": [i["name"] for c in normalized["categories"] for i in c["items"]],
            # 完整回應是 --replay 的原料。少了它，改動解析／合併邏輯時就只能
            # 重打 API，而模型輸出每次都不同，等於同時動了兩個變數。
            "response": response,
        })
        sink.append(entry)
        return response

    return logged


def _replay_vision(recorded: dict[str, str]) -> Any:
    """用錄下來的回應餵給 analyze_menu_image，完全不打 API。

    模型輸出固定住之後，改動解析、合併、校對對帳這些邏輯才有辦法乾淨地
    A/B——否則模型自己每次讀的就不一樣（實測同一張圖，有一次把「魯」
    全讀成「鹿」），分數變化根本無法歸因。
    """
    def replayed(prompt: str, image_url: Any, model: Any = None,
                 timeout: float = 180.0, temperature: Any = None) -> str:
        key = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        if key not in recorded:
            raise RuntimeError(
                "replay 找不到對應的錄音：prompt 已經和錄製當時不同了。"
                "改過 prompt 的話要重新錄一次（--debug）。"
            )
        return recorded[key]

    return replayed


def run_case(
    case: dict[str, Any],
    threshold: float,
    debug: bool = False,
    recorded: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    exchanges: list[dict[str, Any]] = []
    if recorded is not None:
        vision_kwargs: dict[str, Any] = {"vision_func": _replay_vision(recorded)}
    elif debug:
        vision_kwargs = {"vision_func": _logging_vision(exchanges)}
    else:
        vision_kwargs = {}
    try:
        result = analyze_menu_image(
            case["image_path"].read_bytes(),
            case["mime"],
            restaurant_hint="",  # 刻意不給提示，才測得出模型自己認不認得出店名
            **vision_kwargs,
        )
    except Exception as exc:
        return {
            "case": case["name"],
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
            "durationSec": round(time.perf_counter() - started, 1),
            "exchanges": exchanges,
        }
    scored = score_case(case, result, threshold)
    return {
        "case": case["name"],
        "ok": True,
        "durationSec": round(time.perf_counter() - started, 1),
        "models": result.get("models", {}),
        # 原始輸出一定要留。計分規則改了要重算時，有這份就不必再燒一次
        # API——第一版的比對器把「大魯肉飯(外帶)」誤判成幻覺，就是靠重算
        # 才免費修好的。
        "raw": {
            "detectedRestaurantName": result.get("detected_restaurant_name", ""),
            "items": _flatten_result(result),
            "warnings": result.get("warnings", []),
            "quality": result.get("quality", {}),
            "sourceBlocks": result.get("sourceBlocks", []),
        },
        "exchanges": exchanges,
        **scored,
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    good = [row for row in rows if row.get("ok")]
    if not good:
        return {"caseCount": len(rows), "okCount": 0}
    total_expected = sum(row["expectedCount"] for row in good)
    total_matched = sum(row["matchedCount"] for row in good)
    total_predicted = sum(row["predictedCount"] for row in good)
    priced = sum(row["pricedExpected"] for row in good)
    price_correct = sum(
        round((row["priceAccuracy"] or 0) * row["pricedExpected"]) for row in good
    )
    return {
        "caseCount": len(rows),
        "okCount": len(good),
        # 用品項總數加總而不是各案例平均，避免品項少的照片被放大權重。
        "recall": round(total_matched / total_expected, 3) if total_expected else 0.0,
        "precision": round(total_matched / total_predicted, 3) if total_predicted else 0.0,
        "priceAccuracy": round(price_correct / priced, 3) if priced else None,
        "priceInvented": sum(row["priceInvented"] for row in good),
        "restaurantNameOkCount": sum(1 for row in good if row["restaurantNameOk"]),
        "restaurantNameChecked": sum(1 for row in good if row["restaurantNameOk"] is not None),
        "totalDurationSec": round(sum(row["durationSec"] for row in good), 1),
    }


def _pct(value: Any) -> str:
    return "  n/a" if value is None else f"{float(value) * 100:5.1f}%"


def _pad(text: str, width: int) -> str:
    """靠左補到指定的「顯示寬度」。

    f-string 的 :<20 是算字元數，但中文在等寬字型裡佔兩格，直接用會讓
    中英混排的欄位全部歪掉。
    """
    display = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)
    return text + " " * max(0, width - display)


def print_report(run: dict[str, Any], verbose: bool) -> None:
    print()
    print("=" * 72)
    print(f"  菜單 OCR 評測：{run['label']}")
    print(f"  OCR={run['config']['visionModel']}  校對={run['config']['verifyModel']}"
          f"  fast={run['config']['fast']}")
    print("=" * 72)
    print(_pad("案例", 20) + f"{'召回':>6}{'精確':>8}{'價格':>8}{'秒':>7}  店名")
    print("-" * 72)
    for row in run["cases"]:
        if not row.get("ok"):
            print(_pad(row["case"], 20) + f"{'ERROR':>8}  {row['error'][:36]}")
            continue
        name_mark = {True: "✓", False: "✗", None: "-"}[row["restaurantNameOk"]]
        print(
            _pad(row["case"], 20)
            + f"{_pct(row['recall']):>8}"
            f"{_pct(row['precision']):>8}"
            f"{_pct(row['priceAccuracy']):>8}"
            f"{row['durationSec']:>7.1f}"
            f"  {name_mark} {row['restaurantNameDetected'][:16]}"
        )
    print("-" * 72)
    summary = run["summary"]
    if summary.get("okCount"):
        print(
            _pad("總計", 20)
            + f"{_pct(summary['recall']):>8}"
            f"{_pct(summary['precision']):>8}"
            f"{_pct(summary['priceAccuracy']):>8}"
            f"{summary['totalDurationSec']:>7.1f}"
            f"  {summary['restaurantNameOkCount']}/{summary['restaurantNameChecked']}"
        )
    if summary.get("priceInvented"):
        print(f"\n⚠ 有 {summary['priceInvented']} 個品項菜單沒印價格，模型自己編了一個數字")

    for row in run["cases"]:
        if row.get("ok") and row.get("reordered"):
            print(f"\n[{row['case']}] 字對但順序不同 {len(row['reordered'])} 項（算命中）：")
            for item in row["reordered"][:10]:
                print(f"  {item['expected']} → {item['predicted']}")

    # 漏抓的品項是最有行動價值的輸出——它直接告訴你切塊或 max_tokens 出了問題。
    for row in run["cases"]:
        if not row.get("ok"):
            continue
        if row["missed"]:
            print(f"\n[{row['case']}] 漏抓 {len(row['missed'])} 項：")
            print("  " + "、".join(row["missed"][: 40 if verbose else 12])
                  + ("…" if not verbose and len(row["missed"]) > 12 else ""))
        if row["spurious"]:
            print(f"[{row['case']}] 多出 {len(row['spurious'])} 項（菜單上沒有）：")
            print("  " + "、".join(row["spurious"][: 40 if verbose else 12])
                  + ("…" if not verbose and len(row["spurious"]) > 12 else ""))
        if row["priceErrors"]:
            print(f"[{row['case']}] 價格錯誤 {len(row['priceErrors'])} 項：")
            for error in row["priceErrors"][: 20 if verbose else 6]:
                print(f"  {error['item']}：應為 {error['expected']}，讀成 {error['got']}")
        if verbose and row["nearMisses"]:
            print(f"[{row['case']}] 認出來但有錯字 {len(row['nearMisses'])} 項：")
            for near in row["nearMisses"][:20]:
                print(f"  {near['expected']} → {near['predicted']} ({near['similarity']})")
    print()


def load_run(label: str) -> dict[str, Any]:
    candidates = sorted(RUNS_DIR.glob(f"*_{label}.json"))
    if not candidates:
        raise CaseError(f"找不到 label 為「{label}」的執行紀錄（{RUNS_DIR}）")
    return json.loads(candidates[-1].read_text(encoding="utf-8"))


def rescore(label: str, threshold: float) -> dict[str, Any]:
    """用現在的計分規則重算一份舊紀錄，不打 API。

    計分規則本身也會有 bug。發現的時候不該為了重算而再燒一次配額，
    更不該把舊分數跟新分數混在一起比。
    """
    run = load_run(label)
    cases = {case["name"]: case for case in load_cases(None)}
    for row in run["cases"]:
        if not row.get("ok") or "raw" not in row:
            continue
        case = cases.get(row["case"])
        if case is None:
            continue
        fake_result = {
            "detected_restaurant_name": row["raw"]["detectedRestaurantName"],
            "quality": row["raw"].get("quality", {}),
            "categories": [{"name": "全部", "items": row["raw"]["items"]}],
        }
        row.update(score_case(case, fake_result, threshold))
    run["summary"] = aggregate(run["cases"])
    run["label"] = f"{run['label']}(重算)"
    return run


def compare_runs(label_a: str, label_b: str) -> None:
    run_a, run_b = load_run(label_a), load_run(label_b)
    print()
    print("=" * 72)
    print(f"  比較：{label_a}  →  {label_b}")
    print("=" * 72)
    print(_pad("指標", 20) + f"{label_a:>14}{label_b:>14}{'差異':>12}")
    print("-" * 72)
    for key, title in (
        ("recall", "召回率"),
        ("precision", "精確率"),
        ("priceAccuracy", "價格正確率"),
    ):
        left, right = run_a["summary"].get(key), run_b["summary"].get(key)
        if left is None or right is None:
            continue
        delta = (right - left) * 100
        arrow = "▲" if delta > 0.05 else ("▼" if delta < -0.05 else "＝")
        print(_pad(title, 20) + f"{_pct(left):>14}{_pct(right):>14}"
              + f"{arrow} {delta:+.1f}pt".rjust(12))
    left_time = run_a["summary"].get("totalDurationSec", 0)
    right_time = run_b["summary"].get("totalDurationSec", 0)
    print(_pad("總耗時（秒）", 20)
          + f"{left_time:>14.1f}{right_time:>14.1f}{right_time - left_time:>+12.1f}")

    by_case_a = {row["case"]: row for row in run_a["cases"] if row.get("ok")}
    for row in run_b["cases"]:
        if not row.get("ok") or row["case"] not in by_case_a:
            continue
        before = set(by_case_a[row["case"]]["missed"])
        after = set(row["missed"])
        recovered, lost = before - after, after - before
        if recovered:
            print(f"\n[{row['case']}] 這次多抓到：" + "、".join(sorted(recovered)[:15]))
        if lost:
            print(f"[{row['case']}] 這次反而漏掉：" + "、".join(sorted(lost)[:15]))
    print()


def main() -> int:
    # Windows 主控台預設 cp950，報表裡的 ✓ ▲ 和部分中文會直接炸 UnicodeEncodeError。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass

    parser = argparse.ArgumentParser(
        description="菜單 OCR 準確度評測（會真的打 API）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--case", action="append", help="只跑指定案例，可重複")
    parser.add_argument("--label", default="run", help="這次執行的名稱，用於存檔與比較")
    parser.add_argument("--model", help="覆寫 VISION_MODEL")
    parser.add_argument("--verify-model", help="覆寫 VISION_VERIFY_MODEL")
    parser.add_argument("--fast", action="store_true", help="開啟 VISION_FAST（跳過總覽）")
    parser.add_argument("--tile-overlap", type=float, help="覆寫切塊重疊比例，例如 0.2")
    parser.add_argument("--tile-threshold", type=int, help="覆寫切塊觸發的長邊門檻")
    parser.add_argument("--threshold", type=float, default=DEFAULT_MATCH_THRESHOLD,
                        help=f"品名配對相似度門檻（預設 {DEFAULT_MATCH_THRESHOLD}）")
    parser.add_argument("--jobs", type=int, default=1,
                        help="同時跑幾個案例。預設 1，避免一次打爆學校 API")
    parser.add_argument("--dry-run", action="store_true", help="只檢查案例與估算呼叫次數")
    parser.add_argument("--verbose", action="store_true", help="印出完整的漏抓/錯字清單")
    parser.add_argument("--debug", action="store_true",
                        help="記錄每一次 vision 呼叫的原始往返，用來看品項是在哪一關掉的")
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比較兩次執行紀錄")
    parser.add_argument("--rescore", metavar="LABEL",
                        help="用現在的計分規則重算舊紀錄，不打 API")
    parser.add_argument("--replay", metavar="LABEL",
                        help="用某次 --debug 錄下的模型回應重跑整條 pipeline，不打 API。"
                             "模型輸出固定住，改動解析／合併邏輯才能乾淨地 A/B")
    args = parser.parse_args()

    if args.compare:
        compare_runs(*args.compare)
        return 0

    if args.rescore:
        try:
            run = rescore(args.rescore, args.threshold)
        except CaseError as exc:
            print(f"✗ {exc}")
            return 2
        if not any("raw" in row for row in run["cases"]):
            print(f"✗ 「{args.rescore}」是加入原始輸出功能之前跑的，沒有可重算的資料")
            return 2
        print_report(run, args.verbose)
        return 0

    try:
        cases = load_cases(args.case)
    except CaseError as exc:
        print(f"✗ {exc}")
        return 2

    if args.model:
        os.environ["VISION_MODEL"] = args.model
    if args.verify_model:
        os.environ["VISION_VERIFY_MODEL"] = args.verify_model
    os.environ["VISION_FAST"] = "1" if args.fast else ""
    if args.tile_overlap is not None:
        menu_vision.TILE_OVERLAP = args.tile_overlap
    if args.tile_threshold is not None:
        menu_vision.TILE_THRESHOLD = args.tile_threshold

    config = {
        "visionModel": os.getenv("VISION_MODEL", "(未設定)"),
        "verifyModel": os.getenv("VISION_VERIFY_MODEL", "(未設定)"),
        "fast": bool(args.fast),
        "tileOverlap": menu_vision.TILE_OVERLAP,
        "tileThreshold": menu_vision.TILE_THRESHOLD,
        "matchThreshold": args.threshold,
    }

    recordings: dict[str, dict[str, str]] = {}
    if args.replay:
        try:
            source = load_run(args.replay)
        except CaseError as exc:
            print(f"✗ {exc}")
            return 2
        for row in source["cases"]:
            captured = {
                entry["promptSha"]: entry["response"]
                for entry in row.get("exchanges", [])
                if entry.get("promptSha") and entry.get("response") is not None
            }
            if captured:
                recordings[row["case"]] = captured
        if not recordings:
            print(f"✗ 「{args.replay}」沒有可重播的錄音。"
                  f"錄音需要用 --debug 跑，且要是加入完整回應紀錄之後跑的。")
            return 2
        cases = [case for case in cases if case["name"] in recordings]
        print(f"重播模式：{len(cases)} 個案例沿用「{args.replay}」錄下的模型回應，不打 API")

    planned = 0 if args.replay else sum(
        estimate_api_calls(case["image_path"], args.fast) for case in cases
    )
    if not args.replay:
        print(f"案例 {len(cases)} 個，預估 API 呼叫 {planned} 次"
              f"（OCR={config['visionModel']}，校對={config['verifyModel']}）")

    if args.dry_run:
        for case in cases:
            calls = estimate_api_calls(case["image_path"], args.fast)
            print(f"  {case['name']:<20} 答案 {len(case['expected']['items']):>3} 項"
                  f"　預估 {calls} 次呼叫")
        print("\n✓ 案例格式檢查通過（--dry-run 不會真的呼叫 API）")
        return 0

    if not args.replay and not os.getenv("API_KEY"):
        print("✗ 沒有讀到 API_KEY，請確認專案根目錄的 .env")
        return 2

    print()

    def _run(case: dict[str, Any]) -> dict[str, Any]:
        return run_case(case, args.threshold, args.debug, recordings.get(case["name"]) if args.replay else None)

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            rows = list(pool.map(_run, cases))
    else:
        rows = []
        for index, case in enumerate(cases, 1):
            print(f"  [{index}/{len(cases)}] {case['name']} …", flush=True)
            rows.append(_run(case))

    run = {
        "label": args.label,
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "config": config,
        "cases": rows,
        "summary": aggregate(rows),
    }

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{args.label}.json"
    out_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    print_report(run, args.verbose)
    print(f"紀錄已存到 {out_path.relative_to(PROJECT_ROOT)}")
    print(f"比較兩次結果：python {Path(__file__).relative_to(PROJECT_ROOT)} "
          f"--compare <另一個label> {args.label}")
    return 0 if run["summary"].get("okCount") == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
