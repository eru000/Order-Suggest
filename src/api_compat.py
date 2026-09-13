import os, sys, json, re, threading, time, uuid
from typing import Any, Dict, List, Optional, Literal
import base64
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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
    generate_conversation,
    generate_conversation_stream,
)

from restaurant_reviews import (
    identify_restaurant_candidates,
    load_review_cache,
    refresh_restaurant_reviews,
)

try:
    import crawl_menu
    CRAWLER_AVAILABLE = True
    CRAWLER_IMPORT_ERROR = ""
except Exception as exc:
    crawl_menu = None  # type: ignore[assignment]
    CRAWLER_AVAILABLE = False
    CRAWLER_IMPORT_ERROR = str(exc)

CRAWLER_ENABLED = os.getenv(
    "CRAWLER_ENABLED",
    "true" if os.name == "nt" else "false",
).strip().lower() in {"1", "true", "yes", "on"}
from runtime_cache import DistributedLock

CRAWLER_LOCK = DistributedLock("menu-crawler", timeout=10 * 60)

# VLM 菜單辨識（分塊辨識 + 交叉校對 + 人工確認）
from menu_vision import (
    MAX_IMAGE_BYTES,
    analyze_menu_image,
    apply_manual_correction,
    to_persisted_document,
)
from security import rate_limit, require_admin_key
from session_store import SessionStore
from persistence import build_session_repository
from menu_library import load_menu_library
from application_services import build_menu_catalog, conversation_repository, database_ready
from runtime_cache import DistributedLock, ExpiringJsonStore
from conversation_service import ConversationService
from decision_service import StaleDecision
from menu_ingestion_service import MenuIngestionService
from pending_analysis_service import PendingAnalysisService
from observability import configure_logging, emit

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
    allow_origins=[
        value.strip()
        for value in os.getenv(
            "CORS_ORIGINS",
            "http://127.0.0.1:7890,http://localhost:7890",
        ).split(",")
        if value.strip()
    ],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Admin-Key"],
)
# 提供 /web/* 靜態檔案
app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")

configure_logging(LOG_DIR)


@app.middleware("http")
async def _request_timing(request, call_next):
    """記錄每一筆請求的耗時到 logs/events.jsonl。

    Starlette 的 ``call_next`` 一律回傳 streaming 形式的回應，所以這裡分兩個
    數字：``ttfbMs`` 是拿到 header 的時間，``durationMs`` 是 body 真的送完的
    時間。對 /api/chat/stream 這種邊生成邊送的端點，只看前者會嚴重低估。
    """
    started = time.perf_counter()
    response = await call_next(request)
    ttfb_ms = round((time.perf_counter() - started) * 1000, 1)

    def _record(duration_ms: float) -> None:
        emit(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            durationMs=duration_ms,
            ttfbMs=ttfb_ms,
        )

    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        _record(ttfb_ms)
        return response

    async def _timed_body():
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            _record(round((time.perf_counter() - started) * 1000, 1))

    response.body_iterator = _timed_body()
    return response

# 載入共用菜單庫；活動餐廳由各 session 個別保存。
DEFAULT_RESTAURANT_NAME = os.getenv("DEFAULT_RESTAURANT_NAME", "預設餐廳")
if os.getenv("DATABASE_URL", "").strip():
    _LEGACY_MENUS, DEFAULT_ACTIVE_RESTAURANT = {}, DEFAULT_RESTAURANT_NAME
else:
    _LEGACY_MENUS, DEFAULT_ACTIVE_RESTAURANT = load_menu_library(
        PROJECT_ROOT, DEFAULT_RESTAURANT_NAME
    )
