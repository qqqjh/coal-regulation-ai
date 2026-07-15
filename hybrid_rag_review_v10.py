"""混合检索 RAG 审核系统 v10。

v10 在 v9 稳定检索/审查链上针对人工错误注入评测暴露的问题做小步升级：

1. 合规链：将高风险许可措辞和安全阈值作为强制核查清单并入初审，不再追加
   独立漏检复核调用；
2. 错别字链：所有候选先经代码过滤，再用一次批量复核调用完成质量控制；
3. 重复链：优先使用 Word ``source_units`` 做段落级确定性重复检测，避免长
   chunk 向量稀释，也默认关闭高噪声的整 chunk 语义重复告警；
4. Web Worker：禁用未被前端消费的法规立场分类调用，并通过文档级去重钩子
   消除重叠 chunk 的重复告警。

v9 文件保持不变；本文件用继承方式复用其模型加载、检索、数值核验和报告能力。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

import hybrid_rag_review_v9 as v9
from main_agent_v10 import MainAgentConflictAdjudicatorV10
import numeric_compare


V10_TOP_K = max(5, min(12, int(os.getenv("V10_TOP_K", "8"))))
V10_EXACT_DUPLICATE_MIN_CHARS = max(
    20, min(200, int(os.getenv("V10_EXACT_DUPLICATE_MIN_CHARS", "30")))
)
V10_RETRIEVAL_MAX_SEGMENTS = max(
    12, min(24, int(os.getenv("V10_RETRIEVAL_MAX_SEGMENTS", "18")))
)
_PERMISSIVE_RISK_PATTERN = re.compile(
    r"(?:允许|准许|可以|可继续|无需|不必|免于|不需要).{0,12}"
    r"(?:带电|检修|作业|生产|送电|断电|切断|电源|搬迁|进入|启动|运行|撤人|停工|停风|透水)"
    r"|(?:带电|检修|搬迁|电源|停风|透水).{0,12}(?:允许|可以|可继续|无需|不必)"
)
_THRESHOLD_TOPIC_PATTERN = re.compile(
    r"瓦斯|甲烷|一氧化碳|浓度|报警|断电|复电|停工|撤人|喷雾|压力|风量|风速"
)
_NUMERIC_VALUE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|％|MPa|kPa|Pa|m³/min|m3/min|m/s|m|mm|℃)"
)
_PURE_FORMAT_PATTERN = re.compile(r"^[\W\d_a-zA-Z]+$", re.UNICODE)
_HEADING_PATTERN = re.compile(
    r"^(?:第[一二三四五六七八九十百\d]+[章节]|"
    r"[一二三四五六七八九十]{1,3}、[^。；]{0,24})$"
)


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s\u3000]+", "", text).strip().lower()


class HybridRAGReviewerV10(v9.HybridRAGReviewerV9):
    """v10：v9 检索底座 + 初审高风险清单 + 批量错别字复核 + 精确段落重复。"""

    TYPO_SYSTEM_PROMPT = """你是一名煤矿作业规程错别字审校员。只报告能够高置信度确认的错字或写错的专业术语。

只报告：
1. 单字或短词中明确的错字，例如“通疯→通风”“割煤鸡→割煤机”；
2. 上下文能够唯一确定正确写法的煤矿专业术语错误；
3. 原文中真实出现、可直接进行局部替换的错误。

一律不报告：
- 标点、大小写、单位、上标、公式、编号、空格、排版或 OCR 符号问题；
- 语句润色、缺少“为/的”等表达优化、简称扩写、同义词替换；
- 不能唯一确定正确写法的生僻术语；
- 建议中带“可能、或、建议改为某某之一”等不确定说明的候选；
- 原文不存在的字词，或需要改写整句才能成立的候选。

输出严格 JSON 数组，不要 Markdown：
[
  {
    "wrong_char": "原文中实际出现的错误字词",
    "correct_char": "唯一且简短的正确字词",
    "context": "包含错误字词的原文上下文",
    "reason": "为何能确定是错字",
    "confidence": "高|中|低"
  }
]
没有高置信度错别字时返回 []。"""

    TYPO_VERIFY_PROMPT = """你是错别字质量控制智能体。判断候选是否为明确错别字。

原文上下文：{context}
候选：“{wrong_char}”→“{correct_char}”
理由：{reason}

只有同时满足以下条件才回答 true：
1. 错误词确实逐字出现在原文上下文中；
2. 修改是局部错字/术语纠正，不是润色、补词、简称扩写或格式统一；
3. 正确写法唯一确定，不含“可能、或、建议核实”；
4. 不是数字、单位、大小写、编号、标点、公式或 OCR 符号问题。

只回答 true 或 false。"""

    TYPO_BATCH_VERIFY_PROMPT = """你是煤矿作业规程错别字质量控制智能体。请一次性复核下列候选。

【候选列表】
{candidates_json}

只有同时满足以下条件的候选才可保留：
1. wrong_char 确实逐字出现在 context 中；
2. 修改是局部错字或专业术语纠正，不是润色、补词、简称扩写或同义词替换；
3. correct_char 唯一确定，理由不含“可能、或、建议核实”；
4. 不是数字、单位、大小写、编号、标点、公式、排版或 OCR 符号问题。

