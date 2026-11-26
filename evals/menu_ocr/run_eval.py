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
import math
import os
import re
import sys
import time
import traceback
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
RUNS_DIR = EVAL_DIR / "runs"
PROJECT_ROOT = EVAL_DIR.parent.parent
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from run_provenance import (  # noqa: E402
    SCORING_VERSION,
    build_case_provenance,
    build_request_provenance,
    build_run_provenance,
    compare_provenance,
    provenance_from_calls,
)
from transport_trace import trace_transport  # noqa: E402

# Offline scoring, replay and dataset inspection must not initialize the live API
# client (which loads .env). Import the product only inside the live pipeline.
menu_vision: Any = None


def _get_menu_vision() -> Any:
    global menu_vision
    if menu_vision is None:
        import importlib

        menu_vision = importlib.import_module("menu_vision")
    return menu_vision


def analyze_menu_image(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _get_menu_vision().analyze_menu_image(*args, **kwargs)


def _name_key(value: str) -> str:
    # Same normalization as the product, kept local so scoring is offline.
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", value.casefold())


def _similarity(left: str, right: str) -> float:
    return SequenceMatcher(None, _name_key(left), _name_key(right)).ratio()

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
    """Read all declared truths, retaining missing images as explicit skips."""
    if not CASES_DIR.is_dir():
        raise CaseError(f"找不到案例目錄：{CASES_DIR}")

    images: dict[str, Path] = {}
    for image_path in sorted(CASES_DIR.iterdir()):
        if image_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        name = image_path.stem
        if only and name not in only:
            continue
        if name in images:
            raise CaseError(f"「{name}」有多張同名圖片，請使用不同案例名稱")
        images[name] = image_path
    truths = {
        path.name.removesuffix(".expected.json"): path
        for path in sorted(CASES_DIR.glob("*.expected.json"))
        if not path.name.startswith("_")
        and (not only or path.name.removesuffix(".expected.json") in only)
    }
    cases: list[dict[str, Any]] = []
    for name in sorted(set(images) | set(truths)):
        image_path = images.get(name)
        expected_path = truths.get(name, CASES_DIR / f"{name}.expected.json")
        if not expected_path.exists():
            raise CaseError(f"「{name}」缺少答案檔：{expected_path.name}")
        try:
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CaseError(f"「{name}」的答案檔不是合法 JSON：{exc}") from exc

        if not isinstance(expected, dict):
            raise CaseError(f"「{name}」的答案檔必須是 JSON 物件")
        items = expected.get("items")
        if not isinstance(items, list) or not items:
            raise CaseError(f"「{name}」的答案檔缺少非空的 items 陣列")
        for index, item in enumerate(items, 1):
            if not isinstance(item, dict) or not str(item.get("name") or "").strip():
                raise CaseError(f"「{name}」第 {index} 個 item 缺少 name")
            price = item.get("price")
            if price is not None and (
                isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(price) or price < 0
            ):
                raise CaseError(f"「{name}」第 {index} 個 item 的 price 必須是非負有限數字或 null")

        cases.append({
            "name": name,
            "image_path": image_path,
            "expected_path": expected_path,
            "mime": IMAGE_SUFFIXES[image_path.suffix.lower()] if image_path else "",
            "expected": expected,
            "skipReason": "missing_image" if image_path is None else "",
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


def estimate_api_calls(image_path: Path | None, fast: bool) -> int:
    """算這張圖會打幾次 API。純本機計算。

    辨識已改成單次呼叫（見 menu_vision.analyze_menu_image 的說明），所以除非
    用 VISION_TILES=1 把切塊開回來，否則永遠是 1。
    """
    from PIL import Image, ImageOps

    if image_path is None or not image_path.is_file():
        return 0
    if os.getenv("VISION_TILES", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return 1
    with Image.open(image_path) as opened:
        width, height = ImageOps.exif_transpose(opened).size
    if max(width, height) >= 400:
        scale = int(os.getenv("VISION_LONG_SIDE", "3600")) / max(width, height)
        width, height = round(width * scale), round(height * scale)
    tile_threshold = menu_vision.TILE_THRESHOLD if menu_vision is not None else 1600
    tiles = 1 if max(width, height) <= tile_threshold else 4
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
    pairs.sort(key=lambda row: (
        -row[0],
        _name_key(expected[row[1]]["name"]) != _name_key(predicted[row[2]]["name"]),
        _name_key(expected[row[1]]["name"]), _name_key(predicted[row[2]]["name"]),
        row[1], row[2],
    ))

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


def _strict_counts(
    expected: list[dict[str, Any]], predicted: list[dict[str, Any]]
) -> tuple[int, int]:
    """Threshold-independent, one-to-one exact name and name/price-state counts.

    A correctly preserved unknown price is a correct price *state*, not evidence
    of numeric price recognition. Numeric-only recall is reported separately.
    """
    expected_names = Counter(_name_key(str(item["name"])) for item in expected)
    predicted_names = Counter(_name_key(str(item["name"])) for item in predicted)
    exact_names = sum((expected_names & predicted_names).values())
    used: set[int] = set()
    complete = 0
    for item in expected:
        for index, candidate in enumerate(predicted):
            if index in used or _name_key(str(item["name"])) != _name_key(str(candidate["name"])):
                continue
            left, right = item.get("price"), candidate.get("price")
            if (left is None and right is None) or _price_equal(left, right):
                used.add(index)
                complete += 1
                break
    return exact_names, complete


def _edit_distance(left: str, right: str) -> int:
    """Levenshtein 距離，字元錯誤率的分子。

    專案沒裝 python-Levenshtein，為了一個指標多一個依賴不值得；
    菜名只有十幾個字，純 Python 的 DP 完全夠快。
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for i, lchar in enumerate(left, 1):
        current = [i]
        for j, rchar in enumerate(right, 1):
            current.append(
                min(
                    previous[j] + 1,  # 刪除
                    current[j - 1] + 1,  # 插入
                    previous[j - 1] + (lchar != rchar),  # 替換
                )
            )
        previous = current
    return previous[-1]


def score_case(case: dict[str, Any], result: dict[str, Any], threshold: float) -> dict[str, Any]:
    expected_items = case["expected"]["items"]
    predicted_items = _flatten_result(result)
    matching = match_items(expected_items, predicted_items, threshold)
    matches = matching["matches"]

    matched_count = len(matches)
    recall = matched_count / len(expected_items) if expected_items else 0.0
    precision = matched_count / len(predicted_items) if predicted_items else 0.0
    exact_names = sum(1 for m in matches if m["exactName"])
    exact_names_total, complete_items = _strict_counts(expected_items, predicted_items)
    all_priced = sum(item.get("price") is not None for item in expected_items)

    # 只在答案有標價格的品項上算價格分數。答案沒標價（菜單上就沒印）的，
    # 反過來檢查模型是不是自己編了一個數字出來。
    priced = [m for m in matches if m["expectedPrice"] is not None]
    price_correct = sum(1 for m in priced if _price_equal(m["expectedPrice"], m["predictedPrice"]))
    unpriced = [m for m in matches if m["expectedPrice"] is None]
    price_invented = sum(1 for m in unpriced if m["predictedPrice"] is not None)

    # 字元錯誤率。配對門檻是 0.72，所以「大魯肉飯」讀成「魯肉飯」仍算命中，
    # 召回率 100% 並不代表每個字都對；這個指標才看得出差多少。
    #
    # 分兩個數字報，因為混在一起會得到誤導的結論。實測 long-4600 這份紀錄：
    # 用原始字串算是 1.85%，但那 12 個編輯距離全部來自六個品項的全角括號（）
    # 被讀成半角 ()，正規化後真正認錯的字是 0 個。只報原始字串會把標點寬度
    # 說成辨識錯誤，只報正規化後又隱藏了下游用品名做子字串比對時要處理的差異。
    cer_edits = sum(
        _edit_distance(_name_key(m["expected"]), _name_key(m["predicted"])) for m in matches
    )
    cer_chars = sum(len(_name_key(m["expected"])) for m in matches)
    raw_edits = sum(_edit_distance(m["expected"], m["predicted"]) for m in matches)
    raw_chars = sum(len(m["expected"]) for m in matches)
    # Include all omitted/hallucinated names. Alignment still depends on the
    # fuzzy threshold, so also report threshold-independent strict item counts.
    strict_edits = (
        cer_edits
        + sum(len(_name_key(str(item["name"]))) for item in matching["missed"])
        + sum(len(_name_key(str(item["name"]))) for item in matching["spurious"])
    )
    strict_chars = cer_chars + sum(len(_name_key(str(item["name"]))) for item in matching["missed"])

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
        "exactNameCount": exact_names,
        "exactNameRate": round(exact_names / matched_count, 3) if matched_count else 0.0,
        "exactNameTotalCount": exact_names_total,
        "exactNameRecall": round(exact_names_total / len(expected_items), 4)
        if expected_items
        else None,
        "exactNamePriceCount": complete_items,
        "exactNamePriceRate": round(complete_items / len(expected_items), 4)
        if expected_items
        else None,
        "cer": round(cer_edits / cer_chars, 4) if cer_chars else None,
        "cerEdits": cer_edits,
        "cerChars": cer_chars,
        "cerRaw": round(raw_edits / raw_chars, 4) if raw_chars else None,
        "cerRawEdits": raw_edits,
        "cerRawChars": raw_chars,
        "cerStrict": round(strict_edits / strict_chars, 4) if strict_chars else None,
        "cerStrictEdits": strict_edits,
        "cerStrictChars": strict_chars,
        "pricedExpected": len(priced),
        "pricedExpectedTotal": all_priced,
        "priceCorrectCount": price_correct,
        "priceRecall": round(price_correct / all_priced, 4) if all_priced else None,
        "priceAccuracy": round(price_correct / len(priced), 3) if priced else None,
        "priceInvented": price_invented,
        "restaurantNameExpected": expected_name,
        "restaurantNameDetected": detected_name,
        "restaurantNameOk": name_ok,
        "qualityScoreReported": result.get("quality", {}).get("score"),
        "scoringVersion": SCORING_VERSION,
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


def _logging_vision(sink: list[dict[str, Any]], include_response: bool = True) -> Any:
    """包一層 vision_chat，把每一次呼叫的原始往返記下來。

    沒有這個就分不出「切塊根本沒讀到」和「切塊讀到了但最終校對把它刪掉」——
    兩者的最終結果一模一樣，修法卻完全相反。
    """
    from ollama_fuc import vision_chat as _vision_chat

    vision_module = _get_menu_vision()

    def logged(
        prompt: str,
        image_url: Any,
        model: Any = None,
        timeout: float = 180.0,
        temperature: Any = None,
    ) -> str:
        started = time.perf_counter()
        entry: dict[str, Any] = {
            "model": model,
            "imageCount": 1 if isinstance(image_url, str) else len(list(image_url)),
            "promptHead": prompt[:120],
            # 用 prompt 的雜湊當 replay 的鍵，不能用呼叫順序——切塊是並行跑的，
            # 順序每次都不一樣。
            "promptSha": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
        entry.update(
            build_request_provenance(
                prompt,
                image_url,
                model=model,
                temperature=temperature,
                timeout=timeout,
            )
        )
        try:
            response = _vision_chat(
                prompt, image_url, model=model, timeout=timeout, temperature=temperature
            )
        except Exception as exc:
            entry.update(
                {
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                    "durationSec": round(time.perf_counter() - started, 1),
                }
            )
            sink.append(entry)
            raise
        parsed = vision_module._extract_json_value(response)
        normalized = vision_module.normalize_vision_result(parsed)
        entry.update(
            {
                "durationSec": round(time.perf_counter() - started, 1),
                "responseChars": len(response),
                # JSON 解析失敗而且回應很長 = 幾乎確定是被 max_tokens 截斷。
                "jsonParsed": bool(parsed),
                "endsAbruptly": not response.rstrip().endswith(("}", "]", "```")),
                "itemCount": sum(len(c["items"]) for c in normalized["categories"]),
                "items": [i["name"] for c in normalized["categories"] for i in c["items"]],
                # 完整回應是 --replay 的原料。少了它，改動解析／合併邏輯時就只能
                # 重打 API，而模型輸出每次都不同，等於同時動了兩個變數。
            }
        )
        if include_response:
            entry["response"] = response
        sink.append(entry)
        return response

    return logged


def _replay_vision(
    recorded: dict[str, dict[str, Any]], sink: list[dict[str, Any]] | None = None
) -> Any:
    """用錄下來的回應餵給 analyze_menu_image，完全不打 API。

    模型輸出固定住之後，改動解析、合併、校對對帳這些邏輯才有辦法乾淨地
    A/B——否則模型自己每次讀的就不一樣（實測同一張圖，有一次把「魯」
    全讀成「鹿」），分數變化根本無法歸因。
    """

    def replayed(
        prompt: str,
        image_url: Any,
        model: Any = None,
        timeout: float = 180.0,
        temperature: Any = None,
    ) -> str:
        request = build_request_provenance(
            prompt,
            image_url,
            model=model,
            temperature=temperature,
            timeout=timeout,
            run_mode="replay",
        )
        key = request["requestSha256"]
        if key not in recorded:
            raise RuntimeError(
                "replay 找不到完整相同的請求：圖片、prompt、模型或參數已改變，"
                "或舊錄音缺少 requestSha256；不會轉成 live 呼叫。"
            )
        captured = recorded[key]
        if sink is not None:
            sink.append({**request, "response": captured["response"], "durationSec": 0.0})
        return captured["response"]

    return replayed


def run_case(
    case: dict[str, Any],
    threshold: float,
    debug: bool = False,
    recorded: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    exchanges: list[dict[str, Any]] = []
    image_path = case.get("image_path")
    if image_path is None or not Path(image_path).is_file():
        return failure_row(case, "skipped", "missing_image", 0.0, threshold)
    if recorded is not None:
        vision_kwargs: dict[str, Any] = {"vision_func": _replay_vision(recorded, exchanges)}
    else:
        vision_kwargs = {"vision_func": _logging_vision(exchanges, include_response=debug)}
    transport = None
    try:
        with trace_transport(deny_network=recorded is not None) as transport:
            result = analyze_menu_image(
                Path(image_path).read_bytes(),
                case["mime"],
                restaurant_hint="",  # 刻意不給提示，才測得出模型自己認不認得出店名
                **vision_kwargs,
            )
            scored = score_case(case, result, threshold)
    except Exception as exc:
        status = "empty" if "沒有辨識到可用" in str(exc) else "error"
        row = failure_row(
            case,
            status,
            f"{type(exc).__name__}: {exc}",
            time.perf_counter() - started,
            threshold,
        )
        row.update(traceback=traceback.format_exc(limit=5), exchanges=exchanges)
        if transport is not None:
            row["transport"] = transport.to_dict()
        return row
    usable = scored["predictedCount"] > 0
    return {
        "case": case["name"],
        "ok": usable,
        "status": "ok" if usable else "empty",
        "error": "" if usable else "沒有辨識到可用的菜單品項",
        "truth": case["expected"],
        "provenance": build_case_provenance(case, project_root=PROJECT_ROOT),
        "inference": provenance_from_calls(
            exchanges,
            result=result,
            run_mode="replay" if recorded is not None else "live",
        ),
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
        "transport": transport.to_dict() if transport is not None else {},
        **scored,
    }


def failure_row(
    case: dict[str, Any],
    status: str,
    error: str,
    duration: float,
    threshold: float,
) -> dict[str, Any]:
    """Retain ground truth; aggregate excludes unexecuted cases from accuracy."""
    return {
        "case": case["name"],
        "ok": False,
        "status": status,
        "error": error,
        "durationSec": round(duration, 1),
        "truth": case["expected"],
        "provenance": build_case_provenance(case, project_root=PROJECT_ROOT),
        "exchanges": [],
        **score_case(case, {"categories": []}, threshold),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    declared_scored = [row for row in rows if row.get("scoringVersion") == SCORING_VERSION]
    scored = [row for row in declared_scored if row.get("status") != "skipped"]
    unknown = len(rows) - len(declared_scored)

    def total(key: str) -> int:
        return sum(int(row.get(key, 0)) for row in scored)

    total_expected = total("expectedCount")
    total_matched = total("matchedCount")
    total_predicted = total("predictedCount")
    priced = total("pricedExpected")
    all_priced = total("pricedExpectedTotal")
    price_correct = total("priceCorrectCount")
    cer_chars, raw_chars, strict_chars = (
        total("cerChars"),
        total("cerRawChars"),
        total("cerStrictChars"),
    )
    summary = {
        "caseCount": len(rows),
        "okCount": sum(bool(row.get("ok")) for row in rows),
        "failedCount": sum(not row.get("ok") and row.get("status") != "skipped" for row in rows),
        "emptyCount": sum(row.get("status") == "empty" for row in rows),
        "skippedCount": sum(row.get("status") == "skipped" for row in rows),
        "scoredCaseCount": len(scored),
        "executedCount": sum(row.get("status") != "skipped" for row in rows),
        "availableCaseRate": round(
            sum(row.get("status") != "skipped" for row in rows) / len(rows), 4
        )
        if rows
        else None,
        "declaredExpectedCount": sum(row.get("expectedCount", 0) for row in declared_scored),
        "declaredPricedExpectedTotal": sum(
            row.get("pricedExpectedTotal", 0) for row in declared_scored
        ),
        "unknownTruthCount": unknown,
        "datasetComplete": not unknown and not any(row.get("status") == "skipped" for row in rows),
        "scoringVersion": SCORING_VERSION,
        "expectedCount": total_expected,
        "predictedCount": total_predicted,
        "matchedCount": total_matched,
        "pricedExpected": priced,
        "pricedExpectedTotal": all_priced,
        "priceCorrectCount": price_correct,
        "exactNameTotalCount": total("exactNameTotalCount"),
        "exactNamePriceCount": total("exactNamePriceCount"),
        # 用品項總數加總而不是各案例平均，避免品項少的照片被放大權重。
        "recall": round(total_matched / total_expected, 3) if total_expected else None,
        "precision": round(total_matched / total_predicted, 3) if total_predicted else None,
        # 完全文字匹配率與字元錯誤率：召回率看的是「這道菜有沒有被認出來」，
        # 這兩個看的是「認出來的字對不對」。評審指出前者 100% 不代表後者沒問題。
        "exactNameRate": round(total("exactNameCount") / total_matched, 3)
        if total_matched
        else None,
        "exactNameRecall": round(total("exactNameTotalCount") / total_expected, 4)
        if total_expected
        else None,
        "exactNamePriceRate": round(total("exactNamePriceCount") / total_expected, 4)
        if total_expected
        else None,
        "priceRecall": round(price_correct / all_priced, 4) if all_priced else None,
        "cer": round(total("cerEdits") / cer_chars, 4) if cer_chars else None,
        "cerRaw": round(total("cerRawEdits") / raw_chars, 4) if raw_chars else None,
        "cerStrict": round(total("cerStrictEdits") / strict_chars, 4) if strict_chars else None,
        "priceAccuracy": round(price_correct / priced, 3) if priced else None,
        "priceInvented": total("priceInvented"),
        "restaurantNameOkCount": sum(row.get("restaurantNameOk") is True for row in scored),
        "restaurantNameChecked": sum(row.get("restaurantNameOk") is not None for row in scored),
        "totalDurationSec": round(sum(row.get("durationSec", 0) for row in rows), 1),
    }
    # Legacy failures without truth must never create a deceptively complete
    # denominator. Rescore with truth snapshots/files before reporting rates.
    if unknown:
        for key in (
            "recall",
            "precision",
            "exactNameRate",
            "exactNameRecall",
            "exactNamePriceRate",
            "priceRecall",
            "cer",
            "cerRaw",
            "cerStrict",
            "priceAccuracy",
        ):
            summary[key] = None
    return summary


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
    print(
        f"  OCR={run['config']['visionModel']}  校對={run['config']['verifyModel']}"
        f"  fast={run['config']['fast']}"
    )
    print("=" * 72)
    print(_pad("案例", 20) + f"{'召回':>6}{'精確':>8}{'全標價':>8}{'秒':>7}  店名")
    print("-" * 72)
    for row in run["cases"]:
        if not row.get("ok"):
            print(
                _pad(row["case"], 20)
                + f"{row.get('status', 'error').upper():>8}  "
                + str(row.get("error", ""))[:48]
                + f"（真值 {row.get('expectedCount', '?')} 項）"
            )
            continue
        name_mark = {True: "✓", False: "✗", None: "-"}[row["restaurantNameOk"]]
        print(
            _pad(row["case"], 20) + f"{_pct(row['recall']):>8}"
            f"{_pct(row['precision']):>8}"
            f"{_pct(row.get('priceRecall')):>8}"
            f"{row['durationSec']:>7.1f}"
            f"  {name_mark} {row['restaurantNameDetected'][:16]}"
        )
    print("-" * 72)
    summary = run["summary"]
    if summary.get("scoredCaseCount"):
        print(
            _pad("總計", 20) + f"{_pct(summary['recall']):>8}"
            f"{_pct(summary['precision']):>8}"
            f"{_pct(summary.get('priceRecall')):>8}"
            f"{summary['totalDurationSec']:>7.1f}"
            f"  {summary['restaurantNameOkCount']}/{summary['restaurantNameChecked']}"
        )
    print(
        f"\n可用 {summary.get('okCount', 0)}/{summary['caseCount']}；"
        f"失敗 {summary.get('failedCount', 0)}（含空結果 {summary.get('emptyCount', 0)}）；"
        f"略過 {summary.get('skippedCount', 0)}"
    )
    if summary.get("skippedCount"):
        print("資料不完整：略過案例不算模型錯誤；主辨識分母包含所有已執行案例（含失敗／空結果）。")
    print(
        f"已執行案例真值 {summary.get('expectedCount', 0)} 項／"
        f"宣告資料集真值 {summary.get('declaredExpectedCount', 0)} 項；"
        f"案例可用率 {_pct(summary.get('availableCaseRate')).strip()}"
    )
    if summary.get("unknownTruthCount"):
        print("⚠ 部分舊紀錄缺少目前評分所需的真值／分子，請先 --rescore；不顯示不完整準確率。")
    if summary.get("scoredCaseCount"):
        print(
            f"\n全真值菜名完整率 {_pct(summary.get('exactNameRecall')).strip()}"
            f"（{summary.get('exactNameTotalCount', 0)}/{summary.get('expectedCount', 0)}）"
            f"；菜名＋價格狀態完整率 {_pct(summary.get('exactNamePriceRate')).strip()}"
            f"（{summary.get('exactNamePriceCount', 0)}/{summary.get('expectedCount', 0)}）"
            f"\n全標價真值價格召回 {_pct(summary.get('priceRecall')).strip()}"
            f"（{summary.get('priceCorrectCount', 0)}/{summary.get('pricedExpectedTotal', 0)}）"
            f"；已配對價格正確率 {_pct(summary.get('priceAccuracy')).strip()}"
            f"\n已配對菜名完整率 {_pct(summary.get('exactNameRate')).strip()}"
            f"；已配對字元錯誤率 {_pct(summary.get('cer')).strip()}"
            f"（含漏抓與幻覺 {_pct(summary.get('cerStrict')).strip()}；"
            f"含標點與全半角差異 {_pct(summary.get('cerRaw')).strip()}）"
        )
        print("菜名完整率使用程式正規化文字；價格狀態完整率包含正確保留未知價 null。")
    if summary.get("priceInvented"):
        print(f"\n⚠ 有 {summary['priceInvented']} 個品項菜單沒印價格，模型自己編了一個數字")

    for row in run["cases"]:
        if row.get("ok") and row.get("reordered"):
            print(f"\n[{row['case']}] 字對但順序不同 {len(row['reordered'])} 項（算命中）：")
            for item in row["reordered"][:10]:
                print(f"  {item['expected']} → {item['predicted']}")

    # 漏抓的品項是最有行動價值的輸出——它直接告訴你切塊或 max_tokens 出了問題。
    for row in run["cases"]:
        if row.get("status") == "skipped" or "missed" not in row:
            continue
        if row["missed"]:
            print(f"\n[{row['case']}] 漏抓 {len(row['missed'])} 項：")
            print(
                "  "
                + "、".join(row["missed"][: 40 if verbose else 12])
                + ("…" if not verbose and len(row["missed"]) > 12 else "")
            )
        if row["spurious"]:
            print(f"[{row['case']}] 多出 {len(row['spurious'])} 項（菜單上沒有）：")
            print(
                "  "
                + "、".join(row["spurious"][: 40 if verbose else 12])
                + ("…" if not verbose and len(row["spurious"]) > 12 else "")
            )
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
    explicit = Path(label)
    if explicit.is_file():
        candidates = [explicit]
    elif explicit.suffix.lower() == ".json":
        raise CaseError(f"找不到執行紀錄檔：{label}")
    else:
        candidates = sorted(RUNS_DIR.glob(f"*_{label}.json"))
    if not candidates:
        raise CaseError(f"找不到 label 為「{label}」的執行紀錄（{RUNS_DIR}）")
    try:
        run = json.loads(candidates[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CaseError(f"無法讀取執行紀錄：{exc}") from exc
    if not isinstance(run, dict) or not isinstance(run.get("cases"), list):
        raise CaseError("執行紀錄必須包含 cases 陣列")
    return run


def rescore(label: str, threshold: float, allow_current_truth: bool = False) -> dict[str, Any]:
    """用現在的計分規則重算一份舊紀錄，不打 API。

    計分規則本身也會有 bug。發現的時候不該為了重算而再燒一次配額，
    更不該把舊分數跟新分數混在一起比。
    """
    run = load_run(label)
    cases = {}
    if any(not isinstance(row.get("truth"), dict) for row in run["cases"]):
        cases = {case["name"]: case for case in load_cases(None)}
    for row in run["cases"]:
        case = (
            {"name": row["case"], "expected": row["truth"]}
            if isinstance(row.get("truth"), dict)
            else cases.get(row["case"])
        )
        if case is None:
            raise CaseError(f"「{row['case']}」缺少原始真值快照與答案檔，無法完整重算")
        historical = (row.get("provenance") or {}).get("truth", {}).get("sha256")
        if not historical:
            for entry in (run.get("provenance") or {}).get("caseManifest", []):
                if entry.get("caseId") == row["case"]:
                    historical = entry.get("truth", {}).get("sha256")
                    break
        current = build_case_provenance(case, project_root=PROJECT_ROOT)["truth"]["sha256"]
        if historical and historical != current:
            raise CaseError(f"「{row['case']}」真值與原執行雜湊不同，不能悄悄重算")
        if "truth" not in row and not historical:
            if not allow_current_truth:
                raise CaseError(
                    f"「{row['case']}」缺原始真值快照與雜湊；"
                    "確認目前答案後使用 --allow-current-truth"
                )
            run["currentTruthAcknowledged"] = True
        if row.get("ok") and (
            not isinstance(row.get("raw"), dict) or not isinstance(row["raw"].get("items"), list)
        ):
            raise CaseError(f"「{row['case']}」缺少原始輸出，無法重算；請還原當時紀錄")
        raw = row.get("raw") or {}
        fake_result = {
            "detected_restaurant_name": raw.get("detectedRestaurantName", ""),
            "quality": raw.get("quality", {}),
            "categories": [{"name": "全部", "items": raw.get("items", [])}],
        }
        row.update(score_case(case, fake_result, threshold))
        row["truth"] = case["expected"]
        row.setdefault("status", "ok" if row.get("ok") else "error")
        if row.get("ok") and not row["predictedCount"]:
            row.update(ok=False, status="empty", error="原始輸出沒有可用的菜單品項")
    run.setdefault("config", {})["matchThreshold"] = threshold
    run["scoringVersion"] = SCORING_VERSION
    if isinstance(run.get("provenance"), dict):
        run["provenance"].update(scoringVersion=SCORING_VERSION, matchThreshold=threshold)
        run["provenance"].setdefault("settings", {})["matchThreshold"] = threshold
    run["summary"] = aggregate(run["cases"])
    run["label"] = f"{run['label']}(重算)"
    return run


def sweep(label: str, thresholds: list[float], allow_current_truth: bool = False) -> None:
    """掃不同的配對門檻重算同一份紀錄，不打 API。

    評審問「0.72 這個門檻憑什麼」。單看一個門檻下的 100% 召回率答不了，
    要把門檻放寬與收緊的兩端一起列出來：放寬會讓召回率上升但字元錯誤率跟著
    上升，收緊則相反。兩條走勢放在一起，才看得出 0.72 落在什麼位置。
    """
    print()
    print("=" * 72)
    print(f"  配對門檻敏感度：{label}（重算存檔，不打 API）")
    print("=" * 72)
    print(
        f"{'門檻':>6}{'配對數':>8}{'召回':>8}{'精確':>8}"
        f"{'全真值菜名':>11}{'菜名與價態':>11}{'全標價召回':>11}{'嚴格CER':>10}"
    )
    print("-" * 72)
    for threshold in thresholds:
        run = rescore(label, threshold, allow_current_truth)
        summary = run["summary"]
        matched = summary.get("matchedCount", 0)
        mark = "  ←目前採用" if abs(threshold - DEFAULT_MATCH_THRESHOLD) < 1e-9 else ""
        print(
            f"{threshold:>6.2f}{matched:>8d}"
            f"{_pct(summary.get('recall')):>8}"
            f"{_pct(summary.get('precision')):>8}"
            f"{_pct(summary.get('exactNameRecall')):>11}"
            f"{_pct(summary.get('exactNamePriceRate')):>11}"
            f"{_pct(summary.get('priceRecall')):>11}"
            f"{_pct(summary.get('cerStrict')):>10}"
            f"{mark}"
        )
    print("-" * 72)
    print()


def compare_runs(label_a: str, label_b: str) -> None:
    run_a, run_b = load_run(label_a), load_run(label_b)
    compatibility = compare_provenance(run_a.get("provenance"), run_b.get("provenance"))
    if not compatibility["accuracyCompatible"]:
        raise CaseError("不能直接比較：" + "；".join(compatibility["reasons"]))
    if any(
        run.get("summary", {}).get("scoringVersion") != SCORING_VERSION for run in (run_a, run_b)
    ):
        raise CaseError("執行紀錄尚未使用目前評分版本，請先重算")
    if any(not run["summary"].get("datasetComplete") for run in (run_a, run_b)):
        raise CaseError("執行案例不完整，請選擇相同且均已執行的案例再比較")
    print()
    print("=" * 72)
    print(f"  比較：{label_a}  →  {label_b}")
    print("=" * 72)
    print(_pad("指標", 20) + f"{label_a:>14}{label_b:>14}{'差異':>12}")
    print("-" * 72)
    for key, title in (
        ("recall", "召回率"),
        ("precision", "精確率"),
        ("exactNameRecall", "全真值菜名完整率"),
        ("exactNamePriceRate", "菜名與價格狀態完整率"),
        ("priceRecall", "全標價真值價格召回"),
        ("priceAccuracy", "已配對價格正確率"),
    ):
        left, right = run_a["summary"].get(key), run_b["summary"].get(key)
        if left is None or right is None:
            continue
        delta = (right - left) * 100
        arrow = "▲" if delta > 0.05 else ("▼" if delta < -0.05 else "＝")
        print(
            _pad(title, 20)
            + f"{_pct(left):>14}{_pct(right):>14}"
            + f"{arrow} {delta:+.1f}pt".rjust(12)
        )
    left_time = run_a["summary"].get("totalDurationSec", 0)
    right_time = run_b["summary"].get("totalDurationSec", 0)
    if compatibility["latencyCompatible"]:
        print(
            _pad("觀測總耗時（秒）", 20)
            + f"{left_time:>14.1f}{right_time:>14.1f}{right_time - left_time:>+12.1f}"
        )
    for warning in compatibility["warnings"]:
        print("⚠ " + warning)

    by_case_a = {row["case"]: row for row in run_a["cases"]}
    for row in run_b["cases"]:
        if row["case"] not in by_case_a:
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
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_MATCH_THRESHOLD,
        help=f"品名配對相似度門檻（預設 {DEFAULT_MATCH_THRESHOLD}）",
    )
    parser.add_argument(
        "--jobs", type=int, default=1, help="同時跑幾個案例。預設 1，避免一次打爆學校 API"
    )
    parser.add_argument("--dry-run", action="store_true", help="只檢查案例與估算呼叫次數")
    parser.add_argument("--verbose", action="store_true", help="印出完整的漏抓/錯字清單")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="記錄每一次 vision 呼叫的原始往返，用來看品項是在哪一關掉的",
    )
    parser.add_argument("--compare", nargs=2, metavar=("A", "B"), help="比較兩次執行紀錄")
    parser.add_argument("--rescore", metavar="LABEL", help="用現在的計分規則重算舊紀錄，不打 API")
    parser.add_argument(
        "--allow-current-truth",
        action="store_true",
        help="舊紀錄缺真值快照及雜湊時，明確同意用目前答案重算（不補造原始來源）",
    )
    parser.add_argument(
        "--sweep",
        metavar="LABEL",
        help="掃 0.50 到 1.00 的配對門檻重算同一份紀錄，看指標隨門檻"
        "怎麼變，不打 API。用來說明門檻的選擇依據",
    )
    parser.add_argument(
        "--replay",
        metavar="LABEL",
        help="用某次 --debug 錄下的模型回應重跑整條 pipeline，不打 API。"
        "模型輸出固定住，改動解析／合併邏輯才能乾淨地 A/B",
    )
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1 or args.jobs < 1:
        parser.error("--threshold 必須在 0 到 1 之間，--jobs 至少為 1")

    if args.compare:
        try:
            compare_runs(*args.compare)
        except CaseError as exc:
            print(f"✗ {exc}")
            return 2
        return 0

    if args.sweep:
        steps = {round(0.50 + 0.05 * i, 2) for i in range(11)}
        try:
            sweep(args.sweep, sorted(steps | {DEFAULT_MATCH_THRESHOLD}), args.allow_current_truth)
        except CaseError as exc:
            print(f"✗ {exc}")
            return 2
        return 0

    if args.rescore:
        try:
            run = rescore(args.rescore, args.threshold, args.allow_current_truth)
        except CaseError as exc:
            print(f"✗ {exc}")
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

    config = {
        "visionModel": os.getenv("VISION_MODEL", "ornith-35b"),
        "verifyModel": os.getenv("VISION_VERIFY_MODEL", "ornith-35b"),
        "fast": bool(args.fast),
        "tileOverlap": args.tile_overlap if args.tile_overlap is not None else 0.12,
        "tileThreshold": args.tile_threshold if args.tile_threshold is not None else 1600,
        "matchThreshold": args.threshold,
        "jobs": args.jobs,
        "debug": bool(args.debug),
        "scoringVersion": SCORING_VERSION,
        "visionLongSide": os.getenv("VISION_LONG_SIDE", "3600"),
        "visionTiles": os.getenv("VISION_TILES", "0"),
        "visionTemperature": os.getenv("VISION_TEMPERATURE", "0.0"),
    }

    recordings: dict[str, dict[str, dict[str, Any]]] = {}
    source = None
    if args.replay:
        try:
            source = load_run(args.replay)
        except CaseError as exc:
            print(f"✗ {exc}")
            return 2
        for row in source["cases"]:
            captured = {
                entry["requestSha256"]: entry
                for entry in row.get("exchanges", [])
                if entry.get("requestSha256") and entry.get("response") is not None
            }
            if captured:
                recordings[row["case"]] = captured
        if not recordings:
            print(
                f"✗ 「{args.replay}」沒有可重播的錄音。"
                f"錄音需要用 --debug 跑，且包含完整請求 hash 與回應。"
            )
            return 2
        print(f"重播模式：{len(recordings)} 份錄音；缺錄音案例將明確略過，不會呼叫 API")

    planned = (
        0
        if args.replay
        else sum(estimate_api_calls(case["image_path"], args.fast) for case in cases)
    )
    if not args.replay:
        print(
            f"案例 {len(cases)} 個，預估 API 呼叫 {planned} 次"
            f"（OCR={config['visionModel']}，校對={config['verifyModel']}）"
        )

    if args.dry_run:
        skipped = 0
        for case in cases:
            calls = 0 if args.replay else estimate_api_calls(case["image_path"], args.fast)
            missing = case.get("skipReason") or (
                "missing_replay" if args.replay and case["name"] not in recordings else ""
            )
            skipped += bool(missing)
            print(
                f"  {case['name']:<20} 答案 {len(case['expected']['items']):>3} 項"
                f"　預估 {calls} 次呼叫" + (f"　SKIPPED: {missing}" if missing else "")
            )
        print(
            f"\n案例格式檢查：可用 {len(cases) - skipped}/{len(cases)}；"
            "--dry-run 不讀 .env、不呼叫 API；呼叫估算僅依目前程序環境與預設值。"
        )
        return 2 if skipped else 0

    # Live and pipeline replay use the product's normal settings. All inspection,
    # scoring and dry-run branches above remain independent of .env initialization.
    vision_module = _get_menu_vision()
    if args.tile_overlap is not None:
        vision_module.TILE_OVERLAP = args.tile_overlap
    if args.tile_threshold is not None:
        vision_module.TILE_THRESHOLD = args.tile_threshold
    config.update(
        visionModel=os.getenv("VISION_MODEL", vision_module.DEFAULT_OCR_MODEL),
        verifyModel=os.getenv("VISION_VERIFY_MODEL", vision_module.DEFAULT_VERIFY_MODEL),
        tileOverlap=vision_module.TILE_OVERLAP,
        tileThreshold=vision_module.TILE_THRESHOLD,
        visionLongSide=os.getenv("VISION_LONG_SIDE", str(vision_module.TARGET_LONG_SIDE)),
        visionTiles=os.getenv("VISION_TILES", "0"),
        visionTemperature=os.getenv("VISION_TEMPERATURE", "0.0"),
    )

    if not args.replay and not os.getenv("API_KEY"):
        print("✗ 沒有讀到 API_KEY，請確認專案根目錄的 .env")
        return 2

    run_started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    run_provenance = build_run_provenance(
        cases,
        run_mode="replay" if args.replay else "live",
        settings=config,
        replay_source=source.get("provenance") if source else None,
        project_root=PROJECT_ROOT,
    )
    print()

    def _run(case: dict[str, Any]) -> dict[str, Any]:
        if args.replay and case["image_path"] is not None and case["name"] not in recordings:
            return failure_row(case, "skipped", "missing_replay", 0.0, args.threshold)
        return run_case(
            case,
            args.threshold,
            args.debug,
            recordings.get(case["name"]) if args.replay else None,
        )

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
        "startedAt": run_started_at,
        "finishedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": config,
        "scoringVersion": SCORING_VERSION,
        "provenance": run_provenance,
        "cases": rows,
        "summary": aggregate(rows),
    }

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{args.label}.json"
    out_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    print_report(run, args.verbose)
    print(f"紀錄已存到 {out_path.relative_to(PROJECT_ROOT)}")
    print(
        f"比較兩次結果：python {Path(__file__).relative_to(PROJECT_ROOT)} "
        f"--compare <另一個label> {args.label}"
    )
    return 0 if run["summary"].get("okCount") == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
