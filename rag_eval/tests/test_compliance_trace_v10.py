from __future__ import annotations

import unittest

from rag_eval.scripts.evaluate_compliance_trace_v10 import (
    expected_source_status,
    failure_reason,
    locate_case_chunks,
)


class ComplianceTraceV10Tests(unittest.TestCase):
    def test_full_mutated_paragraph_disambiguates_same_short_target(self):
        cases = [
            {
                "error_id": "C007",
                "mutated_text": "可继续作业",
                "mutated_paragraph": "发现透水征兆时可继续作业并汇报",
            },
            {
                "error_id": "C011",
                "mutated_text": "可继续作业",
                "mutated_paragraph": "工作面停风后可继续作业并向调度汇报",
            },
        ]
        chunks = [
            {"content": "防治水措施：发现透水征兆时可继续作业并汇报。"},
            {"content": "停风措施：工作面停风后可继续作业并向调度汇报。"},
        ]
        self.assertEqual({"C007": 0, "C011": 1}, locate_case_chunks(cases, chunks))

    def test_incomplete_mineru_numeric_article_is_source_missing(self):
        chunks = [
            {
                "article": "第七百零四条",
                "content": "内喷雾工作压力不得小于，外喷雾工作压力不得小于。",
                "canonical_rule_id": "demo",
            }
        ]
        status = expected_source_status(
            chunks,
            {
                "expected_articles": ["第七百零四条"],
                "required_terms": ["内喷雾工作压力不得小于2mpa"],
            },
        )
        self.assertTrue(status["source_found"])
        self.assertFalse(status["source_complete"])

    def test_failure_attribution_prioritizes_upstream_stage(self):
        self.assertEqual(
            "source_missing",
            failure_reason(
                final_hit=False,
                source_found=True,
                source_complete=False,
                retrieval_hit=False,
                kind="numeric",
                draft_hit=False,
                pair_found=False,
                comparator_bad=False,
            ),
        )
        self.assertEqual(
            "pairing_miss",
            failure_reason(
                final_hit=False,
                source_found=True,
                source_complete=True,
                retrieval_hit=True,
                kind="numeric",
                draft_hit=False,
                pair_found=False,
                comparator_bad=False,
            ),
        )
        self.assertEqual(
            "verification_dropped",
            failure_reason(
                final_hit=False,
                source_found=True,
                source_complete=True,
                retrieval_hit=True,
                kind="behavior",
                draft_hit=True,
                pair_found=False,
                comparator_bad=False,
            ),
        )


if __name__ == "__main__":
    unittest.main()