RESTAURANT_MENUS = build_menu_catalog(PROJECT_ROOT, _LEGACY_MENUS)
if DEFAULT_ACTIVE_RESTAURANT not in RESTAURANT_MENUS:
    available_restaurants = sorted(RESTAURANT_MENUS)
    DEFAULT_ACTIVE_RESTAURANT = (
        DEFAULT_RESTAURANT_NAME
        if DEFAULT_RESTAURANT_NAME in RESTAURANT_MENUS
        else (available_restaurants[-1] if available_restaurants else DEFAULT_RESTAURANT_NAME)
    )
MENU_INGESTION = MenuIngestionService(
    RESTAURANT_MENUS,
    int(getattr(crawl_menu, "MIN_VISION_MENU_ITEMS", 8)) if crawl_menu else 8,
)

# 每個 session 擁有自己的偏好、對話歷史與活動餐廳。
SESSIONS = SessionStore(
    ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(24 * 60 * 60))),
    repository=build_session_repository(PROJECT_ROOT),
)

# 待確認的 VLM 辨識結果。辨識完不直接落地，等使用者確認過才寫入菜單，
# 所以需要一個有時效的暫存區。
PENDING_ANALYSES = ExpiringJsonStore("pending-analysis", 15 * 60)
PENDING_ANALYSIS_LOCK = threading.Lock()
PENDING_ANALYSIS_TTL_SECONDS = 15 * 60
PENDING_SERVICE = PendingAnalysisService(
    PENDING_ANALYSES,
    apply_manual_correction,
    PENDING_ANALYSIS_TTL_SECONDS,
)


def _safe_menu_path(restaurant_name: str) -> str:
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", restaurant_name).strip(" ._")
    safe_name = re.sub(r"\s+", "_", safe_name)[:100]
    if not safe_name:
        raise ValueError("餐廳名稱無法作為檔名")
    return os.path.join(PROJECT_ROOT, f"menu_{safe_name}.json")


def _prune_pending_analyses(now: Optional[float] = None) -> None:
    PENDING_SERVICE.prune(now)
def _store_pending_analysis(result: Dict[str, Any], session_id: str) -> str:
    return PENDING_SERVICE.create(result, session_id)


def _take_pending_analysis(analysis_id: str, session_id: str) -> Dict[str, Any]:
    return PENDING_SERVICE.take(analysis_id, session_id)


def _correct_pending_analysis(analysis_id: str, instruction: str, session_id: str) -> Dict[str, Any]:
    return PENDING_SERVICE.correct(analysis_id, instruction, session_id)


def _analysis_response(result: Dict[str, Any], analysis_id: str) -> Dict[str, Any]:
    return PENDING_SERVICE.response(result, analysis_id)


def _register_vision_menu(result: Dict[str, Any]) -> Dict[str, Any]:
    return MENU_INGESTION.register_vision(result)


def _run_crawler(restaurant_name: str):
    """在背景執行緒使用獨立 event loop 操作 Windows Chrome。"""
    if not CRAWLER_AVAILABLE or crawl_menu is None:
        raise RuntimeError(CRAWLER_IMPORT_ERROR or "爬蟲模組無法使用")
    if sys.platform.startswith("win") and hasattr(asyncio, "ProactorEventLoop"):
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(crawl_menu.quick_crawl(restaurant_name))
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _normalize_crawled_price(value: Any) -> Optional[float]:
    return MENU_INGESTION.normalize_price(value)


def _is_non_food_crawled_item(name: str) -> bool:
    return MENU_INGESTION.is_non_food(name)


def _register_crawled_menu(restaurant: Any) -> Dict[str, Any]:
    return MENU_INGESTION.register_crawled(restaurant)


def _crawl_payload(
    *,
    success: bool,
    message: str,
    restaurant_name: Optional[str] = None,
    item_count: int = 0,
    menu_items: Optional[List[Dict[str, Any]]] = None,
    error_code: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "success": success,
        "message": message,
        "restaurantName": restaurant_name,
        "itemCount": item_count,
        "menuItems": menu_items or [],
        "errorCode": error_code,
    }


