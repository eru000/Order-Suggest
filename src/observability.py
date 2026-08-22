from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

LOGGER = logging.getLogger("ordersuggest")


def emit(event: str, **fields: Any) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    LOGGER.info(json.dumps(payload, ensure_ascii=False, default=str))
