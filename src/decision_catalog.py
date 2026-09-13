"""Menu-backed choices for one person's meal; unknown facts stay unknown."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from decision_preferences import SOFT_LABELS, menu_signals
from menu_semantics import annotate_item
from recommendation import _flatten_menu


def dish_type(name: str, category: str = "") -> str | None:
    # Item name wins over broad category labels (e.g. 飯麵類).
    for value in (name, category):
        for label, terms in (
            ("麵", ("麵", "麺", "烏龍", "米粉", "冬粉", "河粉", "粄條", "板條")),
            ("飯", ("飯", "粥")),
            ("餃子", ("水餃", "煎餃", "鍋貼", "蒸餃")),
            ("鍋物", ("火鍋", "涮涮鍋", "鍋燒", "小火鍋")),
            ("排餐", ("牛排", "豬排", "雞排", "魚排", "牛小排", "排餐", "香煎鯖魚")),
            ("輕食", ("漢堡", "吐司", "三明治", "貝果", "帕尼尼")),
        ):
            if any(term in value for term in terms):
                return label
    return None


def dish_family(name: str) -> str:
    value = re.sub(r"[（(](?:大|中|小|大份|小份|大碗|小碗)[）)]", "", name)
    # Bilingual menus sometimes repeat the same dish with English/Chinese swapped.
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]+", value))
    return chinese or re.sub(r"\W", "", value.casefold())


def protein_direction(name: str) -> str | None:
    """A comparison of menu wording, never an ingredient/allergen guarantee."""
    if re.search(r"素食|蔬食|素肉|素雞", name):
        return "蔬食"
    found = [label for label, pattern in (
        ("牛肉", r"牛"), ("豬肉", r"豬"), ("雞肉", r"雞"), ("羊肉", r"羊"),
        ("魚類", r"魚|鯖"), ("蝦蟹", r"蝦|蟹"),
    ) if re.search(pattern, name)]
    return found[0] if len(found) == 1 else None


def menu_choices(menu: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    rows: dict[str, dict[str, Any]] = {}
    for raw in _flatten_menu(menu):
        item = annotate_item(raw, raw["category"])
        semantic = item["semantic"]
        name = item["name"]
        kind = dish_type(name, item["category"])
        role = semantic.get("role", "other")
        if role in {"drink", "dessert", "side"} or re.search(
            r"加料|加點|單點配料", item["category"]
        ):
            continue
        price = item["price"]
        if price is not None and not math.isfinite(price):
            price = None
        identity = json.dumps([item["category"], name, price], ensure_ascii=False)
        item_id = hashlib.sha256(identity.encode()).hexdigest()[:20]
        texture = None
        if any(term in name for term in ("乾麵", "乾拌", "拌麵", "炒麵", "炒飯", "燴飯")):
            texture = "乾的"
        elif any(term in name for term in ("湯麵", "湯飯", "湯餃", "鍋燒", "粥")):
            texture = "湯的"
        rows[item_id] = {
            "id": item_id,
            "name": name,
            "price": price,
            "category": item["category"],
            "dishType": kind,
            "texture": texture,
            "protein": protein_direction(name),
            "semantic": semantic,
            "main": role == "main" or kind is not None,
            "family": dish_family(name),
            **menu_signals(item),
        }
    items = list(rows.values())
    if any(row["main"] for row in items):
        items = [row for row in items if row["main"]]
    fingerprint = hashlib.sha256(
        json.dumps([menu, items], ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]
    return items, fingerprint


def service_rate(menu: dict[str, Any], restaurant: str) -> float | None:
    scopes = [menu, menu.get("meta", {})]
    scopes.append(menu.get("restaurants", {}).get(restaurant, {}))
    for scope in scopes:
        if isinstance(scope, dict):
            value = scope.get("serviceRate")
            if isinstance(value, (int, float)) and math.isfinite(value) and 0 <= value <= 1:
                return float(value)
    return None


def eligible(
    rows: list[dict[str, Any]],
    prefs: dict[str, Any],
    rate: float | None,
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        name = row["name"]
        semantic = row["semantic"]
        if any(str(term).casefold() in name.casefold() for term in prefs.get("excludes", [])):
            continue
        if prefs.get("dishType") and row["dishType"] != prefs["dishType"]:
            continue
        if row["dishType"] in prefs.get("rejectedTypes", []):
            continue
        if prefs.get("texture") and row["texture"] != prefs["texture"]:
            continue
        if prefs.get("protein") and row.get("protein") != prefs["protein"]:
            continue
        allergens = semantic.get("allergens", {})
        if set(prefs.get("allergens", [])) & set(allergens.get("values", [])):
            continue
        dietary = set(prefs.get("dietaryRestrictions", []))
        if dietary & set(semantic.get("dietaryConflicts", [])):
            continue
        warnings = []
        if prefs.get("allergens") and not allergens.get("known"):
            warnings.append("成分未確認，請先向店家確認過敏原")
        if dietary and not dietary.issubset(set(semantic.get("dietaryFlags", []))):
            warnings.append("飲食限制尚待店家確認")
        spice = semantic.get("spice", {})
        profile = prefs.get("spiceProfile") or {}
        maximum = profile.get("maximum")
        if profile.get("strict") and maximum is not None:
            if spice.get("min") is not None and spice["min"] > maximum:
                continue
            if not spice.get("known"):
                warnings.append("辣度未標示，請向店家確認")
            elif spice.get("adjustable"):
                warnings.append("點餐時請告知店家需要的辣度")
        price = row["price"]
        total = round(price * (1 + (rate or 0)), 2) if price is not None else None
        budget = prefs.get("budget")
        if budget is not None and (total is None or total > budget):
            continue
        if prefs.get("cheaperThan") is not None and (
            total is None or total >= prefs["cheaperThan"]
        ):
            continue
        likes = sum(str(term) in name for term in prefs.get("likes", []))
        soft_score = 0
        evidence = []
        for field, labels in SOFT_LABELS.items():
            wanted = prefs.get(field)
            if wanted not in labels:
                continue
            known = row.get(field)
            if known == wanted:
                soft_score += 2
                evidence.append(
                    "菜單標示清爽／少油" if field == "taste" and wanted == "light" else
                    "菜單有濃郁口味或油炸線索" if field == "taste" else
                    "菜單標示大份" if wanted == "large" else "菜單標示小份"
                )
            elif known:
                soft_score -= 2
                warnings.append("菜單口味線索與這次偏好不同" if field == "taste"
                                else "菜單標示的份量與這次偏好不同")
            else:
                warnings.append("菜單未標示口味，無法確認是否符合" if field == "taste"
                                else "菜單未標示份量，無法確認大小")
        result.append({**row, "total": total, "warnings": warnings, "likes": likes,
                       "softScore": soft_score, "evidence": evidence})
    return result
