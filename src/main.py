#import
from __future__ import annotations
import os, json, re, shutil, subprocess, random, time
from typing import Dict, List, Optional, TypedDict, Literal, Tuple


DEFAULT_MODEL = (
    os.environ.get("API_MODEL")
    or os.environ.get("AI_MODEL")
    or os.environ.get("MODEL")
    or os.environ.get("OLLAMA_MODEL")
    or "gemma3:12b"
)
OLLAMA_BIN = os.getenv("OLLAMA_BIN", "ollama")

def _cli_available() -> bool:
    return shutil.which(OLLAMA_BIN) is not None

# 啟動 daemon
_DAEMON_SPAWNED = False
def ensure_daemon() -> None:
    global _DAEMON_SPAWNED
    if _DAEMON_SPAWNED:
        return
    if not _cli_available():
        raise RuntimeError(f"找不到 ollama 可執行檔，請設定 PATH 或 OLLAMA_BIN（目前：{OLLAMA_BIN}）。")
    try:
        kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags |= subprocess.CREATE_NO_WINDOW
            DETACHED_PROCESS = 0x00000008
            creationflags |= DETACHED_PROCESS
            kwargs["startupinfo"] = startupinfo
            kwargs["creationflags"] = creationflags
        subprocess.Popen([OLLAMA_BIN, "serve"], **kwargs)  # 已在跑會快速返回
        time.sleep(0.3)  
    except Exception:
        pass
    _DAEMON_SPAWNED = True

def _cli_run(args: List[str], input_text: Optional[str] = None, timeout: float = 120.0) -> str:
    try:
        ensure_daemon()  # 啟動後台在 不開視窗
    except Exception:
        pass
    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags |= subprocess.CREATE_NO_WINDOW

    proc = subprocess.run(
[OLLAMA_BIN, *args],
        input=(input_text.encode("utf-8") if input_text is not None else None),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
        startupinfo=startupinfo,
        creationflags=creationflags,
    )
    out = proc.stdout.decode("utf-8", errors="ignore").strip()
    err = proc.stderr.decode("utf-8", errors="ignore").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"ollama 命令失敗: {' '.join([OLLAMA_BIN, *args])}\n{err}")
    return out or err

# 導入 Ollama 封裝
try:
    from ollama_fuc import (
        recommend as ollama_recommend,
        chat as ollama_chat,
        ensure_daemon as ollama_ensure_daemon,
    )
except Exception:
    ollama_recommend = None  # type: ignore
    ollama_chat = None       # type: ignore
    ollama_ensure_daemon = None  # type: ignore


# 型別定義 
class Option(TypedDict, total=False):
    name: str
    extraPrice: Optional[float]


class MenuItem(TypedDict, total=False):
    name: str
    price: Optional[float]
    options: List[Option]
    tags: List[str]


class Category(TypedDict):
    name: str
    items: List[MenuItem]


class Menu(TypedDict):
    categories: List[Category]


class Preferences(TypedDict, total=False):
    spiceLevel: str
    excludes: List[str]
    budget: Optional[float]
    cuisine: Optional[str]
    notes: str
    needDrink: bool
    people: int
    weights: Dict[str, float]


class ConversationTurn(TypedDict, total=False):
    role: Literal["user", "assistant", "system"]
    content: str
    meta: Dict[str, object]


# JSON -> Menu 讀取

def write_menu_json(menu: Menu, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)


# 正規化規則 
_BEVERAGE_KEYWORDS = [
    "酒",     # 烈酒區、紅酒/清酒 等
    "啤酒",
    "清酒",
    "紅酒",
    "果汁",
    "茶",
    "飲料",
]
_BEVERAGE_EXACT = {"季節限定"}  #此分類皆為飲品


def _is_beverage_category(name: str) -> bool:
    if name in _BEVERAGE_EXACT:
        return True
    return any(k in name for k in _BEVERAGE_KEYWORDS)


