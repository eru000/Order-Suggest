"""Offline evaluation regressions: never initialize the API client or read .env."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from evals.menu_ocr import run_eval as evaluator  # noqa: E402


def case(items: list[dict], name: str = "menu") -> dict:
    return {"name": name, "expected": {"items": items}}


def result(items: list[dict]) -> dict:
    return {"categories": [{"name": "全部", "items": items}]}


def success(sample: dict, items: list[dict], duration: float = 1.0) -> dict:
    return {
        "case": sample["name"],
        "truth": sample["expected"],
        "ok": True,
        "status": "ok",
        "durationSec": duration,
        "raw": {"items": items},
        **evaluator.score_case(sample, result(items), 0.72),
    }


class MenuOcrScoringTests(unittest.TestCase):
    def test_missed_items_stay_in_full_truth_denominators(self):
        sample = case([{"name": "牛肉麵", "price": 100}, {"name": "雞腿飯", "price": 120}])
        scored = evaluator.score_case(sample, result(sample["expected"]["items"][:1]), 0.72)
        self.assertEqual(scored["exactNameRate"], 1)
        self.assertEqual(scored["priceAccuracy"], 1)
        self.assertEqual(scored["exactNameRecall"], 0.5)
        self.assertEqual(scored["exactNamePriceRate"], 0.5)
        self.assertEqual(scored["priceRecall"], 0.5)
        self.assertEqual(scored["pricedExpectedTotal"], 2)

    def test_entire_failure_contributes_truth_and_time(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        good = success(sample, sample["expected"]["items"], 2)
        failed = evaluator.failure_row(sample, "error", "timeout", 7, 0.72)
        summary = evaluator.aggregate([good, failed])
        self.assertEqual(summary["expectedCount"], 2)
        self.assertEqual(summary["exactNamePriceRate"], 0.5)
        self.assertEqual(summary["priceRecall"], 0.5)
        self.assertEqual(summary["cerStrict"], 0.5)
        self.assertEqual(summary["failedCount"], 1)
        self.assertEqual(summary["totalDurationSec"], 9)

    def test_missing_images_reduce_availability_not_model_accuracy(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        missing = evaluator.failure_row(sample, "skipped", "missing_image", 0, 0.72)
        summary = evaluator.aggregate([success(sample, sample["expected"]["items"]), missing])
        self.assertEqual(summary["exactNamePriceRate"], 1)
        self.assertEqual(summary["expectedCount"], 1)
        self.assertEqual(summary["declaredExpectedCount"], 2)
        self.assertEqual(summary["failedCount"], 0)
        self.assertEqual(summary["availableCaseRate"], 0.5)
        self.assertFalse(summary["datasetComplete"])

    def test_all_empty_results_are_reportable_failures(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        failed = evaluator.failure_row(sample, "empty", "empty", 3, 0.72)
        summary = evaluator.aggregate([failed])
        self.assertEqual(summary["exactNamePriceRate"], 0)
        self.assertEqual(summary["priceRecall"], 0)
        self.assertEqual(summary["cerStrict"], 1)
        self.assertEqual(summary["emptyCount"], 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            evaluator.print_report(
                {
                    "label": "empty",
                    "config": {"visionModel": "x", "verifyModel": "x", "fast": False},
                    "cases": [failed],
                    "summary": summary,
                },
                False,
            )
        self.assertIn("0/1", output.getvalue())
        self.assertIn("全真值菜名完整率", output.getvalue())

    def test_legacy_failure_without_denominator_never_yields_success_only_rate(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        summary = evaluator.aggregate(
            [success(sample, sample["expected"]["items"]), {"case": "lost", "ok": False}]
        )
        self.assertEqual(summary["unknownTruthCount"], 1)
        self.assertIsNone(summary["recall"])
        self.assertIsNone(summary["exactNamePriceRate"])

    def test_strict_counts_are_one_to_one_and_threshold_independent(self):
        sample = case([{"name": "牛肉麵", "price": 100}, {"name": "雞腿飯", "price": 120}])
        guessed = [
            {"name": "牛肉麵", "price": 100},
            {"name": "牛肉麵", "price": 100},
            {"name": "雞腿", "price": 120},
        ]
        low = evaluator.score_case(sample, result(guessed), 0.5)
        high = evaluator.score_case(sample, result(guessed), 1.0)
        self.assertEqual(low["exactNamePriceCount"], 1)
        self.assertEqual(low["exactNamePriceRate"], high["exactNamePriceRate"])
        self.assertLess(low["precision"], 1)

    def test_exact_names_win_ties_against_character_reorderings(self):
        items = [{"name": "牛肉炒飯", "price": 50}, {"name": "炒牛肉飯", "price": 80}]
        sample = case(items)
        scored = evaluator.score_case(sample, result(list(reversed(items))), 0.72)
        self.assertEqual(scored["priceRecall"], 1)
        self.assertEqual(scored["exactNameRate"], 1)

    def test_unknown_price_preservation_is_distinct_from_numeric_recognition(self):
        sample = case([{"name": "時價魚", "price": None}, {"name": "雞腿飯", "price": 120}])
        correct = evaluator.score_case(sample, result(sample["expected"]["items"]), 0.72)
        self.assertEqual(correct["exactNamePriceRate"], 1)
        self.assertEqual(correct["pricedExpectedTotal"], 1)
        invented = evaluator.score_case(sample, result([{"name": "時價魚", "price": 300}]), 0.72)
        self.assertEqual(invented["priceInvented"], 1)
        self.assertEqual(invented["exactNamePriceCount"], 0)

    def test_load_cases_includes_truths_with_missing_images(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.expected.json").write_text(
                json.dumps({"items": [{"name": "牛肉麵", "price": 100}]}), encoding="utf-8"
            )
            (folder / "b.expected.json").write_text(
                json.dumps({"items": [{"name": "雞腿飯", "price": 120}]}), encoding="utf-8"
            )
            (folder / "a.png").write_bytes(b"image")
            with mock.patch.object(evaluator, "CASES_DIR", folder):
                samples = evaluator.load_cases()
                self.assertEqual(len(samples), 2)
                self.assertEqual(samples[1]["skipReason"], "missing_image")
                self.assertEqual(len(evaluator.load_cases(["a"])), 1)

    def test_dry_run_is_offline_and_missing_cases_exit_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "a.expected.json").write_text(
                '{"items":[{"name":"飯","price":50}]}', encoding="utf-8"
            )
            with (
                mock.patch.object(evaluator, "CASES_DIR", folder),
                mock.patch.object(
                    evaluator, "_get_menu_vision", side_effect=AssertionError("must stay offline")
                ),
                mock.patch.object(sys, "argv", ["eval", "--dry-run"]),
                mock.patch.dict(os.environ, {}, clear=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(evaluator.main(), 2)

    def test_rescore_uses_frozen_truth_for_success_and_failure(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        rows = [
            success(sample, sample["expected"]["items"]),
            evaluator.failure_row(sample, "error", "timeout", 4, 0.72),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(
                json.dumps({"label": "old", "cases": rows, "config": {}}), encoding="utf-8"
            )
            with mock.patch.object(
                evaluator, "load_cases", side_effect=AssertionError("must use snapshot")
            ):
                rescored = evaluator.rescore(str(path), 1)
            self.assertEqual(rescored["summary"]["expectedCount"], 2)
            self.assertEqual(rescored["summary"]["exactNamePriceRate"], 0.5)
            self.assertEqual(rescored["config"]["matchThreshold"], 1)

    def test_rescore_refuses_changed_truth_and_missing_raw(self):
        sample = case([{"name": "牛肉麵", "price": 100}])
        row = success(sample, sample["expected"]["items"])
        row["provenance"] = evaluator.build_case_provenance(sample)
        changed = copy.deepcopy(row)
        changed["truth"]["items"][0]["price"] = 999
        with mock.patch.object(
            evaluator, "load_run", return_value={"label": "changed", "cases": [changed]}
        ):
            with self.assertRaisesRegex(evaluator.CaseError, "雜湊不同"):
                evaluator.rescore("changed", 0.72)
        del row["raw"]
        with mock.patch.object(
            evaluator, "load_run", return_value={"label": "missing", "cases": [row]}
        ):
            with self.assertRaisesRegex(evaluator.CaseError, "缺少原始輸出"):
                evaluator.rescore("missing", 0.72)

    def test_replay_matches_full_request_and_never_falls_back(self):
        request = evaluator.build_request_provenance(
            "prompt", "data:image/png;base64,eA==", model="m", temperature=0, timeout=10
        )
        captured = {request["requestSha256"]: {**request, "response": "saved"}}
        replay = evaluator._replay_vision(captured)
        self.assertEqual(
            replay("prompt", "data:image/png;base64,eA==", model="m", temperature=0, timeout=10),
            "saved",
        )
        with self.assertRaisesRegex(RuntimeError, "不會轉成 live"):
            replay("prompt", "data:image/png;base64,eQ==", model="m", temperature=0, timeout=10)
        with self.assertRaises(RuntimeError):
            replay("prompt", "data:image/png;base64,eA==", model="other", temperature=0, timeout=10)

    def test_compare_rejects_different_data_before_printing_deltas(self):
        with mock.patch.object(
            evaluator, "load_run", side_effect=[{"provenance": None}, {"provenance": None}]
        ):
            with self.assertRaisesRegex(evaluator.CaseError, "不能直接比較"):
                evaluator.compare_runs("a", "b")


if __name__ == "__main__":
    unittest.main()
