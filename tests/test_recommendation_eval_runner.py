import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
SPEC = importlib.util.spec_from_file_location(
    "recommendation_eval_runner", PROJECT_ROOT / "evals" / "recommendation" / "run_eval.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class RecommendationEvalRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases_path = self.root / "cases.json"
        self.output_path = self.root / "results" / "run.json"
        self.menu = {
            "name": "測試店",
            "categories": [{"name": "主餐", "items": [{"name": "雞腿飯", "price": 80}]}],
        }
        self.menu_path = self.root / "menu_test.json"
        self.menu_path.write_text(json.dumps(self.menu), encoding="utf-8")

    def run_cases(self, cases, *, verbose=False):
        self.cases_path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
        args = [
            "--cases", str(self.cases_path), "--menu-dir", str(self.root),
            "--output-json", str(self.output_path),
        ]
        if verbose:
            args.append("--verbose")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runner.main(args)
        return code, json.loads(self.output_path.read_text(encoding="utf-8")), output.getvalue()

    def test_missing_menu_is_skipped_and_makes_run_unsuccessful(self):
        code, report, output = self.run_cases([
            {"restaurant": "測試店", "text": "預算100元"},
            {"restaurant": "缺少店", "text": "預算100元"},
        ])
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"], {
            "total": 2, "executed": 1, "passed": 1,
            "failed": 0, "skipped": 1, "success": False,
        })
        self.assertEqual(report["cases"][1]["status"], "skipped")
        self.assertEqual(
            report["cases"][1]["skip_reason"], "required_menu_missing_or_unreadable"
        )
        self.assertIsNone(report["cases"][1]["output"])
        self.assertIn("已執行 1，通過 1，失敗 0，略過 1", output)

    def test_output_is_captured_once_and_checked_recorded_and_printed_consistently(self):
        first_output = {
            "items": [{"name": "雞腿飯", "price": 80, "type": "main", "category": "主餐"}],
            "notes": "captured",
            "meta": {"estimatedTotal": 88},
        }
        with patch.object(runner, "recommend", return_value=first_output) as recommend:
            code, report, output = self.run_cases([
                {"id": "known-case", "restaurant": "測試店", "text": "預算100元"},
            ], verbose=True)
        self.assertEqual(code, 0)
        recommend.assert_called_once()
        record = report["cases"][0]
        self.assertEqual(record["case_id"], "known-case")
        self.assertEqual(record["status"], "passed")
        self.assertEqual(record["output"], first_output)
        self.assertEqual(record["preferences"]["budget"], 100)
        self.assertIn("雞腿飯", output)
        source = report["menus"]["測試店"]
        self.assertEqual(record["menu_sha256"], source["runtime_sha256"])
        self.assertEqual(source["runtime_sha256"], runner._json_hash(source["runtime_menu"]))
        self.assertEqual(len(source["file_sha256"]), 64)
        self.assertIn("evals/recommendation/run_eval.py", report["version"]["source_sha256"])
        self.assertEqual(report["evaluation_rules"]["shared_min_budget_use_ratio"], 0.5)
        self.assertEqual(report["evaluation_rules"]["shared_max_items_per_bucket"], 2)

    def test_failed_and_error_cases_are_executed_and_do_not_stop_later_cases(self):
        case = {"restaurant": "測試店", "text": "預算100元"}
        valid = {
            "items": [{"name": "雞腿飯", "price": 80, "type": "main", "category": "主餐"}]
        }
        with patch.object(
            runner, "recommend", side_effect=[{"items": []}, RuntimeError("broken"), valid]
        ) as recommend:
            code, report, _ = self.run_cases([case, case, case])
        self.assertEqual(code, 1)
        self.assertEqual(recommend.call_count, 3)
        self.assertEqual(report["summary"], {
            "total": 3, "executed": 3, "passed": 1,
            "failed": 2, "skipped": 0, "success": False,
        })
        self.assertEqual(report["cases"][0]["output"], {"items": []})
        self.assertEqual(report["cases"][0]["failures"], ["沒有推薦任何品項"])
        self.assertEqual(report["cases"][1]["error"], {
            "type": "RuntimeError", "message": "broken",
        })
        self.assertEqual(report["cases"][2]["status"], "passed")

    def test_unreadable_required_menu_is_not_reported_as_a_pass(self):
        self.menu_path.write_text("invalid json", encoding="utf-8")
        code, report, _ = self.run_cases([
            {"restaurant": "測試店", "text": "預算100元"},
        ])
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"]["executed"], 0)
        self.assertEqual(report["summary"]["passed"], 0)
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(len(report["menu_load_errors"]), 1)

    def test_malformed_output_is_recorded_as_failure_without_crashing_display(self):
        invalid = {"items": [{"name": "壞價格", "price": "invalid", "type": "main"}]}
        with patch.object(runner, "recommend", return_value=invalid):
            code, report, _ = self.run_cases([
                {"restaurant": "測試店", "text": "預算100元"},
            ], verbose=True)
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"]["failed"], 1)
        self.assertEqual(report["cases"][0]["output"], invalid)
        self.assertEqual(report["cases"][0]["error"]["type"], "TypeError")

    def test_empty_suite_does_not_report_success(self):
        code, report, _ = self.run_cases([])
        self.assertEqual(code, 1)
        self.assertEqual(report["summary"]["total"], 0)
        self.assertFalse(report["summary"]["success"])


if __name__ == "__main__":
    unittest.main()