def _log_chat(session_id: str, user_text: str, reply: str, prefs: Preferences) -> None:
    """Persist conversation analytics without making chat availability depend on it."""
    try:
        repository = conversation_repository(PROJECT_ROOT)
        repository.append(session_id, "user", user_text, {"preferences": dict(prefs)})
        repository.append(session_id, "assistant", reply)
    except Exception:
        # Analytics failure must not fail the user request.
        pass

class ChatReq(BaseModel):
    sessionId: str
    text: str

class ChatResp(BaseModel):
    reply: str
    decision: Optional[Dict[str, Any]] = None


class DecisionReq(BaseModel):
    sessionId: str = Field(pattern=r"^[A-Za-z0-9_-]{8,160}$")
    action: Literal[
        "start", "message", "answer", "recommend", "replace", "reject_all",
        "choose", "reconsider", "reset", "review", "relax_budget", "relax_type", "decide",
    ] = "message"
    text: str = Field(default="", max_length=2000)
    revision: Optional[str] = Field(default=None, max_length=64)
    value: Optional[str] = Field(default=None, max_length=100)
    itemId: Optional[str] = Field(default=None, max_length=64)
    reason: Literal["another", "price", "type", "recent", "light", "portion"] = "another"

class CrawlMenuReq(BaseModel):
    restaurantName: str
    sessionId: str

class RestaurantReviewRefreshReq(BaseModel):
    restaurant_name: str
    restaurant_identity: Optional[Dict[str, object]] = None
    source_urls: Optional[List[str]] = None

class RestaurantReviewIdentifyReq(BaseModel):
    restaurant_name: str

class VisionConfirmReq(BaseModel):
    restaurant_name: str = ""
    accept_conflicts: bool = False
    sessionId: str

class VisionCorrectionReq(BaseModel):
    instruction: str
    sessionId: str


def _session(session_id: str):
    try:
        return SESSIONS.get(session_id, DEFAULT_ACTIVE_RESTAURANT)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _session_menu(session_id: str):
    state = _session(session_id)
    selected = state.active_restaurant
    selected_menu = RESTAURANT_MENUS.get(selected or "")
    if not selected_menu:
        raise HTTPException(409, "此 session 尚未選擇可用餐廳")
    return state, selected, selected_menu


CONVERSATIONS = ConversationService(
    SESSIONS,
    RESTAURANT_MENUS,
    DEFAULT_ACTIVE_RESTAURANT,
    _log_chat,
)

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/health/readiness")
def readiness():
    db_ready = database_ready(PROJECT_ROOT)
    ready = db_ready and PENDING_ANALYSES.available
    content = {"ok": ready, "database": db_ready, "redis": PENDING_ANALYSES.available}
    return content if ready else JSONResponse(status_code=503, content=content)

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
def get_current_menu(session_id: str):
    """
    回傳當前活動餐廳的菜單資料
    """
    try:
        state = _session(session_id)
        active_restaurant = state.active_restaurant
        active_menu = RESTAURANT_MENUS.get(active_restaurant or "")
        if not active_restaurant or not active_menu:
            return {
                "success": False,
                "message": "目前未載入任何菜單",
                "restaurantName": None,
                "categories": []
            }
        
        # 獲取當前餐廳的菜單資料
        restaurant_data = active_menu.get("restaurants", {}).get(active_restaurant, {})
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
            "restaurantName": active_restaurant,
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


