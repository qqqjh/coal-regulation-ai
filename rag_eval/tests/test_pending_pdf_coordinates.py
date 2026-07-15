from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docx import Document

from mineru_adapter import run_mineru_api
from pending_doc_chunking_v9 import PendingDocChunkerV9
from v9_worker import V9Worker


def text_block(text: str, bbox: list[int]) -> dict:
    return {
        "type": "text",
        "bbox": bbox,
        "lines": [{"spans": [{"content": text}]}],
    }


class PendingPdfCoordinateTests(unittest.TestCase):
    def test_chunker_preserves_page_bbox_and_stable_unit_id(self):
        payload = {
            "pdf_info": [
                {
                    "page_idx": 0,
                    "para_blocks": [
                        text_block("第一章 采煤工作面安全要求", [70, 80, 520, 112]),
                        text_block(
                            "发现透水征兆时必须立即停止作业，撤出人员并报告矿调度室。",
                            [76, 140, 525, 172],
                        ),
                    ],
                },
                {
                    "page_idx": 1,
                    "para_blocks": [
                        text_block(
                            "发现透水征兆时必须立即停止作业，撤出人员并报告矿调度室。",
                            [75, 310, 524, 342],
                        )
                    ],
                },
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "demo.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            chunks = PendingDocChunkerV9(str(path)).process()

        units = [unit for chunk in chunks for unit in chunk.get("source_units", [])]
        self.assertGreaterEqual(len(units), 3)
        self.assertEqual([1, 2], sorted({unit["page"] for unit in units}))
        self.assertTrue(all(len(unit.get("bbox") or []) == 4 for unit in units))
        self.assertEqual(len(units), len({unit["source_unit_id"] for unit in units}))

    def test_mineru_units_align_repeated_text_to_distinct_word_blocks(self):
        repeated = "发现透水征兆时必须立即停止作业，撤出人员并报告矿调度室。"
        parsed = {
            "blocks": [
                {"block_index": 0, "text": repeated, "kind": "paragraph"},
                {"block_index": 1, "text": "其他内容", "kind": "paragraph"},
                {"block_index": 2, "text": repeated, "kind": "paragraph"},
            ]
        }
        chunks = [
            {"content": repeated, "source_units": [{"source_unit_id": "p1-u0", "text": repeated}]},
            {"content": repeated, "source_units": [{"source_unit_id": "p2-u1", "text": repeated}]},
        ]
        worker = V9Worker.__new__(V9Worker)
        mapping = worker._map_mineru_units_to_docx(parsed, chunks)

        self.assertEqual([0], mapping["p1-u0"])
        self.assertEqual([2], mapping["p2-u1"])

    def test_pdf_editable_docx_is_built_from_unique_mineru_units(self):
        chunks = [
            {
                "content": "第一章 安全要求\n必须立即停止作业。",
                "source_units": [
                    {"source_unit_id": "p1-u0", "text": "第一章 安全要求", "kind": "title"},
                    {"source_unit_id": "p1-u1", "text": "必须立即停止作业。", "kind": "text"},
                ],
            },
            {
                "content": "必须立即停止作业。\n撤出人员。",
                "source_units": [
                    {"source_unit_id": "p1-u1", "text": "必须立即停止作业。", "kind": "text"},
                    {"source_unit_id": "p2-u0", "text": "撤出人员。", "kind": "text"},
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "pdf-review.docx"
            result = V9Worker._build_pdf_editable_docx(chunks, target)
            paragraphs = [p.text for p in Document(result).paragraphs]

        self.assertEqual(["第一章 安全要求", "必须立即停止作业。", "撤出人员。"], paragraphs)

    def test_mineru_http_error_keeps_server_error_detail(self):
        class FailedResponse:
            ok = False
            status_code = 409
            text = '{"error":"No module named ftfy"}'
            reason = "Conflict"

            @staticmethod
            def json():
                return {
                    "task_id": "task-demo",
                    "status": "failed",
                    "error": "No module named 'ftfy'",
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "demo.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            with patch("mineru_adapter.requests.post", return_value=FailedResponse()):
                with self.assertRaisesRegex(RuntimeError, "ftfy") as caught:
                    run_mineru_api(
                        source,
                        Path(temp_dir) / "output",
                        api_url="http://127.0.0.1:51071",
                    )

        self.assertIn("HTTP 409", str(caught.exception))
        self.assertIn("task_id=task-demo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
