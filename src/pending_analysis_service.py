from __future__ import annotations

import threading
import time
import uuid
from collections.abc import MutableMapping
from typing import Any


class PendingAnalysisService:
    def __init__(
        self,
        store: MutableMapping[str, dict[str, Any]],
        corrector,
        ttl_seconds: int = 15 * 60,
    ) -> None:
        self.store = store
        self.corrector = corrector
        self.ttl_seconds = ttl_seconds
        self.lock = threading.RLock()

    def prune(self, now: float | None = None) -> None:
        current = now if now is not None else time.time()
        for analysis_id, entry in list(self.store.items()):
            if float(entry.get("expires_at") or 0) <= current:
                self.store.pop(analysis_id, None)

    def create(self, result: dict[str, Any], session_id: str) -> str:
        analysis_id = uuid.uuid4().hex
        now = time.time()
        with self.lock:
            self.prune(now)
            self.store[analysis_id] = {
                "result": result,
                "session_id": session_id,
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
            }
        return analysis_id

    def take(self, analysis_id: str, session_id: str) -> dict[str, Any]:
        with self.lock:
            self.prune()
            entry = self.store.pop(analysis_id, None)
        if (
            not entry
            or entry.get("session_id") != session_id
            or not isinstance(entry.get("result"), dict)
        ):
            raise ValueError("辨識結果不存在或已超過 15 分鐘，請重新上傳照片")
        return entry["result"]

    def correct(self, analysis_id: str, instruction: str, session_id: str) -> dict[str, Any]:
        with self.lock:
            self.prune()
            entry = self.store.get(analysis_id)
            if (
                not entry
                or entry.get("session_id") != session_id
                or not isinstance(entry.get("result"), dict)
            ):
                raise ValueError("辨識結果不存在或已超過 15 分鐘，請重新上傳照片")
            correction = self.corrector(entry["result"], instruction)
            entry["expires_at"] = time.time() + self.ttl_seconds
            self.store[analysis_id] = entry
            return correction

    def response(self, result: dict[str, Any], analysis_id: str) -> dict[str, Any]:
        raw_quality = result.get("quality")
        quality: dict[str, Any] = dict(raw_quality) if isinstance(raw_quality, dict) else {}
        item_count = int(
            quality.get("itemCount")
            or sum(
                len(category.get("items", []))
                for category in result.get("categories", [])
                if isinstance(category, dict)
            )
        )
        return {
            "success": True,
            "analysisId": analysis_id,
            "expiresInSeconds": self.ttl_seconds,
            "restaurantNameCandidate": result.get("detected_restaurant_name")
            or result.get("restaurant_name")
            or "",
            "requestedRestaurantName": (result.get("identity") or {}).get("userHint", "")
            if isinstance(result.get("identity"), dict)
            else "",
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
