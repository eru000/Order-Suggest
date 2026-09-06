#import
from __future__ import annotations
import os, json, re, shutil, subprocess, random, time
from typing import Dict, List, Optional, TypedDict, Literal, Tuple, cast

from preference_engine import merge_preference_delta, parse_preferences
from observability import emit


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
    preferredDish: str
    spiceProfile: Dict[str, object]
    allergens: List[str]
    dietaryRestrictions: List[str]
    budgetBasis: str
    _operations: List[Dict[str, object]]


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
    """Parse one turn into validated values plus explicit merge operations."""
    llm_extractor = None
    if os.environ.get("USE_LLM_EXTRACTION", "false").lower() == "true":
        llm_extractor = extract_prefs_with_llm
    return cast(Preferences, parse_preferences(text, llm_extractor=llm_extractor))


def merge_prefs_inplace(base: Preferences, delta: Preferences) -> None:
    merge_preference_delta(base, delta)

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

_MENU_PROMPT_MAX_ITEMS = 150


def _format_menu_for_prompt(menu: Optional[Dict[str, object]]) -> str:
    """把整份菜單壓成 prompt 用的分類清單。

    沒有這段的話，LLM 只看得到 recommend() 挑出來的 5 項候選，被問到「有沒有
    飯類」時就會照它看到的東西回答「這家沒有飯」——但菜單上其實有。
    """
    if not isinstance(menu, dict):
        return ""
    try:
        from recommendation import _flatten_menu
    except Exception:
        return ""
    rows = _flatten_menu(menu)
    if not rows:
        return ""
    truncated = len(rows) > _MENU_PROMPT_MAX_ITEMS
    grouped: Dict[str, List[str]] = {}
    for row in rows[:_MENU_PROMPT_MAX_ITEMS]:
        price = row.get("price")
        label = f"{row['name']} ${price:.0f}" if isinstance(price, (int, float)) else f"{row['name']}（價格未標示）"
        grouped.setdefault(str(row.get("category") or "未分類"), []).append(label)
    lines = [f"完整菜單（這家店共 {len(rows)} 項）："]
    for category, labels in grouped.items():
        lines.append(f"[{category}] " + "、".join(labels))
    if truncated:
        lines.append(f"（品項太多，只列出前 {_MENU_PROMPT_MAX_ITEMS} 項）")
    return "\n".join(lines)


def _reply_temperature() -> float:
    """推薦回覆的取樣溫度。

    API_TEMPERATURE 預設 0.7 是給一般對話用的，對「照著給定的菜單資料講人話」
    這種任務偏高——溫度越高越容易冒出菜單上沒有的菜名與價格。這裡預設 0.4：
    夠自然但不會亂編。要調整就設 REPLY_TEMPERATURE。
    """
    raw = os.getenv("REPLY_TEMPERATURE", "").strip()
    if not raw:
        return 0.4
    try:
        return max(0.0, min(2.0, float(raw)))
    except ValueError:
        return 0.4


_REPLY_SYSTEM_PROMPT = """你是熟悉餐廳菜單的真人點餐顧問，講話像朋友或店員，不像客服機器人。

輸出規則（每一條都要遵守）：
- 全程使用繁體中文（台灣用語）。不可以出現簡體字。
- 控制在 300 字以內。
- 用 1 到 3 段自然的話回答，不要條列、不要表格、不要制式標題。
- 不要用「以下是推薦」「希望對您有幫助」「如有需要請告知」這類 AI 感的套語。
- 可以提到少量品名與價格，但不要把資料機械地列出來。
- 只能講資料裡有的東西。價格缺漏就誠實說不清楚，絕對不要編造菜名或數字。"""


