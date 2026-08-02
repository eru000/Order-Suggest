import os, sys, json, re, threading, time, uuid
from typing import Any, Dict, List, Optional
import base64
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio

# 確保可以從 src/ 匯入模組
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# 確保可以從根目錄匯入模組
PROJECT_ROOT = os.path.abspath(os.path.join(SRC_DIR, os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 從主程式匯入
from main import (
    Menu, Preferences, ConversationTurn,
    _validate_menu, normalize_menu, write_menu_json,
    generate_conversation,
    generate_conversation_stream,
)

from restaurant_reviews import load_review_cache, refresh_restaurant_reviews

# VLM 菜單辨識（分塊辨識 + 交叉校對 + 人工確認）
from menu_vision import (
    MAX_IMAGE_BYTES,
    analyze_menu_image,
    apply_manual_correction,
    to_persisted_document,
)

# 專案路徑設定（PROJECT_ROOT 已在上方第16行定義）
WEB_DIR = os.path.join(PROJECT_ROOT, "web")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")

BASE_DIR = PROJECT_ROOT  # 舊變數名稱向下相容

# 啟動時顯示路徑資訊
print(f"\n{'='*60}")
print(f"[路徑設定]")
print(f"{'='*60}")
print(f"SRC_DIR      = {SRC_DIR}")
print(f"PROJECT_ROOT = {PROJECT_ROOT}")
print(f"WEB_DIR      = {WEB_DIR}")
print(f"LOG_DIR      = {LOG_DIR}")
print(f"當前工作目錄  = {os.getcwd()}")
print(f"{'='*60}\n")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
# 提供 /web/* 靜態檔案
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

# 載入菜單
MENU_PATHS = [
    os.path.join(PROJECT_ROOT, "db", "menu.json"),
    os.path.join(PROJECT_ROOT, "menu.json"),
]
MENU_PATH = None
for p in MENU_PATHS:
    if os.path.exists(p):
        MENU_PATH = p
        break

menu: Menu
if MENU_PATH is None:
    # 雲端部署時若沒有帶 menu.json，不要讓整個服務直接掛掉。
    # 仍可啟動前端與 /health，並提示使用者缺菜單資料。
    print(f"[WARN] 找不到菜單檔案 (menu.json)。已嘗試的路徑: {MENU_PATHS}")
    menu = {"categories": []}
else:
    try:
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            menu = json.load(f)  # 把JSON讀成Python物件並存到menu
    except Exception as e:
        raise RuntimeError(f"載入菜單檔案失敗: {MENU_PATH} -> {e}")

    _validate_menu(menu)
    stats = normalize_menu(menu)
    if stats.get("market_price_tagged", 0) > 0 or stats.get("removed_salt_tags", 0) > 0:
        write_menu_json(menu, MENU_PATH)
        # 覆寫後重新讀一次，確保記憶體與檔案一致
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            menu = json.load(f)
        _validate_menu(menu)

# 啟動時自動載入最近爬取的菜單
def load_latest_crawled_menu() -> Optional[Menu]:
    """檢查是否有最近爬取的菜單檔案，並自動載入"""
    import glob
    
    # 尋找所有 menu_*.json 檔案
    menu_files = glob.glob(os.path.join(PROJECT_ROOT, "menu_*.json"))
    
    if not menu_files:
        return None
    
    # 找到最新的檔案（依修改時間）
    latest_file = max(menu_files, key=os.path.getmtime)
    
    try:
        with open(latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # 提取餐廳名稱
        filename = os.path.basename(latest_file)
        restaurant_name = filename.replace("menu_", "").replace(".json", "")
        
        # 轉換為系統菜單格式
        if "menu_items" in data and isinstance(data["menu_items"], list):
            crawled_menu: Menu = {
                "restaurants": {
                    restaurant_name: {
                        "name": data.get('name', restaurant_name),
                        "categories": {
                            "全部菜色": {
                                "items": [
                                    {
                                        "name": item.get('name', ''),
                                        "price": item.get('price', '價格未提供').replace('$', '').replace(',', '').strip() if isinstance(item.get('price'), str) else item.get('price')
                                    }
                                    for item in data.get('menu_items', [])
                                ]
                            }
                        }
                    }
                }
            }
            print(f" 自動載入最近爬取的菜單：{restaurant_name} ({len(data.get('menu_items', []))} 項)")
            return crawled_menu
    except Exception as e:
        print(f" 載入爬取菜單失敗：{e}")
    
    return None

# 多餐廳支援：使用字典管理所有餐廳菜單
RESTAURANT_MENUS: Dict[str, Menu] = {}
ACTIVE_RESTAURANT: Optional[str] = None

DEFAULT_RESTAURANT_NAME = os.getenv("DEFAULT_RESTAURANT_NAME", "預設餐廳")

# 1️ 先載入預設菜單 (menu.json)
if MENU_PATH and os.path.exists(MENU_PATH):
    try:
        with open(MENU_PATH, "r", encoding="utf-8") as f:
            default_menu_data = json.load(f)
        
        # 判斷是舊格式還是新格式
        if "categories" in default_menu_data:
            # 舊格式：轉換成新格式
            RESTAURANT_MENUS[DEFAULT_RESTAURANT_NAME] = {
                "restaurants": {
                    DEFAULT_RESTAURANT_NAME: {
                        "name": DEFAULT_RESTAURANT_NAME,
                        "categories": {
                            cat["name"]: {
                                "items": cat.get("items", [])
                            }
                            for cat in default_menu_data.get("categories", [])
                        }
                    }
                }
            }
            print(f" 載入預設餐廳：{DEFAULT_RESTAURANT_NAME}")
        elif "restaurants" in default_menu_data:
            # 新格式：直接使用
            for rest_name in default_menu_data["restaurants"]:
                RESTAURANT_MENUS[rest_name] = default_menu_data
                print(f" 載入餐廳：{rest_name}")
    except Exception as e:
        print(f" 載入預設菜單失敗：{e}")

# 2️⃣ 載入所有爬取的菜單 (menu_*.json)
import glob
menu_files = glob.glob(os.path.join(PROJECT_ROOT, "menu_*.json"))
for menu_file in menu_files:
    try:
        with open(menu_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        filename = os.path.basename(menu_file)
        restaurant_name = filename.replace("menu_", "").replace(".json", "")
        
        if "menu_items" in data and isinstance(data["menu_items"], list):
            crawled_menu: Menu = {
                "restaurants": {
                    restaurant_name: {
                        "name": data.get('name', restaurant_name),
                        "categories": {
                            "全部菜色": {
                                "items": [
                                    {
                                        "name": item.get('name', ''),
                                        "price": item.get('price', '價格未提供').replace('$', '').replace(',', '').strip() if isinstance(item.get('price'), str) else item.get('price')
                                    }
                                    for item in data.get('menu_items', [])
                                ]
                            }
                        }
                    }
                }
            }
            RESTAURANT_MENUS[restaurant_name] = crawled_menu
            print(f" 載入餐廳菜單：{restaurant_name} ({len(data.get('menu_items', []))} 項)")
    except Exception as e:
        print(f" 載入 {menu_file} 失敗：{e}")

# 設定預設活動餐廳（最新修改的）
if RESTAURANT_MENUS and menu_files:  # 確保 menu_files 不是空列表
    latest_file = max(menu_files, key=os.path.getmtime)
    latest_name = os.path.basename(latest_file).replace("menu_", "").replace(".json", "")
    ACTIVE_RESTAURANT = latest_name
    menu = RESTAURANT_MENUS[latest_name]
    print(f" 當前活動餐廳：{ACTIVE_RESTAURANT}")
else:
    # 使用預設 menu
    if MENU_PATH and os.path.exists(MENU_PATH):
        ACTIVE_RESTAURANT = DEFAULT_RESTAURANT_NAME
        RESTAURANT_MENUS[DEFAULT_RESTAURANT_NAME] = menu

# 簡單 session 記憶
SESSIONS: Dict[str, Dict[str, object]] = {}

# 待確認的 VLM 辨識結果。辨識完不直接落地，等使用者確認過才寫入菜單，
# 所以需要一個有時效的暫存區。
PENDING_ANALYSES: Dict[str, Dict[str, object]] = {}
PENDING_ANALYSIS_LOCK = threading.Lock()
PENDING_ANALYSIS_TTL_SECONDS = 15 * 60


def _safe_menu_path(restaurant_name: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", restaurant_name).strip(" ._")
    safe_name = re.sub(r"\s+", "_", safe_name)[:100]
    if not safe_name:
        raise ValueError("餐廳名稱無法作為檔名")
    return os.path.join(PROJECT_ROOT, f"menu_{safe_name}.json")


def _prune_pending_analyses(now: Optional[float] = None) -> None:
    current = now if now is not None else time.time()
    expired = [
        analysis_id for analysis_id, entry in PENDING_ANALYSES.items()
        if float(entry.get("expires_at") or 0) <= current
    ]
    for analysis_id in expired:
        PENDING_ANALYSES.pop(analysis_id, None)


def _store_pending_analysis(result: Dict[str, Any]) -> str:
    analysis_id = uuid.uuid4().hex
    now = time.time()
    with PENDING_ANALYSIS_LOCK:
        _prune_pending_analyses(now)
        PENDING_ANALYSES[analysis_id] = {
            "result": result,
            "created_at": now,
            "expires_at": now + PENDING_ANALYSIS_TTL_SECONDS,
        }
    return analysis_id


def _take_pending_analysis(analysis_id: str) -> Dict[str, Any]:
    with PENDING_ANALYSIS_LOCK:
        _prune_pending_analyses()
        entry = PENDING_ANALYSES.pop(analysis_id, None)
    if not entry or not isinstance(entry.get("result"), dict):
        raise ValueError("辨識結果不存在或已超過 15 分鐘，請重新上傳照片")
    return entry["result"]  # type: ignore[return-value]


def _correct_pending_analysis(analysis_id: str, instruction: str) -> Dict[str, Any]:
    with PENDING_ANALYSIS_LOCK:
        _prune_pending_analyses()
        entry = PENDING_ANALYSES.get(analysis_id)
        if not entry or not isinstance(entry.get("result"), dict):
            raise ValueError("辨識結果不存在或已超過 15 分鐘，請重新上傳照片")
        correction = apply_manual_correction(entry["result"], instruction)  # type: ignore[arg-type]
        entry["expires_at"] = time.time() + PENDING_ANALYSIS_TTL_SECONDS
        return correction


def _analysis_response(result: Dict[str, Any], analysis_id: str) -> Dict[str, Any]:
    quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
    item_count = int(quality.get("itemCount") or sum(
        len(category.get("items", [])) for category in result.get("categories", []) if isinstance(category, dict)
    ))
    return {
        "success": True,
        "analysisId": analysis_id,
        "expiresInSeconds": PENDING_ANALYSIS_TTL_SECONDS,
        "restaurantNameCandidate": result.get("detected_restaurant_name") or result.get("restaurant_name") or "",
        "requestedRestaurantName": (result.get("identity") or {}).get("userHint", "")
        if isinstance(result.get("identity"), dict) else "",
        "itemCount": item_count,
        "categories": result.get("categories", []),
        "sourceType": result.get("source_type"),
        "quality": quality,
        "priceCoverage": quality.get("priceCoverage", 0),
        "conflicts": result.get("conflicts", []),
        "identity": result.get("identity", {}),
        "identityConflict": bool(result.get("identityConflict")),
        "needsAcceptance": bool(
            result.get("identityConflict")
            or result.get("conflicts")
            or float(quality.get("score") or 0) < 0.75
        ),
        "warnings": result.get("warnings", []),
    }


def _register_vision_menu(result: Dict[str, Any]) -> Dict[str, Any]:
    """把一份已確認的 VLM 辨識結果寫成菜單檔並設為當前餐廳。"""
    global ACTIVE_RESTAURANT, menu
    restaurant_name = str(result.get("restaurant_name") or "").strip()
    if not restaurant_name:
        raise ValueError("無法確認餐廳名稱，請先輸入餐廳名稱")

    category_map = {
        str(category.get("name") or "其他"): {"items": list(category.get("items") or [])}
        for category in result.get("categories", [])
        if isinstance(category, dict) and category.get("items")
    }
    item_count = sum(len(value["items"]) for value in category_map.values())
    if not item_count:
        raise ValueError("沒有可新增的菜單項目")

    runtime_menu: Menu = {
        "restaurants": {
            restaurant_name: {
                "name": restaurant_name,
                "categories": category_map,
            }
        }
    }
    # 先寫暫存檔再 replace，避免中途失敗留下半份菜單
    output_path = _safe_menu_path(restaurant_name)
    temporary_path = f"{output_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as file:
        json.dump(to_persisted_document(result), file, ensure_ascii=False, indent=2)
    os.replace(temporary_path, output_path)

    RESTAURANT_MENUS[restaurant_name] = runtime_menu
    ACTIVE_RESTAURANT = restaurant_name
    menu = runtime_menu
    return {"restaurantName": restaurant_name, "itemCount": item_count, "categories": list(category_map)}


def _log_chat(session_id: str, user_text: str, reply: str, prefs: Preferences) -> None:
    """將每次對話紀錄成一行 JSON 方便之後分析。

    格式：一行一筆 JSON，包含 sessionId、user_text、reply、prefs 等。
    檔案位置：專案根目錄下 logs/chat_log.jsonl
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, "chat_log.jsonl")

        record = {
            "sessionId": session_id,
            "user_text": user_text,
            "reply": reply,
            "prefs": prefs,
        }

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 日誌失敗不影響主流程
        pass

class ChatReq(BaseModel):
    sessionId: str
    text: str

class ChatResp(BaseModel):
    reply: str

class RestaurantReviewRefreshReq(BaseModel):
    restaurant_name: str

class VisionConfirmReq(BaseModel):
    restaurant_name: str = ""
    accept_conflicts: bool = False

class VisionCorrectionReq(BaseModel):
    instruction: str

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/")
def index():
    # no-cache 是「每次都先問伺服器」，不是「不准快取」。沒有這個標頭時瀏覽器
    # 會自行決定要不要快取，可能拿到舊的 web.html —— 而舊 HTML 裡寫的是舊的
    # ?v= 版本號，於是又去載舊的 CSS，改了樣式卻看不到任何變化。
    # 有 ETag 在，重新驗證通常只回 304，成本很低。
    return FileResponse(
        os.path.join(WEB_DIR, "web.html"),
        headers={"Cache-Control": "no-cache"},
    )

@app.get("/api/current-menu")
def get_current_menu():
    """
    回傳當前活動餐廳的菜單資料
    """
    try:
        if not ACTIVE_RESTAURANT or not menu:
            return {
                "success": False,
                "message": "目前未載入任何菜單",
                "restaurantName": None,
                "categories": []
            }
        
        # 獲取當前餐廳的菜單資料
        restaurant_data = menu.get("restaurants", {}).get(ACTIVE_RESTAURANT, {})
        categories_dict = restaurant_data.get("categories", {})
        
        # 轉換成前端友善的格式（陣列）
        categories_array = []
        for category_name, category_data in categories_dict.items():
            categories_array.append({
                "name": category_name,
                "items": category_data.get("items", [])
            })
        
        return {
            "success": True,
            "restaurantName": ACTIVE_RESTAURANT,
            "categories": categories_array
        }
    
    except Exception as e:
        print(f"[錯誤] 獲取菜單失敗: {e}")
        return {
            "success": False,
            "message": f"獲取菜單時發生錯誤: {str(e)}",
            "restaurantName": None,
            "categories": []
        }


@app.post("/api/upload-menu-photo")
async def upload_menu_photo(
    file: UploadFile = File(...),
    restaurantName: str = Form("照片菜單"),
):
    global menu, RESTAURANT_MENUS, ACTIVE_RESTAURANT
    try:
        image_bytes = await file.read()
        content_type = file.content_type or "image/jpeg"
        data_url = f"data:{content_type};base64,{base64.b64encode(image_bytes).decode()}"

        from ollama_fuc import extract_menu_from_image
        data = extract_menu_from_image(data_url, restaurantName)

        items = data.get("menu_items", [])
        if not items:
            return {"success": False, "message": "VLM 無法從圖片辨識出菜品，請確認照片清晰度"}

        # VLM 辨識到餐廳名稱就用它，否則保留時間戳名稱
        finalName = data.get("restaurant_name") or restaurantName

        crawled_menu: Menu = {
            "restaurants": {
                finalName: {
                    "name": finalName,
                    "categories": {
                        "全部菜色": {
                            "items": [
                                {"name": item.get("name", ""), "price": item.get("price")}
                                for item in items
                                if item.get("name")
                            ]
                        }
                    }
                }
            }
        }
        RESTAURANT_MENUS[finalName] = crawled_menu
        ACTIVE_RESTAURANT = finalName
        menu = crawled_menu

        return {
            "success": True,
            "message": f"成功從照片辨識 {len(items)} 道菜",
            "restaurantName": finalName,
            "itemCount": len(items),
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"辨識失敗：{str(e)}"}


@app.post("/api/menu/vision")
async def create_menu_from_photo(
    restaurant_name: str = Form(default=""),
    image: UploadFile = File(...),
):
    """辨識照片，結果先進待確認區，不直接寫入菜單。"""
    image_bytes = await image.read(MAX_IMAGE_BYTES + 1)
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "圖片不可超過 10 MB")
    try:
        # 辨識是同步且會打好幾次 VLM，丟到執行緒避免卡住事件迴圈
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: analyze_menu_image(image_bytes, image.content_type or "", restaurant_name),
        )
        analysis_id = _store_pending_analysis(result)
        return _analysis_response(result, analysis_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"VLM 服務錯誤：{exc}") from exc


@app.post("/api/menu/vision/{analysis_id}/confirm")
def confirm_menu_from_photo(analysis_id: str, req: VisionConfirmReq):
    """使用者確認後才把待確認結果寫成菜單。"""
    try:
        result = _take_pending_analysis(analysis_id)
        restaurant_name = (req.restaurant_name or str(result.get("restaurant_name") or "")).strip()
        if not restaurant_name:
            restaurant_name = str(result.get("detected_restaurant_name") or "").strip()
        if not restaurant_name:
            raise ValueError("請提供餐廳名稱後再確認")
        quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
        needs_acceptance = bool(
            result.get("identityConflict")
            or result.get("conflicts")
            or float(quality.get("score") or 0) < 0.75
        )
        if needs_acceptance and not req.accept_conflicts:
            # 放回待確認區，讓使用者看過摘要後可以再送一次
            with PENDING_ANALYSIS_LOCK:
                PENDING_ANALYSES[analysis_id] = {
                    "result": result,
                    "created_at": time.time(),
                    "expires_at": time.time() + PENDING_ANALYSIS_TTL_SECONDS,
                }
            raise HTTPException(409, "店名、辨識衝突或品質需要人工檢查，請確認摘要後明確接受再送出")
        result["restaurant_name"] = restaurant_name
        summary = _register_vision_menu(result)
        return {"success": True, **summary, "quality": result.get("quality", {}), "warnings": result.get("warnings", [])}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/menu/vision/{analysis_id}/correct")
def correct_menu_from_photo(analysis_id: str, req: VisionCorrectionReq):
    """修正待確認結果，不動到已存檔的菜單。"""
    try:
        correction = _correct_pending_analysis(analysis_id, req.instruction)
        result = correction["result"]
        return {
            **_analysis_response(result, analysis_id),
            "correctionMessage": correction["message"],
            "manualCorrections": result.get("manualCorrections", []),
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/chat", response_model=ChatResp)
def api_chat(req: ChatReq):
    s = SESSIONS.setdefault(req.sessionId, {"prefs": {}, "history": []})
    prefs: Preferences = s["prefs"]  # type: ignore[assignment]
    history: List[ConversationTurn] = s["history"]  # type: ignore[assignment]
    reply, _ = generate_conversation(history, req.text, menu, prefs)

    # 寫入簡單對話日誌，方便之後分析「大家怎麼問」、「實際推薦了什麼」
    _log_chat(req.sessionId, req.text, reply, prefs)

    return {"reply": reply}


@app.post("/api/chat/stream")
def api_chat_stream(req: ChatReq):
    """SSE 串流版對話。

    先送 recommendation 事件（本機計算，約 1 秒內到），再逐段送 AI 說明文字。
    重點在第一個事件：LLM 的第一個字要等 12-42 秒（遠端排隊，與 prompt 長度
    無關），但推薦品項與價格不必等它。

    前端拿不到串流時會自動退回 /api/chat，所以這支掛掉不會讓功能不可用。
    """
    s = SESSIONS.setdefault(req.sessionId, {"prefs": {}, "history": []})
    prefs: Preferences = s["prefs"]  # type: ignore[assignment]
    history: List[ConversationTurn] = s["history"]  # type: ignore[assignment]

    def event_stream():
        pieces: List[str] = []
        try:
            for event in generate_conversation_stream(history, req.text, menu, prefs):
                if event.get("type") == "delta":
                    pieces.append(event.get("text", ""))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            print(f"[/api/chat/stream] 串流中斷: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
        else:
            _log_chat(req.sessionId, req.text, "".join(pieces), prefs)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Render、Cloudflare 之類的反向代理預設會緩衝回應，
            # 沒有這個標頭的話會整段憋到最後才送出，串流就白做了。
            "X-Accel-Buffering": "no",
        },
    )

@app.get("/api/restaurant-review")
def get_restaurant_review(restaurant_name: Optional[str] = None):
    """讀取餐廳評價快取；不觸發網路搜尋。"""
    target = restaurant_name or ACTIVE_RESTAURANT
    if not target:
        return {
            "success": False,
            "message": "尚未選擇餐廳",
            "restaurantName": None,
            "updatedAt": None,
            "overallScore": 0,
            "sentiment": "unknown",
            "summary": "",
            "pros": [],
            "cons": [],
            "recommendedFor": [],
            "riskLevel": "low",
            "riskReasons": [],
            "sources": [],
        }
    return load_review_cache(PROJECT_ROOT, target)

@app.post("/api/restaurant-review/refresh")
async def refresh_restaurant_review(req: RestaurantReviewRefreshReq):
    """手動更新餐廳評價情報，完成後寫入本機 JSON 快取。"""
    restaurant_name = (req.restaurant_name or "").strip()
    if not restaurant_name:
        raise HTTPException(400, "restaurant_name 不可為空")
    if RESTAURANT_MENUS and restaurant_name not in RESTAURANT_MENUS:
        raise HTTPException(404, f"餐廳 '{restaurant_name}' 不存在")

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: refresh_restaurant_reviews(PROJECT_ROOT, restaurant_name),
        )
    except Exception as e:
        print(f"[評價] 更新失敗: {e}")
        raise HTTPException(500, f"更新評價失敗: {str(e)}")

# 多餐廳管理 API
@app.get("/api/restaurants")
def list_restaurants():
    """列出所有可用的餐廳"""
    restaurants_list = []
    
    for name, menu_data in RESTAURANT_MENUS.items():
        # 計算總菜品數量（遍歷所有分類）
        total_items = 0
        restaurant_data = menu_data.get("restaurants", {}).get(name, {})
        categories = restaurant_data.get("categories", {})
        
        for category_name, category_data in categories.items():
            items = category_data.get("items", [])
            total_items += len(items)
        
        restaurants_list.append({
            "name": name,
            "active": name == ACTIVE_RESTAURANT,
            "itemCount": total_items
        })
    
    return {
        "restaurants": restaurants_list,
        "activeRestaurant": ACTIVE_RESTAURANT
    }

@app.post("/api/switch-restaurant")
def switch_restaurant(restaurant_name: str):
    """切換當前活動餐廳"""
    global ACTIVE_RESTAURANT, menu
    
    if restaurant_name not in RESTAURANT_MENUS:
        raise HTTPException(404, f"餐廳 '{restaurant_name}' 不存在")
    
    ACTIVE_RESTAURANT = restaurant_name
    menu = RESTAURANT_MENUS[restaurant_name]
    
    return {
        "success": True,
        "message": f" 已切換至 {restaurant_name}",
        "activeRestaurant": ACTIVE_RESTAURANT
    }

@app.delete("/api/menu/{restaurant_name}")
def delete_menu(restaurant_name: str):
    """刪除指定餐廳的菜單（從記憶體和磁碟）"""
    global ACTIVE_RESTAURANT, menu, RESTAURANT_MENUS
    
    # 檢查餐廳是否存在
    if restaurant_name not in RESTAURANT_MENUS:
        raise HTTPException(404, f"餐廳 '{restaurant_name}' 不存在")
    
    # 1. 從記憶體中移除
    del RESTAURANT_MENUS[restaurant_name]
    
    # 2. 刪除對應的 JSON 檔案
    menu_file = os.path.join(PROJECT_ROOT, f"menu_{restaurant_name}.json")
    
    if os.path.exists(menu_file):
        try:
            os.remove(menu_file)
            print(f"[刪除] 已刪除檔案: {menu_file}")
        except Exception as e:
            print(f"[錯誤] 刪除檔案失敗: {e}")
            raise HTTPException(500, f"刪除檔案失敗: {str(e)}")
    
    # 3. 如果刪除的是當前活動餐廳，切換到其他餐廳
    if ACTIVE_RESTAURANT == restaurant_name:
        if RESTAURANT_MENUS:
            # 切換到第一個可用的餐廳
            ACTIVE_RESTAURANT = next(iter(RESTAURANT_MENUS.keys()))
            menu = RESTAURANT_MENUS[ACTIVE_RESTAURANT]
            print(f"[切換] 已自動切換至: {ACTIVE_RESTAURANT}")
        else:
            # 沒有其他餐廳了
            ACTIVE_RESTAURANT = None
            menu = {"restaurants": {}}
            print(f"[警告] 已無可用餐廳")
    
    return {
        "success": True,
        "message": f"已成功刪除 {restaurant_name}",
        "activeRestaurant": ACTIVE_RESTAURANT
    }

if __name__ == "__main__":
    import uvicorn
    
    # 可以用環境變數自訂 host 和 port
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "7890"))
    
    print(f" 啟動後端服務...")
    print(f" 本機: http://localhost:{port}")
    if host == "0.0.0.0":
        # 同一個 Wi-Fi 下的手機要用這個網址才連得到
        try:
            import socket
            lan_ip = socket.gethostbyname(socket.gethostname())
            print(f" 手機/區網: http://{lan_ip}:{port}")
        except Exception:
            pass
    print(f" 靜態檔案: ../web")
    print(f"\n按 Ctrl+C 停止服務\n")
    uvicorn.run(app, host=host, port=port)