@app.post(
    "/api/menu/crawl",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-crawl", 5, 300))],
)
async def crawl_restaurant_menu(req: CrawlMenuReq):
    state = _session(req.sessionId)
    restaurant_name = re.sub(r"\s+", " ", req.restaurantName).strip()
    if not restaurant_name or len(restaurant_name) > 120:
        return JSONResponse(
            status_code=400,
            content=_crawl_payload(
                success=False,
                message="請輸入 1 到 120 字的餐廳名稱",
                error_code="invalid_restaurant_name",
            ),
        )
    if not CRAWLER_ENABLED:
        return JSONResponse(
            status_code=503,
            content=_crawl_payload(
                success=False,
                message="此環境未啟用爬菜單功能；請在 Windows 本機設定 CRAWLER_ENABLED=true。",
                restaurant_name=restaurant_name,
                error_code="crawler_disabled",
            ),
        )
    if not CRAWLER_AVAILABLE:
        return JSONResponse(
            status_code=503,
            content=_crawl_payload(
                success=False,
                message=f"爬蟲模組無法載入：{CRAWLER_IMPORT_ERROR}",
                restaurant_name=restaurant_name,
                error_code="crawler_unavailable",
            ),
        )
    if not CRAWLER_LOCK.available:
        return JSONResponse(
            status_code=503,
            content=_crawl_payload(
                success=False,
                message="分散式鎖服務暫時無法使用，請稍後再試。",
                restaurant_name=restaurant_name,
                error_code="lock_unavailable",
            ),
        )
    if not CRAWLER_LOCK.acquire(blocking=False):
        return JSONResponse(
            status_code=409,
            content=_crawl_payload(
                success=False,
                message="目前已有餐廳正在爬取，請完成後再試。",
                restaurant_name=restaurant_name,
                error_code="crawler_busy",
            ),
        )

    try:
        restaurant = await asyncio.to_thread(_run_crawler, restaurant_name)
        if not restaurant:
            return JSONResponse(
                status_code=503,
                content=_crawl_payload(
                    success=False,
                    message="無法啟動或連線至 Chrome，請檢查 CHROME_PATH 與 CDP 設定。",
                    restaurant_name=restaurant_name,
                    error_code="chrome_unavailable",
                ),
            )
        if getattr(restaurant, "error", "") == "google_verification_required":
            return JSONResponse(
                status_code=502,
                content=_crawl_payload(
                    success=False,
                    message="Google 要求人機驗證；請在自動開啟的 Chrome 完成驗證後重新爬取。",
                    restaurant_name=restaurant_name,
                    error_code="google_verification_required",
                ),
            )
        if not getattr(restaurant, "menu_items", None):
            return JSONResponse(
                status_code=502,
                content=_crawl_payload(
                    success=False,
                    message="Google 文字菜單與圖片辨識都沒有取得至少 8 項可用資料。",
                    restaurant_name=restaurant_name,
                    error_code="menu_not_found",
                ),
            )
        registered = _register_crawled_menu(restaurant)
        with state.lock:
            state.active_restaurant = registered["restaurantName"]
        SESSIONS.save(req.sessionId)
        return _crawl_payload(
            success=True,
            message=f"成功爬取 {registered['restaurantName']} 的菜單",
            restaurant_name=registered["restaurantName"],
            item_count=registered["itemCount"],
            menu_items=registered["menuItems"],
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=502,
            content=_crawl_payload(
                success=False,
                message=str(exc),
                restaurant_name=restaurant_name,
                error_code="insufficient_menu_data",
            ),
        )
    except Exception as exc:
        print(f"[爬菜單] {restaurant_name} 失敗: {exc}")
        return JSONResponse(
            status_code=500,
            content=_crawl_payload(
                success=False,
                message=f"爬取菜單時發生錯誤：{exc}",
                restaurant_name=restaurant_name,
                error_code="crawl_failed",
            ),
        )
    finally:
        CRAWLER_LOCK.release()


