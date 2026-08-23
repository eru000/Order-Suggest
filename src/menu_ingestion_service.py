from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any


UNNAMED_MENU_NAME = "未命名菜單"


class MenuIngestionService:
    """Normalizes all ingestion sources before one transactional catalog write."""

    def __init__(
        self, catalog: MutableMapping[str, dict[str, Any]], minimum_items: int = 8
    ) -> None:
        self.catalog = catalog
        self.minimum_items = minimum_items

    @staticmethod
    def normalize_price(value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value) if float(value) >= 0 else None
        text = str(value or "").strip().replace(",", "")
        if not text or any(marker in text for marker in ("未提供", "未標示", "時價")):
            return None
        match = re.search(r"(?:NT\$|NTD|\$)?\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
        return float(match.group(1)) if match else None

    @staticmethod
    def is_non_food(name: str) -> bool:
        return any(keyword in name for keyword in ("塑膠袋", "購物袋", "甜心卡"))

    def unnamed_menu_name(self) -> str:
        """照片上沒印店名、模型也認不出來時的退路。

        店名是 catalog 的 key，不能留空——空字串會讓之後切換餐廳、查評價、
        存檔全部對不上。但也沒理由因此逼使用者先想一個名字：辨識結果本身
        是好的，缺的只是一個標籤。所以自動給一個，重複就往後編號。
        """
        if UNNAMED_MENU_NAME not in self.catalog:
            return UNNAMED_MENU_NAME
        index = 2
        while f"{UNNAMED_MENU_NAME} {index}" in self.catalog:
            index += 1
        return f"{UNNAMED_MENU_NAME} {index}"

    def register_vision(self, result: dict[str, Any]) -> dict[str, Any]:
        restaurant_name = str(result.get("restaurant_name") or "").strip()
        if not restaurant_name:
            restaurant_name = str(result.get("detected_restaurant_name") or "").strip()
        if not restaurant_name:
            restaurant_name = self.unnamed_menu_name()
        category_map = {
            str(category.get("name") or "其他"): {"items": list(category.get("items") or [])}
            for category in result.get("categories", [])
            if isinstance(category, dict) and category.get("items")
        }
        item_count = sum(len(value["items"]) for value in category_map.values())
        if not item_count:
            raise ValueError("沒有可新增的菜單項目")
        runtime_menu: dict[str, Any] = {
            "restaurants": {restaurant_name: {"name": restaurant_name, "categories": category_map}}
        }
        self.catalog[restaurant_name] = runtime_menu
        return {
            "restaurantName": restaurant_name,
            "itemCount": item_count,
            "categories": list(category_map),
        }

    def register_crawled(self, restaurant: Any) -> dict[str, Any]:
        restaurant_name = str(getattr(restaurant, "name", "") or "").strip()
        if not restaurant_name:
            raise ValueError("爬取結果缺少餐廳名稱")
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in list(getattr(restaurant, "menu_items", None) or []):
            name = str(getattr(raw, "name", "") or "").strip()
            if len(name) < 2 or name in seen or self.is_non_food(name):
                continue
            items.append({"name": name, "price": self.normalize_price(getattr(raw, "price", None))})
            seen.add(name)
        if len(items) < self.minimum_items:
            raise ValueError(f"有效菜單只有 {len(items)} 項，未達 {self.minimum_items} 項門檻")
        runtime_menu: dict[str, Any] = {
            "restaurants": {
                restaurant_name: {
                    "name": restaurant_name,
                    "categories": {"全部菜色": {"items": items}},
                }
            }
        }
        self.catalog[restaurant_name] = runtime_menu
        return {
            "restaurantName": restaurant_name,
            "itemCount": len(items),
            "menuItems": [{"dish": item["name"], "price": item["price"]} for item in items],
        }