def normalize_menu(menu: Menu) -> Dict[str, int]:
    """依需求正規化：
    1) price == 0 代表時價 → 為該品項加入『時價』標籤（若尚未存在）
    2) 酒與飲料類別（依分類名判定）移除所有『鹹度N』標籤

    回傳變更統計：{"market_price_tagged": x, "removed_salt_tags": y}
    """
    changed_market = 0
    removed_salt = 0

    for cat in menu.get('categories', []):
        is_bev = _is_beverage_category(cat.get('name', ''))
        for item in cat.get('items', []):
            tags = item.get('tags', [])
            # 時價標籤
            if item.get('price') == 0:
                if 'tags' not in item:
                    item['tags'] = []
                    tags = item['tags']
                if '時價' not in tags:
                    tags.append('時價')
                    changed_market += 1

            # 飲品移除鹹度
            if is_bev and tags:
                before = len(tags)
                item['tags'] = [t for t in tags if not (t.startswith('鹹度') and t[2:].isdigit())]
                removed_salt += before - len(item['tags'])

    return {"market_price_tagged": changed_market, "removed_salt_tags": removed_salt}


# 偏好抽取
_SPICE_WORDS = ["不辣", "微辣", "小辣", "中辣", "大辣", "很辣"]

# 辣度關鍵字。順序有意義：「不吃辣」也包含「吃辣」，先比對正面線索會判成相反的意思，
# 所以一律「先否定、後肯定」，明確等級再排在模糊線索前面。
_SPICE_MILD = [
    "不要太辣", "不會太辣", "不能太辣", "別太辣", "不要很辣", "不要超辣", "不要大辣",
    "微微辣", "小小辣", "微辣", "小辣", "一點點辣", "一點辣", "辣度普通", "普通辣",
]
_SPICE_NONE = [
    "不吃辣", "不能吃辣", "不敢吃辣", "不會吃辣", "受不了辣", "怕辣",
    "不要辣", "不加辣", "免辣", "去辣", "無辣", "不辣",
]
_SPICE_STRONG = [("中辣", "中辣"), ("大辣", "大辣"), ("很辣", "大辣"),
                 ("超辣", "大辣"), ("特辣", "大辣"), ("重辣", "大辣")]
_SPICE_YES = ["要辣", "吃辣", "辣一點", "重口味", "嗜辣", "愛吃辣"]

# 出現在忌口欄位時要丟掉的辣度詞：辣度由 spiceLevel 表達，
# 留在 excludes 只會變成比對不到任何菜名的無效關鍵字（例如「太辣」）。
_SPICE_NOISE = {"辣", "太辣", "很辣", "超辣", "大辣", "中辣", "小辣", "微辣", "重辣"}

_NEGATORS = ("不", "別", "免", "去", "沒", "勿", "無", "怕", "少")

# 忌口片語的斷句字元。中文輸入法常打出全形空白與全形標點，漏掉它們會讓整句
# 都被當成同一個忌口片語（「不要牛肉　要飲料」會多出「要飲料」這個假忌口）。
# 注意「、」不在這裡：它是忌口項目之間的並列符號，不是句子結束。
_EXCLUDE_STOPS = ("。", "，", ",", "；", ";", "！", "!", "？", "?",
                  "\n", "\t", " ", "　")


def _cut_at_first_stop(seg: str) -> str:
    """切到最靠前的那個斷句字元。

    不能照清單順序逐一 find 就 break——那樣切到的是「清單裡第一個出現過的」
    符號，不是句子裡位置最前面的，遇到混用標點就會切錯位置。
    """
    positions = [pos for pos in (seg.find(s) for s in _EXCLUDE_STOPS) if pos != -1]
    return seg[:min(positions)] if positions else seg


def _is_negated(text: str, idx: int) -> bool:
    """檢查 text[idx] 這個詞的前 3 個字內有沒有否定詞。

    用來擋掉「不吃大辣」這種：關鍵字「大辣」有命中，但整句其實是否定的。
    """
    return any(n in text[max(0, idx - 3):idx] for n in _NEGATORS)


