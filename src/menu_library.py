from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from main import Menu, _validate_menu, normalize_menu, write_menu_json


def _runtime_menu(restaurant_name: str, data: dict[str, Any]) -> Menu:
    items = [
        {
            "name": item.get("name", ""),
            "price": str(item.get("price", "價格未提供")).replace("$", "").replace(",", "").strip()
            if isinstance(item.get("price"), str)
            else item.get("price"),
        }
        for item in data.get("menu_items", [])
        if isinstance(item, dict)
    ]
    return {
        "restaurants": {
            restaurant_name: {
                "name": data.get("name", restaurant_name),
                "categories": {"全部菜色": {"items": items}},
            }
        }
    }


def load_menu_library(
    project_root: str | Path,
    default_restaurant_name: str,
) -> tuple[dict[str, Menu], str | None]:
    root = Path(project_root)
    menus: dict[str, Menu] = {}
    menu_paths = [root / "db" / "menu.json", root / "menu.json"]
    default_path = next((path for path in menu_paths if path.exists()), None)

    if default_path:
        try:
            default_data = json.loads(default_path.read_text(encoding="utf-8"))
            if "categories" in default_data:
                _validate_menu(default_data)
                stats = normalize_menu(default_data)
                if stats.get("market_price_tagged") or stats.get("removed_salt_tags"):
                    write_menu_json(default_data, str(default_path))
                menus[default_restaurant_name] = {
                    "restaurants": {
                        default_restaurant_name: {
                            "name": default_restaurant_name,
                            "categories": {
                                category["name"]: {"items": category.get("items", [])}
                                for category in default_data.get("categories", [])
                            },
                        }
                    }
                }
            elif isinstance(default_data.get("restaurants"), dict):
                for name in default_data["restaurants"]:
                    menus[str(name)] = default_data
        except Exception as exc:
            raise RuntimeError(f"載入菜單檔案失敗: {default_path} -> {exc}") from exc
    else:
        print(f"[WARN] 找不到菜單檔案 (menu.json)。已嘗試的路徑: {[str(p) for p in menu_paths]}")

    crawled_paths = list(root.glob("menu_*.json"))
    names_by_path: dict[Path, str] = {}
    for path in crawled_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            filename_name = path.stem.removeprefix("menu_")
            restaurant_name = str(data.get("name") or filename_name).strip() or filename_name
            if isinstance(data.get("menu_items"), list):
                menus[restaurant_name] = _runtime_menu(restaurant_name, data)
                names_by_path[path] = restaurant_name
                print(f" 載入餐廳菜單：{restaurant_name} ({len(data['menu_items'])} 項)")
        except Exception as exc:
            print(f" 載入 {path} 失敗：{exc}")

    active = None
    if names_by_path:
        latest = max(names_by_path, key=lambda path: path.stat().st_mtime)
        active = names_by_path[latest]
    elif menus:
        active = next(iter(menus))
    if active:
        print(f" 預設活動餐廳：{active}")
    return menus, active
