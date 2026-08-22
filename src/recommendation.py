from __future__ import annotations

import math
import os
import re
import statistics
from dataclasses import dataclass
from typing import Any

from menu_semantics import annotate_item
from observability import emit

UNKNOWN_PRICE = float("inf")


@dataclass(frozen=True)
class RecommendationPolicy:
    service_rate: float = 0.10
    people_per_main: int = 2
    people_per_side: int = 4
    people_per_drink: int = 4
    people_per_dessert: int = 6
    default_need_drink: bool = True


def _price(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number >= 0 else None
    match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
    if not match:
        return None
    number = float(match.group())
    return number if number >= 0 else None


def _flatten_menu(menu: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    restaurants = menu.get("restaurants")
    if isinstance(restaurants, dict):
        for restaurant_name, restaurant in restaurants.items():
            categories = restaurant.get("categories", {}) if isinstance(restaurant, dict) else {}
            if not isinstance(categories, dict):
                continue
            for category_name, category in categories.items():
                items = category.get("items", []) if isinstance(category, dict) else []
                for item in items:
                    if isinstance(item, dict) and str(item.get("name") or "").strip():
                        rows.append(
                            {
                                **item,
                                "name": str(item.get("name")).strip(),
                                "price": _price(item.get("price")),
                                "category": str(category_name),
                                "restaurant": str(restaurant_name),
                            }
                        )
        return rows

    categories = menu.get("categories")
    if isinstance(categories, list):
        for category in categories:
            if not isinstance(category, dict):
                continue
            for item in category.get("items", []):
                if isinstance(item, dict) and str(item.get("name") or "").strip():
                    rows.append(
                        {
                            **item,
                            "name": str(item.get("name")).strip(),
                            "price": _price(item.get("price")),
                            "category": str(category.get("name") or "未分類"),
                        }
                    )
    return rows


def _classify(name: str) -> str:
    value = name.casefold()
    if any(
        word in value
        for word in (
            "茶",
            "飲料",
            "飲品",
            "果汁",
            "咖啡",
            "奶茶",
            "可樂",
            "汽水",
            "豆漿",
            "拿鐵",
            "摩卡",
            "雪碧",
            "芬達",
            "氣泡",
            "啤酒",
            "紅酒",
            "白酒",
            "酒",
            "beer",
            "wine",
        )
    ):
        return "drink"
    if any(
        word in value
        for word in (
            "薯條",
            "雞塊",
            "魚圈",
            "蝦塊",
            "沙拉",
            "蔬菜棒",
            "小菜",
            "加料",
        )
    ):
        return "side"
    if any(
        word in value
        for word in (
            "冰淇淋",
            "蛋糕",
            "甜點",
            "派",
            "可頌",
            "甜甜圈",
            "蛋撻",
            "大福",
            "布丁",
        )
    ):
        return "dessert"
    if any(
        word in value
        for word in (
            "堡",
            "burger",
            "吐司",
            "貝果",
            "三明治",
            "套餐",
            "義大利麵",
            "燉飯",
            "麵",
            "飯",
            "排餐",
            "主餐",
            "獨享餐",
        )
    ):
        return "main"
    return "other"


_VARIANT_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    # 順序有意義：先判湯，否則「當歸鴨肉湯」會因為帶「肉」被歸到別類。
    ("湯", ("湯", "羹")),
    ("飯", ("飯", "丼", "粥")),
    ("麵", ("麵", "麵線", "冬粉", "米粉", "粄條", "noodle", "pasta")),
    ("點心", ("餅", "包", "餃", "捲", "酥")),
    ("盤", ("盤", "拼盤")),
)


def _variant_key(name: str) -> str:
    """粗略的品項型態，只用來判斷「這兩樣是不是同一種東西」。"""
    value = str(name).casefold()
    for key, words in _VARIANT_WORDS:
        if any(word in value for word in words):
            return key
    return "其他"


def _interleave_variants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """讓同一組推薦不要擠在同一種品項上（例如三碗都是麵）。

    價格相同時 sort_key 只能拿菜名的字碼位當最後順序，那等於隨機——實測會
    出現「乾麵 40 / 香拌麵線 40 都入選，同價的鴨肉飯(小) 40 永遠差一名」。
    這裡照原順序把品項分進型態桶再輪流取，桶內順序不動，所以偏好分數與價格
    的優先權都還在，只是不同型態會被提前。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(_variant_key(row["name"]), []).append(row)
    if len(buckets) < 2:
        return rows
    order = list(buckets.values())
    merged: list[dict[str, Any]] = []
    while any(order):
        for bucket in order:
            if bucket:
                merged.append(bucket.pop(0))
    return merged


def _preferred(item: dict[str, Any], preferred_dish: str | None) -> bool:
    if not preferred_dish:
        return False
    name = str(item["name"]).casefold()
    aliases = {
        "漢堡": ("堡", "burger"),
        "吐司": ("吐司", "toast"),
        "貝果": ("貝果", "bagel"),
        "套餐": ("套餐", "combo"),
    }
    words = aliases.get(str(preferred_dish), (str(preferred_dish).casefold(),))
    return any(word in name for word in words)


def _target_count(people: int, divisor: int) -> int:
    return max(1, math.ceil(people / divisor))


def _meta(
    prefs: dict[str, Any],
    budget: float | None,
    people: int,
    need_drink: bool,
    service_rate: float,
    subtotal: float = 0.0,
) -> dict[str, Any]:
    return {
        "budget": budget,
        "people": people,
        "needDrink": need_drink,
        "spiceLevel": prefs.get("spiceLevel"),
        "cuisine": prefs.get("cuisine"),
        "serviceRate": service_rate,
        "estimatedSubtotal": round(subtotal, 2),
        "estimatedTotal": round(subtotal * (1 + service_rate), 2),
    }


def _recommend_impl(
    menu: dict[str, Any],
    prefs: dict[str, Any] | None = None,
    top_k: int = 5,
    model: str | None = None,
    *,
    policy: RecommendationPolicy | None = None,
    semantic_enabled: bool = True,
) -> dict[str, Any]:
    """Return a deterministic, budget-safe meal plan.

    `top_k` is a hard contract. `budget` includes the configured service rate,
    and `people` changes the requested number of mains/sides/drinks.
    """
    del model  # Kept in the public signature for backward compatibility.
    prefs = dict(prefs or {})
    policy = policy or RecommendationPolicy()
    limit = max(0, int(top_k))

    raw_budget = prefs.get("budget")
    try:
        budget = float(raw_budget) if raw_budget is not None else None
    except (TypeError, ValueError):
        budget = None
    if budget is not None and budget < 0:
        budget = None

    try:
        people = max(1, min(50, int(prefs.get("people") or 1)))
    except (TypeError, ValueError):
        people = 1
    need_drink = bool(prefs.get("needDrink", policy.default_need_drink))
    service_rate = max(0.0, min(1.0, float(prefs.get("serviceRate", policy.service_rate))))
    if budget is not None and prefs.get("budgetBasis") == "per_person":
        budget *= people

    flattened = _flatten_menu(menu)
    all_items = (
        [annotate_item(item, str(item.get("category") or "")) for item in flattened]
        if semantic_enabled
        else flattened
    )
    if not all_items:
        return {
            "items": [],
            "notes": "菜單中沒有找到任何菜品",
            "meta": _meta(prefs, budget, people, need_drink, service_rate),
        }
    if limit == 0:
        return {
            "items": [],
            "notes": "top_k 為 0，未產生推薦",
            "meta": _meta(prefs, budget, people, need_drink, service_rate),
        }

    excludes = [str(value).casefold() for value in prefs.get("excludes", []) if str(value).strip()]
    raw_spice_profile = prefs.get("spiceProfile")
    spice_profile: dict[str, Any] = (
        dict(raw_spice_profile) if semantic_enabled and isinstance(raw_spice_profile, dict) else {}
    )
    if prefs.get("spiceLevel") == "不辣" and not spice_profile:
        excludes.append("辣")
    allergies = (
        {str(value) for value in prefs.get("allergens", []) if str(value).strip()}
        if semantic_enabled
        else set()
    )
    dietary = (
        {str(value) for value in prefs.get("dietaryRestrictions", []) if str(value).strip()}
        if semantic_enabled
        else set()
    )
    candidates = []
    for item in all_items:
        if any(word in str(item["name"]).casefold() for word in excludes):
            continue
        raw_semantic = item.get("semantic")
        semantic: dict[str, Any] = dict(raw_semantic) if isinstance(raw_semantic, dict) else {}
        raw_allergens = semantic.get("allergens")
        allergen_data: dict[str, Any] = (
            dict(raw_allergens) if isinstance(raw_allergens, dict) else {}
        )
        item_allergens = {str(value) for value in allergen_data.get("values", [])}
        if allergies and (not allergen_data.get("known") or allergies & item_allergens):
            continue
        flags = {str(value) for value in semantic.get("dietaryFlags", [])}
        if dietary and not dietary.issubset(flags):
            continue
        raw_spice = semantic.get("spice")
        spice_data: dict[str, Any] = dict(raw_spice) if isinstance(raw_spice, dict) else {}
        if spice_profile.get("strict"):
            maximum = spice_profile.get("maximum")
            item_minimum = spice_data.get("min")
            if (
                maximum is not None
                and item_minimum is not None
                and int(item_minimum) > int(maximum)
            ):
                continue
        candidates.append(item)
    if not candidates:
        return {
            "items": [],
            "notes": "根據您的條件，沒有找到合適的菜品",
            "meta": _meta(prefs, budget, people, need_drink, service_rate),
        }

    priced = [item["price"] for item in candidates if item["price"] is not None]
    addon_names = set()
    if len(priced) >= 3:
        median_price = statistics.median(priced)
        addon_names = {
            item["name"]
            for item in candidates
            if item["price"] is not None and median_price > 0 and item["price"] < median_price * 0.4
        }

    groups: dict[str, list[dict[str, Any]]] = {
        "main": [],
        "side": [],
        "drink": [],
        "dessert": [],
        "other": [],
    }
    preferred_dish = prefs.get("preferredDish")
    liked_terms = [str(value).casefold() for value in prefs.get("likes", [])]
    spice_target = spice_profile.get("target")
    for item in candidates:
        raw_semantic = item.get("semantic")
        semantic = dict(raw_semantic) if isinstance(raw_semantic, dict) else {}
        semantic_role = str(semantic.get("role") or "")
        kind = (
            "side"
            if item["name"] in addon_names
            else (semantic_role if semantic_role in groups else _classify(item["name"]))
        )
        preferred = _preferred(item, preferred_dish)
        score = 1.0 if preferred else 0.0
        if any(term in str(item["name"]).casefold() for term in liked_terms):
            score += 0.8
        raw_spice = semantic.get("spice")
        spice_data = dict(raw_spice) if isinstance(raw_spice, dict) else {}
        item_spice = spice_data.get("max")
        if spice_target is not None:
            score += (
                -0.35
                if item_spice is None
                else max(0.0, 1.0 - abs(int(item_spice) - int(spice_target)) / 5)
            )
        item = {**item, "kind": kind, "preferred": preferred, "preferenceScore": score}
        groups[kind].append(item)

    def sort_key(item: dict[str, Any]):
        return (
            -float(item.get("preferenceScore") or 0),
            item["price"] is None,
            item["price"] if item["price"] is not None else UNKNOWN_PRICE,
            item["name"],
        )

    for kind in list(groups):
        groups[kind].sort(key=sort_key)
        groups[kind] = _interleave_variants(groups[kind])

    food_budget = budget / (1 + service_rate) if budget is not None else None
    selected: list[dict[str, Any]] = []
    subtotal = 0.0

    def take(kind: str, count: int, reason: str, generous: bool = False) -> None:
        nonlocal subtotal
        rows = groups[kind]
        if generous:
            # 由便宜往貴取，三個人六百塊的預算只會點到兩百出頭，AI 每次都得說
            # 「離預算還很遠」。補位這一段改成先挑吃得實在的，仍受 food_budget
            # 卡關，所以預算緊的時候會自然退回便宜品項。
            rows = sorted(rows, key=lambda item: -(item["price"] or 0))
        for item in rows:
            if count <= 0 or len(selected) >= limit:
                break
            price = item["price"]
            if food_budget is not None and (price is None or subtotal + price > food_budget + 1e-9):
                continue
            selected.append(
                {
                    "name": item["name"],
                    "price": price,
                    "category": item["category"],
                    "reason": reason,
                    "type": kind,
                }
            )
            subtotal += price or 0.0
            count -= 1

    take("main", _target_count(people, policy.people_per_main), "主餐推薦")
    take("side", _target_count(people, policy.people_per_side), "搭配配菜")
    if need_drink:
        take("drink", _target_count(people, policy.people_per_drink), "搭配飲品")
    take("dessert", _target_count(people, policy.people_per_dessert), "搭配甜點")
    take("other", limit - len(selected), "額外推薦", generous=food_budget is not None)

    notes = ""
    if not selected:
        notes = "預算不足或沒有符合條件且價格明確的品項"
    return {
        "items": selected[:limit],
        "notes": notes,
        "meta": _meta(prefs, budget, people, need_drink, service_rate, subtotal),
    }


def recommend(
    menu: dict[str, Any],
    prefs: dict[str, Any] | None = None,
    top_k: int = 5,
    model: str | None = None,
    *,
    policy: RecommendationPolicy | None = None,
) -> dict[str, Any]:
    enabled = os.getenv("SEMANTIC_RECOMMENDER_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    shadow = os.getenv("SEMANTIC_SHADOW_MODE", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    semantic = _recommend_impl(
        menu,
        prefs,
        top_k,
        model,
        policy=policy,
        semantic_enabled=enabled or shadow,
    )
    if not shadow:
        return semantic
    legacy = _recommend_impl(
        menu,
        prefs,
        top_k,
        model,
        policy=policy,
        semantic_enabled=False,
    )
    emit(
        "recommendation.shadow_comparison",
        semanticItems=[item.get("name") for item in semantic.get("items", [])],
        legacyItems=[item.get("name") for item in legacy.get("items", [])],
        semanticEmpty=not bool(semantic.get("items")),
        legacyEmpty=not bool(legacy.get("items")),
    )
    return legacy