def _build_recommendation_prompt(
    rec: Dict[str, object],
    user_input: str,
    menu: Optional[Dict[str, object]] = None,
) -> List[Dict[str, str]]:
    """把推薦 JSON + 用戶輸入組成要送給 LLM 的 messages。

    拆成 system + user 兩則而不是塞成一大段 user，是因為完整菜單最多 150 項，
    以前那些「回答要求」全排在那坨資料後面，模型很常讀完資料就忘了規則——
    實測會吐出簡體字、寫得又臭又長。規則搬到 system 之後就穩定多了。

    「全程繁體中文」與「300 字以內」這兩條原本寫在一段永遠執行不到的
    第二個 return 裡，等於重構時被無聲刪掉。現在回到 system 訊息。
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

    # 推薦器的 notes 以前沒進過 prompt，等於「成分無法確認」這類提醒寫了也沒人看得到。
    rec_notes = str(rec.get("notes") or "").strip() if isinstance(rec, dict) else ""
    notes_block = f"\n必須轉達給使用者的提醒：\n{rec_notes}\n" if rec_notes else ""

    items_json = json.dumps(items, ensure_ascii=False, indent=2)
    menu_text = _format_menu_for_prompt(menu)
    menu_block = f"\n{menu_text}\n" if menu_text else ""

    user_content = f"""使用者這次說：
{user_input}

系統依條件挑出的候選餐點（**不是**這家店的全部品項）：
{items_json}
{menu_block}
目前估算：
- 人數：{people or "未指定"}
- 預算：{f"NT${int(budget)}" if budget else "未指定"}
- 是否需要飲料：{"需要" if need_drink else "未特別需要"}
- 小計：NT${subtotal:.0f}
- 服務費估算：NT${service:.0f}
- 合計估算：NT${total:.0f}
{notes_block}
判斷時注意：
- 出現「必須轉達給使用者的提醒」時，一定要在回覆裡講出來，不可以省略。
- 候選清單裡部分項目可能是「加料」（價格明顯低於其他主餐），請優先講主餐。
- 使用者問「有沒有某類餐點」時，一律看「完整菜單」再回答。候選清單裡沒有不代表
  店裡沒有——除非完整菜單裡真的找不到，否則絕對不要說「這家店沒有 XX」。