def _match_spice(t: str) -> Optional[str]:
    """從自然語句判斷辣度，回傳 _SPICE_WORDS 裡的標準值或 None。"""
    for w in _SPICE_MILD:
        if w in t:
            return "小辣"
    for w in _SPICE_NONE:
        if w in t:
            return "不辣"
    for w, level in _SPICE_STRONG:
        idx = t.find(w)
        if idx != -1:
            return "小辣" if _is_negated(t, idx) else level
    for w in _SPICE_YES:
        idx = t.find(w)
        if idx != -1 and not _is_negated(t, idx):
            return "中辣"
    # 前面都沒中，但句子裡有「辣」又帶否定詞（例如「這家不會辣吧」）
    idx = t.find("辣")
    if idx != -1 and _is_negated(t, idx):
        return "不辣"
    return None


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _cn_to_int(s: str) -> Optional[int]:
    """把「兩百」「三百五」「一千二」這類中文數字轉成整數，無法解析回 None。"""
    total = current = 0
    last_unit = 1
    for ch in s:
        if ch in _CN_DIGITS:
            current = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (current or 1) * unit
            current = 0
            last_unit = unit
        else:
            return None
    if current:
        # 「三百五」口語是 350，尾數要補上比前一個單位小一級的位數
        total += current * (last_unit // 10) if last_unit >= 10 and total else current
    return total or None


_BUDGET_CUES = r"預算|不超過|不要超過|沒超過|小於|低於|最多|上限|以內|以下|<="
# 兩個 pattern 都把金額放在具名群組 amount/cn，避免用位置索引取到單位字元。
_BUDGET_PATTERNS = [
    re.compile(rf"(?:{_BUDGET_CUES})\s*(?P<amount>\d{{2,6}})"),
    re.compile(r"(?P<amount>\d{2,6})\s*(?:元|塊|圓|NT\$?|NTD)", re.IGNORECASE),
    re.compile(rf"(?:{_BUDGET_CUES})\s*(?P<cn>[一二兩三四五六七八九十百千]+)"),
    re.compile(r"(?P<cn>[一二兩三四五六七八九十百千]+)\s*(?:元|塊|圓)"),
    # 「一千二以內」「800 以內」——提示詞在金額後面，單位可省略
    re.compile(r"(?P<amount>\d{2,6})\s*(?:元|塊|圓)?\s*(?:以內|以下|之內|左右|上下)"),
    re.compile(r"(?P<cn>[一二兩三四五六七八九十百千]+)\s*(?:元|塊|圓)?\s*(?:以內|以下|之內|左右|上下)"),
]


def _match_budget(t: str) -> Optional[float]:
    """抓預算金額，支援「預算300」「800元」「兩百塊以內」等寫法。"""
    for pat in _BUDGET_PATTERNS:
        m = pat.search(t)
        if not m:
            continue
        groups = m.groupdict()
        if groups.get("amount"):
            try:
                return float(groups["amount"])
            except ValueError:
                continue
        if groups.get("cn"):
            value = _cn_to_int(groups["cn"])
            if value:
                return float(value)
    return None


def extract_prefs_with_llm(text: str) -> Preferences:
    """ 使用 LLM 智能提取使用者偏好（語意理解）"""
    try:
        from ollama_fuc import chat
        
        # 使用更小更快的模型（llama3.1 或 gemma3）
        model = os.environ.get("PREF_MODEL") or DEFAULT_MODEL
        
        prompt = f"""請分析使用者訊息，提取點餐偏好。只回傳 JSON 格式，不要其他文字。

偏好欄位說明：
- preferredDish: 想吃的菜品類型（如："漢堡"、"吐司"、"貝果"、"義大利麵"、"燉飯"等）
- budget: 預算金額（數字）
- spiceLevel: 辣度（"不辣"、"微辣"、"小辣"、"中辣"、"大辣"）
- cuisine: 菜系（"中式"、"日式"、"美式"、"義式"等）
- needDrink: 是否要飲料（true/false）
  * 如果說「不要飲料」、「不含飲料」、「無飲料」→ false
  * 如果說「要飲料」、「加飲料」、「來杯飲料」→ true
  * 沒提到飲料 → 不要包含此欄位
- excludes: 忌口食材列表（陣列）
  * 「不要牛肉」→ ["牛肉"]
  * 「不吃辣、不要花生」→ ["辣", "花生"]

使用者訊息: "{text}"

請回傳 JSON（如果某項沒提到就不要包含該欄位）:
"""
        
        response = chat([{"role": "user", "content": prompt}], model=model, timeout=60.0)
        print(f" [LLM偏好] 原始回應: {response[:200]}")
        
        # 提取 JSON
        import json
        import re
        
        # 嘗試直接解析
        try:
            prefs = json.loads(response)
            print(f" [LLM偏好] 成功: {prefs}")
            return prefs
        except:
            # 嘗試提取 JSON 區塊
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if json_match:
                prefs = json.loads(json_match.group(0))
                print(f" [LLM偏好] 提取成功: {prefs}")
                return prefs
            else:
                print(f" [LLM偏好] 解析失敗，降級")
                return {}
    except Exception as e:
        print(f" [LLM偏好] 錯誤: {e}")
        return {}


def extract_prefs_from_text(text: str) -> Preferences:
    """主要入口：結合 LLM 智能提取 + 關鍵字提取"""
    
    # 檢查是否啟用 LLM（預設 false）
    use_llm = os.environ.get("USE_LLM_EXTRACTION", "false").lower() == "true"
    
    if use_llm:
        # 優先嘗試 LLM 提取
        llm_prefs = extract_prefs_with_llm(text)
    else:
        llm_prefs = {}
    
    # 關鍵字提取（作為補充）
    prefs: Preferences = {}
    t = text.strip()

    # 辣度
    spice = _match_spice(t)
    if spice:
        prefs["spiceLevel"] = spice

    # 忌口
    excludes: List[str] = []
    for cue in ("不要", "不吃", "不敢吃", "忌口", "過敏"):
        idx = t.find(cue)
        if idx != -1:
            seg = t[idx + len(cue):]
            seg = _cut_at_first_stop(seg)
            # 「跟/和/與/及/還有」也是並列詞。不切開的話「牛肉跟豬肉」會變成一個
            # 對不到任何菜名的假關鍵字，等於整條忌口失效。
            for p in re.split(r"[、,\s]+|跟|和|與|及|還有|以及|加上", seg):
                p = p.strip()
                # 辣度已由 spiceLevel 表達，留在這裡只會產生「太辣」這種無效關鍵字
                if p and p not in _SPICE_NOISE:
                    excludes.append(p)
    if excludes:
        prefs["excludes"] = list(dict.fromkeys(excludes))

    # 預算
    budget = _match_budget(t)
    if budget is not None:
        prefs["budget"] = budget

    # 菜系
    for c in ("中式", "日式", "泰式", "美式", "韓式", "義式"):
        if c in t:
            prefs["cuisine"] = c
            break
    
    # 特定菜品類型偏好
    print(f" [DEBUG extract_prefs] 使用者輸入: '{t}'")
    if any(kw in t for kw in ["漢堡", "burger", "堡", "芝加哥堡"]):
        prefs["preferredDish"] = "漢堡"
        print(f" [DEBUG extract_prefs] 識別到漢堡偏好")
    elif any(kw in t for kw in ["吐司", "toast"]):
        prefs["preferredDish"] = "吐司"
        print(f" [DEBUG extract_prefs] 識別到吐司偏好")
    elif any(kw in t for kw in ["貝果", "bagel"]):
        prefs["preferredDish"] = "貝果"
        print(f" [DEBUG extract_prefs] 識別到貝果偏好")
    elif any(kw in t for kw in ["套餐", "combo"]):
        prefs["preferredDish"] = "套餐"
        print(f" [DEBUG extract_prefs] 識別到套餐偏好")
    
    # 改進：檢測否定詞（不要、不含、無）+ 飲料
    need_drink_neg = re.search(r"(不要|不含|無|不需要|別加)\s*飲料", t)
    need_drink_pos = ("飲料" in t) or ("喝" in t) or ("飲品" in t)
    
    # 優先檢查 excludes 中是否有「飲料」
    if excludes and "飲料" in excludes:
        prefs["needDrink"] = False
        print(f" [DEBUG extract_prefs] excludes 中有「飲料」，設定 needDrink=False")
    elif need_drink_neg:
        prefs["needDrink"] = False
        print(f" [DEBUG extract_prefs] 識別到「不要飲料」，設定 needDrink=False")
    elif need_drink_pos and not need_drink_neg:
        # 只有在明確要飲料時才設定 True
        prefs["needDrink"] = True
        print(f" [DEBUG extract_prefs] 識別到「要飲料」，設定 needDrink=True")

    # 人數。「四個人」「兩位」這種口語跟「4人」一樣常見，中文數字也要吃。
    m2 = re.search(r"(\d{1,2})\s*(?:個|位)?\s*人", t)
    if m2:
        try:
            prefs["people"] = int(m2.group(1))
        except ValueError:
            pass
    else:
        m2 = re.search(r"([一二兩三四五六七八九十]+)\s*(?:個|位)?\s*人", t)
        if m2:
            people = _cn_to_int(m2.group(1))
            if people:
                prefs["people"] = people

    # 動態權重線索
    cue_main = any(k in t for k in ["主菜", "吃飽", "份量", "大份", "有菜有肉"])
    cue_variety = any(k in t for k in ["多樣", "不要都一樣", "各點一些", "分著吃", "分享", "拼盤", "試試看"])
    cue_light = any(k in t for k in ["清爽", "清淡", "健康", "少油"])

    has_budget = prefs.get("budget") is not None
    need_drink = prefs.get("needDrink", False)  # 從 prefs 取得，而非重複判斷
    
    constraint_count = sum([
        1 if has_budget else 0,
        1 if need_drink else 0,
        1 if "spiceLevel" in prefs else 0,
        1 if excludes else 0,
        1 if "cuisine" in prefs else 0,
        1 if cue_main else 0,
        1 if cue_variety else 0,
        1 if cue_light else 0,
    ])
    only_budget = has_budget and constraint_count == 1

    # 改進權重計算：考慮「不要飲料」的負面權重
    weights = {
        "price": 1.0 if only_budget else (0.8 if has_budget else 0.3),
        "main": 0.8 if cue_main else 0.5,
        "variety": 0.8 if cue_variety else 0.4,
        "drink": (0.6 if need_drink else -0.8),  # 不要飲料給更大的負權重
        "spice": 0.7 if prefs.get("spiceLevel") == "不辣" or cue_light else 0.2,
        "category": 0.5,  # 類別基本權重
        "cuisine": 0.6 if "cuisine" in prefs else 0.0,
    }
    prefs["weights"] = weights
    
    # 🔄 合併 LLM 提取的結果（LLM 結果優先）
    for key, value in llm_prefs.items():
        if key not in prefs or prefs[key] is None:
            prefs[key] = value
        # 如果 LLM 有值且更具體，覆蓋關鍵字結果
        elif key == "preferredDish" and value:
            prefs[key] = value
    
    print(f" [最終偏好] LLM:{llm_prefs} + 關鍵字 = {prefs}")
    return prefs


def merge_prefs_inplace(base: Preferences, delta: Preferences) -> None:
    if "budget" in delta and delta["budget"] is not None:
        base["budget"] = delta["budget"]
    if "people" in delta:
        base["people"] = delta["people"]
    if "spiceLevel" in delta:
        base["spiceLevel"] = delta["spiceLevel"]
    if "cuisine" in delta:
        base["cuisine"] = delta["cuisine"]
    if "needDrink" in delta:
        base["needDrink"] = delta["needDrink"]  # True 或 False 都接受
    if "excludes" in delta:
        base["excludes"] = list(dict.fromkeys([*base.get("excludes", []), *delta["excludes"]]))  # 去重合併
    if "weights" in delta:
        base["weights"] = delta["weights"]  # 每輪依新輸入動態重算
    if "notes" in delta:
        base["notes"] = delta["notes"]
    # 合併菜品偏好
    if "preferredDish" in delta:
        base["preferredDish"] = delta["preferredDish"]
        print(f" [DEBUG merge_prefs] 更新菜品偏好: {delta['preferredDish']}")

def _fallback_format(rec: Dict[str, object]) -> str:
    """備用模板（LLM 失敗時使用）—— 原 format_recommend_text 邏輯完整保留。"""
    """將推薦結果整理成 Gemini 風格：有段落、理由、預算計算。"""

    items = rec.get("items") if isinstance(rec, dict) else None
    if not isinstance(items, list) or not items:
        return "目前沒有很適合的選項，可以再跟我說說預算、忌口或想吃的風格，我幫你重新搭配。"

    meta = rec.get("meta") if isinstance(rec, dict) else {}
    people = meta.get("people") if isinstance(meta, dict) else None
    budget = meta.get("budget") if isinstance(meta, dict) else None
    need_drink = meta.get("needDrink") if isinstance(meta, dict) else False

    def combo_name() -> str:
        tags: List[str] = []
        if isinstance(people, int) and people >= 5:
            tags.append("多人聚餐")
        elif isinstance(people, int) and people == 2:
            tags.append("雙人小酌")
        elif isinstance(people, int) and people == 3:
            tags.append("三人分享")
        if isinstance(budget, (int, float)):
            if budget <= 2000:
                tags.append("精省")
            elif budget >= 4000:
                tags.append("豪華")
        if need_drink:
            tags.append("含飲料")
        tags.append("暖心組合")
        return "·".join(tags)

    def classify_section(item: Dict[str, object]) -> str:
        return str(item.get("type") or "main")

    def price_text(item: Dict[str, object]) -> Tuple[str, float]:
        price = item.get("price")
        effective = item.get("effectivePrice")
        if isinstance(price, (int, float)):
            label = f"約 $ {price:.0f}"
            fallback = float(price)
        else:
            fallback = float(effective) if isinstance(effective, (int, float)) else 350.0
            label = "價格為時價，可現場再確認"
        return label, fallback

    def enrich_reason(item: Dict[str, object]) -> str:
        base = (item.get("reason") or "符合你的條件").strip()
        itype = classify_section(item)
        extra = ""
        if itype == "drink":
            extra = "，一起暢飲解膩"
        elif itype == "veggie":
            extra = "，補充青菜更清爽"
        elif itype == "core":
            extra = "，當聚餐主角最適合"
        return base + extra

    sections = {
        "core": {
            "title": "🥘 核心主鍋 / 主菜",
            "items": [],
        },
        "main": {
            "title": "🍽️ 分享菜",
            "items": [],
        },
        "veggie": {
            "title": "🥬 時蔬解膩",
            "items": [],
        },
        "drink": {
            "title": "🍹 飲品",
            "items": [],
        },
        "sweet": {
            "title": "🍧 甜點 / 收尾",
            "items": [],
        },
    }

    subtotal = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        label, numeric = price_text(item)
        subtotal += numeric
        entry = {
            "name": item.get("name") or "菜品",
            "category": item.get("category") or "菜色",
            "price_label": label,
            "reason": enrich_reason(item),
        }
        section_key = classify_section(item)
        sections.setdefault(section_key, {"title": "其他", "items": []})["items"].append(entry)

    service_fee = round(subtotal * 0.1, 1)
    total = subtotal + service_fee

    lines: List[str] = []

    intro_bits: List[str] = []
    if isinstance(people, int):
        intro_bits.append(f"{people} 位用餐")
    intro_bits.append("不辣" if any("不辣" in str((item.get("reason") or "")) for item in items) else "口味依偏好")
    if need_drink:
        intro_bits.append("含飲料")
    if isinstance(budget, (int, float)):
        intro_bits.append(f"預算 ≤ ${int(budget)}")
    intro = "、".join(intro_bits) if intro_bits else "需求已更新"
    lines.append(f"收到！{intro}。")
    lines.append(f"我幫你排出 **{combo_name()}**，每一道都有簡單理由：")

    order = ["core", "main", "veggie", "drink", "sweet"]
    for key in order:
        sec = sections.get(key)
        if not sec or not sec["items"]:
            continue
        lines.append("")
        lines.append(sec["title"])
        for entry in sec["items"]:
            lines.append(
                f"- 【{entry['name']}】（{entry['category']}）{entry['price_label']} —— {entry['reason']}"
            )

    lines.append("")
    lines.append(" 預算試算")
    lines.append(f"餐點小計：約 $ {subtotal:.0f}")
    lines.append(f"10% 服務費：約 $ {service_fee:.0f}")
    lines.append(f"總計：約 $ {total:.0f}")
    if isinstance(budget, (int, float)):
        diff = float(budget) - total
        if diff >= 0:
            lines.append(f"離預算還有約 $ {diff:.0f} 的緩衝，可再加點白飯或甜點。")
        else:
            lines.append(f"目前約超出預算 $ {abs(diff):.0f}，可視需求刪減或換成更平價的菜。")

    # 移除小提醒訊息
    # lines.append("")
    # lines.append(" 小提醒：如果想調整份量或菜色方向，直接跟我說，例如加海鮮、換辣味、或再多一壺飲料。")

    lines.append("\n這組合可以嗎？需要我再微調或換一套不同風格的嗎？")

    return "\n".join(lines)


# ──────────────────────────────────────────────────
#  LLM 二次生成回覆
# ──────────────────────────────────────────────────

def _build_recommendation_prompt(rec: Dict[str, object], user_input: str) -> str:
    """把推薦 JSON + 用戶輸入 → 適合丟給 LLM 的 Prompt 字串。

    原理：LLM 的輸出品質 80% 取決於 Prompt 設計。
    好的 Prompt 要有：角色設定、結構化資料、明確格式指示、字數限制。
    """
    items   = rec.get("items") if isinstance(rec, dict) else []
    meta    = rec.get("meta")  if isinstance(rec, dict) else {}
    if not isinstance(items, list): items = []
    if not isinstance(meta,  dict): meta  = {}

    budget     = meta.get("budget")
    people     = meta.get("people")
    need_drink = meta.get("needDrink", False)

    # 計算總價（含 10% 服務費）
    subtotal = sum(
        float(it.get("price") or 0)
        for it in items
        if isinstance(it, dict) and it.get("price") is not None
    )
    service = round(subtotal * 0.1, 1)
    total   = subtotal + service

    items_json = json.dumps(items, ensure_ascii=False, indent=2)

    return f"""你是熟悉餐廳菜單的真人點餐顧問。請根據使用者需求與候選餐點，用自然、像朋友或店員建議的方式回答。

使用者原始需求：
{user_input}

候選餐點 JSON：
{items_json}

目前估算：
- 人數：{people or "未指定"}
- 預算：{f"NT${int(budget)}" if budget else "未指定"}
- 是否需要飲料：{"需要" if need_drink else "未特別需要"}
- 小計：NT${subtotal:.0f}
- 服務費估算：NT${service:.0f}
- 合計估算：NT${total:.0f}

注意事項：
- 清單中部分項目可能是「加料」（價格明顯低於其他主餐），請優先推薦主餐，加料視情況補充建議。

回答要求：
- 不要用固定模板、表格、制式標題或「以下是推薦」這種 AI 感開場。
- 用 1 到 3 段自然中文回答，像真的在幫朋友點餐。
- 可以保留少量品項名稱與價格，但不要把資料機械列出。
- 先講最推薦怎麼點，再自然補充為什麼適合他的預算、口味或人數。
- 如果有預算，請自然提到大概會不會超出。
- 如果資料不足或價格缺失，要誠實說明，不要編造。
"""

    return f"""你是一位親切的台灣中文點餐助理。請根據以下推薦清單，用自然、有溫度的繁體中文回覆使用者。

【使用者需求】
{user_input}

【推薦清單（結構化資料）】
{items_json}

【預算資訊】
- 人數：{people or "未指定"}
- 預算：{f"NT${int(budget)}" if budget else "未指定"}
- 需要飲料：{"是" if need_drink else "否"}
- 餐點小計：約 NT${subtotal:.0f}
- 10% 服務費：約 NT${service:.0f}
- 總計：約 NT${total:.0f}

【回覆要求】
1. 開場要有溫度，自然呼應使用者的需求，不要複製貼上需求文字
2. 逐一介紹推薦菜品，說明為什麼這道適合（別只複製 reason 欄位的字）
3. 最後加一段「預算試算」，數字要跟上面一致
4. 結尾自然詢問是否需要調整，不要用制式的「如有需要請告知」
5. 全程繁體中文，語氣像真人朋友，不要條列太多符號
6. 控制在 300 字以內
"""


def generate_ai_reply(
    rec: Dict[str, object],
    user_input: str,
    model: Optional[str] = None,
    timeout: float = 180.0,
) -> str:
    """呼叫 Gemma3 把推薦 JSON 轉成自然語言回覆。

    原理：這是「同步」函數，因為 ollama_fuc.chat() 底層
    是同步呼叫 ollama CLI subprocess。
    LLM 失敗（超時、模型不存在等）時自動降級到 _fallback_format，
    確保服務不中斷。
    """
    from ollama_fuc import chat as _ollama_chat

    mdl    = model or DEFAULT_MODEL
    prompt = _build_recommendation_prompt(rec, user_input)

    try:
        response = _ollama_chat(
            [{"role": "user", "content": prompt}],
            model=mdl,
            timeout=timeout,
        )
        cleaned = response.strip() if isinstance(response, str) else ""
        if cleaned:
            return cleaned
        print(" [generate_ai_reply] LLM 返回空回覆，降級使用模板")
        return _fallback_format(rec)
    except Exception as e:
        print(f" [generate_ai_reply] 錯誤: {e}，降級使用模板")
        return _fallback_format(rec)


# 向後相容：舊名稱保留為 alias，避免其他地方呼叫出錯
format_recommend_text = _fallback_format


# 既有骨架占位
def menu_to_json():
    # 從自由文字解析 補上文字->JSON 的parser
    return


conversation_history: List[ConversationTurn] = []  # 對話歷史



conversation_history: List[ConversationTurn] = []  # 對話歷史

def generate_conversation(
    history: List[ConversationTurn],
    user_input: str,
    menu: Menu,
    prefs: Preferences,
    model: Optional[str] = None,
) -> Tuple[str, List[ConversationTurn]]:
    history.append({"role": "user", "content": user_input, "meta": {}})

    # 抽取→就地合併（保留上一輪條件）
    dynamic = extract_prefs_from_text(user_input)
    dynamic.setdefault("notes", user_input)
    merge_prefs_inplace(prefs, dynamic)

    # 直接推薦（用累積後的 prefs）
    try:
        if ollama_recommend is None:
            raise RuntimeError("推薦功能未載入")
           # reply="123" #///////////////////////////////////
        rec = ollama_recommend(menu, prefs, top_k=5, model=model)
        reply = generate_ai_reply(rec, user_input)
    except Exception as e:
        reply = f"推薦發生錯誤：{e}"

    history.append({"role": "assistant", "content": reply, "meta": {}})
    return reply, history




def _validate_menu(menu: Menu) -> None:
    if not isinstance(menu, dict) or 'categories' not in menu or not isinstance(menu['categories'], list):
        raise ValueError('menu.json 結構不正確：缺少 categories 或型別錯誤')
    for cat in menu['categories']:
        if not isinstance(cat, dict) or 'name' not in cat or 'items' not in cat:
            raise ValueError('menu.json 結構不正確：Category 需包含 name 與 items')
        if not isinstance(cat['items'], list):
            raise ValueError('menu.json 結構不正確：items 應為陣列')


def main():

    if callable(globals().get("ollama_ensure_daemon", None)):
        try:
            ollama_ensure_daemon()  # type: ignore
        except Exception:
            pass

    """讀取並驗證 utils/menu.json，套用正規化規則，並提供自然語言對話。"""
    base_dir = os.path.dirname(__file__)
    json_path = os.path.join(base_dir, 'menu.json')

    if not os.path.exists(json_path):
        print(f"找不到 menu.json：{json_path}\n請直接建立或編輯此檔案以管理菜單資料。")
        return

    with open(json_path, 'r', encoding='utf-8') as f:
        menu: Menu = json.load(f)

    _validate_menu(menu)

    stats = normalize_menu(menu)
    if stats["market_price_tagged"] > 0 or stats["removed_salt_tags"] > 0:
        write_menu_json(menu, json_path)

    total_items = sum(len(c['items']) for c in menu['categories'])
    print(f"讀取 JSON: {json_path}")
    print(f"分類數: {len(menu['categories'])}，品項數: {total_items}")
    if stats["market_price_tagged"] > 0 or stats["removed_salt_tags"] > 0:
        print(f"已正規化：新增『時價』標籤 {stats['market_price_tagged']} 筆，移除飲品『鹹度N』標籤 {stats['removed_salt_tags']} 筆。")

    # 自然語言 REPL 
    prefs: Preferences = {}  # 作為 session 記憶，會被持續更新
    print("歡迎使用點餐推薦服務！")
    print(f"請問有什麼需求？（例如：預算 300、不辣、不要花生，要有飲料）")
    print(f"輸入 exit 離開。")
    print(f"輸入 reset/清除記憶 重置偏好。")
    while True:
        try:
            text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye")
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            print("Bye")
            break
        if text.lower() in ("reset",) or text in ("清除記憶", "清空", "重置", "重來"):
            conversation_history.clear()
            prefs.clear()
            print("已重置偏好與對話。")
            continue

        reply, _ = generate_conversation(conversation_history, text, menu, prefs)
        print(f"\n>> {reply}")


if __name__ == "__main__":
    main()

