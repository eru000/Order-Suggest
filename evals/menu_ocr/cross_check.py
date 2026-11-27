"""D 組：VLM 擷取＋OCR 交叉複核（期末規劃 節點 5，解法 A）。

VLM 負責擷取結構化菜單；OCR 獨立讀同一張圖，只用來「找疑點」，不改 VLM 的輸出。
兩邊不一致的品項標記給人工複核，回應委員5-3：VLM 補出看似合理的錯價時，靠什麼發現。

標記種類（item["review"]["flags"]）
- price_ambiguous     VLM 的價格本身是看不清、範圍或多價（menu_price 判定）
- name_not_in_ocr     OCR 讀到的文字裡找不到這個品名（可能是幻覺或嚴重錯字）
- ocr_price_mismatch  兩邊都讀到價格，但數字不同
- price_unverified    VLM 給了價格，OCR 在這個品名旁邊沒讀到價格
- price_outlier       價格遠低於同分類其他品項（例如被遮掉一位數，680 讀成 68）。
                      遮蔽實驗顯示 VLM 與 OCR 會讀到同一個殘缺數字、彼此一致，
                      只有合理性檢查抓得到這種「共同盲點」。
結果層級：orphans＝OCR 讀到「品名＋價格」但 VLM 沒有的項目（可能漏抓）。
"""

from __future__ import annotations

from typing import Any

import ocr_baseline
from run_eval import DEFAULT_MATCH_THRESHOLD, _flatten_result, _match_score


# 低於參考中位數的這個比例就標記。2026-11-27 在開發資料掃描 0.15～0.5：0.15～0.25 對 192 個
# 讀對的品項零誤報，0.3 起開始誤報（向宏白飯 $15）；取零誤報範圍內最寬鬆的 0.25。
OUTLIER_RATIO = 0.25
OUTLIER_MIN_PEERS = 3


def price_outliers(vlm_result: dict[str, Any], ratio: float = OUTLIER_RATIO) -> dict[int, float]:
    """回傳 {id(item): 參考中位數}，列出價格明顯偏低的品項。

    參考值優先用同分類「其他」品項的價格中位數（至少 3 個），不夠就用整份菜單。
    """
    import statistics

    categories = [c for c in vlm_result.get("categories", []) if isinstance(c, dict)]
    all_prices = [
        (item, _number(item.get("price"))) for c in categories for item in c.get("items", [])
    ]
    flagged: dict[int, float] = {}
    for category in categories:
        items = category.get("items", [])
        for item in items:
            price = _number(item.get("price"))
            if price is None or price < 0:
                continue
            peers = [p for other, p in ((o, _number(o.get("price"))) for o in items) if other is not item and p]
            if len(peers) < OUTLIER_MIN_PEERS:
                peers = [p for other, p in all_prices if other is not item and p]
            if len(peers) < OUTLIER_MIN_PEERS:
                continue
            reference = statistics.median(peers)
            if price < ratio * reference:
                flagged[id(item)] = reference
    return flagged


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cross_check(
    vlm_result: dict[str, Any],
    ocr_lines: list[dict[str, Any]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    outlier_ratio: float | None = OUTLIER_RATIO,
) -> dict[str, Any]:
    """回傳 {"items": [...含 review], "orphans": [...], "summary": {...}}。不修改 vlm_result。"""
    ocr_items = ocr_baseline.pair_items(ocr_lines)
    ocr_texts = [line["text"] for line in ocr_lines]
    vlm_items = []
    for category in vlm_result.get("categories", []):
        for item in category.get("items", []):
            vlm_items.append(item)

    outliers = price_outliers(vlm_result, outlier_ratio) if outlier_ratio else {}
    claimed: set[int] = set()
    reviewed = []
    for item in vlm_items:
        name = str(item.get("name") or "")
        flags: list[str] = []
        detail: dict[str, Any] = {}
        if id(item) in outliers:
            flags.append("price_outlier")
            detail["referenceMedian"] = outliers[id(item)]
        info = item.get("priceInfo") or {}
        if info.get("status") in {"unreadable", "range", "multi"}:
            flags.append("price_ambiguous")

        best_item, best_score = None, 0.0
        for index, ocr_item in enumerate(ocr_items):
            score = _match_score(name, str(ocr_item["dish"]))
            if score > best_score:
                best_item, best_score = index, score
        best_line = max((_match_score(name, text) for text in ocr_texts), default=0.0)

        price = _number(item.get("price"))
        if max(best_line, best_score) < threshold:
            flags.append("name_not_in_ocr")
        elif best_item is not None and best_score >= threshold:
            claimed.add(best_item)
            ocr_price = _number(ocr_items[best_item]["price"])
            detail["ocrName"] = ocr_items[best_item]["dish"]
            if price is not None:
                if ocr_price is None:
                    flags.append("price_unverified")
                elif abs(ocr_price - price) >= 0.01:
                    flags.append("ocr_price_mismatch")
                    detail["ocrPrice"] = ocr_price
        elif price is not None:
            flags.append("price_unverified")
        reviewed.append({**item, "review": {"flags": flags, "detail": detail}})

    orphans = [
        ocr_item for index, ocr_item in enumerate(ocr_items)
        if index not in claimed and ocr_item.get("price") is not None
        and all(_match_score(str(ocr_item["dish"]), str(v.get("name") or "")) < threshold for v in vlm_items)
    ]
    flagged = sum(1 for item in reviewed if item["review"]["flags"])
    return {
        "items": reviewed,
        "orphans": orphans,
        "summary": {"itemCount": len(reviewed), "flaggedCount": flagged, "orphanCount": len(orphans)},
    }


__all__ = ["cross_check", "_flatten_result"]