@app.post(
    "/api/upload-menu-photo",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-vision", 10, 600))],
)
async def upload_menu_photo(
    file: UploadFile = File(...),
    restaurantName: str = Form("照片菜單"),
    session_id: str = Form(...),
):
    state = _session(session_id)
    try:
        image_bytes = await file.read(MAX_IMAGE_BYTES + 1)
        if len(image_bytes) > MAX_IMAGE_BYTES:
            raise HTTPException(413, "圖片不可超過 10 MB")
        content_type = file.content_type or "image/jpeg"
        if content_type.split(";", 1)[0].strip().lower() not in {
            "image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"
        }:
            raise HTTPException(415, "僅支援 JPEG、PNG、WebP 或 HEIC 圖片")
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
        with state.lock:
            state.active_restaurant = finalName
        SESSIONS.save(session_id)

        return {
            "success": True,
            "message": f"成功從照片辨識 {len(items)} 道菜",
            "restaurantName": finalName,
            "itemCount": len(items),
        }
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": f"辨識失敗：{str(e)}"}


@app.post(
    "/api/menu/vision",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-vision", 10, 600))],
)
async def create_menu_from_photo(
    restaurant_name: str = Form(default=""),
    session_id: str = Form(...),
    image: UploadFile = File(...),
):
    """辨識照片，結果先進待確認區，不直接寫入菜單。"""
    if not PENDING_ANALYSES.available:
        raise HTTPException(503, "待確認快取暫時無法使用，請稍後再試")
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
        _session(session_id)
        analysis_id = _store_pending_analysis(result, session_id)
        return _analysis_response(result, analysis_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"VLM 服務錯誤：{exc}") from exc


def _restore_pending_analysis(analysis_id: str, result: Dict[str, Any], session_id: str) -> None:
    """把取走的待確認結果放回去。

    _take_pending_analysis 是「取走」，一旦取出就不在快取裡了。原本只有 409
    那條路徑會放回去，其他任何失敗都會讓使用者 22 秒的辨識結果永久消失，
    只能重新上傳整張照片重跑一次——這是實際回報的問題。
    """
    with PENDING_ANALYSIS_LOCK:
        PENDING_ANALYSES[analysis_id] = {
            "result": result,
            "session_id": session_id,
            "created_at": time.time(),
            "expires_at": time.time() + PENDING_ANALYSIS_TTL_SECONDS,
        }


@app.post(
    "/api/menu/vision/{analysis_id}/confirm",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-vision-confirm", 30, 60))],
)
def confirm_menu_from_photo(analysis_id: str, req: VisionConfirmReq):
    """使用者確認後才把待確認結果寫成菜單。"""
    state = _session(req.sessionId)
    try:
        result = _take_pending_analysis(analysis_id, req.sessionId)
    except ValueError as exc:
        # 取不出來（過期、session 不符）就沒有東西可以還原
        raise HTTPException(422, str(exc)) from exc

    try:
        restaurant_name = (req.restaurant_name or str(result.get("restaurant_name") or "")).strip()
        if not restaurant_name:
            restaurant_name = str(result.get("detected_restaurant_name") or "").strip()
        # 兩邊都沒有就交給 ingestion 產生「未命名菜單」。照片上本來就常常沒印
        # 店名（手寫木牌、只有品項的價目表），辨識結果是好的，不該因為少一個
        # 標籤就整批丟掉、逼使用者回頭補打。
        quality = result.get("quality") if isinstance(result.get("quality"), dict) else {}
        needs_acceptance = bool(
            result.get("identityConflict")
            or result.get("conflicts")
            or float(quality.get("score") or 0) < 0.75
        )
        if needs_acceptance and not req.accept_conflicts:
            raise HTTPException(409, "店名、辨識衝突或品質需要人工檢查，請確認摘要後明確接受再送出")
        result["restaurant_name"] = restaurant_name
        summary = _register_vision_menu(result)
    except HTTPException:
        # 409 要讓使用者勾了「接受」再送一次，結果必須還在
        _restore_pending_analysis(analysis_id, result, req.sessionId)
        raise
    except ValueError as exc:
        _restore_pending_analysis(analysis_id, result, req.sessionId)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        _restore_pending_analysis(analysis_id, result, req.sessionId)
        raise

    with state.lock:
        state.active_restaurant = summary["restaurantName"]
    SESSIONS.save(req.sessionId)
    return {"success": True, **summary, "quality": result.get("quality", {}), "warnings": result.get("warnings", [])}


