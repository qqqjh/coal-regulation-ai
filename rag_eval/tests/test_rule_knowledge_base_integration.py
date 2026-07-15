from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.vector_store import VectorStoreService, vector_store_service
from hybrid_rag_review_v10 import HybridRAGReviewerV10
from rule_applicability import (
    APPLICABILITY_GENERAL,
    APPLICABILITY_OUTBURST_ONLY,
    classify_rule_applicability,
)
from v9_worker import V9Worker


def reviewer_without_models() -> HybridRAGReviewerV10:
    reviewer = HybridRAGReviewerV10.__new__(HybridRAGReviewerV10)
    reviewer._worker_seen_lock = threading.Lock()
    reviewer._worker_seen = defaultdict(set)
    reviewer.mine_type = "non_outburst"
    reviewer.kb_chunks = []
    reviewer.kb_texts = []
    reviewer.kb_dense = None
    reviewer.kb_postings = None
    reviewer.kb_json_path = None
    reviewer._default_kb_json_path = None
    reviewer._kb_raw_data = {}
    reviewer._kb_source_key = ""
    reviewer._kb_source_hash = ""
    return reviewer


class RuleApplicabilityTests(unittest.TestCase):
    def test_auto_mode_uses_document_and_structure_not_body_mentions(self):
        body_only, _basis = classify_rule_applicability(
            {"chapter": "通风管理", "content": "突出矿井还应当采取专项措施。"},
            "煤矿安全规程.pdf",
        )
        structured, basis = classify_rule_applicability(
            {"chapter": "第五章 防治煤与瓦斯突出", "content": "区域验证。"},
            "煤矿安全规程.pdf",
        )
        whole_document, _basis = classify_rule_applicability(
            {"chapter": "第一章 总则"},
            "防治煤与瓦斯突出细则.pdf",
        )

        self.assertEqual(APPLICABILITY_GENERAL, body_only)
        self.assertEqual(APPLICABILITY_OUTBURST_ONLY, structured)
        self.assertEqual("structure_heading", basis["reason"])
        self.assertEqual(APPLICABILITY_OUTBURST_ONLY, whole_document)

    def test_manual_upload_override_is_document_wide(self):
        forced_general, basis = classify_rule_applicability(
            {"chapter": "煤与瓦斯突出防治"},
            "防治煤与瓦斯突出细则.pdf",
            mode="general",
        )
        forced_outburst, _basis = classify_rule_applicability(
            {"chapter": "总则"},
            "普通规则.pdf",
            mode="outburst_only",
        )

        self.assertEqual(APPLICABILITY_GENERAL, forced_general)
        self.assertEqual("manual", basis["mode"])
        self.assertEqual(APPLICABILITY_OUTBURST_ONLY, forced_outburst)

    def test_uploaded_rule_metadata_is_persisted_per_chunk(self):
        service = VectorStoreService.__new__(VectorStoreService)
        captured = []

        def capture(documents, kb_id):
            captured.extend(documents)
            self.assertEqual(7, kb_id)
            return len(documents)

        service.add_documents = capture
        with tempfile.NamedTemporaryFile(delete=False) as file_obj:
            file_path = Path(file_obj.name)
        try:
            added = service._add_rule_chunks_to_vector_store(
                {
                    "煤矿安全规程.pdf": [
                        {
                            "content": "采掘工作面必须保持独立通风。",
                            "chapter": "通风管理",
                            "retrievable": True,
                        }
                    ]
                },
                filename="煤矿安全规程.pdf",
                file_path=file_path,
                doc_id=11,
                kb_id=7,
                mineru_json_path="demo.json",
                applicability_mode="general",
            )
        finally:
            file_path.unlink(missing_ok=True)

        self.assertEqual(1, added)
        self.assertEqual(APPLICABILITY_GENERAL, captured[0].metadata["applicability"])
        self.assertEqual("manual", captured[0].metadata["applicability_mode"])
        self.assertEqual("document_override", captured[0].metadata["applicability_reason"])


class FrontendKnowledgeBaseSelectionTests(unittest.TestCase):
    def test_non_outburst_task_filters_uploaded_outburst_chunks(self):
        reviewer = reviewer_without_models()
        records = [
            {
                "id": "general",
                "doc_name": "煤矿安全规程.pdf",
                "content": "所有矿井必须建立安全生产责任制。",
                "retrieval_text": "所有矿井必须建立安全生产责任制。",
                "applicability": "general",
            },
            {
                "id": "outburst",
                "doc_name": "煤矿安全规程.pdf",
                "content": "突出矿井必须编制专项防突设计。",
                "retrieval_text": "突出矿井必须编制专项防突设计。",
                "applicability": "outburst_only",
            },
        ]

        with patch.object(reviewer, "build_index") as build_index:
            reviewer.load_knowledge_base_records(
                records, kb_id=9, mine_type="non_outburst"
            )
            self.assertEqual(["general"], [item["source_chunk_id"] for item in reviewer.kb_chunks])
            self.assertEqual(1, reviewer.excluded_outburst_count)

            reviewer.set_mine_type("outburst")

        self.assertEqual(2, len(reviewer.kb_chunks))
        self.assertEqual(0, reviewer.excluded_outburst_count)
        self.assertEqual(2, build_index.call_count)

    def test_worker_activates_the_kb_selected_by_frontend(self):
        class DummyEngine:
            kb_chunks = []
            excluded_outburst_count = 0

            def __init__(self):
                self.calls = []

            def load_knowledge_base_records(self, records, *, kb_id, mine_type):
                self.calls.append((records, kb_id, mine_type))
                self.kb_chunks = list(records)
                return True

        records = [{"content": "规则", "doc_name": "规程.pdf"}]
        worker = V9Worker.__new__(V9Worker)
        worker.engine = DummyEngine()
        with patch.object(
            vector_store_service,
            "get_knowledge_base_review_chunks",
            return_value=records,
        ) as load_records:
            runtime = worker._configure_job_knowledge_base(
                {"kb_id": 23}, "non_outburst"
            )

        load_records.assert_called_once_with(23)
        self.assertEqual([(records, 23, "non_outburst")], worker.engine.calls)
        self.assertEqual("frontend_kb", runtime["source"])
        self.assertEqual(23, runtime["kb_id"])


if __name__ == "__main__":
    unittest.main()
