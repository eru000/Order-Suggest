"""D 組：VLM 擷取＋OCR 交叉複核（期末規劃 節點 5，解法 A）。

VLM 負責擷取結構化菜單；OCR 獨立讀同一張圖，只用來「找疑點」，不改 VLM 的輸出。
兩邊不一致的品項標記給人工複核，回應委員5-3：VLM 補出看似合理的錯價時，靠什麼發現。

標記種類（item["review"]["flags"]）
- price_ambiguous     VLM 的價格本身是看不清、範圍或多價（menu_price 判定）
- name_not_in_ocr     OCR 讀到的文字裡找不到這個品名（可能是幻覺或嚴重錯字）
- ocr_price_mismatch  兩邊都讀到價格，但數字不同
- price_unverified    VLM 給了價格，OCR 在這個品名旁邊沒讀到價格
結果層級：orphans＝OCR 讀到「品名＋價格」但 VLM 沒有的項目（可能漏抓）。
"""

from __future__ import annotations

from typing import Any

import ocr_baseline
from run_eval import DEFAULT_MATCH_THRESHOLD, _flatten_result, _match_score


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def cross_check(
    vlm_result: dict[str, Any],
    ocr_lines: list[dict[str, Any]],
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, Any]:
    """回傳 {"items": [...含 review], "orphans": [...], "summary": {...}}。不修改 vlm_result。"""
    ocr_items = ocr_baseline.pair_items(ocr_lines)
    ocr_texts = [line["text"] for line in ocr_lines]
    vlm_items = []
    for category in vlm_result.get("categories", []):
        for item in category.get("items", []):
            vlm_items.append(item)

    claimed: set[int] = set()
    reviewed = []
    for item in vlm_items:
        name = str(item.get("name") or "")
        flags: list[str] = []
        detail: dict[str, Any] = {}
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