@app.post(
    "/api/menu/vision/{analysis_id}/correct",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-vision-correct", 20, 60))],
)
def correct_menu_from_photo(analysis_id: str, req: VisionCorrectionReq):
    """修正待確認結果，不動到已存檔的菜單。"""
    try:
        _session(req.sessionId)
        correction = _correct_pending_analysis(analysis_id, req.instruction, req.sessionId)
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
    try:
        decision = CONVERSATIONS.discovery(req.sessionId, req.text)
        if decision:
            return {
                "reply": "\n".join(filter(None, [decision["message"], decision.get("answer")])),
                "decision": decision,
            }
        return {"reply": CONVERSATIONS.chat(req.sessionId, req.text)}
    except LookupError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/decision")
def get_decision(session_id: str):
    try:
        return JSONResponse(
            content={"decision": CONVERSATIONS.decisions.current(session_id)},
            headers={"Cache-Control": "no-store"},
        )
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/decision")
def update_decision(req: DecisionReq):
    try:
        decision = CONVERSATIONS.decisions.handle(
            req.sessionId, action=req.action, text=req.text, revision=req.revision,
            value=req.value, item_id=req.itemId, reason=req.reason,
        )
        return {"decision": decision}
    except StaleDecision as exc:
        return JSONResponse(status_code=409, content={
            "detail": str(exc), "decision": CONVERSATIONS.decisions.current(req.sessionId),
        })
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/chat/stream")
def api_chat_stream(req: ChatReq):
    """SSE 串流版對話。

    先送 recommendation 事件（本機計算，約 1 秒內到），再逐段送 AI 說明文字。
    重點在第一個事件：LLM 的第一個字要等 12-42 秒（遠端排隊，與 prompt 長度
    無關），但推薦品項與價格不必等它。

    前端拿不到串流時會自動退回 /api/chat，所以這支掛掉不會讓功能不可用。
    """
    def event_stream():
        try:
            for event in CONVERSATIONS.stream(req.sessionId, req.text):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            print(f"[/api/chat/stream] 串流中斷: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"
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
def get_restaurant_review(
    restaurant_name: Optional[str] = None,
    session_id: Optional[str] = None,
):
    """讀取餐廳評價快取；不觸發網路搜尋。"""
    target = restaurant_name
    if not target and session_id:
        target = _session(session_id).active_restaurant
    if not target:
        return {
            "success": False,
            "message": "尚未選擇餐廳",
            "restaurantName": None,
            "schemaVersion": 3,
            "restaurantIdentity": None,
            "needsIdentity": True,
            "needsRefresh": False,
            "updatedAt": None,
            "recommendationScore": 0,
            "confidenceScore": 0,
            "overallScore": 0,
            "sentiment": "unknown",
            "summary": "",
            "pros": [],
            "cons": [],
            "prosEvidence": [],
            "consEvidence": [],
            "recommendedFor": [],
            "aspects": {},
            "riskLevel": "unknown",
            "riskReasons": ["尚未選擇餐廳"],
            "riskSignals": [],
            "evidence": [],
            "sources": [],
            "searchMeta": {
                "status": "no_relevant_sources",
                "provider": None,
                "attemptedProviders": [],
                "rawResultCount": 0,
                "relevantSourceCount": 0,
            },
        }
    return load_review_cache(PROJECT_ROOT, target)

@app.post("/api/restaurant-review/identify")
async def identify_restaurant_review(req: RestaurantReviewIdentifyReq):
    """搜尋可能的餐廳分店，交由使用者確認後才更新評價。"""
    restaurant_name = (req.restaurant_name or "").strip()
    if not restaurant_name:
        raise HTTPException(400, "restaurant_name 不可為空")
    if len(restaurant_name) > 120:
        raise HTTPException(400, "restaurant_name 不可超過 120 個字元")
    loop = asyncio.get_event_loop()
    try:
        candidates = await loop.run_in_executor(
            None,
            lambda: identify_restaurant_candidates(restaurant_name),
        )
        return {
            "success": bool(candidates),
            "restaurantName": restaurant_name,
            "candidates": candidates,
            "message": "請確認要分析的餐廳分店" if candidates else "找不到可確認的餐廳分店",
        }
    except Exception as e:
        print(f"[評價] 店家辨識失敗: {e}")
        raise HTTPException(500, f"店家辨識失敗: {str(e)}")

@app.post("/api/restaurant-review/refresh")
async def refresh_restaurant_review(req: RestaurantReviewRefreshReq):
    """手動更新餐廳評價情報，完成後寫入本機 JSON 快取。"""
    restaurant_name = (req.restaurant_name or "").strip()
    if not restaurant_name:
        raise HTTPException(400, "restaurant_name 不可為空")
    if len(restaurant_name) > 120:
        raise HTTPException(400, "restaurant_name 不可超過 120 個字元")

    lock = DistributedLock(f"review-refresh:{restaurant_name}", timeout=5 * 60)
    if not lock.available:
        raise HTTPException(503, "分散式鎖服務暫時無法使用，請稍後再試")
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "此餐廳的評價正在更新")
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(
            None,
            lambda: refresh_restaurant_reviews(
                PROJECT_ROOT,
                restaurant_name,
                req.restaurant_identity,
                req.source_urls,
            ),
        )
    except Exception as e:
        print(f"[評價] 更新失敗: {e}")
        raise HTTPException(500, f"更新評價失敗: {str(e)}")
    finally:
        lock.release()

