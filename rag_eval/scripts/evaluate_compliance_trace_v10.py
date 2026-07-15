"""v10 合规金标分层诊断评测。

该脚本只使用金标定位需要审查的待审 chunk，并在审查完成后评价各阶段；
金标中的预期法规、标准值和错误类型不会进入检索 query、LLM prompt 或审核结果。

输出用于区分：法规源缺失、正确法规未召回、数值配对失败、代码比较失败、
初审推理失败、复核删除以及最终命中。
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import hybrid_rag_review_v10 as v10  # noqa: E402
from rag_eval.scripts.evaluate_manual_error_injection_v1 import (  # noqa: E402
    match_cases,
)


DEFAULT_GOLD = (
    PROJECT_ROOT
    / "new_docs"
    / "test_doc"
    / "S1302人工错误注入测试集_v1"
    / "004 S1302工作面作业规程（综采）_人工错误金标_v1.json"
)
DEFAULT_EVIDENCE_GOLD = (
    PROJECT_ROOT / "rag_eval" / "data" / "s1302_compliance_evidence_gold_v1.json"
)
DEFAULT_PENDING_DIR = PROJECT_ROOT / "chunks_visualization"
DEFAULT_REPORT_ROOT = PROJECT_ROOT / "rag_eval" / "reports" / "s1302_compliance_trace_v10"
MATCH_THRESHOLD = 0.56


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("％", "%").replace("＜", "<").replace("＞", ">")
    return re.sub(r"[\s,，。；;、:：()（）\[\]【】“”\"'`]+", "", text)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def unwrap_pending_chunks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        raise ValueError("待审 chunks JSON 既不是数组也不是对象")
    if isinstance(payload.get("chunks"), list):
        return [item for item in payload["chunks"] if isinstance(item, dict)]
    lists = [value for value in payload.values() if isinstance(value, list)]
    if len(lists) != 1:
        raise ValueError("无法从待审 chunks JSON 中唯一确定 chunk 数组")
    return [item for item in lists[0] if isinstance(item, dict)]


def compliance_cases(gold: dict[str, Any]) -> list[dict[str, Any]]:
    cases = [case for case in gold.get("cases", []) if case.get("type") == "compliance"]
    if len(cases) != 12:
        raise ValueError(f"预期12项合规金标，实际{len(cases)}项")
    return cases


def case_chunk_score(case: dict[str, Any], chunk: dict[str, Any]) -> int:
    content = normalize(chunk.get("content"))
    paragraph = normalize(case.get("mutated_paragraph"))
    target = normalize(case.get("mutated_text"))
    anchor = normalize(case.get("anchor"))[:18]
    if paragraph and paragraph in content:
        return 4
    if target and target in content and anchor and anchor in content:
        return 3
    if target and target in content:
        # “可继续作业”等短语在全文多次出现，不能单独用于定位。
        return 1 if len(target) >= 8 else 0
    return 0


def locate_case_chunks(
    cases: list[dict[str, Any]], chunks: list[dict[str, Any]]
) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for case in cases:
        ranked = sorted(
            (
                (case_chunk_score(case, chunk), index)
                for index, chunk in enumerate(chunks)
            ),
            reverse=True,
        )
        if ranked and ranked[0][0] > 0:
            mapping[str(case["error_id"])] = ranked[0][1]
    return mapping


def resolve_pending_chunks_path(
    explicit: Optional[Path], cases: list[dict[str, Any]]
) -> tuple[Path, list[dict[str, Any]], dict[str, int]]:
    if explicit:
        path = explicit.resolve()
        chunks = unwrap_pending_chunks(read_json(path))
        mapping = locate_case_chunks(cases, chunks)
        if len(mapping) != len(cases):
            missing = sorted({case["error_id"] for case in cases} - set(mapping))
            raise ValueError(f"指定 chunks 文件缺少合规金标段落: {missing}")
        return path, chunks, mapping

    candidates = sorted(
        DEFAULT_PENDING_DIR.glob("pending_doc_chunks_v9_single_*.json"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    best: Optional[tuple[int, float, Path, list[dict[str, Any]], dict[str, int]]] = None
    for path in candidates:
        try:
            chunks = unwrap_pending_chunks(read_json(path))
            mapping = locate_case_chunks(cases, chunks)
        except Exception:
            continue
        candidate = (len(mapping), path.stat().st_mtime, path, chunks, mapping)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
        if len(mapping) == len(cases):
            return path.resolve(), chunks, mapping
    if best:
        missing = sorted({case["error_id"] for case in cases} - set(best[4]))
        raise FileNotFoundError(
            f"没有 pending chunks 文件覆盖全部12项合规金标；最佳文件 {best[2]} 缺少 {missing}"
        )
    raise FileNotFoundError("未找到 pending_doc_chunks_v9_single_*.json")


def source_blocks(chunk: dict[str, Any]) -> list[int]:
    values: list[Any] = list(chunk.get("source_blocks") or [])
    for unit in chunk.get("source_units") or []:
        if isinstance(unit, dict):
            values.extend(unit.get("source_blocks") or [])
    blocks: list[int] = []
    for value in values:
        try:
            blocks.append(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(set(blocks))


def compact_chunk(chunk: dict[str, Any], rank: Optional[int] = None, score: Any = None) -> dict[str, Any]:
    return {
        "rank": rank,
        "score": score,
        "doc_name": chunk.get("doc_name", ""),
        "article": chunk.get("article", ""),
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "page_range": chunk.get("page_range", ""),
        "canonical_rule_id": chunk.get("canonical_rule_id", ""),
        "content": str(chunk.get("content", "")),
    }


class TracingReviewer(v10.HybridRAGReviewerV10):
    """在不改变审核输入和输出的前提下，旁路记录各智能体阶段。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.active_trace: Optional[dict[str, Any]] = None

    def review_chunk_with_llm(self, pending_chunk: dict, kb_results: list[dict]) -> dict:
        result = super().review_chunk_with_llm(pending_chunk, kb_results)
        if self.active_trace is not None:
            self.active_trace["draft"] = result
        return result

    def run_numeric_checks(self, pending_chunk: dict, issues: list[dict]) -> list[dict]:
        result = super().run_numeric_checks(pending_chunk, issues)
        if self.active_trace is not None:
            self.active_trace["forward_numeric_checks"] = result
        return result

    def run_counter_numeric_check(
        self, pending_chunk: dict, kb_results: list[dict]
    ) -> dict:
        result = super().run_counter_numeric_check(pending_chunk, kb_results)
        if self.active_trace is not None:
            self.active_trace["counter_numeric_check"] = result
        return result

    def verify_review_result(
        self,
        pending_chunk: dict,
        kb_results: list[dict],
        initial_result: dict,
        numeric_checks: Optional[list[dict]] = None,
    ) -> dict:
        result = super().verify_review_result(
            pending_chunk, kb_results, initial_result, numeric_checks
        )
        if self.active_trace is not None:
            self.active_trace["verification_input"] = initial_result
            self.active_trace["verification"] = result
        return result