输出严格 JSON 对象，只返回应保留候选的 index：
{{"accepted_indices":[1,3]}}
没有候选应保留时返回：{{"accepted_indices":[]}}。"""

    def __init__(self, mine_type: str = v9.MINE_TYPE):
        # v9 方法读取模块级 TOP_K；v10 进程独立运行，安全地扩大最终法规上下文。
        v9.TOP_K = V10_TOP_K
        super().__init__(mine_type=mine_type)
        self._worker_seen_lock = threading.Lock()
        self._worker_seen: Dict[str, set[Tuple[Any, ...]]] = defaultdict(set)

    # ---------- Worker 文档级去重钩子 ----------

    def begin_worker_job(self, job_id: str) -> None:
        """开始新任务时清空该任务的跨 chunk 告警去重集合。"""
        with self._worker_seen_lock:
            self._worker_seen.pop(str(job_id), None)

    def claim_worker_issue(
        self,
        job_id: str,
        issue_type: str,
        original_text: str,
        suggestion: str,
        block_indices: Iterable[Any],
    ) -> bool:
        """相同错误、建议和 Word 定位只允许 Worker 落库一次。

        定位参与键值，因而不同段落中真实重复出现的同一错字仍会分别保留；只消除
        MinerU 重叠 chunk 对同一物理位置产生的重复告警。
        """
        blocks: List[int] = []
        for value in block_indices or []:
            try:
                blocks.append(int(value))
            except (TypeError, ValueError):
                continue
        key = (
            str(issue_type or "").strip().lower(),
            _compact(original_text),
            _compact(suggestion),
            tuple(sorted(set(blocks))),
        )
        with self._worker_seen_lock:
            seen = self._worker_seen[str(job_id)]
            if key in seen:
                return False
            seen.add(key)
            return True

    # ---------- 合规链：高风险清单直接并入初审 ----------

    @staticmethod
    def _atomic_risk_segments(content: str) -> List[str]:
        """把许可行为和单个数值阈值拆成独立检索query。

        这里只使用待审原文，不注入法规值或金标答案。用于避免“允许带电检修”等
        短句被同一长chunk中的防灭火、设备参数等内容稀释。
        """
        selected: List[str] = []
        for segment in re.split(r"[\r\n]+|(?<=[。；;])", str(content or "")):
            segment = segment.strip()
            if len(segment) < 8:
                continue
            permissive = bool(_PERMISSIVE_RISK_PATTERN.search(segment))
            numeric_threshold = (
                bool(_THRESHOLD_TOPIC_PATTERN.search(segment))
                and bool(_NUMERIC_VALUE_PATTERN.search(segment))
            )
            if not permissive and not numeric_threshold:
                continue
            selected.append(segment[:500])
        return list(dict.fromkeys(selected))

    @classmethod
    def _retrieval_segments(cls, content: str) -> List[str]:
        atomic = cls._atomic_risk_segments(content)
        expanded: List[str] = []
        for segment in atomic:
            if _PERMISSIVE_RISK_PATTERN.search(segment):
                expanded.append(
                    segment
                    + "\n法规禁止性要求：严禁 不得 必须停止 必须切断电源 必须撤人"
                )
            if (
                _THRESHOLD_TOPIC_PATTERN.search(segment)
                and _NUMERIC_VALUE_PATTERN.search(segment)
            ):
                expanded.append(
                    segment
                    + "\n同对象安全数值规则：报警浓度 断电浓度 复电浓度 "
                    "停工阈值 恢复条件 数值上限 数值下限"
                )
                if re.search(r"瓦斯|甲烷", segment) and re.search(
                    r"恢复|复电|送电|开启|开机|方可", segment
                ):
                    # 待审常写“恢复作业/开启设备”，法规表头常写“复电浓度”；
                    # 只做字段同义查询，不加入任何法规标准值。
                    expanded.append(
                        "甲烷传感器设置地点 报警浓度 断电浓度 复电浓度 "
                        "断电范围\n同对象安全数值规则：恢复作业 恢复送电 开启设备"
                    )
        generic = v9.split_chunk_segments(content)
        combined: List[str] = []
        seen: set[str] = set()
        for segment in [*expanded, *atomic, *generic]:
            key = _compact(segment)
            if not key or key in seen:
                continue
            seen.add(key)
            combined.append(segment)
        return combined[:V10_RETRIEVAL_MAX_SEGMENTS]

    @classmethod
    def _pending_encoding_inputs(
        cls, chunks: List[Dict]
    ) -> Tuple[List[str], List[List[str]], List[str], List[Tuple[int, int]], str]:
        """v10检索输入：整chunk + 通用片段 + 高风险原子句。"""
        texts = [str(chunk.get("content", "")) for chunk in chunks]
        segments_per_chunk = [cls._retrieval_segments(text) for text in texts]
        flat: List[str] = list(texts)
        seg_slices: List[Tuple[int, int]] = []
        for segments in segments_per_chunk:
            start = len(flat)
            flat.extend(segments)
            seg_slices.append((start, len(flat)))
        payload = json.dumps(
            {
                "version": "pending-v10-atomic-risk-v1",
                "max_length": v9.BGE_MAX_LENGTH,
                "texts": flat,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        cache_key = (
            "pending_v10_atomic_"
            + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        )
        return texts, segments_per_chunk, flat, seg_slices, cache_key

    def _rank_recalled_pairs(
        self,
        pairs: List[Tuple[str, int]],
        score_by_pair: Dict[Tuple[str, int], float],
        top_k: int = V10_TOP_K,
    ) -> List[Dict]:
        """按风险原子query保留法规覆盖，再用全局最高分补齐Top-K。

        v9只取所有query下的全局最高分，长chunk中多个错误可能被同一主题的高分
        条款占满。v10先给每个高风险扩展query一轮候选覆盖，再按全局分数补齐；
        该过程不使用金标或预期法规ID。
        """
        best_global: Dict[int, float] = {}
        coverage_queries: List[str] = []
        candidates_by_query: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        seen_queries: set[str] = set()
        for query, idx in pairs:
            score = score_by_pair.get((query, idx))
            if score is None:
                continue
            best_global[idx] = max(score, best_global.get(idx, -1.0))
            if "法规禁止性要求：" in query or "同对象安全数值规则：" in query:
                candidates_by_query[query].append((idx, score))
                if query not in seen_queries:
                    seen_queries.add(query)
                    coverage_queries.append(query)

        for query in coverage_queries:
            candidates_by_query[query].sort(key=lambda item: item[1], reverse=True)

        selected: List[int] = []
        selected_set: set[int] = set()
        # 两轮覆盖：第一轮每个风险query的首选，第二轮补充次选。
        for depth in range(2):
            for query in coverage_queries:
                candidates = candidates_by_query[query]
                if depth >= len(candidates):
                    continue
                idx, score = candidates[depth]
                if score < v9.RERANK_SCORE_THRESHOLD or idx in selected_set:
                    continue
                selected.append(idx)
                selected_set.add(idx)
                if len(selected) >= top_k:
                    break
            if len(selected) >= top_k:
                break

        for idx, score in sorted(
            best_global.items(), key=lambda item: item[1], reverse=True
        ):
            if len(selected) >= top_k:
                break
            if score < v9.RERANK_SCORE_THRESHOLD or idx in selected_set:
                continue
            selected.append(idx)
            selected_set.add(idx)

        # 最终仍按该法规在所有query中的最高相关分排序，保持上下文优先级稳定。
        selected.sort(key=lambda idx: best_global.get(idx, -1.0), reverse=True)
        return [
            {
                "chunk": self.kb_chunks[idx],
                "score": round(best_global[idx], 4),
            }
            for idx in selected
        ]

    @staticmethod
    def _risk_segments(content: str) -> List[str]:
        selected: List[str] = []
        for segment in v9.split_chunk_segments(content):
            permissive = bool(_PERMISSIVE_RISK_PATTERN.search(segment))
            numeric_threshold = (
                bool(_THRESHOLD_TOPIC_PATTERN.search(segment))
                and bool(_NUMERIC_VALUE_PATTERN.search(segment))
            )
            if permissive or numeric_threshold:
                selected.append(segment[:500])
            if len(selected) >= 8:
                break
        return selected

    @staticmethod
    def _human_feedback_experience_note(
        pending_chunk: Dict,
        allowed_types: set[str],
    ) -> str:
        examples = [
            item
            for item in (pending_chunk.get("_human_feedback_examples") or [])
            if str(item.get("issue_type") or "") in allowed_types
        ][:3]
        if not examples:
            return ""

        action_labels = {
            "accept": "人工确认问题属实并采纳建议",
            "custom": "人工确认问题属实并给出改写",
            "reject": "人工判定为误报",
        }
        lines = []
        for index, item in enumerate(examples, 1):
            extra = item.get("extra") or {}
            original = str(item.get("pending_content") or "").strip()[:220]
            final_text = str(extra.get("final_text") or "").strip()[:180]
            action = str(item.get("action") or "")
            line = (
                f"[{index}] {action_labels.get(action, action or '人工反馈')}；"
                f"历史原文：{original}"
            )
            if final_text and final_text != original:
                line += f"；人工最终文本：{final_text}"
            lines.append(line)
        return """

【历史人工反馈经验】
{examples}

这些是人工已经裁决的相似样本，只用于提醒容易确认或容易误报的模式：
- “人工判定为误报”的样本不得照搬为问题；
- “确认属实/人工改写”的样本仍须与本次原文和本次法规证据逐项核对；
- 历史反馈不能替代法规依据，场景、对象或动作不一致时不得套用。
""".format(examples="\n".join(lines))

    def _compliance_focus_note(self, pending_chunk: Dict) -> str:
        risk_segments = self._risk_segments(str(pending_chunk.get("content", "")))
        feedback_note = self._human_feedback_experience_note(
            pending_chunk,
            {"compliance", "numeric", "escalation"},
        )
        if not risk_segments:
            return feedback_note
        numbered = "\n".join(
            f"[{index}] {segment}" for index, segment in enumerate(risk_segments, 1)
        )
        risk_note = f"""