# 多餐廳管理 API
@app.get("/api/restaurants")
def list_restaurants(session_id: str):
    """列出所有可用的餐廳"""
    active_restaurant = _session(session_id).active_restaurant
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
            "active": name == active_restaurant,
            "itemCount": total_items
        })
    
    return {
        "restaurants": restaurants_list,
        "activeRestaurant": active_restaurant
    }

@app.post("/api/switch-restaurant")
def switch_restaurant(restaurant_name: str, session_id: str):
    """切換當前活動餐廳"""
    if restaurant_name not in RESTAURANT_MENUS:
        raise HTTPException(404, f"餐廳 '{restaurant_name}' 不存在")
    state = _session(session_id)
    with state.lock:
        state.active_restaurant = restaurant_name
        state.decision = {}
    SESSIONS.save(session_id)
    
    return {
        "success": True,
        "message": f" 已切換至 {restaurant_name}",
        "activeRestaurant": restaurant_name
    }

@app.delete(
    "/api/menu/{restaurant_name}",
    dependencies=[Depends(require_admin_key), Depends(rate_limit("menu-delete", 10, 60))],
)
def delete_menu(restaurant_name: str):
    """刪除指定餐廳的菜單與其資料庫版本。"""
    # 檢查餐廳是否存在
    if restaurant_name not in RESTAURANT_MENUS:
        raise HTTPException(404, f"餐廳 '{restaurant_name}' 不存在")
    
    del RESTAURANT_MENUS[restaurant_name]
    fallback = next(iter(RESTAURANT_MENUS), None)
    SESSIONS.replace_deleted_restaurant(restaurant_name, fallback)
    
    return {
        "success": True,
        "message": f"已成功刪除 {restaurant_name}",
        "activeRestaurant": fallback
    }

if __name__ == "__main__":
    import uvicorn
    
    # 可以用環境變數自訂 host 和 port
    # 本機開發預設只綁定 loopback，Uvicorn 會顯示可直接開啟的本機網址。
    # 若要讓同一個 Wi-Fi 的手機連線，部署／區網啟動時再設 HOST=0.0.0.0。
    host = os.environ.get("HOST", "127.0.0.1")
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
