"""Offline checks for evidence identity and comparison safeguards."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / "evals/menu_ocr/run_provenance.py"
SPEC = importlib.util.spec_from_file_location("menu_eval_provenance", MODULE_PATH)
assert SPEC and SPEC.loader
provenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provenance)


class MenuEvalProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / "menu.jpg"
        self.image.write_bytes(b"original menu image")
        self.truth = {"items": [{"name": "乾麵", "price": 50}]}
        self.truth_path = self.root / "menu.expected.json"
        self.truth_path.write_text(json.dumps(self.truth), encoding="utf-8")
        self.case = {
            "name": "menu", "image_path": self.image,
            "mime": "image/jpeg", "expected": self.truth,
        }

    def build(self, **kwargs):
        with patch.object(provenance, "capture_code_provenance", return_value={}):
            return provenance.build_run_provenance(
                [self.case], project_root=self.root, run_mode=kwargs.pop("run_mode", "live"),
                settings=kwargs.pop("settings", {"matchThreshold": 0.72, "jobs": 1}), **kwargs,
            )

    def test_truth_hash_uses_scored_snapshot_and_ignores_file_formatting(self):
        before = provenance.build_case_provenance(self.case, project_root=self.root)
        self.truth_path.write_text(json.dumps(self.truth, indent=2), encoding="utf-8")
        after = provenance.build_case_provenance(self.case, project_root=self.root)
        self.assertEqual(before["truth"]["sha256"], after["truth"]["sha256"])
        self.assertNotEqual(before["truth"]["fileSha256"], after["truth"]["fileSha256"])
        changed = dict(self.case, expected={"items": [{"name": "乾麵", "price": 60}]})
        self.assertNotEqual(
            before["truth"]["sha256"],
            provenance.build_case_provenance(changed)["truth"]["sha256"],
        )

    def test_changed_image_or_truth_prevents_accuracy_comparison(self):
        before = self.build()
        self.image.write_bytes(b"different menu photograph")
        after = self.build()
        self.assertFalse(provenance.compare_provenance(before, after)["accuracyCompatible"])
        self.image.write_bytes(b"original menu image")
        self.truth["items"][0]["price"] = 60
        self.assertFalse(provenance.compare_provenance(before, self.build())["compatible"])

    def test_missing_image_and_legacy_runs_never_silently_compare(self):
        self.case["image_path"] = None
        self.case["status"] = "skipped"
        self.case["skipReason"] = "missing_image"
        missing = self.build()
        self.assertFalse(provenance.compare_provenance(missing, missing)["compatible"])
        self.assertFalse(provenance.compare_provenance(None, None)["compatible"])
        self.assertIsNone(missing["caseManifest"][0]["sourceImage"]["sha256"])

    def test_threshold_or_scorer_change_requires_common_rescoring(self):
        before = self.build()
        after = self.build(settings={"matchThreshold": 0.9, "jobs": 1})
        self.assertFalse(provenance.compare_provenance(before, after)["compatible"])
        after = self.build()
        after["scoringVersion"] = "different-scorer"
        self.assertFalse(provenance.compare_provenance(before, after)["compatible"])

    def test_duplicate_case_ids_do_not_claim_unique_case_comparability(self):
        run = self.build()
        run["caseManifest"].append(run["caseManifest"][0])
        self.assertFalse(provenance.compare_provenance(run, run)["compatible"])

    def test_model_factor_is_allowed_but_replay_and_concurrency_affect_latency(self):
        before = self.build(settings={"matchThreshold": 0.72, "jobs": 1, "visionModel": "a"})
        after = self.build(settings={"matchThreshold": 0.72, "jobs": 1, "visionModel": "b"})
        comparison = provenance.compare_provenance(before, after)
        self.assertTrue(comparison["accuracyCompatible"])
        self.assertTrue(comparison["warnings"])
        replay = self.build(run_mode="replay", replay_source=before)
        self.assertTrue(provenance.compare_provenance(before, replay)["accuracyCompatible"])
        self.assertFalse(provenance.compare_provenance(before, replay)["latencyCompatible"])
        parallel = self.build(settings={"matchThreshold": 0.72, "jobs": 2})
        self.assertFalse(provenance.compare_provenance(before, parallel)["latencyCompatible"])

    def test_settings_do_not_persist_credentials_or_read_env_file(self):
        secret = "do-not-persist-this-secret"
        self.root.joinpath(".env").write_text(f"API_KEY={secret}", encoding="utf-8")
        captured = self.build(settings={
            "matchThreshold": 0.72, "API_KEY": secret,
            "apiKey": secret, "API_BASE_URL": f"https://{secret}@example.test",
        })
        self.assertNotIn(secret, json.dumps(captured))
        self.assertEqual(captured["cacheStatus"], "unknown")

    def test_request_hash_captures_preprocessed_bytes_and_full_prompt(self):
        image = b"resized JPEG bytes"
        image_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        before = provenance.build_request_provenance("讀取菜單", image_url, model="alias")
        after = provenance.build_request_provenance("讀取菜單 ", image_url, model="alias")
        self.assertEqual(before["inputImages"][0]["sha256"], hashlib.sha256(image).hexdigest())
        self.assertNotEqual(before["requestSha256"], after["requestSha256"])
        self.assertEqual(before["underlyingModelVersion"], "unknown")
        self.assertEqual(before["cacheStatus"], "unknown")

    def test_cache_and_fallback_are_not_inferred_from_speed_or_absence(self):
        empty = provenance.provenance_from_calls([])
        self.assertIsNone(empty["fallback"])
        self.assertIsNone(empty["activeModel"])
        captured = provenance.provenance_from_calls(
            [{"model": "a", "durationSec": 0.01}],
            result={"models": {"ocrRequested": "a", "ocr": "b"}},
        )
        self.assertTrue(captured["fallback"])
        self.assertEqual(captured["activeModel"], "b")
        self.assertEqual(captured["cacheStatus"], "unknown")

    def test_code_hashes_survive_missing_git_and_exclude_env(self):
        self.root.joinpath("src").mkdir()
        source = self.root / "src/example.py"
        source.write_text("VALUE = 1\n", encoding="utf-8")
        self.root.joinpath(".env").write_text("API_KEY=secret", encoding="utf-8")
        with patch.object(provenance.subprocess, "run", side_effect=FileNotFoundError):
            before = provenance.capture_code_provenance(self.root)
            source.write_text("VALUE = 2\n", encoding="utf-8")
            after = provenance.capture_code_provenance(self.root)
        self.assertIsNone(before["commit"])
        self.assertIsNone(before["dirty"])
        self.assertNotEqual(before["sourceFingerprint"], after["sourceFingerprint"])
        self.assertEqual(list(before["sourceHashes"]), ["src/example.py"])


if __name__ == "__main__":
    unittest.main()
