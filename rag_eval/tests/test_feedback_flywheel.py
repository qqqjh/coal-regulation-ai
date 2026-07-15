from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hybrid_rag_review_v10 import HybridRAGReviewerV10
from review_queue import ReviewQueue


class FeedbackFlywheelTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.queue = ReviewQueue(Path(self.temp_dir.name) / "flywheel.db")

    def tearDown(self):
        self.queue.close()
        self.temp_dir.cleanup()

    def _upsert(self, *, issue_id=11, action="accept", issue_type="compliance"):
        return self.queue.upsert_human_annotation(
            doc_name="测试作业规程.docx",
            chunk_index=3,
            pending_content="瓦斯浓度达到1.5%时可以继续作业。",
            final_verdict="不合规" if action != "reject" else "合规",
            issue_id=issue_id,
            job_id="job-a",
            user_id="tester",
            action=action,
            issue_type=issue_type,
            model_verdict="不合规",
            reason="人工复核结论",
            extra={
                "suggestion": "瓦斯超限时必须停止作业。",
                "final_text": "瓦斯超限时必须停止作业。",
            },
        )

    def test_same_issue_updates_one_active_sample(self):
        first = self._upsert(action="accept")
        second = self._upsert(action="reject")

        items = self.queue.list_human_feedback()
        self.assertEqual(first, second)
        self.assertEqual(1, len(items))
        self.assertEqual("reject", items[0]["action"])
        self.assertEqual("合规", items[0]["final_verdict"])

    def test_similar_feedback_is_reused_and_counted(self):
        annotation_id = self._upsert()

        matches = self.queue.search_human_feedback(
            "本工作面规定瓦斯浓度达到1.5%时可以继续作业，是否合规？",
            limit=3,
            mark_reused=True,
        )
        stats = self.queue.flywheel_stats()

        self.assertEqual([annotation_id], [item["id"] for item in matches])
        self.assertGreater(matches[0]["similarity"], 0.5)
        self.assertEqual(1, stats["reuse_events"])
        self.assertEqual(1, stats["reused_samples"])

    def test_revoked_feedback_is_not_reused_or_exported(self):
        self._upsert()
        self.assertTrue(self.queue.deactivate_human_annotation(11))

        matches = self.queue.search_human_feedback(
            "瓦斯浓度达到1.5%时可以继续作业。"
        )
        export_path = Path(self.temp_dir.name) / "gold.json"
        exported = self.queue.export_gold_cases(export_path, sources=("human",))

        self.assertEqual([], matches)
        self.assertEqual(0, self.queue.flywheel_stats()["total_samples"])
        self.assertEqual(0, exported)

    def test_prompt_uses_only_matching_feedback_type(self):
        pending = {
            "_human_feedback_examples": [
                {
                    "id": 1,
                    "issue_type": "compliance",
                    "action": "reject",
                    "pending_content": "严禁进入老塘作业。",
                    "extra": {"final_text": "严禁进入老塘作业。"},
                },
                {
                    "id": 2,
                    "issue_type": "typo",
                    "action": "custom",
                    "pending_content": "甲完浓度",
                    "extra": {"final_text": "甲烷浓度"},
                },
            ]
        }

        compliance_note = HybridRAGReviewerV10._human_feedback_experience_note(
            pending, {"compliance"}
        )
        typo_note = HybridRAGReviewerV10._human_feedback_experience_note(
            pending, {"typo"}
        )

        self.assertIn("人工判定为误报", compliance_note)
        self.assertNotIn("甲完浓度", compliance_note)
        self.assertIn("甲完浓度", typo_note)
        self.assertIn("甲烷浓度", typo_note)

    def test_pdf_job_keeps_separate_mineru_and_editable_sources(self):
        self.queue.create_job(
            "pdf-job",
            "测试规程.pdf",
            "D:/work/pdf-job.docx",
            source_format="pdf",
            mineru_source_path="D:/work/pdf-job_original.pdf",
            original_pdf_path="D:/work/pdf-job_original.pdf",
        )
        job = self.queue.get_job("pdf-job")

        self.assertEqual("pdf", job["source_format"])
        self.assertTrue(job["mineru_source_path"].endswith("_original.pdf"))
        self.assertTrue(job["docx_path"].endswith(".docx"))


if __name__ == "__main__":
    unittest.main()
