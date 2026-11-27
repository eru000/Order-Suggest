"""Audit scheduled trials and independently rescore the public (development) outputs."""

import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict

from dataset import PHOTOS, ROOT, SPLITS, validate

sys.path.insert(0, str(ROOT / "evals/menu_ocr"))
from run_eval import score_case  # noqa: E402

ARCHIVE = ROOT / "evals/results/2026-11-26-step2"
METRICS = (
    "expectedCount", "predictedCount", "matchedCount", "exactNameTotalCount",
    "exactNamePriceCount", "priceCorrectCount", "pricedExpectedTotal", "priceInvented",
    "cerStrictEdits", "cerStrictChars",
)


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def protected_path(path):
    path = (ROOT / path).resolve()
    require(path.is_relative_to(ARCHIVE), f"Unexpected artifact location: {path}")
    return path


def pooled(rows):
    counts = {k: sum(r[k] for r in rows) for k in METRICS}
    counts["imageTrials"] = len(rows)
    counts["medianSeconds"] = statistics.median(r["seconds"] for r in rows) if rows else None
    return counts


def main():
    manifest = read(PHOTOS / "manifest.json")
    validate(manifest)
    cases = {r["id"]: r for r in manifest}
    protocol_path = ARCHIVE / "protocol.json"
    protocol = read(protocol_path)
    require(protocol["manifestSha256"] == digest(PHOTOS / "manifest.json"), "Changed manifest")
    expected_keys = {(t["split"], t["caseId"], t["round"]) for t in protocol["tasks"]}
    public = read(ARCHIVE / "public_receipts.json")
    keys = [(r["split"], r["caseId"], r["round"]) for r in public["cells"]]
    require(len(keys) == len(set(keys)) and set(keys) == expected_keys, "Incomplete schedule")
    statuses = defaultdict(Counter)
    trials = []
    http_counts = Counter()
    technical_retries = 0
    verified_archives = 0
    first_attempt_totals = defaultdict(list)
    protocol_sha = digest(protocol_path)
    for cell in public["cells"]:
        split, case_id = cell["split"], cell["caseId"]
        case = cases[case_id]
        require(cell["complete"] and cell["protocolSha256"] == protocol_sha, "Incomplete cell")
        require(cell["truthSha256"] == case["expectedSha256"] and
                cell["inputSha256"] == case["inputSha256"], f"Receipt mismatch: {case_id}")
        statuses[split][cell["technicalStatus"]] += 1
        attempts = cell["attempts"]
        technical_retries += max(0, len(attempts) - 1)
        if not case["inputSha256"]:
            require(cell["technicalStatus"] == "skipped_missing_image" and not attempts, case_id)
            continue
        require(attempts, f"No attempt: {case_id}")
        truth = read(PHOTOS / SPLITS[split] / f"{case_id}.expected.json")
        for pos, attempt in enumerate(attempts):
            http_counts[split] += attempt.get("transportRequestCount") or 0
            if attempt.get("artifactPath"):
                artifact = protected_path(attempt["artifactPath"])
                require(digest(artifact) == attempt["artifactSha256"], f"Changed {artifact.name}")
            else:
                artifact = None
            if attempt.get("resultCaseIdsMatch"):
                require(attempt["sourceImageSha256"] == case["inputSha256"], case_id)
            if split == "holdout":
                require(artifact is not None and artifact.suffix == ".zip" and attempt["sealed"],
                        f"Unsealed {case_id}")
                verified_archives += 1
                continue
            require(not attempt.get("sealed"), case_id)
            row = None
            if artifact and attempt.get("resultCaseIdsMatch"):
                run = read(artifact)
                require(run["config"]["matchThreshold"] == protocol["settings"]["threshold"],
                        f"Threshold mismatch: {case_id}")
                require(all(protocol["sourceHashes"].get(k) == v
                            for k, v in run["provenance"]["code"]["sourceHashes"].items()),
                        f"Source changed: {case_id}")
                row, = run["cases"]
                require(row["case"] == case_id and row["truth"] == truth, f"Truth drift: {case_id}")
            items = ((row or {}).get("raw") or {}).get("items", [])
            actual = score_case({"name": case_id, "expected": truth},
                                {"categories": [{"name": "", "items": items}]},
                                protocol["settings"]["threshold"])
            if row:
                require(all(actual[k] == row[k] for k in METRICS), f"Rescore differs: {case_id}")
            counts = {k: actual[k] for k in METRICS}
            entry = dict(case=case_id, split=split, round=cell["round"], parent=case["parent"],
                         seconds=(row or {}).get("durationSec", attempt["elapsedSec"]),
                         status=attempt["technicalStatus"], **counts)
            if pos == 0:
                first_attempt_totals[split].append(entry)
            if pos == len(attempts) - 1:
                entry["outputSha256"] = hashlib.sha256(
                    json.dumps(items, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest()
                trials.append(entry)
    dev = [r for r in trials if r["split"] == "dev"]
    stress = [r for r in trials if r["split"] == "stress"]
    parents = {r["parent"] for r in stress}
    groups = defaultdict(list)
    for r in dev:
        groups[r["case"]].append(r)
    by_effect = defaultdict(list)
    for r in stress:
        by_effect[r["case"][len(r["parent"]) + 1:]].append(r)
    result = {
        "protocolSha256": protocol_sha, "manifestSha256": protocol["manifestSha256"],
        "completedCells": len(keys), "statuses": statuses, "recordedHttpAttempts": http_counts,
        "additionalTechnicalAttempts": technical_retries, "verifiedSealedFiles": verified_archives,
        "holdoutPredictionsRead": False, "publicRescoredTrials": len(trials),
        "firstScheduledAttempts": {k: pooled(v) for k, v in first_attempt_totals.items()},
        "developmentAllRounds": pooled(dev),
        "developmentByRound": {i: pooled([r for r in dev if r["round"] == i]) for i in (1, 2, 3)},
        "matchedCleanFirstRound": pooled([r for r in dev if r["case"] in parents
                                           and r["round"] == 1]),
        "stressByEffect": {k: pooled(v) for k, v in sorted(by_effect.items())},
        "perDevelopmentMenu": {k: {"uniqueOrderedOutputs": len({r["outputSha256"] for r in v}),
                                   "rounds": len(v), **pooled(v)} for k, v in groups.items()},
        "publicTrials": trials,
    }
    out = ARCHIVE / "public_analysis.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {len(keys)} cells; rescored {len(trials)} public trials; "
          f"verified {verified_archives} sealed ZIP hashes without opening them.")


if __name__ == "__main__":
    main()
