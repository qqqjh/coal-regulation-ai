from __future__ import annotations

import threading
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import numpy as np

from hybrid_rag_review_v10 import HybridRAGReviewerV10
from hybrid_rag_review_v9 import build_mine_applicability_note
from rag_eval.scripts.evaluate_manual_error_injection_v1 import match_cases


def reviewer_without_models() -> HybridRAGReviewerV10:
    reviewer = HybridRAGReviewerV10.__new__(HybridRAGReviewerV10)
    reviewer._worker_seen_lock = threading.Lock()
    reviewer._worker_seen = defaultdict(set)
    return reviewer


class HybridRAGReviewerV10Tests(unittest.TestCase):
    def test_exact_repetition_uses_distinct_word_blocks_once(self):
        repeated = "（1）发现透水征兆时必须立即停止作业，撤出人员并报告矿调度室。"
        chunks = [
            {
                "content": repeated,
                "source_units": [
                    {"text": repeated, "source_blocks": [10], "kind": "paragraph"}
                ],
            },
            {
                "content": repeated,
                "source_units": [
                    {"text": repeated, "source_blocks": [20], "kind": "paragraph"}
                ],
            },
            # 相邻 MinerU chunk 重叠携带同一 Word block，不应算第三次重复。
            {
                "content": repeated,
                "source_units": [
                    {"text": repeated, "source_blocks": [20], "kind": "paragraph"}
                ],
            },
        ]
        reviewer = reviewer_without_models()
        results, metrics = reviewer.check_document_repetition(
            "demo.docx", chunks, np.zeros((3, 2), dtype=np.float32)
        )

        duplicates = [
            item
            for result in results.values()
            for item in result.get("duplicates", [])
        ]
        self.assertEqual(1, len(duplicates))
        self.assertEqual([10, 20], duplicates[0]["block_indices"])
        self.assertEqual("exact_paragraph", duplicates[0]["match_method"])
        self.assertEqual(1, metrics["exact_groups"])

    def test_exact_repetition_keeps_two_native_pdf_locations(self):
        repeated = "发现透水征兆时必须立即停止作业，撤出全部受威胁区域人员并立即报告矿调度室。"
        chunks = [
            {
                "content": repeated,
                "source_units": [{
                    "source_unit_id": "p3-u8",
                    "text": repeated,
                    "page": 3,
                    "bbox": [80, 120, 520, 148],
                    "page_width": 595,
                    "page_height": 842,
                    "coordinate_space": "original_pdf",
                    "source_blocks": [10],
                }],
            },
            {
                "content": repeated,
                "source_units": [{
                    "source_unit_id": "p9-u31",
                    "text": repeated,
                    "page": 9,
                    "bbox": [76, 420, 518, 448],
                    "page_width": 595,
                    "page_height": 842,
                    "coordinate_space": "original_pdf",
                    "source_blocks": [20],
                }],
            },
        ]
        reviewer = reviewer_without_models()
        results, _metrics = reviewer.check_document_repetition(
            "demo.pdf", chunks, np.zeros((2, 2), dtype=np.float32)
        )
        duplicate = next(
            item
            for result in results.values()
            for item in result.get("duplicates", [])
        )

        self.assertEqual(3, duplicate["source_pdf_locations"][0]["page"])
        self.assertEqual(9, duplicate["duplicate_pdf_locations"][0]["page"])
        self.assertEqual([80.0, 120.0, 520.0, 148.0], duplicate["source_pdf_locations"][0]["bbox"])

    def test_worker_dedupe_keeps_different_locations(self):
        reviewer = reviewer_without_models()
        reviewer.begin_worker_job("job")
        self.assertTrue(
            reviewer.claim_worker_issue("job", "typo", "通疯", "通风", [400])
        )
        self.assertFalse(
            reviewer.claim_worker_issue("job", "typo", "通疯", "通风", [400])
        )
        self.assertTrue(
            reviewer.claim_worker_issue("job", "typo", "通疯", "通风", [500])
        )

    def test_compliance_recall_trigger_is_targeted(self):
        risky = HybridRAGReviewerV10._risk_segments(
            "（1）瓦斯浓度达到1.8%时方可断电。\n（2）可以带电检修设备。"
        )
        safe = HybridRAGReviewerV10._risk_segments("本班召开班前会并清点工具。")
        self.assertGreaterEqual(len(risky), 1)
        self.assertEqual([], safe)

    def test_atomic_risk_segments_separate_permission_and_numeric_thresholds(self):
        content = (
            "防火设施必须配置齐全。\n"
            "（5）机电设备检修时，允许带电作业；"
            "当回风巷瓦斯浓度＜1.3%时，方可恢复作业。\n"
            "打开外壳前无需切断上级电源。\n"
            "本班清点工具。"
        )
        segments = HybridRAGReviewerV10._atomic_risk_segments(content)
        self.assertTrue(any("允许带电作业" in item for item in segments))
        self.assertTrue(any("1.3%" in item for item in segments))
        self.assertTrue(any("无需切断上级电源" in item for item in segments))
        self.assertFalse(any("清点工具" in item for item in segments))

    def test_v10_pending_inputs_put_atomic_risk_query_before_generic_segments(self):
        chunks = [
            {
                "content": (
                    "防灭火设施应保持完好。\n"
                    "⑤工作面可以带电检修、搬迁电气设备。检修前必须切断电源。"
                )
            }
        ]
        _texts, segments, flat, slices, cache_key = (
            HybridRAGReviewerV10._pending_encoding_inputs(chunks)
        )
        self.assertIn("可以带电检修", segments[0][0])
        self.assertIn("法规禁止性要求", segments[0][0])
        self.assertEqual(1, slices[0][0])
        self.assertEqual(len(flat), slices[0][1])
        self.assertTrue(cache_key.startswith("pending_v10_atomic_"))

        methane_chunks = [
            {"content": "当回风巷瓦斯浓度低于1.3%时，方可恢复作业。"}
        ]
        _texts, methane_segments, _flat, _slices, _key = (
            HybridRAGReviewerV10._pending_encoding_inputs(methane_chunks)
        )
        self.assertTrue(
            any("复电浓度" in segment for segment in methane_segments[0])
        )

    def test_risk_query_coverage_keeps_distinct_candidate_before_global_fill(self):
        reviewer = reviewer_without_models()
        reviewer.kb_chunks = [
            {"id": "same-topic"},
            {"id": "second-risk"},
            {"id": "global-fill"},
        ]
        query_one = "阈值一\n同对象安全数值规则：报警浓度"
        query_two = "阈值二\n同对象安全数值规则：复电浓度"
        full = "长chunk"
        pairs = [
            (full, 2),
            (query_one, 0),
            (query_one, 2),
            (query_two, 0),
            (query_two, 1),
        ]
        scores = {
            (full, 2): 0.96,
            (query_one, 0): 0.99,
            (query_one, 2): 0.80,
            (query_two, 0): 0.98,
            (query_two, 1): 0.90,
        }
        ranked = reviewer._rank_recalled_pairs(pairs, scores, top_k=3)
        self.assertEqual(
            {"same-topic", "second-risk", "global-fill"},
            {item["chunk"]["id"] for item in ranked},
        )

    def test_high_risk_segments_are_merged_into_initial_review_note(self):
        reviewer = reviewer_without_models()
        note = reviewer._compliance_focus_note(
            {"content": "瓦斯浓度达到1.8%时方可断电。可以带电检修设备。"}
        )
        self.assertIn("高风险句强制核查清单", note)
        self.assertIn("1.8%", note)
        self.assertIn("可以带电检修", note)

    def test_initial_review_receives_high_risk_note_without_extra_call(self):
        reviewer = reviewer_without_models()
        calls = []

        def fake_chat(messages, **kwargs):
            calls.append(messages)
            return '{"compliance_status":"合规","issues":[],"summary":"未发现冲突"}'

        reviewer._chat = fake_chat
        reviewer._build_kb_context = lambda results: "法规证据"
        result = reviewer.review_chunk_with_llm(
            {"content": "可以带电检修设备。", "chapter": "测试", "section": "测试"},
            [{"chunk": {"content": "严禁带电检修"}}],
        )

        self.assertEqual("合规", result["compliance_status"])
        self.assertEqual(1, len(calls))
        self.assertIn("高风险句强制核查清单", calls[0][-1]["content"])

    def test_v10_disables_unused_kb_stance_classifier(self):
        reviewer = reviewer_without_models()
        reviewer._chat = lambda *args, **kwargs: self.fail("不应调用立场分类模型")
        self.assertEqual([], reviewer.classify_kb_chunks({}, []))

    def test_grounded_batch_numeric_conflict_becomes_review_issue(self):
        checks = [
            {
                "has_pairs": True,
                "overall": "不合规",
                "details": [
                    {
                        "parameter": "工作面甲烷复电浓度",
                        "pending_quote": "低于1.5%时方可开启照明灯",
                        "rule_quote": "复电浓度<1.0%",
                        "verdict": "不合规",
                        "relation": "later_trigger",
                        "explanation": "待审1.5%高于法规1.0%",
                        "evidence_grounded": True,
                    },
                    {
                        "parameter": "无依据参数",
                        "pending_quote": "压力0.5MPa",
                        "rule_quote": "压力不得小于",
                        "verdict": "不合规",
                        "relation": "below_lower",
                        "explanation": "无依据",
                        "evidence_grounded": False,
                    },
                ],
            }
        ]

        issues = HybridRAGReviewerV10._grounded_numeric_issues(checks)

        self.assertEqual(1, len(issues))
        self.assertEqual("低于1.5%时方可开启照明灯", issues[0]["pending_content"])
        self.assertTrue(issues[0]["numeric_evidence_grounded"])

    def test_verifier_conflict_is_decided_by_main_agent(self):
        reviewer = reviewer_without_models()
        calls = []

        def fake_chat(messages, **kwargs):
            calls.append(messages)
            return (
                '{"decisions":[{"conflict_id":"C1",'
                '"decision":"保留初审","reason":"许可表述与法规禁令直接冲突"}],'
                '"summary":"保留初审意见"}'
            )

        reviewer._chat = fake_chat
        pending = {
            "content": (
                "严禁带电检修和搬迁电气设备；"
                "打开外壳前，无需切断上级电源。"
            )
        }
        issue = {
            "type": "规则冲突",
            "pending_content": pending["content"],
            "regulation_content": "检修电气设备前必须切断上级电源，严禁带电作业。",
            "description": "无需切断上级电源与强制断电要求冲突",
            "suggestion": "改为必须切断上级电源",
        }
        parent_result = {
            "compliance_status": "合规",
            "issues": [],
            "summary": "复核删除",
            "verification_notes": "因句首严禁而删除",
        }

        with patch(
            "hybrid_rag_review_v9.HybridRAGReviewerV9.verify_review_result",
            return_value=parent_result,
        ):
            verified = reviewer.verify_review_result(
                pending,
                [],
                {"compliance_status": "不合规", "issues": [issue]},
                [],
            )

        self.assertEqual("不合规", verified["compliance_status"])
        self.assertEqual(1, len(verified["issues"]))
        self.assertEqual(1, len(calls))
        self.assertTrue(verified["main_agent_adjudication"]["called"])
        self.assertEqual(1, verified["main_agent_adjudication"]["kept_initial"])
        self.assertEqual(
            "保留初审",
            verified["issues"][0]["main_agent_adjudication"]["decision"],
        )

    def test_main_agent_can_accept_verifier_deletion(self):
        reviewer = reviewer_without_models()
        reviewer._chat = lambda *args, **kwargs: (
            '{"decisions":[{"conflict_id":"C1","decision":"采纳复核",'
            '"reason":"初审引用场景不一致"}],"summary":"采纳复核"}'
        )
        initial_issue = {
            "type": "规则冲突",
            "pending_content": "设备停机后进行检修。",
            "regulation_content": "严禁带电检修。",
            "description": "初审认为属于带电检修",
        }
        parent_result = {
            "compliance_status": "合规",
            "issues": [],
            "verification_notes": "待审已经停机，不构成带电检修",
        }

        with patch(
            "hybrid_rag_review_v9.HybridRAGReviewerV9.verify_review_result",
            return_value=parent_result,
        ):
            verified = reviewer.verify_review_result(
                {"content": "设备停机后进行检修。"},
                [],
                {"compliance_status": "不合规", "issues": [initial_issue]},
                [],
            )

        self.assertEqual("合规", verified["compliance_status"])
        self.assertEqual([], verified["issues"])
        self.assertEqual(1, verified["main_agent_adjudication"]["accepted_verifier"])

    def test_unparseable_main_agent_result_goes_to_human(self):
        reviewer = reviewer_without_models()
        reviewer._chat = lambda *args, **kwargs: "无法确定"
        initial_issue = {
            "type": "规则冲突",
            "pending_content": "允许带电检修。",
            "regulation_content": "严禁带电检修。",
        }
        with patch(
            "hybrid_rag_review_v9.HybridRAGReviewerV9.verify_review_result",
            return_value={"compliance_status": "合规", "issues": []},
        ):
            verified = reviewer.verify_review_result(
                {"content": "允许带电检修。"},
                [],
                {"compliance_status": "不合规", "issues": [initial_issue]},
                [],
            )

        self.assertEqual("不确定", verified["compliance_status"])
        self.assertEqual(1, verified["main_agent_adjudication"]["needs_human"])
        self.assertEqual(1, len(verified["_v10_adjudication_escalations"]))

    def test_numeric_tool_can_remove_llm_false_positive_after_main_adjudication(self):
        reviewer = reviewer_without_models()
        reviewer.mine_type = "non_outburst"
        reviewer._build_kb_context = lambda _results: "法规数值原文"
        reviewer._chat = lambda *args, **kwargs: (
            '{"decisions":[{"conflict_id":"N1",'
            '"decision":"采纳数值工具","reason":"待审触发阈值更严"}],'
            '"summary":"采纳工具结论"}'
        )
        quote = "甲烷浓度达到0.8%时报警"
        result = {
            "compliance_status": "不合规",
            "issues": [
                {
                    "type": "数值冲突",
                    "pending_content": quote,
                    "regulation_content": "报警浓度≥1.0%",
                    "description": "初审认为0.8%低于法规值",
                }
            ],
        }
        checks = [
            {
                "details": [
                    {
                        "pending_quote": quote,
                        "rule_quote": "报警浓度≥1.0%",
                        "verdict": "合规",
                        "explanation": "0.8%更早报警，更严格",
                        "evidence_grounded": True,
                    }
                ]
            }
        ]

        resolved, escalations, _elapsed = reviewer._adjudicate_numeric_only(
            {"content": quote}, [], result, checks
        )

        self.assertEqual("合规", resolved["compliance_status"])
        self.assertEqual([], resolved["issues"])
        self.assertEqual([], escalations)
        self.assertEqual(
            1,
            resolved["numeric_main_agent_adjudication"]["numeric_issues_removed"],
        )

    def test_numeric_tool_can_add_missed_issue_only_after_main_adjudication(self):
        reviewer = reviewer_without_models()
        reviewer.mine_type = "non_outburst"
        reviewer._build_kb_context = lambda _results: "法规数值原文"
        reviewer._chat = lambda *args, **kwargs: (
            '{"decisions":[{"conflict_id":"N1",'
            '"decision":"采纳数值工具","reason":"双侧原文可直接比较"}],'
            '"summary":"确认数值漏检"}'
        )
        quote = "甲烷浓度低于1.5%时方可复电"
        checks = [
            {
                "details": [
                    {
                        "parameter": "复电浓度",
                        "pending_quote": quote,
                        "rule_quote": "复电浓度<1.0%",
                        "verdict": "不合规",
                        "relation": "later_trigger",
                        "explanation": "1.5%高于法规1.0%",
                        "evidence_grounded": True,
                    }
                ]
            }
        ]

        resolved, escalations, _elapsed = reviewer._adjudicate_numeric_only(
            {"content": quote}, [], {"compliance_status": "合规", "issues": []}, checks
        )

        self.assertEqual("不合规", resolved["compliance_status"])
        self.assertEqual(1, len(resolved["issues"]))
        self.assertEqual([], escalations)
        self.assertEqual(
            1, resolved["numeric_main_agent_adjudication"]["numeric_issues_added"]
        )

    def test_unparseable_numeric_adjudication_stays_uncertain_and_escalates(self):
        reviewer = reviewer_without_models()
        reviewer.mine_type = "non_outburst"
        reviewer._build_kb_context = lambda _results: "法规数值原文"
        reviewer._chat = lambda *args, **kwargs: "无法确定"
        quote = "甲烷浓度低于1.5%时方可复电"
        checks = [
            {
                "details": [
                    {
                        "pending_quote": quote,
                        "rule_quote": "复电浓度<1.0%",
                        "verdict": "不合规",
                        "evidence_grounded": True,
                    }
                ]
            }
        ]

        resolved, escalations, _elapsed = reviewer._adjudicate_numeric_only(
            {"content": quote}, [], {"compliance_status": "合规", "issues": []}, checks
        )

        self.assertEqual("不确定", resolved["compliance_status"])
        self.assertEqual([], resolved["issues"])
        self.assertEqual(1, len(escalations))
        self.assertEqual("numeric_conflict", escalations[0]["type"])

    def test_reverse_numeric_conflict_is_resolved_in_main_review_chain(self):
        reviewer = reviewer_without_models()
        reviewer.mine_type = "non_outburst"
        reviewer._fatal = threading.Event()
        reviewer.review_chunk_with_llm = lambda pending, kb: {
            "compliance_status": "合规",
            "issues": [],
            "summary": "初审未发现问题",
        }
        reviewer._build_kb_context = lambda _results: "复电浓度<1.0%"
        reviewer._chat = lambda *args, **kwargs: (
            '{"decisions":[{"conflict_id":"N1",'
            '"decision":"采纳数值工具","reason":"双侧原文可比较"}],'
            '"summary":"确认漏检"}'
        )
        quote = "甲烷浓度低于1.5%时方可复电"
        numeric_result = {
            "has_pairs": True,
            "overall": "不合规",
            "details": [
                {
                    "parameter": "复电浓度",
                    "pending_quote": quote,
                    "rule_quote": "复电浓度<1.0%",
                    "verdict": "不合规",
                    "relation": "later_trigger",
                    "explanation": "1.5%高于法规1.0%",
                    "evidence_grounded": True,
                }
            ],
        }
        reviewer._run_v10_batch_numeric_check = lambda pending, kb: numeric_result
        kb_results = [
            {
                "chunk": {
                    "doc_name": "煤矿安全规程",
                    "content": "复电浓度<1.0%",
                }
            }
        ]

        result, _classes, _checks, escalations, timings = (
            reviewer.review_chunk_complete({"content": quote}, kb_results)
        )

        self.assertEqual("不合规", result["compliance_status"])
        self.assertEqual(1, result["numeric_batch_added"])
        self.assertEqual(1, len(result["issues"]))
        self.assertEqual([], escalations)
        self.assertGreaterEqual(timings["main_adjudication"], 0.0)

    def test_mine_type_note_and_index_are_switched_together(self):
        self.assertIn("非突出矿井", build_mine_applicability_note("non_outburst"))
        self.assertIn("通用条款", build_mine_applicability_note("outburst"))

        reviewer = reviewer_without_models()
        reviewer.mine_type = "non_outburst"
        reviewer.kb_chunks = [{"id": "kb_0"}]
        reviewer.kb_json_path = Path("demo_kb.json")
        with patch.object(reviewer, "load_knowledge_base") as load_kb, patch.object(
            reviewer, "build_index"
        ) as build_index:
            changed = reviewer.set_mine_type("outburst")

        self.assertTrue(changed)
        self.assertEqual("outburst", reviewer.mine_type)
        load_kb.assert_called_once_with("demo_kb.json")
        build_index.assert_called_once_with()

    def test_compliance_issue_dedupe_keeps_one_same_type_same_source(self):
        text = "机电设备检修时允许带电作业。"
        issues, removed = HybridRAGReviewerV10._dedupe_review_issues(
            [
                {"type": "规则冲突", "pending_content": text, "description": "A"},
                {"type": "规则冲突", "pending_content": text, "description": "B"},
                {"type": "数值冲突", "pending_content": text, "description": "C"},
            ]
        )

        self.assertEqual(2, len(issues))
        self.assertEqual(1, removed)

    def test_numeric_candidates_are_batched_once_per_chunk(self):
        reviewer = reviewer_without_models()
        reviewer._fatal = threading.Event()
        reviewer._llm_semaphore = threading.Semaphore(1)
        reviewer.llm_client = object()
        pending = {
            "content": (
                "瓦斯浓度达到1.8%时必须停止工作；"
                "瓦斯浓度低于1.3%时方可恢复作业。"
            ),
            "_v10_numeric_kb_results": [
                {
                    "chunk": {
                        "doc_name": "煤矿安全规程",
                        "article": "第一条",
                        "content": "达到1.5%时必须停止工作；复电浓度<1.0%。",
                    }
                }
            ],
        }
        calls = []

        def fake_numeric_check(*args, **kwargs):
            calls.append((args, kwargs))
            return {
                "has_pairs": False,
                "overall": "无可比数值",
                "details": [],
                "summary": "测试",
            }

        with patch("hybrid_rag_review_v10.numeric_compare.numeric_check", fake_numeric_check):
            checks = reviewer.run_numeric_checks(pending, [{"type": "数值冲突"}])

        self.assertEqual(1, len(calls))
        self.assertEqual(1, len(checks))
        self.assertEqual(2, checks[0]["candidate_segment_count"])

    def test_numeric_kb_context_keeps_long_table_prefix(self):
        long_table = (
            "第五百三十六条 甲烷传感器报警、断电、复电浓度必须符合表格要求。\n"
            "设置地点 | 报警浓度 | 断电浓度 | 复电浓度 | 断电范围\n"
            "采煤工作面回风隅角 | ≥1.0 | ≥1.5 | <1.0 | 工作面全部电源\n"
            + "其他表格内容" * 200
        )
        context = HybridRAGReviewerV10._numeric_kb_context(
            [
                {
                    "chunk": {
                        "doc_name": "煤矿安全规程",
                        "article": "第五百三十六条",
                        "content": long_table,
                    }
                }
            ]
        )

        self.assertIn("复电浓度", context)
        self.assertIn("<1.0", context)

    def test_review_status_is_normalized_when_issues_exist(self):
        reviewer = reviewer_without_models()
        result = reviewer._cohere_review_status(
            {"compliance_status": "合规", "issues": [{"type": "规则冲突"}]}
        )
        self.assertEqual("不合规", result["compliance_status"])
        self.assertTrue(result["status_normalized"])

    def test_review_error_is_not_normalized_to_compliant(self):
        reviewer = reviewer_without_models()
        reviewer._fatal = threading.Event()
        reviewer.review_chunk_with_llm = lambda pending, kb: {
            "error": "Connection error."
        }
        result, _classifications, _checks, escalations, _timings = (
            reviewer.review_chunk_complete({"content": "测试"}, [])
        )

        self.assertEqual("Connection error.", result["error"])
        self.assertNotIn("compliance_status", result)
        self.assertEqual("error", escalations[0]["type"])

    def test_strict_typo_filter_rejects_format_and_accepts_local_typo(self):
        reviewer = reviewer_without_models()
        reviewer._chat = lambda *args, **kwargs: "true"
        self.assertFalse(
            reviewer._verify_typo_issue(
                {
                    "wrong_char": "MPA",
                    "correct_char": "MPa",
                    "context": "压力为10MPA",
                    "reason": "大小写格式",
                }
            )
        )
        self.assertTrue(
            reviewer._verify_typo_issue(
                {
                    "wrong_char": "通疯",
                    "correct_char": "通风",
                    "context": "工作面通疯系统采用U型通风",
                    "reason": "通风系统为固定专业术语",
                }
            )
        )

    def test_typo_candidates_are_verified_in_one_batch_call(self):
        reviewer = reviewer_without_models()
        responses = iter(
            [
                """[
                    {"wrong_char":"通疯","correct_char":"通风","context":"工作面通疯系统正常","reason":"固定术语","confidence":"高"},
                    {"wrong_char":"割煤鸡","correct_char":"割煤机","context":"启动割煤鸡前先检查","reason":"设备名称","confidence":"高"}
                ]""",
                '{"accepted_indices":[1,2]}',
            ]
        )
        calls = []

        def fake_chat(*args, **kwargs):
            calls.append((args, kwargs))
            return next(responses)

        reviewer._chat = fake_chat
        result = reviewer.check_typos(
            {"content": "工作面通疯系统正常，启动割煤鸡前先检查设备。"}
        )

        self.assertEqual(2, len(calls))
        self.assertEqual(2, len(result["issues"]))
        self.assertEqual("通疯", result["issues"][0]["original"])
        self.assertEqual(2, result["diagnostics"]["initial_candidates"])
        self.assertEqual(2, result["diagnostics"]["after_local_filter"])
        self.assertEqual(2, result["diagnostics"]["batch_accepted"])
        self.assertIn("通疯", result["diagnostics"]["trace"])

    def test_evaluator_prefers_same_type_short_typo(self):
        gold = [
            {
                "error_id": "T001",
                "type": "typo",
                "subtype": "homophone",
                "mutated_text": "通疯系统",
                "original_text": "通风系统",
                "mutated_paragraph": "S1302工作面通疯系统采用U型通风系统。",
                "baseline_block_index": 400,
            }
        ]
        issues = [
            {
                "id": 1,
                "issue_type": "redundancy",
                "status": "重复",
                "title": "重复内容",
                "original_text": "S1302工作面通疯系统采用U型通风系统。",
                "block_indices": [400],
            },
            {
                "id": 2,
                "issue_type": "typo",
                "status": "错别字",
                "title": "错别字",
                "original_text": "通疯",
                "suggestion": "通风",
                "block_indices": [400],
            },
        ]
        rows, _ = match_cases(gold, issues, threshold=0.56)
        self.assertTrue(rows[0]["strict_hit"])
        self.assertEqual(2, rows[0]["predicted_issue_id"])


if __name__ == "__main__":
    unittest.main()
