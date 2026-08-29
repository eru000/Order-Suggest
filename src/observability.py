from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import Any

LOGGER = logging.getLogger("ordersuggest")

_CONFIGURED = False


class _AsciiSafeFormatter(logging.Formatter):
    """主控台在 cp950 下輸出中文會整行噴 UnicodeEncodeError，escape 掉才安全。

    完整的中文內容仍然寫得進 logs/events.jsonl（UTF-8），主控台只是預覽。
    """

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return text.encode("ascii", "backslashreplace").decode("ascii")


def configure_logging(log_dir: str | None = None, *, level: int = logging.INFO) -> None:
    """把 emit() 的事件接到主控台與 logs/events.jsonl。

    在此之前 ``ordersuggest`` logger 沒有任何 handler，emit() 呼叫等於寫進黑洞
    ——vision 路徑量不到耗時就是這個原因。重複呼叫不會疊加 handler。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    LOGGER.setLevel(level)
    # uvicorn 會接管 root logger，不關掉 propagate 每個事件會印兩次。
    LOGGER.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_AsciiSafeFormatter("[event] %(message)s"))
    LOGGER.addHandler(console)

    # 測試會 import api_compat，若不擋掉，mock 出來的辨識事件會混進 events.jsonl，
    # 看起來跟真的跑過一模一樣——這個專案最不需要的就是這種假證據。
    if log_dir and "unittest" not in sys.modules:
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = RotatingFileHandler(
                os.path.join(log_dir, "events.jsonl"),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            LOGGER.addHandler(handler)
        except OSError as exc:
            # 檔案寫不進去不該讓服務起不來，主控台那份還在。
            LOGGER.warning('{"event": "observability.file_sink_unavailable", "error": "%s"}', exc)

    _CONFIGURED = True


def emit(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))
