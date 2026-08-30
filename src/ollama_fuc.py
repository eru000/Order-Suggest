
import os, json, re, shutil, subprocess, random, time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib import request, error

# 修正導入路徑（src 目錄下要用 db.db_client）


#從環境變數讀取設定
def _load_env_file() -> None:
    """Load simple KEY=value or PowerShell-style $env:KEY = "value" lines."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("$env:"):
                line = line[len("$env:"):]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and (key not in os.environ or not os.environ.get(key)):
                os.environ[key] = value
    except Exception:
        pass


_load_env_file()

DEFAULT_MODEL = (
    os.environ.get("API_MODEL")
    or os.environ.get("AI_MODEL")
    or os.environ.get("MODEL")
    or os.environ.get("OLLAMA_MODEL")
    or "gemma3:12b"
)
OLLAMA_BIN = os.getenv("OLLAMA_BIN", "ollama")
API_BASE_URL = os.getenv("API_BASE_URL", "").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
VISION_MODEL = os.getenv("VISION_MODEL", "ornith-35b")
VISION_JSON_TOOL_NAME = "submit_menu_json"


def _vision_json_tool() -> Dict[str, Any]:
    """Schema shared by overview, tile OCR, verification, and the legacy endpoint."""
    menu_item = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "price": {"type": ["number", "string", "null"]},
        },
        "required": ["name", "price"],
        "additionalProperties": True,
    }
    return {
        "type": "function",
        "function": {
            "name": VISION_JSON_TOOL_NAME,
            "description": "回傳提示要求的菜單 OCR JSON，不執行其他動作",
            "parameters": {
                "type": "object",
                "properties": {
                    "restaurant_name": {"type": ["string", "null"]},
                    "brand_candidates": {"type": "array", "items": {"type": "string"}},
                    "source_type": {"type": "string"},
                    "menu_type": {"type": "string"},
                    "layout": {"type": "string"},
                    "warnings": {"type": "array", "items": {"type": "string"}},
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "items": {"type": "array", "items": menu_item},
                            },
                            "required": ["name", "items"],
                            "additionalProperties": True,
                        },
                    },
                    "menu_items": {"type": "array", "items": menu_item},
                },
                "additionalProperties": True,
            },
        },
    }


def _extract_json_fragment(text: Any) -> Optional[str]:
    """Return the first complete JSON object/array embedded in model reasoning."""
    if not isinstance(text, str):
        return None
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
    return None


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _cli_available() -> bool:
    return shutil.which(OLLAMA_BIN) is not None #檢查路徑是否找到執行檔

# 靜默啟動 daemon
_DAEMON_SPAWNED = False
def ensure_daemon() -> None:
    global _DAEMON_SPAWNED
    if _DAEMON_SPAWNED: #避免重複啟動
        return
    if not _cli_available():
        raise RuntimeError(f"找不到 ollama 可執行檔，請設定 PATH 或 OLLAMA_BIN（目前：{OLLAMA_BIN}）。")
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    #Windows啟動子行程時隱藏視窗並背景執行而設計的參數設定
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            # |= (or)-> a = a or b
            creationflags |= subprocess.CREATE_NO_WINDOW
        #讓子行程與父行程的主控台分離，成為背景行程，不會跟隨父行程的主控台顯示與訊號（例如 Ctrl+C）而受影響。
        DETACHED_PROCESS = 0x00000008
        creationflags |= DETACHED_PROCESS
        kwargs["startupinfo"] = startupinfo
        kwargs["creationflags"] = creationflags
    subprocess.Popen([OLLAMA_BIN, "serve"], **kwargs)
    time.sleep(0.3) #給服務0.3秒時間啟動
    _DAEMON_SPAWNED = True


#呼叫外部的 ollama 可執行檔並回傳結果
def _cli_run(args: List[str], input_text: Optional[str] = None, timeout: float = 120.0) -> str:
    try:
        ensure_daemon()
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


#把一串對話訊息 messages組裝成一段適合丟給 CLI/文字模型的提示字串
def _build_prompt_from_messages(messages: List[Dict[str, str]]) -> str:
    parts: List[str] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            parts.append(f"[系統] {content}")
        elif role == "assistant":
            parts.append(f"助理: {content}")
        else:
            parts.append(f"使用者: {content}")
    parts.append("助理:")
    return "\n".join(parts)
_PAYLOAD_KEYS = ("categories", "menu_items", "items", "restaurant_name")


def _looks_like_payload(value: Any) -> bool:
    """Reject the coordinate arrays and stray braces that litter thinking text."""
    if isinstance(value, dict):
        return any(key in value for key in _PAYLOAD_KEYS)
    # _legacy_analyze 的重試會要一個 menu_items 陣列，所以頂層 list 也算數，
    # 但必須是物件組成的——[686, 880, 1558, 2000] 這種座標要擋掉。
    if isinstance(value, list):
        return bool(value) and all(isinstance(entry, dict) for entry in value)
    return False


def _find_json_payload(text: str) -> Optional[str]:
    """Return the first balanced JSON span that actually carries data, else None."""
    for start, opener in enumerate(text):
        if opener not in "{[":
            continue
        closer = "}" if opener == "{" else "]"
        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(text)):
            char = text[end]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    span = text[start:end + 1]
                    try:
                        parsed = json.loads(span)
                    except ValueError:
                        break
                    if _looks_like_payload(parsed):
                        return span
                    break
    return None


#把多輪對話messages轉成一段提示字串，再用CLI方式呼叫Ollama
def _api_chat(
    messages: List[Dict[str, Any]],
    model: str,
    timeout: float = 180.0,
    temperature: Optional[float] = None,
    allow_reasoning_fallback: bool = False,
    json_mode: bool = False,
) -> str:
    url = API_BASE_URL
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        # 辨識類呼叫要 temperature=0，不能套用對話用的預設值
        "temperature": _float_env("API_TEMPERATURE", 0.7) if temperature is None else temperature,
    }
    if json_mode:
        # 學校端的 vLLM 圖片路徑會忽略 tool_choice="none"，仍把「鍋燒類」等
        # 分類誤判成函式名稱。指定唯一合法函式後，結構化答案會穩定放在它的
        # arguments；這比依賴經常為空的 message.content 更可靠。
        payload.update({
            "tools": [_vision_json_tool()],
            "tool_choice": {
                "type": "function",
                "function": {"name": VISION_JSON_TOOL_NAME},
            },
        })
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    max_attempts = 2 if json_mode else 1
    last_finish_reason: Any = None
    last_tool_names: List[str] = []

    for attempt in range(max_attempts):
        req = request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"model API request failed: HTTP {exc.code} {detail}") from exc

        obj = json.loads(body)
        choices = obj.get("choices") or []
        if not choices:
            raise RuntimeError(f"model API returned no choices: {body[:500]}")
        choice = choices[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                continue
            function = call["function"]
            if function.get("name") != VISION_JSON_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str) and arguments.strip():
                return arguments.strip()
            if isinstance(arguments, (dict, list)):
                return json.dumps(arguments, ensure_ascii=False)

        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()

        # Ornith thinking model 有時把答案留在 reasoning_content。只有裡面真的
        # 有完整 JSON 才救援，避免把未完成的思考文字交給菜單解析器。
        reasoning_json = _extract_json_fragment(message.get("reasoning_content"))
        if allow_reasoning_fallback and reasoning_json:
            print(f"[API] {model} 的 content 是空的，改用 reasoning_content（finish_reason="
                  f"{choice.get('finish_reason')}）")
            return reasoning_json

        last_finish_reason = choice.get("finish_reason")
        last_tool_names = [
            str(call.get("function", {}).get("name") or "")[:80]
            for call in tool_calls
            if isinstance(call, dict) and isinstance(call.get("function"), dict)
        ]
        if attempt + 1 < max_attempts:
            detail = (
                f"，誤判工具：{', '.join(filter(None, last_tool_names))}"
                if last_tool_names
                else ""
            )
            print(f"[API] {model} 回傳空 content{detail}，以 JSON 模式重試一次")

    tool_detail = (
        f", tool_calls={','.join(filter(None, last_tool_names))}"
        if last_tool_names
        else ""
    )
    raise RuntimeError(
        f"model API returned empty content after {max_attempts} attempt(s) "
        f"(finish_reason={last_finish_reason}{tool_detail})"
    )


def chat(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    timeout: float = 180.0,
    temperature: Optional[float] = None,
) -> str:
    mdl = model or DEFAULT_MODEL
    if API_BASE_URL and API_KEY:
        api_model = os.environ.get("API_MODEL") or os.environ.get("AI_MODEL") or os.environ.get("MODEL") or mdl
        return _api_chat(messages, api_model, timeout=timeout, temperature=temperature)
    prompt = _build_prompt_from_messages(messages)
    return _cli_run(["run", mdl], input_text=prompt, timeout=timeout)


def _api_chat_stream(
    messages: List[Dict[str, Any]],
    model: str,
    timeout: float = 180.0,
    temperature: Optional[float] = None,
) -> Iterator[str]:
    """OpenAI 相容的 SSE 串流，逐段 yield 文字。

    總耗時跟非串流版差不多，差別在第一個字什麼時候出現：
    實測整段要 11-28 秒，但第一個字約 4-5 秒就到，等待感完全不同。
    """
    url = API_BASE_URL
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": _float_env("API_TEMPERATURE", 0.7) if temperature is None else temperature,
        "stream": True,
    }
    req = request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        resp = request.urlopen(req, timeout=timeout)
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"model API stream failed: HTTP {exc.code} {detail}") from exc

    with resp:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                return
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                # 心跳或被切斷的片段，跳過就好，不該讓整條串流掛掉
                continue
            choices = obj.get("choices") or [{}]
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                yield piece


def chat_stream(
    messages: List[Dict[str, str]],
    model: Optional[str] = None,
    timeout: float = 180.0,
    temperature: Optional[float] = None,
) -> Iterator[str]:
    """串流版 chat()。沒有設定遠端 API 時，退回一次性回應並整段 yield。"""
    mdl = model or DEFAULT_MODEL
    if API_BASE_URL and API_KEY:
        api_model = (os.environ.get("API_MODEL") or os.environ.get("AI_MODEL")
                     or os.environ.get("MODEL") or mdl)
        yield from _api_chat_stream(messages, api_model, timeout=timeout, temperature=temperature)
        return
    # 本機 Ollama CLI 沒有串流介面，只能等它跑完再一次吐出來
    prompt = _build_prompt_from_messages(messages)
    yield _cli_run(["run", mdl], input_text=prompt, timeout=timeout)


def vision_chat(
    prompt: str,
    image_url: str | Sequence[str],
    model: Optional[str] = None,
    timeout: float = 180.0,
    temperature: Optional[float] = None,
) -> str:
    """Call an OpenAI-compatible vision model with one or more images."""
    if not API_BASE_URL or not API_KEY:
        raise RuntimeError("vision model requires API_BASE_URL and API_KEY")

    content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
    urls = [image_url] if isinstance(image_url, str) else list(image_url)
    content.extend({"type": "image_url", "image_url": {"url": url}} for url in urls)
    return _api_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是菜單 OCR JSON 服務。必須使用指定的 submit_menu_json 函式回傳；"
                    "只能把完整結果放進函式 arguments，不得把分類或菜名當成函式名稱。"
                ),
            },
            {"role": "user", "content": content},
        ],
        model or VISION_MODEL,
        timeout=timeout,
        temperature=_float_env("VISION_TEMPERATURE", 0.0) if temperature is None else temperature,
        # 辨識這條路只要 JSON，推理文字裡撈得到就有用。對話那條路不開，
        # 否則使用者會看到模型的思考過程而不是回覆。
        allow_reasoning_fallback=True,
        json_mode=True,
    )

_MENU_EXTRACT_PROMPT = """這是一張餐廳菜單照片（可能是繁體中文紙本菜單、木牌、黑板或螢幕截圖）。
請仔細辨識所有菜品名稱與價格，只回傳以下 JSON 格式，不要其他文字：
{
  "restaurant_name": "餐廳名稱或null",
  "menu_items": [
    {"dish": "菜名", "price": "價格數字"},
    {"dish": "菜名2", "price": null}
  ]
}
規則：
- restaurant_name：如果圖片中能看出餐廳名稱請填入，否則填 null
- price 只填數字字串（例如 "150"），看不到價格填 null
- 無法辨識的項目請略過，不要猜測
- 只回傳 JSON，不要說明文字"""


def extract_menu_from_image(
    image_data: str,
    restaurant_name: str = "照片菜單",
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """從菜單圖片（base64 data URL 或網址）提取菜品列表。"""
    raw = vision_chat(_MENU_EXTRACT_PROMPT, image_data, model=model, timeout=120.0)
    result = _extract_json(raw)
    if isinstance(result, dict) and "menu_items" in result:
        result.setdefault("name", restaurant_name)
        return result
    if isinstance(result, list):
        return {"menu_items": result, "name": restaurant_name}
    return {"menu_items": [], "name": restaurant_name, "error": "無法解析 VLM 回應"}


def _extract_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            return None
    return None

# The recommendation engine is pure and independently testable.
from recommendation import RecommendationPolicy, recommend
