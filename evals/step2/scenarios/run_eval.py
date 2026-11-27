"""Frozen demand scenarios: live production rules for dev, static truth oracle for holdout.

No API, model, .env, database, product mutation or holdout product execution is allowed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CATEGORIES = {"compound", "budget_change", "cancel_condition", "replace", "no_solution"}
ATTEMPTS = {"network": 0, "env": 0}


def guard(event: str, args: tuple) -> None:
    if event in {"socket.connect", "socket.getaddrinfo", "urllib.Request"}:
        ATTEMPTS["network"] += 1
        raise RuntimeError("scenario evaluation forbids network/model requests")
    if event == "open" and args and isinstance(args[0], (str, bytes)):
        name = Path(str(args[0])).name
        if name.startswith(".env"):
            ATTEMPTS["env"] += 1
            raise RuntimeError("scenario evaluation forbids .env reads")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def display_name(item: dict) -> str:
    variant = item.get("variant")
    return str(item["name"]) + (f"（{variant}）" if variant else "")


def candidates(items: list[dict], constraints: dict) -> list[dict]:
    """Independent oracle: only printed prices, names and explicit variants.

    This does not call the product's parsers, eligibility, taxonomy or ranking.
    It makes no assumptions about unknown ingredients, portions or taste.
    """
    result = []
    for item in items:
        name, price = str(item["name"]), item.get("price")
        if constraints.get("requireKnownPrice") and price is None:
            continue
        if constraints.get("budget") is not None and (
            price is None or price > constraints["budget"]
        ):
            continue
        if any(
            token.casefold() in name.casefold() for token in constraints.get("excludeTokens", [])
        ):
            continue
        if constraints.get("nameAny") and not any(
            token in name for token in constraints["nameAny"]
        ):
            continue
        if constraints.get("variant") and item.get("variant") != constraints["variant"]:
            continue
        result.append(item)
    return result


def validate() -> tuple[dict, dict, dict]:
    manifest = read(HERE / "frozen_manifest.json")
    datasets = {}
    for name in ("development.json", "holdout.json"):
        if digest(HERE / name) != manifest["scenarioFiles"][name]:
            raise ValueError(f"Frozen scenario file changed: {name}; create a new version")
        datasets[name] = read(HERE / name)
    groups: dict[str, set[str]] = {"dev": set(), "holdout": set()}
    ids = set()
    for filename, dataset in datasets.items():
        split = "dev" if filename == "development.json" else "holdout"
        counts = {category: 0 for category in CATEGORIES}
        for sample in dataset["cases"]:
            if (
                sample["id"] in ids
                or sample["split"] != split
                or sample["category"] not in CATEGORIES
            ):
                raise ValueError(f"Invalid or duplicate scenario: {sample['id']}")
            ids.add(sample["id"])
            counts[sample["category"]] += 1
            path = (ROOT / sample["menu"]).resolve()
            if not path.is_relative_to((ROOT / "evals/step2/menus" / split).resolve()):
                raise ValueError("Scenario must bind to the same split's truth menu")
            if digest(path) != sample["truthSha256"]:
                raise ValueError(f"Bound truth changed: {sample['id']}")
            groups[split].add(str(read(path.parent / "source.json")["group"]))
            if len(sample["turns"]) < 3:
                raise ValueError("Every frozen scenario must retain three turns")
        required = 1 if split == "dev" else 2
        if any(count < required for count in counts.values()):
            raise ValueError(f"Missing scenario category coverage: {counts}")
    overlap = groups["dev"] & groups["holdout"]
    if overlap:
        raise ValueError(f"Restaurant/menu groups cross splits: {sorted(overlap)}")
    return (
        datasets["development.json"],
        datasets["holdout.json"],
        {
            "frozenFilesVerified": True,
            "truthHashesVerified": True,
            "developmentCases": len(datasets["development.json"]["cases"]),
            "holdoutCases": len(datasets["holdout.json"]["cases"]),
            "developmentGroups": sorted(groups["dev"]),
            "holdoutGroups": sorted(groups["holdout"]),
            "groupOverlap": sorted(overlap),
        },
    )


def static_eval(dataset: dict) -> dict:
    records = []
    for sample in dataset["cases"]:
        truth = read(ROOT / sample["menu"])
        turns = []
        for number, turn in enumerate(sample["turns"], 1):
            eligible = candidates(truth["items"], turn["constraints"])
            feasible = bool(eligible)
            turns.append(
                {
                    "turn": number,
                    "text": turn["text"],
                    "action": turn["action"],
                    "constraints": turn["constraints"],
                    "expectedSolution": turn["expectSolution"],
                    "eligibleCount": len(eligible),
                    "distinctEligibleNames": len({row["name"] for row in eligible}),
                    "feasible": feasible,
                    "passed": feasible == turn["expectSolution"],
                    "replacementHasAlternative": len({row["name"] for row in eligible}) > 1
                    if turn["action"] == "replace"
                    else None,
                }
            )
        records.append(
            {
                "id": sample["id"],
                "category": sample["category"],
                "menu": sample["menu"],
                "turns": turns,
                "passed": all(row["passed"] for row in turns),
            }
        )
    flat = [turn for record in records for turn in record["turns"]]
    return {
        "mode": "independent_static_truth_feasibility",
        "productExecuted": False,
        "caseCount": len(records),
        "passedCases": sum(row["passed"] for row in records),
        "turnCount": len(flat),
        "passedTurns": sum(row["passed"] for row in flat),
        "cases": records,
    }


@dataclass
class MemoryState:
    active_restaurant: str
    prefs: dict = field(default_factory=dict)
    decision: dict = field(default_factory=dict)
    history: list = field(default_factory=list)
    lock: Any = field(default_factory=threading.RLock)


class MemorySessions:
    """SessionStore protocol adapter; production DecisionService is unmodified."""

    def __init__(self, restaurant: str):
        self.state = MemoryState(restaurant)

    def get(self, _session_id: str, _default: str = "") -> MemoryState:
        return self.state

    def save(self, _session_id: str) -> None:
        return None


def check_preferences(actual: dict, expected: dict) -> list[str]:
    errors = []
    for key, value in expected.items():
        if key == "absent":
            errors.extend(
                f"preference {field} was not removed" for field in value if field in actual
            )
        elif key == "excludesContains":
            errors.extend(
                f"missing exclusion {token}"
                for token in value
                if not any(token in str(entry) for entry in actual.get("excludes", []))
            )
        elif key == "excludesAbsent":
            errors.extend(
                f"exclusion {token} was not removed"
                for token in value
                if any(token in str(entry) for entry in actual.get("excludes", []))
            )
        elif actual.get(key) != value:
            errors.append(f"preference {key}: expected {value!r}, observed {actual.get(key)!r}")
    return errors


def development_eval(dataset: dict, *, allowed_split: str = "dev") -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from decision_service import DecisionService

    records, call_count = [], 0
    for sample in dataset["cases"]:
        # Intentionally never accept a holdout dataset on this execution path.
        if sample["split"] != allowed_split:
            raise ValueError("Wrong split on the product execution path")
        truth = read(ROOT / sample["menu"])
        restaurant = Path(sample["menu"]).parent.name
        sessions = MemorySessions(restaurant)
        menu = {
            "serviceRate": 0,
            "categories": [
                {
                    "name": "原始標註區塊",
                    "items": [
                        {"name": display_name(row), "price": row.get("price")}
                        for row in truth["items"]
                    ],
                }
            ],
        }
        service = DecisionService(sessions, {restaurant: menu}, restaurant)
        view = None
        rejected_ids: set[str] = set()
        turns = []
        for number, turn in enumerate(sample["turns"], 1):
            calls, errors, technical_errors = [], [], []
            previous = view
            target_id = None
            try:
                kwargs = {
                    "action": turn["action"],
                    "text": turn["text"],
                    "revision": view["revision"] if view else None,
                }
                if turn["action"] == "replace":
                    if view and view.get("items"):
                        target_id = view["items"][0]["id"]
                        kwargs["item_id"] = target_id
                        rejected_ids.add(target_id)
                    else:
                        # Still exercise the normal service action. No fabricated
                        # recommendation is supplied to rescue an unavailable target.
                        kwargs["item_id"] = "__no_displayed_item__"
                call_count += 1
                view = service.handle(sample["id"], **kwargs)
                calls.append({"input": kwargs, "output": view})
                # The protocol explicitly presses the existing recommend button if
                # clarification is offered; it never changes any declared constraint.
                if view.get("type") == "question":
                    followup = {"action": "recommend", "revision": view["revision"]}
                    call_count += 1
                    view = service.handle(sample["id"], **followup)
                    calls.append({"input": followup, "output": view})
                allowed = {
                    display_name(row) for row in candidates(truth["items"], turn["constraints"])
                }
                items = view.get("items") or []
                if turn["expectSolution"]:
                    if not items or view.get("type") == "no_match":
                        errors.append("no actionable recommendation despite a known feasible item")
                elif items or view.get("type") != "no_match":
                    errors.append("expected explicit no_match with no items")
                for item in items:
                    if item["name"] not in allowed:
                        errors.append("item violates independent oracle: " + item["name"])
                    limit = turn["constraints"].get("budget")
                    if limit is not None and (item.get("total") is None or item["total"] > limit):
                        errors.append("unknown or over-budget amount: " + item["name"])
                actual_prefs = sessions.state.decision.get("prefs", {})
                errors.extend(check_preferences(actual_prefs, turn.get("expectPrefs", {})))
                if target_id is not None:
                    ids = {item["id"] for item in items}
                    if ids & rejected_ids:
                        errors.append("replacement repeats a rejected item")
                    preserved = {item["id"] for item in previous.get("items", [])[1:]}
                    if not preserved.issubset(ids):
                        errors.append("replacement removed an untargeted displayed item")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                technical_errors.append(f"{type(exc).__name__}: {exc}")
                calls.append({"input": kwargs, "exception": technical_errors[-1]})
                actual_prefs = sessions.state.decision.get("prefs", {})
            turns.append(
                {
                    "turn": number,
                    "expected": turn,
                    "calls": calls,
                    "observedPreferences": dict(actual_prefs),
                    "errors": errors,
                    "technicalErrors": technical_errors,
                    "passed": not errors,
                }
            )
        records.append(
            {
                "id": sample["id"],
                "category": sample["category"],
                "menu": sample["menu"],
                "turns": turns,
                "passed": all(row["passed"] for row in turns),
            }
        )
    flat = [turn for record in records for turn in record["turns"]]
    return {
        "mode": "production_DecisionService_" + allowed_split,
        "productExecuted": True,
        "sessionAdapter": "in-memory SessionStore protocol; no persistence or UI routing",
        "caseCount": len(records),
        "passedCases": sum(row["passed"] for row in records),
        "turnCount": len(flat),
        "passedTurns": sum(row["passed"] for row in flat),
        "serviceCallCount": call_count,
        "cases": records,
    }


def _stable_behavior(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _stable_behavior(item)
            for key, item in value.items()
            if key
            not in {
                "revision",
                "mealId",
                "generatedAt",
                "startedAt",
                "finishedAt",
                "timestamp",
                "durationMs",
                "durationSec",
            }
        }
    if isinstance(value, list):
        return [_stable_behavior(item) for item in value]
    return value


def seal_blind(dataset: dict, evidence: dict, repetitions: int) -> dict:
    """Only public execution receipts leave this function; scores stay in ZIPs."""
    public_runs = []
    fingerprints = []
    for repetition in range(1, repetitions + 1):
        started = datetime.now(UTC).isoformat()
        sealed = development_eval(dataset, allowed_split="holdout")
        finished = datetime.now(UTC).isoformat()
        stable = json.dumps(_stable_behavior(sealed), ensure_ascii=False, sort_keys=True)
        behavior_hash = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        fingerprints.append(behavior_hash)
        payload = {
            **evidence,
            "startedAt": started,
            "finishedAt": finished,
            "execution": sealed,
            "holdoutProductExecuted": True,
            "policy": "Blind archive: no human inspection of outputs or scores.",
        }
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archive = HERE / f"holdout_blind_{stamp}_repeat{repetition}.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(
                "sealed_results.json",
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            )
            for name in (
                "development.json", "holdout.json", "frozen_manifest.json",
                "blind_protocol.json", "run_eval.py",
            ):
                bundle.write(HERE / name, "protocol/" + name)
            for name in evidence["productSourceHashes"]:
                bundle.write(ROOT / "src" / name, "engine/" + name)
            for sample in dataset["cases"]:
                path = ROOT / sample["menu"]
                bundle.write(path, "menus/" + path.parent.name + "/truth.json")
                bundle.write(
                    path.parent / "source.json", "menus/" + path.parent.name + "/source.json"
                )
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError("Blind archive failed CRC integrity verification")
        cases_receipt = []
        for record in sealed["cases"]:
            cases_receipt.append(
                {
                    "id": record["id"],
                    "scriptedTurns": len(record["turns"]),
                    "actualServiceCalls": sum(len(turn["calls"]) for turn in record["turns"]),
                    "technicalFailureCount": sum(
                        len(turn["technicalErrors"]) for turn in record["turns"]
                    ),
                }
            )
        current_hashes = {
            name: digest(ROOT / "src" / name) for name in evidence["productSourceHashes"]
        }
        public_runs.append(
            {
                "repetition": repetition,
                "startedAt": started,
                "finishedAt": finished,
                "caseCount": len(sealed["cases"]),
                "scriptedTurnCount": sealed["turnCount"],
                "actualServiceCalls": sealed["serviceCallCount"],
                "unexecutedTurns": 0,
                "technicalFailureCount": sum(row["technicalFailureCount"] for row in cases_receipt),
                "cases": cases_receipt,
                "archive": archive.name,
                "archiveSha256": digest(archive),
                "behaviorSha256": behavior_hash,
                "archiveCrcVerified": True,
                "engineUnchangedDuringExecution": current_hashes == evidence["productSourceHashes"],
            }
        )
    receipt = {
        "mode": "sealed_holdout_blind_execution",
        "outputsAndScoresInspected": False,
        "scenariosChangedAfterFreeze": False,
        "repetitions": repetitions,
        "sameConditionReproduction": len(set(fingerprints)) == 1 if repetitions > 1 else None,
        "fingerprintNormalization": "Remove random revision/mealId and time/duration fields only",
        "scenarioHashes": evidence["scenarioHashes"],
        "runnerSha256": evidence["runnerSha256"],
        "productSourceHashes": evidence["productSourceHashes"],
        "blockedAttempts": dict(ATTEMPTS),
        "runs": public_runs,
        "limitations": [
            "Repeatability does not establish correctness or independent new samples.",
            "Outputs and automatic scores remain uninspected in the sealed archives.",
            "In-memory sessions and printed base-price truth adapter; no UI or ASR/vision model.",
        ],
    }
    (HERE / "holdout_blind_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["validate", "dev", "holdout-static", "holdout-blind", "all"],
        default="all",
    )
    parser.add_argument("--blind-repetitions", type=int, default=1)
    parser.add_argument("--output", type=Path, default=HERE / "results.json")
    args = parser.parse_args()
    if args.blind_repetitions < 1:
        parser.error("--blind-repetitions must be at least one")
    sys.addaudithook(guard)
    dev, holdout, validation = validate()
    output = {
        "generatedAt": datetime.now(UTC).isoformat(),
        "validation": validation,
        "runnerSha256": digest(Path(__file__)),
        "scenarioHashes": {
            name: digest(HERE / name) for name in ("development.json", "holdout.json")
        },
        "productSourceHashes": {
            name: digest(ROOT / "src" / name)
            for name in (
                "decision_service.py",
                "decision_catalog.py",
                "decision_preferences.py",
                "preference_engine.py",
                "recommendation.py",
                "table_menu.py",
                "menu_semantics.py",
                "menu_vocabulary.py",
            )
        },
        "limitations": [
            "Developer-authored objective scenarios, not user satisfaction or external validation.",
            "Printed single-item base prices; service fees, portion sufficiency and "
            "unprinted ingredients are outside this benchmark.",
            "Development has only two source menus; scripted turns are not independent users.",
            "Holdout static feasibility validates the dataset, not product generalization.",
            "If asked a question, press recommend without relaxing constraints.",
        ],
    }
    if args.mode == "holdout-blind":
        receipt = seal_blind(holdout, output, args.blind_repetitions)
        print(
            json.dumps(
                {
                    "receipt": "holdout_blind_receipt.json",
                    "runs": [
                        {
                            key: run[key]
                            for key in (
                                "repetition",
                                "caseCount",
                                "scriptedTurnCount",
                                "actualServiceCalls",
                                "unexecutedTurns",
                                "technicalFailureCount",
                                "archive",
                                "archiveSha256",
                            )
                        }
                        for run in receipt["runs"]
                    ],
                    "sameConditionReproduction": receipt["sameConditionReproduction"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.mode in {"all", "dev"}:
        output["developmentStatic"] = static_eval(dev)
        output["development"] = development_eval(dev)
    if args.mode in {"all", "holdout-static"}:
        output["holdoutStatic"] = static_eval(holdout)
    output["blockedAttempts"] = dict(ATTEMPTS)
    output["holdoutProductExecuted"] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    summary = {
        key: {field: value for field, value in output[key].items() if field != "cases"}
        for key in ("development", "holdoutStatic")
        if key in output
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    failed = any(
        output[key]["passedTurns"] != output[key]["turnCount"]
        for key in ("development", "developmentStatic", "holdoutStatic")
        if key in output
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