【v10 高风险句强制核查清单】
{numbered}

以上句子已经由代码筛出，必须在本次初审中逐句完成两类检查：
1. 禁止行为弱化：法规要求“严禁/必须停止/必须切断/必须撤人”，待审句却写成“允许/可以/可继续/无需/不必”，应报告规则冲突。不要被同一句中其他正确要求抵消。
2. 数值阈值：逐个列出待审句中的安全阈值，只与法规中同对象、同动作、同单位的数值比较。普通上下限按上下限比较；报警、断电、停工等保护触发值越大越晚触发，复电上限越大越宽松。

只允许引用本次初审提供的法规证据；证据对象、动作或单位不对应时不要猜测；
``pending_content`` 必须使用待审句中的最小原文片段；同一冲突只输出一次；
待审要求严于法规时不得报错。"""
        return risk_note + feedback_note

    def classify_kb_chunks(
        self, pending_chunk: Dict, kb_results: List[Dict]
    ) -> List[Dict]:
        """v10 前端链不消费法规立场分类，直接跳过对应 LLM 调用。"""
        return []

    # ---------- 数值链：chunk 内候选批量配对 + 双侧证据落地 ----------

    @classmethod
    def _numeric_risk_segments(cls, content: str) -> List[str]:
        return [
            segment
            for segment in cls._atomic_risk_segments(content)
            if _THRESHOLD_TOPIC_PATTERN.search(segment)
            and _NUMERIC_VALUE_PATTERN.search(segment)
        ]

    @staticmethod
    def _numeric_evidence_excerpt(text: str, max_chars: int = 650) -> str:
        """提取法规数值片段；超长表格保留开头，而不是因整句过长直接跳过。"""
        out = ""
        previous_non_numeric = ""
        for sentence in re.split(r"(?<=[。；;])|[\r\n]+", str(text or "")):
            if not re.search(r"\d", sentence):
                previous_non_numeric = sentence[-220:].strip()
                continue
            if previous_non_numeric:
                prefix = previous_non_numeric + "\n"
                remaining = max_chars - len(out)
                out += prefix[:remaining]
                previous_non_numeric = ""
            remaining = max_chars - len(out)
            if remaining <= 0:
                break
            excerpt = sentence[:remaining]
            out += excerpt
            if len(out) < max_chars:
                out += "\n"
            if len(sentence) > remaining:
                break
        return out.strip()

    @staticmethod
    def _numeric_kb_context(kb_results: List[Dict]) -> str:
        """压缩 Top-K 法规中的数值句，保留表格字段和原文数值。"""
        parts: List[str] = []
        total = 0
        for index, result in enumerate(kb_results, 1):
            chunk = result.get("chunk") or {}
            numeric_text = HybridRAGReviewerV10._numeric_evidence_excerpt(
                str(chunk.get("content", "")), 650
            )
            if not numeric_text:
                continue
            header = " | ".join(
                value
                for value in (
                    str(chunk.get("doc_name", "")).strip(),
                    str(chunk.get("article", "")).strip(),
                )
                if value
            )
            block = f"【法规{index}】{header}\n{numeric_text}"
            if total + len(block) > 5200:
                remaining = 5200 - total
                if remaining > 160:
                    parts.append(block[:remaining])
                break
            parts.append(block)
            total += len(block)
        return "\n".join(parts)

    def _run_v10_batch_numeric_check(
        self, pending_chunk: Dict, kb_results: List[Dict]
    ) -> Dict:
        segments = self._numeric_risk_segments(
            str(pending_chunk.get("content", ""))
        )
        rule_text = self._numeric_kb_context(kb_results)
        if not segments or not rule_text:
            return {
                "has_pairs": False,
                "overall": "无可比数值",
                "details": [],
                "summary": "待审内容或检索法规中无可批量配对的数值约束",
                "is_v10_batch_check": True,
                "candidate_segment_count": len(segments),
            }
        segment_batches = [segments[index:index + 5] for index in range(0, len(segments), 5)]

        def check_batch(batch: List[str]) -> Dict:
            pending_text = "\n".join(
                f"【待审数值句{index}】{segment}"
                for index, segment in enumerate(batch, 1)
            )
            with self._llm_semaphore:
                return numeric_compare.numeric_check(
                    self.llm_client,
                    v9.QWEN_MODEL,
                    pending_text,
                    rule_text,
                    pending_max_chars=2600,
                    rule_max_chars=5200,
                )

        if len(segment_batches) == 1:
            batch_results = [check_batch(segment_batches[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(2, len(segment_batches))) as pool:
                batch_results = list(pool.map(check_batch, segment_batches))

        details: List[Dict] = []
        rejected_pairs: List[Dict] = []
        raw_pair_count = 0
        seen_details: set[Tuple[str, str, str]] = set()
        for batch_result in batch_results:
            raw_pair_count += int(batch_result.get("raw_pair_count") or 0)
            rejected_pairs.extend(batch_result.get("rejected_pairs") or [])
            for detail in batch_result.get("details") or []:
                key = (
                    _compact(detail.get("pending_quote")),
                    str(detail.get("constraint_type", "")),
                    str(detail.get("verdict", "")),
                )
                if key in seen_details:
                    continue
                seen_details.add(key)
                details.append(detail)
        verdicts = [str(detail.get("verdict", "")) for detail in details]
        if "不合规" in verdicts:
            overall = "不合规"
        elif verdicts and all(verdict == "合规" for verdict in verdicts):
            overall = "合规"
        elif verdicts:
            overall = "不确定"
        else:
            overall = "无可比数值"
        result = {
            "has_pairs": bool(details),
            "overall": overall,
            "details": details,
            "summary": (
                f"数值核验 {len(details)} 对：合规 {verdicts.count('合规')}，"
                f"不合规 {verdicts.count('不合规')}，不确定 {verdicts.count('不确定')}"
                if details
                else "未抽取到有双侧原文依据的可配对数值约束"
            ),
            "raw_pair_count": raw_pair_count,
            "rejected_pair_count": len(rejected_pairs),
            "rejected_pairs": rejected_pairs,
            "numeric_batch_count": len(segment_batches),
        }
        result["is_v10_batch_check"] = True
        result["candidate_segment_count"] = len(segments)
        return result

    def run_numeric_checks(
        self, pending_chunk: Dict, issues: List[Dict]
    ) -> List[Dict]:
        """v10 每个 chunk 只调用一次配对模型，同时覆盖初审可能漏掉的数值句。"""
        kb_results = list(pending_chunk.get("_v10_numeric_kb_results") or [])
        if not kb_results or not self._numeric_risk_segments(
            str(pending_chunk.get("content", ""))
        ):
            return []
        if self._fatal.is_set():
            return []
        future = pending_chunk.get("_v10_numeric_future")
        if isinstance(future, Future):
            return [future.result()]
        return [self._run_v10_batch_numeric_check(pending_chunk, kb_results)]

    def run_counter_numeric_check(
        self, pending_chunk: Dict, kb_results: List[Dict]
    ) -> Dict:
        """初审未报问题时复用同一个批量配对入口。"""
        future = pending_chunk.get("_v10_numeric_future")
        if isinstance(future, Future):
            return future.result()
        return self._run_v10_batch_numeric_check(pending_chunk, kb_results)

    @staticmethod
    def _grounded_numeric_details(numeric_checks: List[Dict]) -> List[Dict]:
        """只返回可进入同步裁决的数值证据。

        工具结论必须同时具有待审原文、法规原文和明确结论；
        缺任一项都不允许覆盖 LLM 结果。
        """
        grounded: List[Dict] = []
        for check in numeric_checks:
            for detail in check.get("details") or []:
                if (
                    detail.get("verdict") not in {"合规", "不合规"}
                    or detail.get("evidence_grounded") is not True
                ):
                    continue
                pending_quote = str(detail.get("pending_quote", "")).strip()
                rule_quote = str(detail.get("rule_quote", "")).strip()
                if not pending_quote or not rule_quote:
                    continue
                grounded.append(dict(detail))
        return grounded

    @staticmethod
    def _numeric_detail_to_issue(detail: Dict) -> Dict:
        return {
            "type": "数值冲突",
            "pending_content": str(detail.get("pending_quote", "")).strip(),
            "regulation_content": str(detail.get("rule_quote", "")).strip(),
            "description": str(detail.get("explanation", "")).strip(),
            "suggestion": "按引用法规的同对象、同动作数值约束修正该参数。",
            "numeric_evidence_grounded": True,
            "numeric_parameter": str(detail.get("parameter", "")),
            "numeric_relation": str(detail.get("relation", "")),
        }

    @classmethod
    def _grounded_numeric_issues(cls, numeric_checks: List[Dict]) -> List[Dict]:
        additions: List[Dict] = []
        for detail in cls._grounded_numeric_details(numeric_checks):
            if detail.get("verdict") == "不合规":
                additions.append(cls._numeric_detail_to_issue(detail))
        return additions

    @staticmethod
    def _issue_matches_numeric_detail(issue: Dict, detail: Dict) -> bool:
        issue_text = _compact(issue.get("pending_content", ""))
        detail_text = _compact(detail.get("pending_quote", ""))
        if not issue_text or not detail_text:
            return False
        if detail_text not in issue_text and issue_text not in detail_text:
            return False
        issue_evidence = " ".join(
            str(issue.get(key, ""))
            for key in ("type", "pending_content", "regulation_content", "description")
        )
        return "数值" in str(issue.get("type", "")) or bool(
            _NUMERIC_VALUE_PATTERN.search(issue_evidence)
        )

    @classmethod
    def _numeric_llm_conflicts(
        cls,
        final_result: Dict,
        numeric_checks: List[Dict],
        initial_result: Optional[Dict] = None,
    ) -> List[Dict]:
        """逐项找出数值工具与 LLM 最终意见相反的有根据结论。"""
        details = cls._grounded_numeric_details(numeric_checks)
        final_issues = list(final_result.get("issues") or [])
        initial_issues = list((initial_result or {}).get("issues") or [])
        conflicts: List[Dict] = []
        next_id = 1

        # 最终仍报数值问题，但工具对同一原文判为合规：两级 LLM vs 数值工具。
        for issue_index, issue in enumerate(final_issues):
            matching = [
                detail
                for detail in details
                if cls._issue_matches_numeric_detail(issue, detail)
            ]
            verdicts = {str(detail.get("verdict", "")) for detail in matching}
            if verdicts != {"合规"}:
                continue
            conflicts.append(
                {
                    "conflict_id": f"N{next_id}",
                    "conflict_type": "numeric_tool_says_compliant",
                    "llm_issue": issue,
                    "llm_conclusion": "初审和复核均保留该数值问题",
                    "numeric_evidence": matching,
                    "_issue_index": issue_index,
                }
            )
            next_id += 1

        # 工具发现有根据的违规数值，但 LLM 最终结果未报该项。
        for detail in details:
            if detail.get("verdict") != "不合规":
                continue
            if any(cls._issue_matches_numeric_detail(issue, detail) for issue in final_issues):
                continue
            # 若初审曾报、仅被复核删除，属于 verifier_removed，不重复生成数值分歧。
            if any(cls._issue_matches_numeric_detail(issue, detail) for issue in initial_issues):
                continue
            conflicts.append(
                {
                    "conflict_id": f"N{next_id}",
                    "conflict_type": "numeric_tool_says_noncompliant",
                    "llm_conclusion": "LLM最终结果未报告该数值问题",
                    "numeric_evidence": [detail],
                    "_candidate_issue": cls._numeric_detail_to_issue(detail),
                }
            )
            next_id += 1
        return conflicts

    @staticmethod
    def _public_conflicts(conflicts: List[Dict]) -> List[Dict]:
        return [
            {key: value for key, value in conflict.items() if not key.startswith("_")}
            for conflict in conflicts
        ]

    @staticmethod
    def _apply_numeric_adjudication(
        result: Dict,
        conflicts: List[Dict],
        decision_by_id: Dict[str, Dict],
        adjudication: Dict,
        elapsed: float,
    ) -> Tuple[List[Dict], Dict]:
        """把主智能体对数值分歧的逐项决定回写为最终问题。"""
        issues = list(result.get("issues") or [])
        remove_indexes: set[int] = set()
        additions: List[Dict] = []
        escalations: List[Dict] = []
        decisions: List[Dict] = []
        kept_llm = 0
        accepted_tool = 0
        added = 0
        removed = 0
        unresolved = 0

        for conflict in conflicts:
            conflict_id = str(conflict.get("conflict_id", ""))
            conflict_type = str(conflict.get("conflict_type", ""))
            decision_item = decision_by_id.get(conflict_id) or {}
            decision = str(decision_item.get("decision", "")).strip()
            reason = str(decision_item.get("reason", "")).strip()
            allowed = {"保留LLM", "采纳数值工具"}
            if decision not in allowed:
                decision = "转人工"

            if decision == "保留LLM":
                kept_llm += 1
                issue_index = conflict.get("_issue_index")
                if isinstance(issue_index, int) and 0 <= issue_index < len(issues):
                    issues[issue_index] = dict(issues[issue_index])
                    issues[issue_index]["numeric_main_agent_adjudication"] = {
                        "decision": decision,
                        "reason": reason,
                        "conflict_id": conflict_id,
                    }
            elif decision == "采纳数值工具":
                accepted_tool += 1
                if conflict_type == "numeric_tool_says_compliant":
                    issue_index = conflict.get("_issue_index")
                    if isinstance(issue_index, int):
                        remove_indexes.add(issue_index)
                        removed += 1
                else:
                    candidate = dict(conflict.get("_candidate_issue") or {})
                    if candidate:
                        candidate["numeric_main_agent_adjudication"] = {
                            "decision": decision,
                            "reason": reason,
                            "conflict_id": conflict_id,
                        }
                        additions.append(candidate)
                        added += 1
            else:
                unresolved += 1
                escalations.append(
                    {
                        "type": v9.ESC_NUMERIC_CONFLICT,
                        "reason": (
                            f"主智能体未能稳定裁决{conflict_id}："
                            f"{reason or adjudication.get('summary') or '证据不足'}"
                        ),
                        "conflict_id": conflict_id,
                    }
                )
            decisions.append(
                {
                    "conflict_id": conflict_id,
                    "conflict_type": conflict_type,
                    "decision": decision,
                    "reason": reason,
                }
            )

        result["issues"] = [
            issue for index, issue in enumerate(issues) if index not in remove_indexes
        ] + additions
        metadata = {
            "called": bool(conflicts),
            "conflict_count": len(conflicts),
            "kept_llm": kept_llm,
            "accepted_numeric_tool": accepted_tool,
            "numeric_issues_added": added,
            "numeric_issues_removed": removed,
            "needs_human": unresolved,
            "decisions": decisions,
            "summary": adjudication.get("summary", ""),
            "parse_error": bool(adjudication.get("parse_error")),
            "elapsed_seconds": round(elapsed, 4),
        }
        result["numeric_main_agent_adjudication"] = metadata
        return escalations, metadata

    @staticmethod
    def _dedupe_review_issues(issues: List[Dict]) -> Tuple[List[Dict], int]:
        deduped: List[Dict] = []
        removed = 0
        for candidate in issues:
            candidate_type = str(candidate.get("type", ""))
            candidate_text = _compact(candidate.get("pending_content", ""))
            duplicate = False
            for current in deduped:
                if str(current.get("type", "")) != candidate_type:
                    continue
                current_text = _compact(current.get("pending_content", ""))
                if candidate_text and current_text and (
                    candidate_text in current_text or current_text in candidate_text
                ):
                    duplicate = True
                    break
            if duplicate:
                removed += 1
            else:
                deduped.append(candidate)
        return deduped, removed

    @staticmethod
    def _grounded_permissive_conflict(
        pending_chunk: Dict, issue: Dict
    ) -> bool:
        if str(issue.get("type", "")) != "规则冲突":
            return False
        pending_quote = str(issue.get("pending_content", "")).strip()
        rule_quote = str(issue.get("regulation_content", "")).strip()
        if not pending_quote or not rule_quote:
            return False
        if _compact(pending_quote) not in _compact(pending_chunk.get("content", "")):
            return False
        if not _PERMISSIVE_RISK_PATTERN.search(pending_quote):
            return False
        return bool(
            re.search(
                r"严禁|不得|禁止|必须(?:停止|切断|撤出|撤人)", rule_quote
            )
        )

    @staticmethod
    def _review_issues_equivalent(initial_issue: Dict, verified_issue: Dict) -> bool:
        """判断复核结果中是否仍保留了同一条初审问题。"""
        initial_type = str(initial_issue.get("type", "")).strip()
        verified_type = str(verified_issue.get("type", "")).strip()
        if initial_type and verified_type and initial_type != verified_type:
            return False
        initial_text = _compact(initial_issue.get("pending_content", ""))
        verified_text = _compact(verified_issue.get("pending_content", ""))
        if not initial_text or not verified_text:
            return False
        return initial_text in verified_text or verified_text in initial_text

    @classmethod
    def _verification_conflicts(
        cls,
        pending_chunk: Dict,
        initial_result: Dict,
        verified_result: Dict,
    ) -> List[Dict]:
        """找出初审提出、但复核删除的意见；使用贪心匹配保留一对一关系。"""
        verified_issues = list(verified_result.get("issues") or [])
        used_verified: set[int] = set()
        conflicts: List[Dict] = []
        for index, initial_issue in enumerate(initial_result.get("issues") or [], 1):
            matched_index: Optional[int] = None
            for current_index, verified_issue in enumerate(verified_issues):
                if current_index in used_verified:
                    continue
                if cls._review_issues_equivalent(initial_issue, verified_issue):
                    matched_index = current_index
                    break
            if matched_index is not None:
                used_verified.add(matched_index)
                continue
            conflicts.append(
                {
                    "conflict_id": f"C{index}",
                    "conflict_type": "verifier_removed",
                    "initial_issue": initial_issue,
                    "verifier_decision": "删除初审问题",
                    "verifier_notes": verified_result.get("verification_notes", ""),
                    "high_risk_direct_conflict": cls._grounded_permissive_conflict(
                        pending_chunk, initial_issue
                    ),
                }
            )
        return conflicts

    def verify_review_result(
        self,
        pending_chunk: Dict,
        kb_results: List[Dict],
        initial_result: Dict,
        numeric_checks: Optional[List[Dict]] = None,
    ) -> Dict:
        """把初审/复核分歧与数值工具/LLM 分歧合并为一次同步裁决。"""
        verified = super().verify_review_result(
            pending_chunk, kb_results, initial_result, numeric_checks
        )
        verifier_conflicts = self._verification_conflicts(
            pending_chunk, initial_result, verified
        )
        numeric_conflicts = self._numeric_llm_conflicts(
            verified, list(numeric_checks or []), initial_result
        )
        conflicts = verifier_conflicts + numeric_conflicts
        if not conflicts:
            verified["main_agent_adjudication"] = {
                "called": False,
                "conflict_count": 0,
                "reason": "初审、复核与数值工具无意见冲突",
            }
            verified["numeric_main_agent_adjudication"] = {
                "called": False,
                "conflict_count": 0,
            }
            return verified

        started = time.perf_counter()
        try:
            adjudication = MainAgentConflictAdjudicatorV10(self._chat).adjudicate(
                pending_content=str(pending_chunk.get("content", "")),
                kb_context=self._build_kb_context(kb_results),
                initial_result=initial_result,
                verified_result=verified,
                conflicts=self._public_conflicts(conflicts),
                numeric_checks=list(numeric_checks or []),
                mine_type=getattr(self, "mine_type", "non_outburst"),
            )
        except v9.FatalAPIError:
            raise
        except Exception as exc:
            adjudication = {
                "decisions": [],
                "summary": "主智能体调用失败，已转人工",
                "parse_error": True,
                "error": str(exc)[:500],
            }

        decision_by_id = {
            str(item.get("conflict_id", "")): item
            for item in adjudication.get("decisions") or []
            if isinstance(item, dict)
        }
        issues = list(verified.get("issues") or [])
        decisions: List[Dict] = []
        unresolved_escalations: List[Dict] = []
        kept = 0
        dropped = 0
        unresolved = 0
        for conflict in verifier_conflicts:
            conflict_id = str(conflict["conflict_id"])
            decision_item = decision_by_id.get(conflict_id) or {}
            decision = str(decision_item.get("decision", "")).strip()
            reason = str(decision_item.get("reason", "")).strip()
            if decision == "保留初审":
                restored_issue = dict(conflict["initial_issue"])
                restored_issue["main_agent_adjudication"] = {
                    "decision": decision,
                    "reason": reason,
                    "conflict_id": conflict_id,
                }
                issues.append(restored_issue)
                kept += 1
            elif decision == "采纳复核":
                dropped += 1
            else:
                decision = "转人工"
                unresolved += 1
                unresolved_escalations.append(
                    {
                        "type": v9.ESC_DISAGREEMENT,
                        "reason": (
                            f"主智能体未能稳定裁决{conflict_id}："
                            f"{reason or adjudication.get('summary') or '证据不足'}"
                        ),
                        "conflict_id": conflict_id,
                    }
                )
            decisions.append(
                {
                    "conflict_id": conflict_id,
                    "conflict_type": "verifier_removed",
                    "decision": decision,
                    "reason": reason,
                }
            )

        verified["issues"] = issues
        elapsed = time.perf_counter() - started
        numeric_escalations, numeric_metadata = self._apply_numeric_adjudication(
            verified,
            numeric_conflicts,
            decision_by_id,
            adjudication,
            elapsed,
        )
        unresolved_escalations.extend(numeric_escalations)
        total_unresolved = unresolved + int(numeric_metadata.get("needs_human") or 0)
        if total_unresolved:
            verified["compliance_status"] = "不确定"
        elif verified.get("issues"):
            verified["compliance_status"] = "不合规"
        else:
            verified["compliance_status"] = "合规"
        verified["main_agent_adjudication"] = {
            "called": True,
            "conflict_count": len(conflicts),
            "kept_initial": kept,
            "accepted_verifier": dropped,
            "kept_llm": numeric_metadata.get("kept_llm", 0),
            "accepted_numeric_tool": numeric_metadata.get(
                "accepted_numeric_tool", 0
            ),
            "numeric_issues_added": numeric_metadata.get("numeric_issues_added", 0),
            "numeric_issues_removed": numeric_metadata.get(
                "numeric_issues_removed", 0
            ),
            "needs_human": total_unresolved,
            "decisions": decisions + list(numeric_metadata.get("decisions") or []),
            "summary": adjudication.get("summary", ""),
            "parse_error": bool(adjudication.get("parse_error")),
            "elapsed_seconds": round(elapsed, 4),
        }
        if unresolved_escalations:
            verified["_v10_adjudication_escalations"] = unresolved_escalations
        verified["verification_notes"] = (
            str(verified.get("verification_notes", "")).rstrip("。")
            + (
                f"；主智能体裁决{len(conflicts)}项分歧："
                f"保留初审{kept}项、采纳复核{dropped}项、"
                f"保留LLM{numeric_metadata.get('kept_llm', 0)}项、"
                f"采纳数值工具{numeric_metadata.get('accepted_numeric_tool', 0)}项、"
                f"转人工{total_unresolved}项"
            )
        ).lstrip("；")
        return verified

    def _adjudicate_numeric_only(
        self,
        pending_chunk: Dict,
        kb_results: List[Dict],
        result: Dict,
        numeric_checks: List[Dict],
    ) -> Tuple[Dict, List[Dict], float]:
        """处理“初审未报问题”路径上的反向数值分歧。"""
        conflicts = self._numeric_llm_conflicts(result, numeric_checks)
        if not conflicts:
            result["numeric_main_agent_adjudication"] = {
                "called": False,
                "conflict_count": 0,
            }
            return result, [], 0.0

        started = time.perf_counter()
        try:
            adjudication = MainAgentConflictAdjudicatorV10(self._chat).adjudicate(
                pending_content=str(pending_chunk.get("content", "")),
                kb_context=self._build_kb_context(kb_results),
                initial_result=result,
                verified_result=result,
                conflicts=self._public_conflicts(conflicts),
                numeric_checks=numeric_checks,
                mine_type=getattr(self, "mine_type", "non_outburst"),
            )
        except v9.FatalAPIError:
            raise
        except Exception as exc:
            adjudication = {
                "decisions": [],
                "summary": "主智能体调用失败，已转人工",
                "parse_error": True,
                "error": str(exc)[:500],
            }
        elapsed = time.perf_counter() - started
        decision_by_id = {
            str(item.get("conflict_id", "")): item
            for item in adjudication.get("decisions") or []
            if isinstance(item, dict)
        }
        escalations, metadata = self._apply_numeric_adjudication(
            result, conflicts, decision_by_id, adjudication, elapsed
        )
        if metadata.get("needs_human"):
            result["compliance_status"] = "不确定"
        elif result.get("issues"):
            result["compliance_status"] = "不合规"
        else:
            result["compliance_status"] = "合规"

        previous = dict(result.get("main_agent_adjudication") or {})
        previous_decisions = list(previous.get("decisions") or [])
        result["main_agent_adjudication"] = {
            **previous,
            "called": True,
            "conflict_count": int(previous.get("conflict_count") or 0)
            + len(conflicts),
            "kept_llm": int(previous.get("kept_llm") or 0)
            + int(metadata.get("kept_llm") or 0),
            "accepted_numeric_tool": int(previous.get("accepted_numeric_tool") or 0)
            + int(metadata.get("accepted_numeric_tool") or 0),
            "numeric_issues_added": int(previous.get("numeric_issues_added") or 0)
            + int(metadata.get("numeric_issues_added") or 0),
            "numeric_issues_removed": int(previous.get("numeric_issues_removed") or 0)
            + int(metadata.get("numeric_issues_removed") or 0),
            "needs_human": int(previous.get("needs_human") or 0)
            + int(metadata.get("needs_human") or 0),
            "decisions": previous_decisions + list(metadata.get("decisions") or []),
            "summary": adjudication.get("summary", ""),
            "parse_error": bool(adjudication.get("parse_error")),
            "elapsed_seconds": round(
                float(previous.get("elapsed_seconds") or 0.0) + elapsed, 4
            ),
        }
        result["summary"] = (
            str(result.get("summary", "")).rstrip("。")
            + f"；主智能体同步裁决{len(conflicts)}项数值分歧："
            + f"保留LLM{metadata.get('kept_llm', 0)}项、"
            + f"采纳数值工具{metadata.get('accepted_numeric_tool', 0)}项、"
            + f"转人工{metadata.get('needs_human', 0)}项"
        ).lstrip("；")
        return result, escalations, elapsed

    def review_chunk_complete(
        self, pending_chunk: Dict, kb_results: List[Dict]
    ) -> Tuple[Dict, List[Dict], List[Dict], List[Dict], Dict[str, float]]:
        """运行v10审查、数值复核与同步主智能体裁决链。"""
        numeric_pending_chunk = dict(pending_chunk)
        numeric_pending_chunk["_v10_numeric_kb_results"] = kb_results
        numeric_executor: Optional[ThreadPoolExecutor] = None
        if kb_results and self._numeric_risk_segments(
            str(numeric_pending_chunk.get("content", ""))
        ) and not self._fatal.is_set():
            numeric_executor = ThreadPoolExecutor(max_workers=1)
            numeric_pending_chunk["_v10_numeric_future"] = numeric_executor.submit(
                self._run_v10_batch_numeric_check,
                numeric_pending_chunk,
                kb_results,
            )
        try:
            result, _classifications, numeric_checks, escalations, timings = (
                super().review_chunk_complete(numeric_pending_chunk, kb_results)
            )
        finally:
            if numeric_executor is not None:
                numeric_executor.shutdown(wait=True)
        adjudication = result.get("main_agent_adjudication") or {}
        for escalation in result.pop("_v10_adjudication_escalations", []):
            if not any(
                item.get("type") == escalation.get("type")
                and item.get("reason") == escalation.get("reason")
                for item in escalations
            ):
                escalations.append(escalation)
        if not result.get("error") and not result.get("parse_error"):
            grounded_additions = self._grounded_numeric_issues(numeric_checks)
            result["numeric_batch_candidates"] = len(grounded_additions)
            # 有问题的路径已在 verify_review_result 中与初审/复核分歧
            # 合并为一次裁决；初审无问题时在此裁决反向工具结果。
            if "numeric_main_agent_adjudication" not in result:
                result, numeric_escalations, _numeric_elapsed = (
                    self._adjudicate_numeric_only(
                        numeric_pending_chunk, kb_results, result, numeric_checks
                    )
                )
                escalations.extend(numeric_escalations)

            numeric_meta = result.get("numeric_main_agent_adjudication") or {}
            result["numeric_batch_added"] = int(
                numeric_meta.get("numeric_issues_added", 0)
            )
            if numeric_meta.get("called") and not numeric_meta.get("needs_human"):
                # v9 在同步裁决之前生成的泛化数值升级项已经过期。
                escalations = [
                    item
                    for item in escalations
                    if item.get("type")
                    not in {v9.ESC_NUMERIC_CONFLICT, v9.ESC_COUNTER_CHECK}
                ]
            deduped_issues, deduped_count = self._dedupe_review_issues(
                list(result.get("issues") or [])
            )
            result["issues"] = deduped_issues
            result["deduped_compliance_issues"] = deduped_count
        adjudication = result.get("main_agent_adjudication") or {}
        timings["main_adjudication"] = float(
            adjudication.get("elapsed_seconds") or 0.0
        )
        # 调用错误/JSON解析错误不是业务“合规”，不得被状态归一逻辑掩盖。
        if not result.get("error") and not result.get("parse_error"):
            result = self._cohere_review_status(result)
        return result, [], numeric_checks, escalations, timings

    # ---------- 错别字链：本地预过滤 + 单次批量复核 ----------

    @staticmethod
    def _is_plausible_typo_issue(issue: Dict) -> bool:
        wrong = str(issue.get("wrong_char", "")).strip()
        correct = str(issue.get("correct_char", "")).strip()
        context = str(issue.get("context", "")).strip()
        combined = f"{wrong}{correct}"
        if not wrong or not correct or wrong == correct:
            return False
        if context and wrong not in context:
            return False
        if _PURE_FORMAT_PATTERN.fullmatch(combined):
            return False
        if len(correct) > max(12, len(wrong) + 4):
            return False
        uncertainty = "可能|或为|之一|建议核实|不确定|ocr|格式|编号|上标|大小写"
        return not re.search(
            uncertainty, f"{correct} {issue.get('reason', '')}", re.IGNORECASE
        )

    def _verify_typo_issue(self, issue: Dict) -> bool:
        """保留单候选兼容入口；生产 check_typos 使用批量复核。"""
        if not self._is_plausible_typo_issue(issue):
            return False
        wrong = str(issue.get("wrong_char", "")).strip()
        correct = str(issue.get("correct_char", "")).strip()
        context = str(issue.get("context", "")).strip()
        prompt = self.TYPO_VERIFY_PROMPT.format(
            context=context,
            wrong_char=wrong,
            correct_char=correct,
            reason=issue.get("reason", ""),
        )
        try:
            answer = self._chat(
                [{"role": "user", "content": prompt}], temperature=0, max_tokens=5
            )
            return answer.strip().lower().startswith("true")
        except v9.FatalAPIError:
            raise
        except Exception:
            # 复核失败时宁可不报，避免把大量存疑候选灌入前端。
            return False

    def _verify_typo_issues_batch(self, issues: List[Dict]) -> List[Dict]:
        plausible = [item for item in issues if self._is_plausible_typo_issue(item)]
        if not plausible:
            return []
        payload = [
            {
                "index": index,
                "wrong_char": item.get("wrong_char", ""),
                "correct_char": item.get("correct_char", ""),
                "context": item.get("context", ""),
                "reason": item.get("reason", ""),
            }
            for index, item in enumerate(plausible, 1)
        ]
        prompt = self.TYPO_BATCH_VERIFY_PROMPT.format(
            candidates_json=json.dumps(payload, ensure_ascii=False, indent=2)
        )
        try:
            raw = self._chat(
                [{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max(80, min(600, 30 + len(plausible) * 12)),
            )
            parsed = self._parse_json_response(raw)
            accepted: set[int] = set()
            for value in (parsed or {}).get("accepted_indices", []):
                try:
                    accepted.add(int(value))
                except (TypeError, ValueError):
                    continue
            return [
                item for index, item in enumerate(plausible, 1) if index in accepted
            ]
        except v9.FatalAPIError:
            raise
        except Exception:
            # 与原严格复核策略一致：批量复核失败时不把存疑候选灌入前端。
            return []

    @staticmethod
    def _typo_diagnostics(
        initial: List[Any], filtered: List[Dict], accepted: List[Dict]
    ) -> Dict[str, Any]:
        def terms(items: List[Any]) -> List[str]:
            values: List[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("wrong_char", "")).strip()
                if value and value not in values:
                    values.append(value)
                if len(values) >= 10:
                    break
            return values

        initial_terms = terms(initial)
        accepted_terms = terms(accepted)
        trace = f"候选{len(initial)}/{len(filtered)}/{len(accepted)}"
        if initial_terms:
            trace += f"[初:{','.join(initial_terms)}|留:{','.join(accepted_terms) or '-'}]"
        return {
            "initial_candidates": len(initial),
            "after_local_filter": len(filtered),
            "batch_accepted": len(accepted),
            "initial_terms": initial_terms,
            "accepted_terms": accepted_terms,
            "trace": trace[:180],
        }

    def check_typos(self, pending_chunk: Dict) -> Dict:
        content = pending_chunk.get("content", "").strip()
        empty_diagnostics = self._typo_diagnostics([], [], [])
        default = {
            "has_issues": False,
            "issues": [],
            "summary": "未发现错别字",
            "diagnostics": empty_diagnostics,
        }
        if len(content) < v9.TYPO_MIN_CHUNK_LEN:
            return default

        feedback_note = self._human_feedback_experience_note(
            pending_chunk,
            {"typo"},
        )
        user_msg = f"请检查以下文本中的错别字：\n\n{content}{feedback_note}"
        last_error = ""
        for attempt in range(3):
            try:
                raw = self._strip_json_fence(
                    self._chat(
                        [
                            {"role": "system", "content": self.TYPO_SYSTEM_PROMPT},
                            {"role": "user", "content": user_msg},
                        ],
                        temperature=0.0,
                    )
                )
                if not raw:
                    raise ValueError("空响应")
                result = json.loads(raw)
                if not isinstance(result, list):
                    return default

                skip_keywords = ("未出现", "暂不报", "无需报告", "不报告", "不存在", "无错")
                seen: set[Tuple[str, str, str]] = set()
                candidates: List[Dict] = []
                for item in result:
                    if not isinstance(item, dict):
                        continue
                    wrong = str(item.get("wrong_char", "")).strip()
                    correct = str(item.get("correct_char", "")).strip()
                    if not wrong or wrong == correct:
                        continue
                    if any(kw in correct for kw in skip_keywords):
                        continue
                    if any(kw in str(item.get("reason", "")) for kw in skip_keywords):
                        continue
                    key = (_compact(wrong), _compact(correct), _compact(item.get("context")))
                    if key in seen or not self._is_plausible_typo_issue(item):
                        continue
                    seen.add(key)
                    candidates.append(item)

                verified = self._verify_typo_issues_batch(candidates)
                diagnostics = self._typo_diagnostics(result, candidates, verified)
                issues = [
                    {
                        **item,
                        "original": item.get("wrong_char", ""),
                        "suggestion": item.get("correct_char", ""),
                    }
                    for item in verified
                ]
                return {
                    "has_issues": bool(issues),
                    "issues": issues,
                    "summary": (
                        f"发现 {len(issues)} 处高置信度错别字"
                        if issues else "未发现错别字"
                    ),
                    "diagnostics": diagnostics,
                }
            except v9.FatalAPIError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(1)
        return {
            **default,
            "summary": f"检查异常（重试3次）: {last_error[:60]}",
        }

    # ---------- 重复链：段落级确定性检测 ----------

    @staticmethod
    def _normalise_paragraph(text: str) -> str:
        value = unicodedata.normalize("NFKC", str(text or ""))
        value = value.replace("；", ";").replace("，", ",")
        return re.sub(r"[\s\u3000]+", "", value).strip()

    @staticmethod
    def _unit_pdf_location(unit: Dict[str, Any]) -> Dict[str, Any]:
        if str(unit.get("coordinate_space") or "") != "original_pdf":
            return {}
        try:
            page = int(unit.get("page") or 0)
            bbox = [round(float(value), 3) for value in (unit.get("bbox") or [])]
        except (TypeError, ValueError):
            return {}
        if page < 1 or len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return {}
        location: Dict[str, Any] = {
            "page": page,
            "bbox": bbox,
            "source_unit_id": str(unit.get("source_unit_id") or ""),
        }
        for key in ("page_width", "page_height"):
            try:
                value = float(unit.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                location[key] = round(value, 3)
        return location

    @classmethod
    def _paragraph_occurrences(cls, chunks: List[Dict]) -> List[Dict]:
        occurrences: List[Dict] = []
        physical_seen: set[Tuple[Any, ...]] = set()
        for chunk_index, chunk in enumerate(chunks):
            units = list(chunk.get("source_units") or [])
            if not units:
                units = [
                    {"text": line, "source_blocks": [], "kind": "paragraph"}
                    for line in str(chunk.get("content", "")).splitlines()
                    if line.strip()
                ]
            for unit_index, unit in enumerate(units):
                text = str(unit.get("text", "")).strip()
                normalised = cls._normalise_paragraph(text)
                if len(normalised) < V10_EXACT_DUPLICATE_MIN_CHARS:
                    continue
                if len(normalised) < 50 and _HEADING_PATTERN.fullmatch(normalised):
                    continue
                blocks: List[int] = []
                for value in unit.get("source_blocks") or []:
                    try:
                        blocks.append(int(value))
                    except (TypeError, ValueError):
                        continue
                pdf_location = cls._unit_pdf_location(unit)
                source_unit_id = str(unit.get("source_unit_id") or "").strip()
                physical_key: Tuple[Any, ...]
                if pdf_location and source_unit_id:
                    physical_key = ("pdf_unit", source_unit_id)
                elif blocks:
                    physical_key = ("blocks", *sorted(set(blocks)))
                else:
                    physical_key = ("fallback", chunk_index, unit_index)
                # 同一 Word 段落可能被相邻 MinerU chunk 重复携带，不算内容重复。
                if physical_key in physical_seen:
                    continue
                physical_seen.add(physical_key)
                occurrences.append(
                    {
                        "chunk_index": chunk_index,
                        "text": text,
                        "normalised": normalised,
                        "block_indices": sorted(set(blocks)),
                        "pdf_location": pdf_location,
                        "physical_key": physical_key,
                    }
                )
        return occurrences

    def check_document_repetition(
        self, doc_name: str, chunks: List[Dict], dense_vecs: np.ndarray
    ) -> Tuple[Dict[int, Dict], Dict[str, float]]:
        """检测规范化后完全一致的 Word 段落，每组只输出一条精确定位告警。"""
        started = time.time()
        results = {
            index: self._empty_repetition_result() for index in range(len(chunks))
        }
        groups: Dict[str, List[Dict]] = defaultdict(list)
        for occurrence in self._paragraph_occurrences(chunks):
            groups[occurrence["normalised"]].append(occurrence)

        duplicate_groups = 0
        duplicate_occurrences = 0
        for occurrences in groups.values():
            if len(occurrences) < 2:
                continue
            ordered = sorted(
                occurrences,
                key=lambda item: (
                    int((item.get("pdf_location") or {}).get("page") or 10**9),
                    float(((item.get("pdf_location") or {}).get("bbox") or [10**9, 10**9])[1]),
                    item["block_indices"][0] if item["block_indices"] else 10**9,
                    item["chunk_index"],
                ),
            )
            all_blocks = sorted(
                {
                    block
                    for occurrence in ordered
                    for block in occurrence["block_indices"]
                }
            )
            pdf_unit_ids = {
                str((occurrence.get("pdf_location") or {}).get("source_unit_id") or "")
                for occurrence in ordered
                if occurrence.get("pdf_location")
            }
            if all_blocks and len(all_blocks) < 2 and len(pdf_unit_ids) < 2:
                continue
            owner = ordered[-1]
            source = ordered[0]
            duplicate_groups += 1
            duplicate_occurrences += len(ordered)
            block_label = "、".join(str(value) for value in all_blocks) or "未映射"
            duplicate = {
                "redundancy_type": "精确重复",
                "similarity": 1.0,
                "description": f"规范化后完全一致的段落，Word块：{block_label}",
                "note": f"发现完全重复段落，Word块：{block_label}",
                "match_method": "exact_paragraph",
                "original_text": owner["text"],
                "block_indices": all_blocks,
                "source_block_indices": source["block_indices"],
                "duplicate_block_indices": owner["block_indices"],
                "source_pdf_locations": [source["pdf_location"]] if source.get("pdf_location") else [],
                "duplicate_pdf_locations": [owner["pdf_location"]] if owner.get("pdf_location") else [],
                "chunk_ref": f"Chunk#{source['chunk_index'] + 1}",
                "occurrence_count": len(ordered),
            }
            owner_result = results[owner["chunk_index"]]
            owner_result["duplicates"].append(duplicate)
            owner_result["has_duplicates"] = True
            owner_result["persistence_mode"] = "v10_exact_paragraph"

        for result in results.values():
            if result["has_duplicates"]:
                result["summary"] = (
                    f"发现 {len(result['duplicates'])} 组段落级精确重复"
                )
        elapsed = time.time() - started
        return results, {
            "embedding": 0.0,
            "verification": 0.0,
            "exact_detection": elapsed,
            "total": elapsed,
            "candidates": duplicate_groups,
            "exact_groups": duplicate_groups,
            "duplicate_occurrences": duplicate_occurrences,
        }

    # ---------- v10 独立产物名 ----------

    def save_results(self, results: Dict, output_path: Optional[str] = None):
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = v9.OUTPUT_DIR / f"review_result_v10_{timestamp}.json"
        return super().save_results(results, output_path)

    def generate_report(self, results: Dict, output_path: Optional[str] = None):
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = v9.OUTPUT_DIR / f"review_report_v10_{timestamp}.html"
        report_path = Path(super().generate_report(results, output_path))
        html = report_path.read_text(encoding="utf-8")
        html = html.replace("审核报告 v9", "审核报告 v10")
        report_path.write_text(html, encoding="utf-8")
        return report_path


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="煤矿作业规程合规审核系统 v10")
    parser.add_argument("--kb", default=None, help="知识库 chunks JSON 路径")
    parser.add_argument("--pending", default=None, help="待审 chunks JSON 路径")
    parser.add_argument("--doc-filter", default="", help="待审文档名过滤，逗号分隔")
    parser.add_argument("--max-docs", type=int, default=v9.MAX_PENDING_DOCS or 0)
    parser.add_argument(
        "--mine-type", default=v9.MINE_TYPE, choices=["non_outburst", "outburst"]
    )
    parser.add_argument("--concurrency", type=int, default=v9.CHUNK_CONCURRENCY)
    parser.add_argument("--no-report", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    args = parser.parse_args(argv)

    reviewer = HybridRAGReviewerV10(mine_type=args.mine_type)
    reviewer.load_knowledge_base(args.kb)
    reviewer.build_index()
    results = reviewer.review_pending_document(
        pending_json_path=args.pending,
        doc_filter=[item.strip() for item in args.doc_filter.split(",") if item.strip()],
        max_documents=args.max_docs if args.max_docs > 0 else None,
        chunk_concurrency=args.concurrency,
        fresh=args.fresh,
    )
    if results:
        reviewer.save_results(results)
        if not args.no_report:
            reviewer.generate_report(results)
    return results


if __name__ == "__main__":
    main()
