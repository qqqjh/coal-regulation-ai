from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "rag_eval" / "scripts" / "evaluate_manual_error_injection_v1.py"
GOLD_PATH = (
    PROJECT_ROOT
    / "new_docs"
    / "test_doc"
    / "S1302人工错误注入测试集_v1"
    / "004 S1302工作面作业规程（综采）_人工错误金标_v1.json"
)

SPEC = importlib.util.spec_from_file_location("manual_eval", MODULE_PATH)
assert SPEC and SPEC.loader
manual_eval = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = manual_eval
SPEC.loader.exec_module(manual_eval)


def synthetic_issue(case: dict, issue_id: int, all_cases: list[dict]) -> dict:
    expected_blocks = manual_eval.expected_blocks(case, all_cases)
    if case["type"] == "compliance":
        original = case["mutated_paragraph"]
        status = "不合规"
    elif case["type"] == "typo":
        original = case["mutated_text"]
        status = "错别字"
    else:
        original = case["duplicated_text"][:80]
        status = "重复"
    return {
        "id": issue_id,
        "issue_type": case["type"],
        "status": status,
        "title": status,
        "original_text": original,
        "block_indices": expected_blocks,
        "reason": "synthetic",
        "suggestion": "",
        "detail": {},
    }


class ManualErrorInjectionEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gold = json.loads(GOLD_PATH.read_text(encoding="utf-8"))
        cls.cases = cls.gold["cases"]

    def perfect_issues(self):
        return [synthetic_issue(case, index + 1, self.cases) for index, case in enumerate(self.cases)]

    def test_service_manager_accepts_explicit_v10_worker_entry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = manual_eval.ServiceManager(
                Path(sys.executable),
                Path(temp_dir),
                "http://127.0.0.1:8000/api/v9",
                use_mineru=False,
                worker_script=Path("v10_worker.py"),
            )
            self.assertEqual("v10_worker.py", manager.worker_script.name)
            self.assertTrue(manager.worker_script.is_file())

    def test_v10_worker_disables_legacy_escalation_loop_by_default(self):
        import v10_worker

        original = v10_worker.v9_worker.DISABLE_ESCALATION_LOOP
        try:
            with patch.dict(
                os.environ,
                {"V10_ENABLE_LEGACY_ESCALATION_LOOP": ""},
            ):
                enabled = v10_worker.configure_runtime()
            self.assertFalse(enabled)
            self.assertTrue(v10_worker.v9_worker.DISABLE_ESCALATION_LOOP)

            with patch.dict(
                os.environ,
                {"V10_ENABLE_LEGACY_ESCALATION_LOOP": "1"},
            ):
                enabled = v10_worker.configure_runtime()
            self.assertTrue(enabled)
            self.assertFalse(v10_worker.v9_worker.DISABLE_ESCALATION_LOOP)
        finally:
            v10_worker.v9_worker.DISABLE_ESCALATION_LOOP = original

    def test_perfect_predictions_score_one(self):
        result = manual_eval.evaluate(self.gold, self.perfect_issues())
        metrics = result["metrics"]
        self.assertEqual(metrics["strict_tp"], 30)
        self.assertEqual(metrics["unmatched_prediction_count"], 0)
        self.assertAlmostEqual(metrics["strict_precision"], 1.0)
        self.assertAlmostEqual(metrics["strict_recall"], 1.0)

    def test_one_miss_and_one_false_positive(self):
        issues = self.perfect_issues()[:-1]
        issues.append(
            {
                "id": 999,
                "issue_type": "typo",
                "status": "错别字",
                "original_text": "完全不存在于金标的随机错误文本XYZ",
                "block_indices": [9999],
            }
        )
        result = manual_eval.evaluate(self.gold, issues)
        metrics = result["metrics"]
        self.assertEqual(metrics["strict_tp"], 29)
        self.assertEqual(metrics["missed_count"], 1)
        self.assertEqual(metrics["unmatched_prediction_count"], 1)
        self.assertAlmostEqual(metrics["strict_precision"], 29 / 30)
        self.assertAlmostEqual(metrics["strict_recall"], 29 / 30)

    def test_escalation_counts_as_detected_but_not_strict(self):
        issues = self.perfect_issues()
        issues[0] = {
            **issues[0],
            "issue_type": "escalation",
            "status": "不确定",
            "title": "需人工裁决",
        }
        result = manual_eval.evaluate(self.gold, issues)
        first = next(row for row in result["case_results"] if row["error_id"] == "C001")
        self.assertTrue(first["matched"])
        self.assertFalse(first["strict_hit"])
        self.assertEqual(first["outcome"], "detected_not_strict")
        self.assertEqual(result["metrics"]["detected_count"], 30)
        self.assertEqual(result["metrics"]["strict_tp"], 29)

    def test_report_bundle_is_written(self):
        result = manual_eval.evaluate(self.gold, self.perfect_issues())
        with tempfile.TemporaryDirectory() as temporary:
            paths = manual_eval.save_reports(
                Path(temporary),
                result,
                {"mode": "offline", "job_id": "test-job", "document": "fixture.docx"},
            )
            self.assertEqual(set(paths), {"json", "csv", "html", "issues_raw"})
            for output in paths.values():
                self.assertGreater(Path(output).stat().st_size, 100)
            html_text = Path(paths["html"]).read_text(encoding="utf-8")
            self.assertIn("严格 F1", html_text)
            self.assertIn("C001", html_text)

    def test_process_memory_sampler(self):
        rss = manual_eval.process_rss_bytes(os.getpid())
        self.assertIsNotNone(rss)
        self.assertGreater(rss, 1024 * 1024)

    def test_mineru_is_detected_from_service_environment(self):
        service_python = Path(r"D:\Anaconda\envs\langchain0.3\python.exe")
        if not service_python.exists():
            self.skipTest("langchain0.3 environment is not installed")
        executable = manual_eval.detect_mineru_executable(service_python)
        self.assertEqual(executable.name.lower(), "mineru-api.exe")
        self.assertTrue(executable.exists())


if __name__ == "__main__":
    unittest.main()
