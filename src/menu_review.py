"""辨識結果的價格合理性檢查（產品端；期末規劃 節點 B／5）。

遮蔽實驗（evals/occlusion）發現：價格被遮掉一位數時，VLM 會照抄剩下的數字
（680→68、55→5），OCR 也讀到同一個數字，兩者一致，交叉比對抓不到；但這種價格
會遠低於同分類其他品項。這裡把明顯偏低的價格標記給人工確認。

門檻不是主觀設定：evals/review_policy/calibrate.py 以 Conformal Risk Control
（Angelopoulos et al., ICLR 2024, arXiv:2208.02814）在開發資料（44 張影像、592 個品項）
校準，可接受漏標率 α＝0.10（文獻範例值）時，選出的門檻 λ̂＝0.10；同時顯示在 α＝0.10
下不需要 OCR 交叉複核即可達標，因此產品端只放這個檢查。
"""

from __future__ import annotations

import statistics
from typing import Any

# 價格／同分類其他品項中位數 < 此值就標記。來源：evals/results/review_policy/calibration-dev.json
REVIEW_OUTLIER_RATIO = 0.10
MIN_PEERS = 3


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def outlier_scores(result: dict[str, Any]) -> dict[int, tuple[float, float]]:
    """{id(item): (價格／參考中位數, 參考中位數)}；比值越小越可疑，0 元的比值為 0。

    參考值優先用同分類「其他」品項的價格中位數（至少 3 個），不夠就用整份菜單；
    仍不足 3 個就不評分。
    """
    categories = [c for c in result.get("categories", []) if isinstance(c, dict)]
    all_prices = [
        (item, _number(item.get("price"))) for c in categories for item in c.get("items", [])
    ]
    scores: dict[int, tuple[float, float]] = {}
    for category in categories:
        items = category.get("items", [])
        for item in items:
            price = _number(item.get("price"))
            if price is None or price < 0:
                continue
            peers = [
                p for other in items if other is not item and (p := _number(other.get("price")))
            ]
            if len(peers) < MIN_PEERS:
                peers = [p for other, p in all_prices if other is not item and p]
            if len(peers) < MIN_PEERS:
                continue
            reference = statistics.median(peers)
            scores[id(item)] = (price / reference, reference)
    return scores


def apply_price_review(result: dict[str, Any], ratio: float = REVIEW_OUTLIER_RATIO) -> int:
    """在品項上標記 review.flags＝["price_outlier"]（就地修改），回傳標記數。

    只增減 price_outlier 這一個標記，其他來源的標記保留。人工修正價格後再呼叫一次即可重算。
    """
    scores = outlier_scores(result)
    count = 0
    for category in result.get("categories", []):
        for item in category.get("items", []) if isinstance(category, dict) else []:
            review = item.get("review") if isinstance(item.get("review"), dict) else {}
            flags = [f for f in review.get("flags", []) if f != "price_outlier"]
            detail = {
                k: v for k, v in (review.get("detail") or {}).items() if k != "referenceMedian"
            }
            score = scores.get(id(item))
            if score is not None and score[0] < ratio:
                flags.append("price_outlier")
                detail["referenceMedian"] = round(score[1], 2)
                count += 1
            if flags or detail:
                item["review"] = {"flags": flags, "detail": detail}
            else:
                item.pop("review", None)
    return count
