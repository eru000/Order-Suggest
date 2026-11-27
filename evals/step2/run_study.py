"""Resume a frozen step-2 study; holdout predictions are sealed without inspection."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "菜單照片資料" / "第二項"
RESULTS = ROOT / "evals" / "results" / "2026-11-26-step2"
RUNNER = ROOT / "evals" / "menu_ocr" / "run_eval.py"
SPLITS = {"dev": ("開發原圖", 3), "stress": ("開發合成干擾", 1), "holdout": ("獨立封存", 3)}
TAIPEI = timezone(timedelta(hours=8))
MODEL = "ornith-35b"


def now() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    temporary.replace(path)


def import_runner() -> Any:
    spec = importlib.util.spec_from_file_location("step2_menu_runner", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_hashes() -> dict[str, str]:
    paths = [*ROOT.joinpath("src").rglob("*.py"), *RUNNER.parent.glob("*.py"), Path(__file__)]
    return {p.relative_to(ROOT).as_posix(): sha(p) for p in sorted(set(paths))}


def make_protocol(jobs: int) -> dict[str, Any]:
    manifest_path = DATA / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runner = import_runner()
    vision = runner._get_menu_vision()
    settings = {
        "model": MODEL,
        "verifyModel": MODEL,
        "threshold": 0.72,
        "debug": True,
        "workerConcurrency": jobs,
        "runnerJobsPerWorker": 1,
        "visionLongSide": os.getenv("VISION_LONG_SIDE", str(vision.TARGET_LONG_SIDE)),
        "visionTiles": os.getenv("VISION_TILES", "0"),
        "visionTemperature": vision._float_env("VISION_TEMPERATURE", 0.0),
        "visionPriceText": os.getenv("VISION_PRICE_TEXT", "0"),
        "tileOverlap": vision.TILE_OVERLAP,
        "tileThreshold": vision.TILE_THRESHOLD,
        "fast": False,
        "cacheStatus": "unknown",
        "repeatsAreIndependent": False,
    }
    tasks = []
    for split, (directory, rounds) in SPLITS.items():
        selected = [row for row in manifest if row["split"] == split]
        for round_index in range(1, rounds + 1):
            for row in selected:
                case_id = row["id"]
                expected = DATA / directory / f"{case_id}.expected.json"
                if sha(expected) != row["expectedSha256"]:
                    raise ValueError(f"Frozen expected hash mismatch: {case_id}")
                images = [
                    path
                    for path in (DATA / directory).glob(f"{case_id}.*")
                    if path.suffix.lower() in runner.IMAGE_SUFFIXES
                ]
                if len(images) > 1:
                    raise ValueError(f"Ambiguous input image: {case_id}")
                digest = sha(images[0]) if images else None
                if digest != row.get("inputSha256"):
                    raise ValueError(f"Frozen image hash mismatch: {case_id}")
                tasks.append(
                    {
                        "caseId": case_id,
                        "split": split,
                        "round": round_index,
                        "casesDir": str((DATA / directory).relative_to(ROOT)),
                        "inputSha256": digest,
                        "truthSha256": row["expectedSha256"],
                        "plannedStatus": "ready" if images else "skipped_missing_image",
                    }
                )
    ready = sum(task["plannedStatus"] == "ready" for task in tasks)
    if ready != 69 or len(tasks) != 78:
        raise ValueError(f"Unexpected frozen plan: {ready} ready / {len(tasks)} total")
    return {
        "studyId": "2026-11-26-step2",
        "createdAt": now(),
        "manifestSha256": sha(manifest_path),
        "manifestPath": str(manifest_path.relative_to(ROOT)),
        "settings": settings,
        "sourceHashes": source_hashes(),
        "tasks": tasks,
        "plannedLiveImageRounds": ready,
        "plannedMissingImageSkips": len(tasks) - ready,
        "retryPolicy": {
            "maxAdditionalAttempts": 1,
            "backoffSeconds": 3,
            "eligible": "technical transport/process failure only; never recognition scores",
        },
        "holdoutPolicy": "Seal full runner JSON and logs into ZIP. Public receipts whitelist only technical status, IDs, calls and hashes; do not inspect predictions or scores.",
    }


def cell_id(task: dict[str, Any]) -> str:
    return f"{task['split']}__{task['caseId']}__r{task['round']}"


def worker(args: argparse.Namespace) -> int:
    runner = import_runner()
    runner.RUNS_DIR = Path(args.work_dir)
    sys.argv = [
        str(RUNNER),
        "--cases-dir",
        args.cases_dir,
        "--case",
        args.case_id,
        "--label",
        args.label,
        "--model",
        MODEL,
        "--verify-model",
        MODEL,
        "--threshold",
        "0.72",
        "--debug",
        "--jobs",
        "1",
    ]
    return runner.main()


def technical_kind(row: dict[str, Any]) -> str:
    status = row.get("status") or ("ok" if row.get("ok") else "error")
    if status != "error":
        return status
    # Classification only. Never publish the underlying error body or trace.
    message = str(row.get("error") or "").casefold()
    if any(
        marker in message
        for marker in (
            "http",
            "urlerror",
            "timeout",
            "timed out",
            "connection",
            "ssl",
            "remote",
            "network",
            "api request",
        )
    ):
        return "transport_error"
    return "pipeline_error"


def run_attempt(task: dict[str, Any], attempt: int) -> dict[str, Any]:
    cell = cell_id(task)
    work = RESULTS / ".private_work" / f"{cell}__a{attempt}"
    work.mkdir(parents=True, exist_ok=True)
    started = now()
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        "--worker",
        "--work-dir",
        str(work),
        "--cases-dir",
        str(ROOT / task["casesDir"]),
        "--case-id",
        task["caseId"],
        "--label",
        f"{cell}__a{attempt}",
    ]
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    tick = time.perf_counter()
    with (
        (work / "stdout.log").open("w", encoding="utf-8") as out,
        (work / "stderr.log").open("w", encoding="utf-8") as err,
    ):
        try:
            result = subprocess.run(
                command, cwd=ROOT, env=environment, stdout=out, stderr=err, check=False, timeout=600
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = -9
    run_paths = sorted(work.glob("*.json"))
    receipt: dict[str, Any] = {
        "attempt": attempt,
        "startedAt": started,
        "finishedAt": now(),
        "elapsedSec": round(time.perf_counter() - tick, 3),
        "workerExitCode": exit_code,
        "technicalStatus": "process_error",
        "resultCaseIdsMatch": False,
        "wrapperCallCount": None,
        "transportRequestCount": None,
        "requestHashes": [],
    }
    run_path = run_paths[0] if len(run_paths) == 1 else None
    if run_path is not None:
        # Deliberately whitelist technical fields, particularly for holdout.
        run = json.loads(run_path.read_text(encoding="utf-8"))
        rows = run.get("cases") or []
        receipt["resultCaseIdsMatch"] = [row.get("case") for row in rows] == [task["caseId"]]
        if len(rows) == 1 and receipt["resultCaseIdsMatch"]:
            row = rows[0]
            receipt["technicalStatus"] = technical_kind(row)
            exchanges = row.get("exchanges") or []
            transport = row.get("transport") or {}
            receipt["wrapperCallCount"] = len(exchanges)
            receipt["transportRequestCount"] = transport.get("requestCount")
            receipt["requestHashes"] = [
                entry.get("requestSha256") for entry in exchanges if entry.get("requestSha256")
            ]
            receipt["transportBodyHashes"] = [
                entry.get("bodySha256")
                for entry in transport.get("requests", [])
                if entry.get("bodySha256")
            ]
            receipt["sourceImageSha256"] = (
                (row.get("provenance") or {}).get("sourceImage", {}).get("sha256")
            )
            receipt["truthSnapshotSha256"] = (
                (row.get("provenance") or {}).get("truth", {}).get("sha256")
            )
        receipt["runSha256"] = sha(run_path)
        del run
    if task["split"] == "holdout":
        artifact = RESULTS / "sealed" / f"{cell}__a{attempt}.zip"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(work.iterdir()):
                if path.is_file():
                    archive.write(path, arcname=path.name)
        receipt.update(
            artifactPath=str(artifact.relative_to(ROOT)), artifactSha256=sha(artifact), sealed=True
        )
        # Only this explicitly-created cell directory is cleaned, non-recursively.
        if work.resolve().parent != (RESULTS / ".private_work").resolve():
            raise ValueError("Unsafe work directory")
        for path in work.iterdir():
            if path.is_file():
                path.unlink()
        work.rmdir()
    else:
        destination = (
            RESULTS / task["split"] / f"{task['caseId']}__r{task['round']}" / f"attempt-{attempt}"
        )
        destination.mkdir(parents=True, exist_ok=True)
        for path in list(work.iterdir()):
            if path.is_file():
                path.replace(destination / path.name)
        work.rmdir()
        if run_path is not None:
            artifact = destination / run_path.name
            receipt.update(
                artifactPath=str(artifact.relative_to(ROOT)),
                artifactSha256=sha(artifact),
                sealed=False,
            )
    return receipt


def run_cell(task: dict[str, Any], protocol_hash: str) -> dict[str, Any]:
    target = RESULTS / "receipts" / f"{cell_id(task)}.json"
    prior = None
    if target.is_file():
        prior = json.loads(target.read_text(encoding="utf-8"))
        if prior.get("protocolSha256") != protocol_hash:
            raise ValueError("Receipt protocol mismatch")
        if prior.get("complete"):
            return prior
    receipt = prior or {**task, "protocolSha256": protocol_hash, "attempts": [], "complete": False}
    if task["plannedStatus"] == "skipped_missing_image":
        receipt.update(complete=True, technicalStatus="skipped_missing_image", finishedAt=now())
        write_json(target, receipt)
        return receipt
    attempts = receipt["attempts"]
    if attempts and (
        attempts[-1]["technicalStatus"] not in {"transport_error", "process_error"}
        or len(attempts) >= 2
    ):
        receipt.update(complete=True, finishedAt=now())
        write_json(target, receipt)
        return receipt
    for attempt in range(len(attempts) + 1, 3):
        observation = run_attempt(task, attempt)
        receipt["attempts"].append(observation)
        receipt["technicalStatus"] = observation["technicalStatus"]
        write_json(target, receipt)
        if (
            observation["technicalStatus"] not in {"transport_error", "process_error"}
            or attempt == 2
        ):
            break
        time.sleep(3)
    receipt.update(complete=True, finishedAt=now())
    write_json(target, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    for name in ("work-dir", "cases-dir", "case-id", "label"):
        parser.add_argument("--" + name, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker(args)
    RESULTS.mkdir(parents=True, exist_ok=True)
    protocol_path = RESULTS / "protocol.json"
    if protocol_path.is_file():
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        if (
            protocol["manifestSha256"] != sha(DATA / "manifest.json")
            or protocol["sourceHashes"] != source_hashes()
        ):
            raise ValueError("Frozen study code or manifest changed; do not overwrite the protocol")
        if protocol["settings"]["workerConcurrency"] != args.jobs:
            raise ValueError("Resume concurrency differs from frozen protocol")
    else:
        protocol = make_protocol(args.jobs)
        write_json(protocol_path, protocol)
    protocol_hash = sha(protocol_path)
    print(
        f"Frozen plan: 69 image-rounds + 9 missing-image skips; jobs={args.jobs}; protocol={protocol_hash}",
        flush=True,
    )
    if args.plan_only:
        return 0
    completed = {}
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(run_cell, task, protocol_hash): task for task in protocol["tasks"]}
        for future in as_completed(futures):
            row = future.result()
            completed[cell_id(row)] = row
            ordered = [
                completed[cell_id(task)] for task in protocol["tasks"] if cell_id(task) in completed
            ]
            write_json(
                RESULTS / "public_receipts.json",
                {
                    "studyId": protocol["studyId"],
                    "protocolSha256": protocol_hash,
                    "updatedAt": now(),
                    "plannedCells": len(protocol["tasks"]),
                    "completedCells": len(ordered),
                    "cells": ordered,
                },
            )
            print(f"[{len(completed)}/78] {cell_id(row)} {row['technicalStatus']}", flush=True)
    expected_ids = {cell_id(task) for task in protocol["tasks"]}
    all_done = set(completed) == expected_ids
    write_json(
        RESULTS / "completion.json",
        {
            "finishedAt": now(),
            "protocolSha256": protocol_hash,
            "plannedCellIdsMatch": all_done,
            "completedCells": len(completed),
            "plannedLiveImageRounds": 69,
            "plannedMissingImageSkips": 9,
            "technicalStatuses": {
                status: sum(row["technicalStatus"] == status for row in completed.values())
                for status in sorted({row["technicalStatus"] for row in completed.values()})
            },
            "holdoutScoresInspected": False,
        },
    )
    return 0 if all_done else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
