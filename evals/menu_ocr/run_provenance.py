"""Offline evidence for menu evaluations; never imports the API client or reads .env.

Model names here identify the requested/selected API alias, not its undisclosed weights.
An absent observation remains unknown: a fast response is not evidence of a cache hit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
SCORING_VERSION = "menu-ocr-score-v2"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# An allowlist prevents an accidentally supplied config from persisting credentials.
SAFE_SETTING_KEYS = frozenset({
    "visionModel", "verifyModel", "fast", "tileOverlap", "tileThreshold",
    "matchThreshold", "jobs", "debug", "visionLongSide", "visionTiles",
    "visionTemperature", "visionPriceText", "visionMaxTokens", "method", "ocrEngine",
    "ocrPostprocess", "rapidocr-onnxruntime", "onnxruntime", "opencc-python-reimplemented", "targetLongSide", "minUpscaleLongSide",
    "jpegQuality", "transport", "endpointHost", "cacheStatus", "cachePolicy",
    "scoringVersion", "longSide", "temperature", "tiles", "maxTokens",
})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_digest(value: Any) -> str:
    return _digest(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8"))


def _file_digest(path: Path) -> str | None:
    try:
        return _digest(path.read_bytes())
    except OSError:
        return None


def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def build_case_provenance(
    case: Mapping[str, Any], *, project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Hash supplied source bytes and truth, also supporting missing-image cases.

    The in-memory expected object is the scoring truth. Its canonical hash is distinct
    from the optional .expected.json file hash, which also detects formatting edits.
    Actual preprocessed model inputs are captured by build_request_provenance instead.
    """
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    image = Path(case["image_path"]) if case.get("image_path") is not None else None
    expected_path = case.get("expected_path")
    truth_file = Path(expected_path) if expected_path is not None else (
        image.parent / f"{image.stem}.expected.json" if image is not None else None
    )
    image_hash = _file_digest(image) if image is not None else None
    truth = case.get("expected")
    return {
        "caseId": str(case.get("name") or case.get("case") or ""),
        "sourceImage": {
            "path": _display_path(image, root) if image is not None else None,
            "sha256": image_hash,
            "status": "present" if image_hash is not None else "missing",
        },
        "truth": {
            "sha256": _json_digest(truth) if isinstance(truth, Mapping) else None,
            "fileSha256": _file_digest(truth_file) if truth_file is not None else None,
            "path": _display_path(truth_file, root) if truth_file is not None else None,
        },
        "status": case.get("status") or ("ready" if image_hash else "missing_image"),
        "skipReason": case.get("skipReason"),
    }


def capture_code_provenance(project_root: str | Path | None = None) -> dict[str, Any]:
    """Capture relevant Python content even when Git is absent or the tree is dirty."""
    root = Path(project_root) if project_root is not None else PROJECT_ROOT
    paths = sorted({
        *root.joinpath("src").rglob("*.py"),
        *root.joinpath("evals", "menu_ocr").glob("*.py"),
    })
    hashes = {_display_path(path, root): _file_digest(path) for path in paths if path.is_file()}
    commit: str | None = None
    dirty: bool | None = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
            text=True, timeout=5, check=False,
        )
        if result.returncode == 0:
            commit = result.stdout.strip() or None
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root, capture_output=True, text=True, timeout=5, check=False,
            )
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "commit": commit, "dirty": dirty, "sourceHashes": hashes,
        "sourceFingerprint": _json_digest(hashes) if hashes else None,
    }


def build_run_provenance(
    cases: Iterable[Mapping[str, Any]], *, run_mode: str,
    settings: Mapping[str, Any] | None = None,
    replay_source: Mapping[str, Any] | None = None,
    project_root: str | Path | None = None,
) -> dict[str, Any]:
    """Record a run without reading environment variables or loading product code."""
    config = {key: value for key, value in (settings or {}).items() if key in SAFE_SETTING_KEYS}
    manifest = sorted(
        (build_case_provenance(case, project_root=project_root) for case in cases),
        key=lambda item: item["caseId"],
    )
    identities = [{
        "caseId": item["caseId"],
        "sourceImageSha256": item["sourceImage"]["sha256"],
        "truthSha256": item["truth"]["sha256"],
    } for item in manifest]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runMode": run_mode,
        "cacheStatus": config.get("cacheStatus") or "unknown",
        "settings": config,
        "scoringVersion": config.get("scoringVersion") or SCORING_VERSION,
        "matchThreshold": config.get("matchThreshold"),
        "caseManifest": manifest,
        "datasetFingerprint": _json_digest(identities),
        "code": capture_code_provenance(project_root),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "replaySource": {
            key: replay_source.get(key)
            for key in ("datasetFingerprint", "code", "runMode", "cacheStatus")
        } if replay_source is not None else None,
    }