- 完整菜單裡若有更符合他這次需求的品項，可以直接改推薦那一項。
- 先講最推薦怎麼點，再自然補充為什麼適合他的預算、口味或人數。
- 有預算的話，自然提一下大概會不會超出。"""

    return [
        {"role": "system", "content": _REPLY_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def generate_ai_reply(
    rec: Dict[str, object],
    user_input: str,
    model: Optional[str] = None,
    timeout: float = 180.0,
    menu: Optional[Dict[str, object]] = None,
) -> str:
    """呼叫 Gemma3 把推薦 JSON 轉成自然語言回覆。

    原理：這是「同步」函數，因為 ollama_fuc.chat() 底層
    是同步呼叫 ollama CLI subprocess。
    LLM 失敗（超時、模型不存在等）時自動降級到 _fallback_format，
    確保服務不中斷。
    """
    from ollama_fuc import chat as _ollama_chat

    mdl      = model or DEFAULT_MODEL
    messages = _build_recommendation_prompt(rec, user_input, menu)

    try:
        response = _ollama_chat(
            messages,
            model=mdl,
            timeout=timeout,
            temperature=_reply_temperature(),
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
conversation_history: List[ConversationTurn] = []  # 對話歷史


def prepare_recommendation(
    history: List[ConversationTurn],
    user_input: str,
    menu: Menu,
    prefs: Preferences,
    model: Optional[str] = None,
) -> Dict[str, object]:
    """Apply one preference delta and calculate one deterministic recommendation."""
    started = time.perf_counter()
    history.append({"role": "user", "content": user_input, "meta": {}})
    dynamic = extract_prefs_from_text(user_input)
    dynamic.setdefault("notes", user_input)
    merge_prefs_inplace(prefs, dynamic)
    if ollama_recommend is None:
        raise RuntimeError("推薦功能未載入")
    # 固定 5 項對一個人剛好，對四個人就太少——會出現「六百塊預算只點兩百」。
    people = prefs.get("people")
    top_k = max(5, min(8, people * 2)) if isinstance(people, int) and people > 0 else 5
    result = ollama_recommend(menu, prefs, top_k=top_k, model=model)
    meta = result.get("meta") if isinstance(result, dict) else {}
    estimated_total = meta.get("estimatedTotal") if isinstance(meta, dict) else None
    budget = meta.get("budget") if isinstance(meta, dict) else None
    emit(
        "recommendation.completed",
        durationMs=round((time.perf_counter() - started) * 1000, 2),
        itemCount=len(result.get("items") or []),
        noCandidates=not bool(result.get("items")),
        budgetViolation=bool(
            isinstance(budget, (int, float))
            and isinstance(estimated_total, (int, float))
            and estimated_total > budget
        ),
        semanticPreferences=bool(prefs.get("spiceProfile") or prefs.get("allergens")),
    )
    return result

def generate_conversation(
    history: List[ConversationTurn],
    user_input: str,
    menu: Menu,
    prefs: Preferences,
    model: Optional[str] = None,
) -> Tuple[str, List[ConversationTurn]]:
    try:
        rec = prepare_recommendation(history, user_input, menu, prefs, model)
        reply = generate_ai_reply(rec, user_input, menu=menu)
    except Exception as e:
        reply = f"推薦發生錯誤：{e}"

    history.append({"role": "assistant", "content": reply, "meta": {}})
    return reply, history


def generate_ai_reply_stream(
    rec: Dict[str, object],
    user_input: str,
    model: Optional[str] = None,
    timeout: float = 180.0,
    menu: Optional[Dict[str, object]] = None,
):
    """串流版 generate_ai_reply()。

    降級策略跟非串流版不同：一旦已經吐出任何文字，就不能再改用模板補救，
    否則畫面上會出現「半段 AI 回覆 + 一整段模板」的重複內容。
    所以只有在「一個字都還沒吐出來」時才降級。
    """
    from ollama_fuc import chat_stream as _ollama_chat_stream

    mdl = model or DEFAULT_MODEL
    messages = _build_recommendation_prompt(rec, user_input, menu)
    produced = False

    try:
        for piece in _ollama_chat_stream(
            messages, model=mdl, timeout=timeout, temperature=_reply_temperature()
        ):
            if piece:
                produced = True
                yield piece
    except Exception as e:
        print(f" [generate_ai_reply_stream] 錯誤: {e}"
              f"{'，已輸出部分內容，不再降級' if produced else '，降級使用模板'}")
        if not produced:
            yield _fallback_format(rec)
        return

    if not produced:
        print(" [generate_ai_reply_stream] LLM 返回空回覆，降級使用模板")
        yield _fallback_format(rec)


def generate_conversation_stream(
    history: List[ConversationTurn],
    user_input: str,
    menu: Menu,
    prefs: Preferences,
    model: Optional[str] = None,
):
    """與 generate_conversation() 相同的流程，但拆成多個事件逐步吐出。

    事件順序刻意設計成「先資料、後文字」：

      1. {"type": "recommendation"} —— recommend() 是純本機計算，實測 0.0 秒，
         所以推薦品項與價格可以立刻送到畫面上。
      2. {"type": "delta"} —— AI 的說明文字，逐段補上。

    這個順序很重要。實測 LLM 的等待幾乎全部落在「第一個字出現之前」
    （12-42 秒，且與 prompt 長度無關，是遠端排隊），生成本身只要 1.4-2.3 秒。
    所以單純把回覆改成串流幾乎沒有幫助——真正有感的是先把已經算好的推薦
    結果送出去，讓使用者一秒內就看到答案，再等 AI 補說明。

    history 會在串流結束後才寫入完整回覆——中途中斷的半截內容不該被當成
    有效的對話紀錄帶進下一輪。
    """
    try:
        rec = prepare_recommendation(history, user_input, menu, prefs, model)
    except Exception as e:
        text = f"推薦發生錯誤：{e}"
        history.append({"role": "assistant", "content": text, "meta": {}})
        yield {"type": "delta", "text": text}
        return

    items = [i for i in (rec.get("items") or []) if isinstance(i, dict)]
    subtotal = sum(
        float(i.get("price") or 0)
        for i in items
        if isinstance(i.get("price"), (int, float))
    )
    yield {
        "type": "recommendation",
        "items": items,
        "meta": rec.get("meta") or {},
        "subtotal": subtotal,
    }

    pieces: List[str] = []
    for piece in generate_ai_reply_stream(rec, user_input, model=model, menu=menu):
        pieces.append(piece)
        yield {"type": "delta", "text": piece}

    history.append({"role": "assistant", "content": "".join(pieces), "meta": {}})




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