def review_to_eval_issues(
    traces: list[dict[str, Any]], stage: str
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for trace in traces:
        review = trace.get(stage) or {}
        if not isinstance(review, dict):
            continue
        review_issues = review.get("issues") or []
        status = str(review.get("compliance_status") or "")
        # draft 阶段只评价是否发现问题，避免“有问题但状态为合规”的旧一致性缺陷干扰。
        if stage == "draft" and review_issues:
            status = "不合规"
        for index, issue in enumerate(review_issues):
            if not isinstance(issue, dict):
                continue
            issues.append(
                {
                    "id": f"{stage}-{trace['focused_index']}-{index}",
                    "issue_type": "compliance",
                    "status": status or "不合规",
                    "title": issue.get("type", "合规问题"),
                    "original_text": issue.get("pending_content", ""),
                    "suggestion": issue.get("suggestion", ""),
                    "regulation": issue.get("regulation_content", ""),
                    "reason": issue.get("description", ""),
                    "detail": {"summary": review.get("summary", "")},
                    "block_indices": trace.get("source_blocks", []),
                }
            )
    return issues


def stage_rows(
    all_gold_cases: list[dict[str, Any]], issues: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    rows, _unmatched = match_cases(all_gold_cases, issues, threshold=MATCH_THRESHOLD)
    return {
        row["error_id"]: row
        for row in rows
        if row.get("gold_type") == "compliance"
    }


def expected_source_status(
    kb_chunks: Iterable[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    articles = [normalize(value) for value in spec.get("expected_articles", [])]
    terms = [normalize(value) for value in spec.get("required_terms", [])]
    matched: list[dict[str, Any]] = []
    complete: list[dict[str, Any]] = []
    for chunk in kb_chunks:
        article_blob = normalize(f"{chunk.get('article', '')} {chunk.get('content', '')}")
        if not any(article and article in article_blob for article in articles):
            continue
        matched.append(chunk)
        content = normalize(chunk.get("content"))
        if all(term in content for term in terms):
            complete.append(chunk)
    return {
        "source_found": bool(matched),
        "source_complete": bool(complete),
        "source_matches": [compact_chunk(chunk) for chunk in matched],
        "complete_rule_ids": [chunk.get("canonical_rule_id", "") for chunk in complete],
    }


def retrieval_status(
    retrieval: list[dict[str, Any]], spec: dict[str, Any]
) -> dict[str, Any]:
    articles = [normalize(value) for value in spec.get("expected_articles", [])]
    terms = [normalize(value) for value in spec.get("required_terms", [])]
    article_rank: Optional[int] = None
    complete_rank: Optional[int] = None
    for item in retrieval:
        chunk = item.get("chunk") or {}
        rank = int(item.get("rank") or 0)
        article_blob = normalize(f"{chunk.get('article', '')} {chunk.get('content', '')}")
        if not any(article and article in article_blob for article in articles):
            continue
        article_rank = rank if article_rank is None else min(article_rank, rank)
        content = normalize(chunk.get("content"))
        if all(term in content for term in terms):
            complete_rank = rank if complete_rank is None else min(complete_rank, rank)
    return {
        "expected_article_rank": article_rank,
        "complete_evidence_rank": complete_rank,
        "retrieval_hit": complete_rank is not None,
    }


def build_retrieval_only_result(
    *,
    cases: list[dict[str, Any]],
    evidence_specs: dict[str, dict[str, Any]],
    case_to_chunk: dict[str, int],
    original_to_focused: dict[int, int],
    kb_results_all: list[list[dict[str, Any]]],
    kb_chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        case_id = str(case["error_id"])
        spec = evidence_specs[case_id]
        original_index = case_to_chunk[case_id]
        focused_index = original_to_focused[original_index]
        retrieval = [
            {"rank": rank, "score": item.get("score"), "chunk": item.get("chunk") or {}}
            for rank, item in enumerate(kb_results_all[focused_index], 1)
        ]
        source = expected_source_status(kb_chunks, spec)
        retrieved = retrieval_status(retrieval, spec)
        rows.append(
            {
                "error_id": case_id,
                "subtype": case.get("subtype", ""),
                "kind": spec.get("kind", ""),
                "gold_target": case.get("mutated_text", ""),
                "chunk_index": original_index,
                "expected_articles": spec.get("expected_articles", []),
                "required_terms": spec.get("required_terms", []),
                **source,
                **retrieved,
                "top_k": [
                    compact_chunk(item["chunk"], rank=item["rank"], score=item["score"])
                    for item in retrieval
                ],
            }
        )
    complete_rows = [row for row in rows if row["source_complete"]]
    metrics = {
        "gold_count": len(rows),
        "source_found": sum(row["source_found"] for row in rows),
        "source_complete": sum(row["source_complete"] for row in rows),
        "retrieval_hit": sum(row["retrieval_hit"] for row in rows),
        "retrieval_recall_all": final_div(
            sum(row["retrieval_hit"] for row in rows), len(rows)
        ),
        "retrieval_recall_when_source_complete": final_div(
            sum(row["retrieval_hit"] for row in complete_rows), len(complete_rows)
        ),
        "source_missing_cases": [
            row["error_id"] for row in rows if not row["source_complete"]
        ],
        "retrieval_miss_cases": [
            row["error_id"]
            for row in rows
            if row["source_complete"] and not row["retrieval_hit"]
        ],
    }
    return rows, metrics


def write_retrieval_only_html(path: Path, result: dict[str, Any]) -> None:
    metrics = result["metrics"]
    body: list[str] = []
    for row in result["cases"]:
        if not row["source_complete"]:
            verdict = "法规源缺失/不完整"
            css = "bad"
        elif not row["retrieval_hit"]:
            verdict = "正确法规未进入Top-K"
            css = "bad"
        else:
            verdict = f"命中，第{row['complete_evidence_rank']}名"
            css = "ok"
        top_k = [
            {
                "rank": item["rank"],
                "score": item["score"],
                "doc_name": item["doc_name"],
                "article": item["article"],
                "page_range": item["page_range"],
                "content": item["content"],
            }
            for item in row["top_k"]
        ]
        body.append(
            "<tr>"
            f"<td><b>{html.escape(row['error_id'])}</b><br>{html.escape(row['kind'])}</td>"
            f"<td>{html.escape(' / '.join(row['expected_articles']))}</td>"
            f"<td>{'完整' if row['source_complete'] else '缺失/不完整'}</td>"
            f"<td class='{css}'>{html.escape(verdict)}</td>"
            f"<td><details><summary>查看Top-K</summary><pre>{json_pre(top_k)}</pre></details></td>"
            "</tr>"
        )
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>v10 合规检索层诊断</title><style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:28px;background:#f5f7fb;color:#172033}}
.cards{{display:flex;gap:12px;margin:20px 0}} .card{{background:#fff;padding:14px 20px;border:1px solid #e3e8ef;border-radius:10px}}
.card b{{display:block;font-size:24px}} table{{width:100%;border-collapse:collapse;background:#fff}}
th,td{{border:1px solid #e3e8ef;padding:9px;vertical-align:top}} th{{background:#eef2f7;text-align:left}}
.ok{{color:#087443;font-weight:bold}} .bad{{color:#b42318;font-weight:bold}} pre{{white-space:pre-wrap;max-width:780px;max-height:440px;overflow:auto}}
</style></head><body><h1>v10 合规检索层诊断（完全本地）</h1>
<p>本报告不调用LLM；金标仅用于选择待审chunk和事后判断正确法规是否进入Top-K。</p>
<div class="cards"><div class="card">法规源完整<b>{metrics['source_complete']}/{metrics['gold_count']}</b></div>
<div class="card">正确法规进入Top-K<b>{metrics['retrieval_hit']}/{metrics['gold_count']}</b></div>
<div class="card">源完整条件下召回率<b>{metrics['retrieval_recall_when_source_complete']:.2%}</b></div></div>
<p>源缺失：{html.escape(', '.join(metrics['source_missing_cases']) or '无')}；检索漏召回：{html.escape(', '.join(metrics['retrieval_miss_cases']) or '无')}</p>
<table><thead><tr><th>案例</th><th>预期法规</th><th>法规源</th><th>检索结果</th><th>详情</th></tr></thead><tbody>{''.join(body)}</tbody></table>
</body></html>"""
    path.write_text(document, encoding="utf-8")


def save_retrieval_only_report(
    report_dir: Path,
    *,
    pending_path: Path,
    kb_path: Path,
    rows: list[dict[str, Any]],
    metrics: dict[str, Any],
    retrieval_seconds: float,
) -> tuple[Path, Path]:
    result = {
        "schema": "s1302_compliance_retrieval_trace_v10",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fairness": {
            "llm_called": False,
            "gold_used_for": ["选择待审chunk", "事后评价Top-K"],
            "gold_not_used_for": ["检索query", "检索排序"],
        },
        "pending_chunks": str(pending_path),
        "knowledge_base": str(kb_path),
        "metrics": metrics,
        "retrieval_seconds": retrieval_seconds,
        "cases": rows,
    }
    json_path = report_dir / "s1302_compliance_retrieval_trace_v10.json"
    html_path = report_dir / "s1302_compliance_retrieval_trace_v10.html"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_retrieval_only_html(html_path, result)
    return json_path, html_path


def numeric_details(trace: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    checks.extend(trace.get("forward_numeric_checks") or [])
    counter = trace.get("counter_numeric_check")
    if isinstance(counter, dict):
        checks.append(counter)
    final_checks = trace.get("numeric_checks") or []
    checks.extend(final_checks)

    unique: dict[str, dict[str, Any]] = {}
    for check in checks:
        if not isinstance(check, dict):
            continue
        for detail in check.get("details") or []:
            if not isinstance(detail, dict):
                continue
            key = json.dumps(detail, ensure_ascii=False, sort_keys=True)
            unique[key] = detail
    return list(unique.values())


def detail_matches_case(detail: dict[str, Any], case: dict[str, Any]) -> bool:
    quote = normalize(detail.get("pending_quote") or detail.get("explanation"))
    target = normalize(case.get("mutated_text"))
    paragraph = normalize(case.get("mutated_paragraph"))
    if target and (target in quote or (len(quote) >= 6 and quote in target)):
        return True
    if quote and len(quote) >= 8 and quote in paragraph:
        target_numbers = set(re.findall(r"\d+(?:\.\d+)?", target))
        quote_numbers = set(re.findall(r"\d+(?:\.\d+)?", quote))
        return bool(target_numbers & quote_numbers)
    return False


def failure_reason(
    *,
    final_hit: bool,
    source_found: bool,
    source_complete: bool,
    retrieval_hit: bool,
    kind: str,
    draft_hit: bool,
    pair_found: bool,
    comparator_bad: bool,
) -> str:
    if final_hit:
        return "final_hit"
    if not source_found or not source_complete:
        return "source_missing"
    if not retrieval_hit:
        return "retrieval_miss"
    if kind == "numeric":
        if not pair_found:
            return "pairing_miss"
        if not comparator_bad:
            return "comparison_error"
        return "decision_integration_miss"
    if not draft_hit:
        return "initial_reasoning_miss"
    return "verification_dropped"


def case_trace_rows(
    *,
    all_cases: list[dict[str, Any]],
    evidence_specs: dict[str, dict[str, Any]],
    case_to_chunk: dict[str, int],
    source_to_focused: dict[int, int],
    traces: list[dict[str, Any]],
    kb_chunks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    draft_issues = review_to_eval_issues(traces, "draft")
    final_issues = review_to_eval_issues(traces, "final")
    draft_matches = stage_rows(all_cases, draft_issues)
    final_matches = stage_rows(all_cases, final_issues)
    compliance = [case for case in all_cases if case.get("type") == "compliance"]

    rows: list[dict[str, Any]] = []
    for case in compliance:
        case_id = str(case["error_id"])
        spec = evidence_specs[case_id]
        original_chunk_index = case_to_chunk[case_id]
        trace = traces[source_to_focused[original_chunk_index]]
        source = expected_source_status(kb_chunks, spec)
        retrieved = retrieval_status(trace.get("retrieval") or [], spec)
        details = [
            detail
            for detail in numeric_details(trace)
            if detail_matches_case(detail, case)
        ]
        draft_row = draft_matches.get(case_id, {})
        final_row = final_matches.get(case_id, {})
        draft_hit = bool(draft_row.get("strict_hit"))
        final_hit = bool(final_row.get("strict_hit"))
        pair_found = bool(details)
        comparator_bad = any(detail.get("verdict") == "不合规" for detail in details)
        reason = failure_reason(
            final_hit=final_hit,
            source_found=source["source_found"],
            source_complete=source["source_complete"],
            retrieval_hit=retrieved["retrieval_hit"],
            kind=str(spec.get("kind", "behavior")),
            draft_hit=draft_hit,
            pair_found=pair_found,
            comparator_bad=comparator_bad,
        )
        rows.append(
            {
                "error_id": case_id,
                "subtype": case.get("subtype", ""),
                "kind": spec.get("kind", ""),
                "gold_target": case.get("mutated_text", ""),
                "chunk_index": original_chunk_index,
                "focused_index": trace["focused_index"],
                "expected_articles": spec.get("expected_articles", []),
                "required_terms": spec.get("required_terms", []),
                **source,
                **retrieved,
                "draft_hit": draft_hit,
                "draft_outcome": draft_row.get("outcome", "missed"),
                "numeric_pair_found": pair_found,
                "numeric_details": details,
                "comparator_bad": comparator_bad,
                "verification_called": bool(trace.get("verification")),
                "escalated": bool(trace.get("escalations")),
                "final_hit": final_hit,
                "final_outcome": final_row.get("outcome", "missed"),
                "failure_reason": reason,
                "draft": trace.get("draft", {}),
                "verification": trace.get("verification", {}),
                "final": trace.get("final", {}),
            }
        )

    failures = Counter(row["failure_reason"] for row in rows)
    numeric_rows = [row for row in rows if row["kind"] == "numeric"]
    source_complete_rows = [row for row in rows if row["source_complete"]]
    final_issue_count = len(final_issues)
    final_hits = sum(row["final_hit"] for row in rows)
    metrics = {
        "gold_count": len(rows),
        "focused_chunk_count": len(traces),
        "source_found": sum(row["source_found"] for row in rows),
        "source_complete": sum(row["source_complete"] for row in rows),
        "retrieval_hit": sum(row["retrieval_hit"] for row in rows),
        "retrieval_recall_all": final_div(
            sum(row["retrieval_hit"] for row in rows), len(rows)
        ),
        "retrieval_recall_when_source_complete": final_div(
            sum(row["retrieval_hit"] for row in source_complete_rows),
            len(source_complete_rows),
        ),
        "draft_strict_hits": sum(row["draft_hit"] for row in rows),
        "numeric_pair_hits": sum(row["numeric_pair_found"] for row in numeric_rows),
        "numeric_comparator_bad_hits": sum(row["comparator_bad"] for row in numeric_rows),
        "final_strict_hits": final_hits,
        "final_predicted_compliance_issues": final_issue_count,
        "final_strict_recall": final_div(final_hits, len(rows)),
        "focused_strict_precision": final_div(final_hits, final_issue_count),
        "failure_counts": dict(sorted(failures.items())),
    }
    return rows, metrics


def final_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def json_pre(value: Any) -> str:
    return html.escape(json.dumps(value, ensure_ascii=False, indent=2))


def write_html(path: Path, result: dict[str, Any]) -> None:
    metrics = result["metrics"]
    failure_labels = {
        "final_hit": "最终命中",
        "source_missing": "法规源缺失",
        "retrieval_miss": "正确法规未召回",
        "pairing_miss": "数值配对失败",
        "comparison_error": "代码比较未判违规",
        "decision_integration_miss": "违规结论未进入最终结果",
        "initial_reasoning_miss": "初审推理漏检",
        "verification_dropped": "复核删除",
    }
    table_rows: list[str] = []
    for row in result["cases"]:
        reason = str(row["failure_reason"])
        cls = "ok" if reason == "final_hit" else "bad"
        retrieved = (
            f"第{row['complete_evidence_rank']}名"
            if row.get("complete_evidence_rank")
            else "未进入Top-K"
        )
        details = {
            "gold_target": row["gold_target"],
            "expected_articles": row["expected_articles"],
            "required_terms": row["required_terms"],
            "numeric_details": row["numeric_details"],
            "draft": row["draft"],
            "verification": row["verification"],
            "final": row["final"],
        }
        table_rows.append(
            "<tr>"
            f"<td><b>{html.escape(row['error_id'])}</b><br><span>{html.escape(row['kind'])}</span></td>"
            f"<td>{'完整' if row['source_complete'] else '缺失/不完整'}</td>"
            f"<td>{html.escape(retrieved)}</td>"
            f"<td>{'是' if row['draft_hit'] else '否'}</td>"
            f"<td>{'是' if row['numeric_pair_found'] else ('—' if row['kind'] != 'numeric' else '否')}</td>"
            f"<td>{'是' if row['final_hit'] else '否'}</td>"
            f"<td class='{cls}'>{html.escape(failure_labels.get(reason, reason))}</td>"
            f"<td><details><summary>查看</summary><pre>{json_pre(details)}</pre></details></td>"
            "</tr>"
        )

    cards = [
        ("最终严格命中", f"{metrics['final_strict_hits']}/{metrics['gold_count']}"),
        ("法规源完整", f"{metrics['source_complete']}/{metrics['gold_count']}"),
        ("正确法规召回", f"{metrics['retrieval_hit']}/{metrics['gold_count']}"),
        ("初审严格命中", f"{metrics['draft_strict_hits']}/{metrics['gold_count']}"),
        ("数值成功配对", f"{metrics['numeric_pair_hits']}/7"),
        ("聚焦精确率", f"{metrics['focused_strict_precision']:.2%}"),
    ]
    card_html = "".join(
        f"<div class='card'><span>{html.escape(label)}</span><b>{html.escape(value)}</b></div>"
        for label, value in cards
    )
    failure_text = "；".join(
        f"{failure_labels.get(key, key)} {value}"
        for key, value in metrics["failure_counts"].items()
    )
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>v10 合规分层诊断</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:28px;background:#f5f7fb;color:#172033}}
h1{{margin-bottom:6px}} .muted{{color:#667085}} .cards{{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}}
.card{{background:white;border:1px solid #e3e8ef;border-radius:10px;padding:14px 18px;min-width:145px}}
.card span{{display:block;color:#667085;font-size:13px}} .card b{{font-size:24px}}
table{{border-collapse:collapse;width:100%;background:white;font-size:13px}} th,td{{border:1px solid #e3e8ef;padding:9px;vertical-align:top}}
th{{background:#eef2f7;text-align:left}} .ok{{color:#087443;font-weight:bold}} .bad{{color:#b42318;font-weight:bold}}
pre{{white-space:pre-wrap;max-width:760px;max-height:420px;overflow:auto;background:#f8fafc;padding:10px}}
.note{{background:#fff8e6;border:1px solid #f3d28b;padding:12px;border-radius:8px;margin:16px 0}}
</style></head><body>
<h1>v10 合规金标分层诊断</h1>
<div class="muted">生成时间：{html.escape(result['generated_at'])}<br>待审chunks：{html.escape(result['pending_chunks'])}</div>
<div class="note">本报告使用金标定位和事后评分，但金标预期法规与标准值没有进入检索查询、LLM提示词或审核结果。</div>
<div class="cards">{card_html}</div>
<p><b>归因：</b>{html.escape(failure_text)}</p>
<table><thead><tr><th>案例</th><th>法规源</th><th>Top-K召回</th><th>初审命中</th><th>数值配对</th><th>最终命中</th><th>归因</th><th>详情</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody></table>
</body></html>"""
    path.write_text(document, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v10 12项合规金标分层诊断")
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--evidence-gold", type=Path, default=DEFAULT_EVIDENCE_GOLD)
    parser.add_argument("--pending-chunks", type=Path)
    parser.add_argument("--kb", type=Path)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只验证12项金标能否定位到待审chunks，不加载模型、不调用LLM",
    )
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="只运行完全本地的知识库完整性与Top-K检索诊断，不调用LLM",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    gold_path = args.gold.resolve()
    gold = read_json(gold_path)
    all_cases = list(gold.get("cases") or [])
    cases = compliance_cases(gold)
    evidence_payload = read_json(args.evidence_gold.resolve())
    evidence_specs = {
        str(item["error_id"]): item for item in evidence_payload.get("cases", [])
    }
    if set(evidence_specs) != {str(case["error_id"]) for case in cases}:
        raise ValueError("合规证据金标与12项合规案例ID不一致")

    pending_path, chunks, case_to_chunk = resolve_pending_chunks_path(
        args.pending_chunks, cases
    )
    focused_original_indices = sorted(set(case_to_chunk.values()))
    focused_chunks = [dict(chunks[index]) for index in focused_original_indices]
    original_to_focused = {
        original: focused for focused, original in enumerate(focused_original_indices)
    }
    cases_by_original: dict[int, list[str]] = {}
    for case_id, original_index in case_to_chunk.items():
        cases_by_original.setdefault(original_index, []).append(case_id)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = (args.report_dir or (DEFAULT_REPORT_ROOT / stamp)).resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    selection = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gold": str(gold_path),
        "pending_chunks": str(pending_path),
        "all_chunk_count": len(chunks),
        "focused_chunk_count": len(focused_chunks),
        "case_to_chunk": case_to_chunk,
        "focused_original_indices": focused_original_indices,
    }
    (report_dir / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[定位] {len(cases)}项合规金标 -> {len(focused_chunks)}个唯一chunk")
    print(f"[待审chunks] {pending_path}")
    for original_index in focused_original_indices:
        print(f"  chunk#{original_index}: {','.join(sorted(cases_by_original[original_index]))}")
    if args.dry_run:
        print(f"[完成] dry-run定位报告: {report_dir / 'selection.json'}")
        return 0

    started = time.perf_counter()
    reviewer = TracingReviewer()
    reviewer.load_knowledge_base(str(args.kb.resolve()) if args.kb else None)
    reviewer.build_index()
    retrieval_started = time.perf_counter()
    kb_results_all, _dense = reviewer.prepare_retrieval(focused_chunks)
    retrieval_seconds = time.perf_counter() - retrieval_started

    retrieval_rows, retrieval_metrics = build_retrieval_only_result(
        cases=cases,
        evidence_specs=evidence_specs,
        case_to_chunk=case_to_chunk,
        original_to_focused=original_to_focused,
        kb_results_all=kb_results_all,
        kb_chunks=reviewer.kb_chunks,
    )
    retrieval_json, retrieval_html = save_retrieval_only_report(
        report_dir,
        pending_path=pending_path,
        kb_path=reviewer.kb_json_path or Path(""),
        rows=retrieval_rows,
        metrics=retrieval_metrics,
        retrieval_seconds=retrieval_seconds,
    )
    if args.retrieval_only:
        print("===== v10 12项合规检索层诊断（完全本地） =====")
        print(
            f"法规源完整 {retrieval_metrics['source_complete']}/{len(cases)} | "
            f"正确法规召回 {retrieval_metrics['retrieval_hit']}/{len(cases)} | "
            f"源完整条件下召回率 "
            f"{retrieval_metrics['retrieval_recall_when_source_complete']:.2%}"
        )
        print(f"源缺失: {retrieval_metrics['source_missing_cases']}")
        print(f"检索漏召回: {retrieval_metrics['retrieval_miss_cases']}")
        print(f"JSON: {retrieval_json}")
        print(f"HTML: {retrieval_html}")
        return 0

    traces: list[dict[str, Any]] = []
    review_started = time.perf_counter()
    for focused_index, (original_index, chunk, kb_results) in enumerate(
        zip(focused_original_indices, focused_chunks, kb_results_all)
    ):
        trace: dict[str, Any] = {
            "focused_index": focused_index,
            "original_chunk_index": original_index,
            "case_ids": sorted(cases_by_original[original_index]),
            "source_blocks": source_blocks(chunk),
            "chunk": compact_chunk(chunk),
            "retrieval": [
                {
                    "rank": rank,
                    "score": item.get("score"),
                    "chunk": compact_chunk(
                        item.get("chunk") or {}, rank=rank, score=item.get("score")
                    ),
                }
                for rank, item in enumerate(kb_results, 1)
            ],
        }
        reviewer.active_trace = trace
        chunk_started = time.perf_counter()
        try:
            final, classifications, numeric_checks, escalations, timings = (
                reviewer.review_chunk_complete(chunk, kb_results)
            )
            trace.update(
                {
                    "final": final,
                    "classifications": classifications,
                    "numeric_checks": numeric_checks,
                    "escalations": escalations,
                    "timings": timings,
                }
            )
        except Exception as exc:
            trace["error"] = f"{type(exc).__name__}: {exc}"
            trace.setdefault(
                "final",
                {"compliance_status": "不确定", "issues": [], "error": str(exc)},
            )
        trace["wall_seconds"] = time.perf_counter() - chunk_started
        traces.append(trace)
        reviewer.active_trace = None
        print(
            f"[审查] {focused_index + 1}/{len(focused_chunks)} "
            f"chunk#{original_index} cases={','.join(trace['case_ids'])} "
            f"issues={len((trace.get('final') or {}).get('issues') or [])} "
            f"{trace['wall_seconds']:.1f}s"
        )
    review_seconds = time.perf_counter() - review_started

    failed_traces = [
        trace
        for trace in traces
        if trace.get("error")
        or (trace.get("draft") or {}).get("error")
        or (trace.get("draft") or {}).get("parse_error")
    ]
    if failed_traces:
        failed_path = report_dir / "s1302_compliance_trace_v10_failed.json"
        failed_payload = {
            "schema": "s1302_compliance_trace_v10_failed",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "reason": "存在初审调用或JSON解析错误，本次结果不计入业务指标",
            "pending_chunks": str(pending_path),
            "failed_chunk_count": len(failed_traces),
            "valid_retrieval_report": {
                "json": str(retrieval_json),
                "html": str(retrieval_html),
                "metrics": retrieval_metrics,
            },
            "traces": failed_traces,
        }
        failed_path.write_text(
            json.dumps(failed_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"[失败] {len(failed_traces)}/{len(traces)}个聚焦chunk调用失败，"
            "本次不生成召回/推理归因指标"
        )
        print(f"失败详情: {failed_path}")
        return 2

    rows, metrics = case_trace_rows(
        all_cases=all_cases,
        evidence_specs=evidence_specs,
        case_to_chunk=case_to_chunk,
        source_to_focused=original_to_focused,
        traces=traces,
        kb_chunks=reviewer.kb_chunks,
    )
    result = {
        "schema": "s1302_compliance_trace_v10",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fairness": {
            "gold_used_for": ["选择待审chunk", "事后计算阶段指标"],
            "gold_not_used_for": ["检索query", "LLM prompt", "审核结论"],
        },
        "gold": str(gold_path),
        "evidence_gold": str(args.evidence_gold.resolve()),
        "pending_chunks": str(pending_path),
        "knowledge_base": str(reviewer.kb_json_path or ""),
        "metrics": metrics,
        "timings": {
            "retrieval_seconds": retrieval_seconds,
            "review_seconds": review_seconds,
            "total_seconds": time.perf_counter() - started,
        },
        "cases": rows,
        "traces": traces,
    }
    json_path = report_dir / "s1302_compliance_trace_v10.json"
    html_path = report_dir / "s1302_compliance_trace_v10.html"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_html(html_path, result)

    print("===== v10 12项合规分层诊断 =====")
    print(
        f"法规源完整 {metrics['source_complete']}/{metrics['gold_count']} | "
        f"正确法规召回 {metrics['retrieval_hit']}/{metrics['gold_count']} | "
        f"初审命中 {metrics['draft_strict_hits']}/{metrics['gold_count']} | "
        f"数值配对 {metrics['numeric_pair_hits']}/7 | "
        f"最终命中 {metrics['final_strict_hits']}/{metrics['gold_count']}"
    )
    print(f"归因: {metrics['failure_counts']}")
    print(f"JSON: {json_path}")
    print(f"HTML: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