def _image_provenance(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.startswith("data:") and "," in value:
        header, encoded = value.split(",", 1)
        if ";base64" in header:
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, base64.binascii.Error):
                return {"kind": "invalid_data_url", "sha256": None}
            return {
                "kind": "embedded_image", "sha256": _digest(data),
                "byteCount": len(data), "mime": header[5:].split(";", 1)[0],
            }
    if isinstance(value, str):
        # Hash references without saving URLs, which may contain credentials.
        return {"kind": "url_reference", "sha256": _digest(value.encode("utf-8"))}
    return {"kind": "unknown", "sha256": None}


def build_request_provenance(
    prompt: str, image_url: Any, *, model: Any = None,
    temperature: Any = None, timeout: Any = None, run_mode: str = "live",
) -> dict[str, Any]:
    """Hash the exact prompt and image bytes handed to the vision API adapter."""
    values = [image_url] if isinstance(image_url, str) else (
        list(image_url) if isinstance(image_url, (list, tuple)) else [None]
    )
    fields = {
        "promptSha": _digest(prompt.encode("utf-8")),
        "inputImages": [_image_provenance(value) for value in values],
        "requestedModel": model, "temperature": temperature, "timeout": timeout,
    }
    return {
        **fields, "requestSha256": _json_digest(fields),
        "executionMode": run_mode, "cacheStatus": "unknown",
        "underlyingModelVersion": "unknown",
    }


def provenance_from_calls(
    calls: Iterable[Mapping[str, Any]], *, result: Mapping[str, Any] | None = None,
    run_mode: str = "live",
) -> dict[str, Any]:
    """Summarize observations; no calls/results means unknown, never zero-cost inference."""
    rows = list(calls)
    models = (result or {}).get("models") or {}
    quality = (result or {}).get("quality") or {}
    requested = models.get("ocrRequested")
    active = models.get("ocr")
    fallback = quality.get("modelFallback")
    if fallback is None and requested is not None and active is not None:
        fallback = requested != active
    return {
        "executionMode": run_mode,
        "requestedModel": requested,
        "activeModel": active,
        "fallback": fallback,
        "underlyingModelVersion": "unknown",
        "cacheStatus": "unknown",
        "capturedCallCount": len(rows),
        "requestedModels": sorted({
            str(row.get("requestedModel") or row.get("model")) for row in rows
            if row.get("requestedModel") is not None or row.get("model") is not None
        }),
        "promptHashes": sorted({str(row["promptSha"]) for row in rows if row.get("promptSha")}),
        "inputImageHashes": sorted({
            str(image["sha256"]) for row in rows for image in row.get("inputImages", [])
            if image.get("kind") == "embedded_image" and image.get("sha256")
        }),
    }


def compare_provenance(
    left: Mapping[str, Any] | None, right: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Check score comparability; configuration differences are experimental factors.

    Latency requires matching live execution mode and concurrency. Unknown service
    caching remains an explicit limitation even when observed durations can be shown.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    if not left or not right:
        reasons.append("缺少執行來源紀錄，無法確認資料與評分規則相同。")
    else:
        for key, label in (
            ("datasetFingerprint", "資料集與真值"),
            ("scoringVersion", "評分版本"),
            ("matchThreshold", "品名配對門檻"),
        ):
            if left.get(key) is None or right.get(key) is None:
                reasons.append(f"{label}來源資訊不足。")
            elif left[key] != right[key]:
                reasons.append(f"{label}不同，請先以共同資料與規則重算。")
        for provenance in (left, right):
            manifest = provenance.get("caseManifest")
            if not manifest or any(
                not item.get("sourceImage", {}).get("sha256")
                or not item.get("truth", {}).get("sha256") for item in manifest
            ):
                reasons.append("案例包含缺圖或缺真值雜湊，無法確認完整資料一致。")
                break
            case_ids = [item.get("caseId") for item in manifest]
            if any(not case_id for case_id in case_ids) or len(set(case_ids)) != len(case_ids):
                reasons.append("案例識別碼缺失或重複，無法建立唯一案例對照。")
                break
        if left.get("settings") != right.get("settings"):
            warnings.append("執行設定不同；解讀結果時請列出改變的實驗因子。")
        if left.get("code") != right.get("code"):
            warnings.append("程式版本或來源內容不同；不能把差異全歸因於模型。")
    modes = (left or {}).get("runMode"), (right or {}).get("runMode")
    latency = not reasons and modes == ("live", "live")
    if modes != ("live", "live"):
        warnings.append("包含重播或未知執行模式，耗時不能作為模型推論延遲比較。")
    if (left or {}).get("settings", {}).get("jobs") != (
        right or {}
    ).get("settings", {}).get("jobs"):
        latency = False
        warnings.append("並行案例數不同，耗時受併發條件影響。")
    if any((provenance or {}).get("cacheStatus", "unknown") == "unknown"
           for provenance in (left, right)):
        warnings.append("服務端快取狀態未知；耗時僅是觀測值，不能視為獨立無快取推論。")
    return {
        "compatible": not reasons, "accuracyCompatible": not reasons,
        "latencyCompatible": latency, "reasons": reasons, "warnings": warnings,
    }
