"""Build chapter-6 evaluation artifacts for sections 6.1-6.4.

Outputs:
- a machine-readable JSON report;
- a Markdown report with tables matching the paper sections;
- a filled RAGAS JSONL dataset with non-empty `response`;
- a per-chunk agent-output JSONL file for manual inspection.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top3_gold_annotations.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "chapter6_evaluation_report_v1.json"
OUTPUT_MD = PROJECT_ROOT / "rag_eval" / "reports" / "chapter6_evaluation_report_v1.md"
OUTPUT_RAGAS_JSONL = PROJECT_ROOT / "rag_eval" / "data" / "chapter6_ragas_dataset_filled_v1.jsonl"
OUTPUT_AGENT_JSONL = PROJECT_ROOT / "rag_eval" / "data" / "chapter6_agent_outputs_v1.jsonl"
STRATEGY_EVAL = PROJECT_ROOT / "rag_eval" / "data" / "main_agent_strategy_eval_v1.json"
STRATEGY_GEN_EVAL = PROJECT_ROOT / "rag_eval" / "data" / "main_agent_strategy_gen_eval_v1.json"
PARALLEL_EVAL = PROJECT_ROOT / "rag_eval" / "data" / "parallel_performance_eval_v1.json"
RAGAS_METRICS = PROJECT_ROOT / "rag_eval" / "data" / "chapter6_ragas_metrics_v1.json"
RETRIEVAL_ABLATION = PROJECT_ROOT / "rag_eval" / "data" / "retrieval_ablation_v1.json"
GROUNDEDNESS_TYPO = PROJECT_ROOT / "rag_eval" / "data" / "ragas_groundedness_typo_v1.json"
GROUNDEDNESS_REDUNDANCY = PROJECT_ROOT / "rag_eval" / "data" / "ragas_groundedness_redundancy_v1.json"
ABLATION_VARIANTS = (
    ("dense", "稠密检索基线", "BGE-M3 dense"),
    ("hybrid", "混合检索", "dense+sparse+RRF"),
    ("hybrid_rerank", "混合检索+重排", "+bge-reranker"),
    ("parent_multi", "父块保底+片段补充", "+多查询(v9生产)"),
)
KS = (1, 3, 5, 10, 15)

OUTBURST_DOC_PATTERNS = ("防治煤与瓦斯突出",)
OUTBURST_STRUCT_KEYWORDS = ("突出矿井", "煤与瓦斯突出", "突出煤层", "突出危险", "防突", "石门揭煤")


def latest_review_result() -> Path:
    files = sorted(
        PROJECT_ROOT.glob("review_results/review_result_v9_20*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError("No review_results/review_result_v9_20*.json found.")
    return files[0]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def evidence_id(item: dict[str, Any]) -> str:
    return item.get("evidence_group_id") or item.get("chunk_id", "")


def tag_applicability(chunk: dict[str, Any], doc_name: str) -> str:
    if any(pattern in doc_name for pattern in OUTBURST_DOC_PATTERNS):
        return "outburst_only"
    struct_text = " ".join(
        [
            str(chunk.get("part", "")),
            str(chunk.get("chapter", "")),
            str(chunk.get("section", "")),
            str(chunk.get("context_prefix", "") or ""),
            " ".join(chunk.get("parent_context") or []),
        ]
    )
    if any(keyword in struct_text for keyword in OUTBURST_STRUCT_KEYWORDS):
        return "outburst_only"
    return "general"


def build_runtime_kb_id_map(kb_path: Path, mine_type: str = "non_outburst") -> dict[str, str]:
    kb_data = load_json(kb_path)
    mapping: dict[str, str] = {}
    runtime_index = 0
    for doc_name, chunks in kb_data.items():
        for source_index, chunk in enumerate(chunks, start=1):
            if chunk.get("retrievable", True) is False:
                continue
            applicability = tag_applicability(chunk, doc_name)
            if mine_type == "non_outburst" and applicability == "outburst_only":
                continue
            mapping[f"kb_{runtime_index}"] = f"{doc_name}::chunk{source_index}"
            runtime_index += 1
    return mapping


def load_chunk_to_group(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return dict((load_json(path).get("chunk_to_group") or {}))


def iter_review_chunks(review: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for doc_name, chunks in review.items():
        if not isinstance(chunks, list):
            continue
        for item in chunks:
            row = dict(item)
            row["doc_name"] = doc_name
            yield row


def build_review_index(review: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    return {
        (row["doc_name"], int(row.get("chunk_index", -1))): row
        for row in iter_review_chunks(review)
    }


def normalize_review_refs(
    refs: list[dict[str, Any]],
    runtime_kb_id_map: dict[str, str],
    chunk_to_group: dict[str, str],
) -> list[str]:
    ranked_ids: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        runtime_id = str(ref.get("chunk_id", ""))
        stable_id = runtime_kb_id_map.get(runtime_id)
        if not stable_id and ref.get("doc") and runtime_id.startswith("chunk"):
            stable_id = f"{ref['doc']}::{runtime_id}"
        if not stable_id:
            continue
        eval_id = chunk_to_group.get(stable_id) or stable_id
        if eval_id not in seen:
            ranked_ids.append(eval_id)
            seen.add(eval_id)
    return ranked_ids


def dcg(values: list[int]) -> float:
    return sum(value / math.log2(rank + 1) for rank, value in enumerate(values, start=1))


def avg(values: Iterable[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return mean(valid) if valid else None


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    index = max(0, min(len(values) - 1, math.ceil(len(values) * q) - 1))
    return values[index]


def summarize_values(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "median": None, "p95": None, "max": None, "min": None}
    return {
        "count": len(values),
        "mean": mean(values),
        "median": median(values),
        "p95": percentile(values, 0.95),
        "max": max(values),
        "min": min(values),
    }


def evaluate_ranked_ids(case: dict[str, Any], ranked_ids: list[str], candidate_count: int) -> dict[str, Any]:
    gold = {evidence_id(item) for item in case["evidence_annotations"] if item["label"] == "useful"}
    relevance = [int(item_id in gold) for item_id in ranked_ids]
    first_rank = next((index + 1 for index, value in enumerate(relevance) if value), None)
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "candidate_count": candidate_count,
        "dedup_candidate_count": len(ranked_ids),
        "gold_evidence_count": len(gold),
        "first_relevant_rank": first_rank,
        "mrr": 1.0 / first_rank if first_rank else 0.0,
        "final_label": case.get("final_label", ""),
        "missing_correct_evidence": bool(case.get("missing_correct_evidence")),
        "not_eval_suitable": bool(case.get("not_eval_suitable")),
    }
    for k in KS:
        top = relevance[:k]
        hits = sum(top)
        ideal = dcg([1] * min(k, len(gold)))
        row[f"hit@{k}"] = float(hits > 0)
        row[f"recall@{k}"] = hits / len(gold) if gold else None
        row[f"precision@{k}"] = hits / k
        row[f"ndcg@{k}"] = dcg(top) / ideal if ideal else None
    return row


def aggregate_case_rows(case_rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in case_rows if row["gold_evidence_count"] > 0]
    aggregate: dict[str, Any] = {
        "case_count": len(case_rows),
        "evaluated_case_count": len(eligible),
        "excluded_zero_gold_case_count": len(case_rows) - len(eligible),
        "missing_correct_evidence_case_count": sum(row["missing_correct_evidence"] for row in case_rows),
        "not_eval_suitable_case_count": sum(row["not_eval_suitable"] for row in case_rows),
        "average_candidates_per_case": mean(row["candidate_count"] for row in case_rows) if case_rows else 0.0,
        "mrr": avg(row["mrr"] for row in eligible),
    }
    for metric in ("hit", "recall", "precision", "ndcg"):
        aggregate[f"{metric}_at"] = {
            str(k): avg(row[f"{metric}@{k}"] for row in eligible) for k in KS
        }
    return {"aggregate": aggregate, "cases": case_rows}


def extract_review_response(row: dict[str, Any]) -> str:
    review = row.get("review_result") or {}
    lines = [
        f"合规结论：{review.get('compliance_status', '')}",
        f"摘要：{review.get('summary', '')}",
    ]
    issues = review.get("issues") or []
    if issues:
        lines.append("问题清单：")
        for idx, issue in enumerate(issues, start=1):
            if isinstance(issue, dict):
                issue_text = "；".join(
                    str(issue.get(key, ""))
                    for key in ("type", "description", "reason", "evidence", "suggestion")
                    if issue.get(key)
                )
            else:
                issue_text = str(issue)
            lines.append(f"{idx}. {issue_text}")
    numeric = row.get("numeric_checks") or []
    if numeric:
        lines.append("数值核验：")
        for check in numeric:
            lines.append(f"- {check.get('overall', '')}：{check.get('summary', '')}")
    typo = row.get("typo_result") or {}
    if typo.get("has_issues"):
        lines.append(f"错别字检查：{typo.get('summary', '')}")
    repetition = row.get("repetition_result") or {}
    if repetition.get("has_duplicates"):
        lines.append(f"重复性检查：{repetition.get('summary', '')}")
    return "\n".join(line for line in lines if line.strip())


def label_to_status(label: str) -> str | None:
    return {
        "compliant": "合规",
        "non_compliant": "不合规",
        "uncertain": "不确定",
    }.get(label)


def status_match(expected: str | None, predicted: str) -> bool:
    if expected is None:
        return False
    return expected in predicted


def binary_prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def build_ragas_rows(gold: dict[str, Any], review_index: dict[tuple[str, int], dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case in gold["cases"]:
        pending = case["pending"]
        key = (pending.get("doc_name", ""), int(pending.get("chunk_index", -1)))
        review_row = review_index.get(key, {})
        refs = review_row.get("retrieved_kb_refs") or review_row.get("kb_refs") or []
        retrieved_contexts = [ref.get("content", "") for ref in refs if ref.get("content")]
        reference_contexts = [
            item["content"]
            for item in sorted(case["evidence_annotations"], key=lambda item: item["parent_rank"])
            if item["label"] == "useful"
        ]
        rows.append(
            {
                "case_id": case["case_id"],
                "user_input": pending["content"],
                "retrieved_contexts": retrieved_contexts,
                "response": extract_review_response(review_row) if review_row else "",
                "reference": f"{case.get('final_label', '')}：{case.get('final_reason', '')}",
                "reference_contexts": reference_contexts,
                "metadata": {
                    "doc_name": pending.get("doc_name", ""),
                    "chunk_index": pending.get("chunk_index", -1),
                    "retrieved_context_count": len(retrieved_contexts),
                    "reference_context_count": len(reference_contexts),
                    "missing_correct_evidence": bool(case.get("missing_correct_evidence")),
                    "not_eval_suitable": bool(case.get("not_eval_suitable")),
                },
            }
        )
    return rows


def evaluate_function_quality(
    gold: dict[str, Any],
    review_index: dict[tuple[str, int], dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    tp = fp = fn = tn = 0
    for case in gold["cases"]:
        if case.get("not_eval_suitable"):
            continue
        expected_status = label_to_status(case.get("final_label", ""))
        pending = case["pending"]
        review_row = review_index.get((pending.get("doc_name", ""), int(pending.get("chunk_index", -1))), {})
        predicted_status = str((review_row.get("review_result") or {}).get("compliance_status", ""))
        exact = status_match(expected_status, predicted_status)
        expected_bad = expected_status == "不合规"
        predicted_bad = "不合规" in predicted_status
        if expected_bad and predicted_bad:
            tp += 1
        elif not expected_bad and predicted_bad:
            fp += 1
        elif expected_bad and not predicted_bad:
            fn += 1
        else:
            tn += 1
        rows.append(
            {
                "case_id": case["case_id"],
                "expected_status": expected_status,
                "predicted_status": predicted_status,
                "exact_status_match": exact,
                "expected_non_compliant": expected_bad,
                "predicted_non_compliant": predicted_bad,
            }
        )
    prf = binary_prf(tp, fp, fn)
    return {
        "status_case_count": len(rows),
        "exact_status_accuracy": mean(float(row["exact_status_match"]) for row in rows) if rows else None,
        "non_compliance_confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "non_compliance_precision": prf["precision"],
        "non_compliance_recall": prf["recall"],
        "non_compliance_f1": prf["f1"],
        "cases": rows,
    }


def agent_output_summary(review_rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output_rows = []
    status_counter: Counter[str] = Counter()
    numeric_overall: Counter[str] = Counter()

    def count_field(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, (list, tuple, dict, set)):
            return len(value)
        return 1

    for row in review_rows:
        review = row.get("review_result") or {}
        typo = row.get("typo_result") or {}
        repetition = row.get("repetition_result") or {}
        numeric_checks = row.get("numeric_checks") or []
        timings = row.get("timings") or {}
        status = str(review.get("compliance_status", ""))
        status_counter[status] += 1
        for check in numeric_checks:
            if check.get("overall"):
                numeric_overall[str(check.get("overall"))] += 1
        output_rows.append(
            {
                "doc_name": row["doc_name"],
                "chunk_index": row.get("chunk_index"),
                "review_status": status,
                "review_issue_count": len(review.get("issues") or []),
                "retrieved_ref_count": len(row.get("retrieved_kb_refs") or []),
                "generation_ref_count": count_field(row.get("generation_kb_matches")),
                "numeric_check_count": len(numeric_checks),
                "numeric_overalls": [check.get("overall", "") for check in numeric_checks],
                "typo_issue_count": len(typo.get("issues") or []),
                "repetition_duplicate_count": len(repetition.get("duplicates") or []),
                "escalation_count": len(row.get("escalations") or []),
                "timings": timings,
                "response": extract_review_response(row),
            }
        )
    summary = {
        "chunk_count": len(review_rows),
        "status_counts": dict(status_counter),
        "compliance_agent": {
            "issue_chunk_count": sum(1 for row in output_rows if row["review_issue_count"] > 0),
            "total_issue_count": sum(row["review_issue_count"] for row in output_rows),
            "compliance_review_time": summarize_values([
                float(row["timings"].get("compliance_review", 0.0)) for row in output_rows
            ]),
        },
        "retrieval_agent": {
            "average_retrieved_refs": mean(row["retrieved_ref_count"] for row in output_rows) if output_rows else 0.0,
            "chunks_with_retrieved_refs": sum(row["retrieved_ref_count"] > 0 for row in output_rows),
        },
        "numeric_agent": {
            "chunks_with_numeric_checks": sum(row["numeric_check_count"] > 0 for row in output_rows),
            "total_numeric_checks": sum(row["numeric_check_count"] for row in output_rows),
            "overall_counts": dict(numeric_overall),
            "numeric_time": summarize_values([
                float(row["timings"].get("numeric_total", 0.0)) for row in output_rows
            ]),
        },
        "verification_agent": {
            "verified_chunk_count": sum(bool((row.get("timings") or {}).get("verification", 0.0)) for row in review_rows),
            "verification_time": summarize_values([
                float(row["timings"].get("verification", 0.0)) for row in output_rows
            ]),
        },
        "typo_agent": {
            "chunks_with_typo_issues": sum(row["typo_issue_count"] > 0 for row in output_rows),
            "total_typo_issues": sum(row["typo_issue_count"] for row in output_rows),
            "typo_time": summarize_values([
                float(row["timings"].get("typo", 0.0)) for row in output_rows
            ]),
        },
        "repetition_agent": {
            "chunks_with_duplicates": sum(row["repetition_duplicate_count"] > 0 for row in output_rows),
            "total_duplicate_groups": sum(row["repetition_duplicate_count"] for row in output_rows),
        },
        "escalation": {
            "chunks_with_escalations": sum(row["escalation_count"] > 0 for row in output_rows),
            "total_escalations": sum(row["escalation_count"] for row in output_rows),
        },
    }
    return summary, output_rows


def evaluate_retrieval(
    gold: dict[str, Any],
    review_index: dict[tuple[str, int], dict[str, Any]],
    runtime_kb_id_map: dict[str, str],
    chunk_to_group: dict[str, str],
) -> dict[str, Any]:
    case_rows = []
    retrieval_times = []
    missing_review_rows = 0
    for case in gold["cases"]:
        pending = case["pending"]
        review_row = review_index.get((pending.get("doc_name", ""), int(pending.get("chunk_index", -1))))
        if not review_row:
            missing_review_rows += 1
            refs = []
        else:
            refs = review_row.get("retrieved_kb_refs") or review_row.get("kb_refs") or []
            timing = (review_row.get("timings") or {}).get("phase_a_avg_per_chunk")
            if timing is not None:
                retrieval_times.append(float(timing))
        ranked_ids = normalize_review_refs(refs, runtime_kb_id_map, chunk_to_group)
        case_rows.append(evaluate_ranked_ids(case, ranked_ids, len(refs)))
    result = aggregate_case_rows(case_rows)
    result["aggregate"]["missing_review_row_count"] = missing_review_rows
    result["aggregate"]["average_retrieval_time_ms"] = (
        mean(retrieval_times) * 1000 if retrieval_times else None
    )
    result["aggregate"]["retrieval_time_sec"] = summarize_values(retrieval_times)
    return result


def summarize_runtime(review_rows: list[dict[str, Any]]) -> dict[str, Any]:
    timing_keys = [
        "phase_a_avg_per_chunk",
        "chunk_total",
        "generation_like_llm",
        "compliance_review",
        "numeric_total",
        "verification",
        "kb_classification",
        "typo",
    ]
    metrics = {}
    for key in timing_keys:
        metrics[key] = summarize_values([
            float((row.get("timings") or {}).get(key, 0.0))
            for row in review_rows
            if key in (row.get("timings") or {})
        ])

    doc_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in review_rows:
        doc_groups[row["doc_name"]].append(row)
    doc_level = []
    for doc_name, rows in doc_groups.items():
        first_timings = rows[0].get("timings") or {}
        chunk_totals = [float((row.get("timings") or {}).get("chunk_total", 0.0)) for row in rows]
        doc_level.append(
            {
                "doc_name": doc_name,
                "chunk_count": len(rows),
                "phase_a_doc_total_sec": first_timings.get("phase_a_doc_total"),
                "repetition_doc_total_sec": first_timings.get("repetition_doc_total"),
                "sum_chunk_total_sec_serial_upper_bound": sum(chunk_totals),
                "mean_chunk_total_sec": mean(chunk_totals) if chunk_totals else None,
            }
        )
    return {
        "chunk_count": len(review_rows),
        "metrics": metrics,
        "doc_level": doc_level,
        "parallel_baseline_status": (
            "current_review_result_has_parallel_chunk_timings_only; "
            "run rag_eval/scripts/run_parallel_performance_eval_v1.py for serial-vs-parallel speedup"
        ),
    }


def load_optional_json(path: Path) -> dict[str, Any] | None:
    if path.exists():
        return load_json(path)
    return None


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def build_markdown(payload: dict[str, Any]) -> str:
    retrieval = payload["section_6_1_retrieval"]["aggregate"]
    function = payload["section_6_2_function_quality"]
    agents = payload["agent_output_summary"]
    runtime = payload["section_6_4_parallel_runtime"]
    strategy = payload["section_6_3_strategy_eval"]
    ragas = payload["ragas_dataset"]
    ragas_metrics = payload.get("ragas_metrics") or {"status": "not_run", "metrics": {}}
    rm = ragas_metrics.get("metrics") or {}
    ablation = payload.get("retrieval_ablation")
    strategy_gen = payload.get("section_6_3_strategy_gen_eval")
    groundedness = payload.get("groundedness") or {}

    def gnd_metric(label: str, *names: str) -> Any:
        block = groundedness.get(label) or {}
        metrics = block.get("metrics") or {}
        for name in names:
            row = metrics.get(name)
            if isinstance(row, dict) and row.get("mean") is not None:
                return row.get("mean")
        return None

    def metric_mean(*names: str) -> Any:
        for name in names:
            row = rm.get(name)
            if isinstance(row, dict) and row.get("mean") is not None:
                return row.get("mean")
        return None

    lines = [
        "# 第六章 6.1-6.4 评估报告",
        "",
        f"生成时间：`{payload['generated_at']}`",
        f"审查结果：`{payload['sources']['review_result']}`",
        f"黄金集：`{payload['sources']['gold']}`",
        "",
        "## 6.1 检索效果测试",
        "",
    ]
    if ablation and ablation.get("variants"):
        variants = ablation["variants"]
        lines += [
            f"四档检索消融（同一黄金集 {ablation.get('evaluated_case_count', '-')} 个可评价案例，同一指标口径，TopK={ablation.get('topk', 15)}）：",
            "",
            "| 检索方案 | 主要设置 | 覆盖率(Hit@15) | Hit@5 | Recall@5 | MRR | NDCG@10 | 平均耗时/ms |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for key, name, setting in ABLATION_VARIANTS:
            v = variants.get(key)
            if not v:
                continue
            agg = v["aggregate"]
            lines.append(
                f"| {name} | {setting} | {fmt(agg['hit_at']['15'])} | {fmt(agg['hit_at']['5'])} | "
                f"{fmt(agg['recall_at']['5'])} | {fmt(agg['mrr'])} | {fmt(agg['ndcg_at']['10'])} | "
                f"{fmt(v.get('avg_retrieval_ms'), 1)} |"
            )
        lines += ["", "（逐 K 的 Hit/Recall/NDCG/Precision 全表见 `rag_eval/reports/retrieval_ablation_v1.md`）", ""]
    lines += [
        "在线流水线实际检索（v9生产方案在黄金集待审块上的落盘 TopK，作为交叉校验）：",
        "",
        "| 检索方案 | Top-K | Hit@K | Recall@K | MRR | NDCG@K | 平均检索时间/ms | 备注 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for k in (1, 3, 5, 10, 15):
        lines.append(
            f"| v9 BGE-M3混合检索+重排 | {k} | "
            f"{fmt(retrieval['hit_at'][str(k)])} | {fmt(retrieval['recall_at'][str(k)])} | "
            f"{fmt(retrieval['mrr'])} | {fmt(retrieval['ndcg_at'][str(k)])} | "
            f"{fmt(retrieval['average_retrieval_time_ms'], 1)} | "
            f"{retrieval['evaluated_case_count']}个可评价案例 |"
        )

    lines.extend(
        [
            "",
            "## 6.2 各专业智能体功能测试",
            "",
            "| 测试对象 | 测试任务 | Faithfulness | Response Relevancy | Context Precision | Context Recall | Tool Call Accuracy/F1 | Agent Goal Accuracy | 备注 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---|",
            "| 审查链整体 | 合规/不合规/不确定结论匹配 | "
            f"{fmt(metric_mean('faithfulness'))} | {fmt(metric_mean('answer_relevancy', 'response_relevancy'))} | "
            f"{fmt(metric_mean('llm_context_precision_with_reference'))} | {fmt(metric_mean('context_recall'))} | "
            f"- | {pct(function['exact_status_accuracy'])} | RAGAS + 人工黄金结论匹配 |",
            "| 合规审查智能体 | 发现规则/数值冲突 | "
            f"{fmt(metric_mean('faithfulness'))} | {fmt(metric_mean('answer_relevancy', 'response_relevancy'))} | - | - | - | "
            f"{pct(function['non_compliance_f1'])} | 非合规二分类F1 |",
            "| 数值核验智能体 | 数值约束抽取与比较 | - | - | - | - | - | - | "
            f"{agents['numeric_agent']['chunks_with_numeric_checks']}个chunk触发数值核验 |",
            "| 错别字智能体 | 错别字检测(无金标,groundedness) | "
            f"{fmt(gnd_metric('typo', 'faithfulness'))} | "
            f"{fmt(gnd_metric('typo', 'answer_relevancy', 'response_relevancy'))} | - | - | - | - | "
            f"{agents['typo_agent']['chunks_with_typo_issues']}个chunk发现错别字 |",
            "| 重复性智能体 | 文档内重复检测(无金标,groundedness) | "
            f"{fmt(gnd_metric('redundancy', 'faithfulness'))} | "
            f"{fmt(gnd_metric('redundancy', 'answer_relevancy', 'response_relevancy'))} | - | - | - | - | "
            f"{agents['repetition_agent']['chunks_with_duplicates']}个chunk发现重复 |",
            "",
            f"RAGAS数据集：`{ragas['path']}`，共 {ragas['row_count']} 行，"
            f"非空response {ragas['non_empty_response_count']} 行；"
            f"RAGAS运行状态：{ragas_metrics['status']}。"
            + (
                " 已使用 `langchain0.3` 环境、DashScope LLM judge 和本地 BGE-M3 embedding 完成评估。"
                if ragas_metrics["status"] == "available"
                else " 当前报告生成环境未必安装 RAGAS，需切换到 `langchain0.3` 运行。"
            ),
            (
                f"注意：`llm_context_precision_with_reference` 有效样本数为 "
                f"{rm.get('llm_context_precision_with_reference', {}).get('count', 0)}，"
                f"失败/超时样本数为 {rm.get('llm_context_precision_with_reference', {}).get('missing_or_failed', 0)}，"
                "论文中建议将该项标注为“RAGAS有效样本均值”。"
                if ragas_metrics["status"] == "available"
                else ""
            ),
            "",
            "## 6.3 主智能体策略生成时间与决策质量测试",
            "",
            "| 评测方式 | 平均策略生成时间/s | P95时间/s | 任务识别准确率 | 工具选择正确率 | 策略执行匹配度 | 升级决策准确率 | 备注 |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    if strategy_gen and strategy_gen.get("aggregate"):
        agg = strategy_gen["aggregate"]
        lines.append(
            "| 策略生成节点(真实分类) | "
            f"{fmt(agg['mean_elapsed_sec'], 3)} | {fmt(agg['p95_elapsed_sec'], 3)} | "
            f"{pct(agg['task_recognition_accuracy'])} | {pct(agg['mean_tool_f1'])} | "
            f"{pct(agg['mean_step_match'])} | {pct(agg['escalation_decision_accuracy'])} | "
            f"五类任务×{agg['count']}，generate_strategy 真实输出 |"
        )
    if strategy["status"] == "available":
        agg = strategy["aggregate"]
        lines.append(
            "| 工具循环(mock,任务识别为工具召回代理) | "
            f"{fmt(agg['mean_elapsed_sec'], 3)} | {fmt(agg['p95_elapsed_sec'], 3)} | "
            f"{pct(agg['task_recognition_accuracy'])} | {pct(agg['mean_tool_selection_f1'])} | "
            f"{pct(agg['step_validity_rate'])} | {pct(agg['escalation_condition_completeness'])} | "
            "来自 main_agent_strategy_eval_v1（旧） |"
        )
    if not (strategy_gen and strategy_gen.get("aggregate")) and strategy["status"] != "available":
        lines.append(
            "| 主智能体策略任务 | - | - | - | - | - | - | "
            "`python rag_eval/scripts/run_main_agent_strategy_gen_eval_v1.py` 后补齐 |"
        )

    lines.extend(
        [
            "",
            "## 6.4 多智能体异步并行审查性能测试",
            "",
            "| 执行模式 | 文档规模/块数 | 总审查时间/s | 首条问题返回时间/s | 吞吐量/块每分钟 | 加速比 | 结果一致率 | 异常率 | 备注 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for doc in runtime["doc_level"]:
        chunk_count = doc["chunk_count"]
        serial_upper = doc["sum_chunk_total_sec_serial_upper_bound"]
        throughput = chunk_count / serial_upper * 60 if serial_upper else None
        lines.append(
            f"| v9并行结果的串行上界估计 | {chunk_count} | {fmt(serial_upper, 1)} | - | "
            f"{fmt(throughput, 2)} | - | - | - | {doc['doc_name']} |"
        )
    if payload["parallel_performance_eval"]["status"] == "available":
        for row in payload["parallel_performance_eval"].get("runs", []):
            lines.append(
                f"| concurrency={row.get('concurrency')}实测 | {row.get('chunk_count', '-')} | "
                f"{fmt(row.get('total_elapsed_sec'), 1)} | {fmt(row.get('first_issue_latency_sec'), 1)} | "
                f"{fmt(row.get('throughput_chunks_per_min'), 2)} | {fmt(row.get('speedup_vs_serial'), 2)} | "
                f"{fmt(row.get('result_consistency'), 4)} | {fmt(row.get('error_rate'), 4)} | 重跑性能实验 |"
            )
    else:
        lines.append(
            "| 串行/并行对照实测 | - | - | - | - | - | - | - | "
            "`python rag_eval/scripts/run_parallel_performance_eval_v1.py --concurrency 1 4` 后补齐 |"
        )

    lines.extend(
        [
            "",
            "## 运行命令",
            "",
            "```powershell",
            "# 1) 检索四档消融(6.1, 需BGE环境)",
            "python rag_eval/scripts/run_retrieval_ablation_v1.py",
            "# 2) 主智能体策略生成评测(6.3)",
            "python rag_eval/scripts/run_main_agent_strategy_gen_eval_v1.py",
            "# 3) 错别字/重复性 groundedness(6.2, 需BGE环境)",
            "python rag_eval/scripts/build_typo_redundancy_ragas_dataset_v1.py",
            "python rag_eval/scripts/run_ragas_groundedness_v1.py --dataset rag_eval/data/typo_agent_ragas_dataset_v1.jsonl --label typo",
            "python rag_eval/scripts/run_ragas_groundedness_v1.py --dataset rag_eval/data/redundancy_agent_ragas_dataset_v1.jsonl --label redundancy",
            "# 4) 串行/并行性能对照(6.4, 需BGE环境)",
            "python rag_eval/scripts/run_parallel_performance_eval_v1.py --doc-filter 066 --max-docs 1 --concurrency 1 4 --fresh",
            "# 5) 汇总(固定到完整审查结果, 避免被6.4的单文档结果污染)",
            "python rag_eval/scripts/build_chapter6_evaluation_report_v1.py --review-result review_results/review_result_v9_20260616_111810.json",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--review-result", type=Path, default=None)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--report", type=Path, default=OUTPUT_MD)
    parser.add_argument("--ragas-jsonl", type=Path, default=OUTPUT_RAGAS_JSONL)
    parser.add_argument("--agent-jsonl", type=Path, default=OUTPUT_AGENT_JSONL)
    args = parser.parse_args()

    review_path = args.review_result or latest_review_result()
    gold = load_json(args.gold)
    review = load_json(review_path)
    review_rows = list(iter_review_chunks(review))
    review_index = build_review_index(review)
    runtime_kb_id_map = build_runtime_kb_id_map(args.kb)
    chunk_to_group = load_chunk_to_group(args.groups)

    retrieval = evaluate_retrieval(gold, review_index, runtime_kb_id_map, chunk_to_group)
    function_quality = evaluate_function_quality(gold, review_index)
    agents, agent_rows = agent_output_summary(review_rows)
    ragas_rows = build_ragas_rows(gold, review_index)
    ragas_count = write_jsonl(args.ragas_jsonl, ragas_rows)
    agent_count = write_jsonl(args.agent_jsonl, agent_rows)
    runtime = summarize_runtime(review_rows)

    strategy_payload = load_optional_json(STRATEGY_EVAL)
    if strategy_payload:
        strategy_eval = {"status": "available", **strategy_payload}
    else:
        strategy_eval = {
            "status": "not_run",
            "required_command": "python rag_eval/scripts/run_main_agent_strategy_eval_v1.py",
        }

    parallel_payload = load_optional_json(PARALLEL_EVAL)
    if parallel_payload:
        parallel_eval = {"status": "available", **parallel_payload}
    else:
        parallel_eval = {
            "status": "not_run",
            "required_command": "python rag_eval/scripts/run_parallel_performance_eval_v1.py --concurrency 1 4",
        }

    retrieval_ablation = load_optional_json(RETRIEVAL_ABLATION)
    strategy_gen_eval = load_optional_json(STRATEGY_GEN_EVAL)
    groundedness = {
        "typo": load_optional_json(GROUNDEDNESS_TYPO),
        "redundancy": load_optional_json(GROUNDEDNESS_REDUNDANCY),
    }

    payload = {
        "schema": "coal_chapter6_evaluation_report_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "gold": str(args.gold.resolve()),
            "review_result": str(review_path.resolve()),
            "kb": str(args.kb.resolve()),
            "evidence_groups": str(args.groups.resolve()),
        },
        "section_6_1_retrieval": retrieval,
        "section_6_2_function_quality": function_quality,
        "agent_output_summary": agents,
        "section_6_3_strategy_eval": strategy_eval,
        "section_6_3_strategy_gen_eval": strategy_gen_eval,
        "section_6_4_parallel_runtime": runtime,
        "parallel_performance_eval": parallel_eval,
        "retrieval_ablation": retrieval_ablation,
        "groundedness": groundedness,
        "ragas_dataset": {
            "path": str(args.ragas_jsonl.resolve()),
            "row_count": ragas_count,
            "non_empty_response_count": sum(bool(row.get("response", "").strip()) for row in ragas_rows),
            "dependencies": {
                "ragas": importlib.util.find_spec("ragas") is not None,
                "langchain_openai": importlib.util.find_spec("langchain_openai") is not None,
            },
        },
        "ragas_metrics": (
            {"status": "available", **load_json(RAGAS_METRICS)}
            if RAGAS_METRICS.exists()
            else {
                "status": "not_run",
                "required_command": (
                    "conda run -n langchain0.3 python "
                    "rag_eval/scripts/run_ragas_current_method_v1.py "
                    "--dataset rag_eval/data/chapter6_ragas_dataset_filled_v1.jsonl "
                    "--output rag_eval/data/chapter6_ragas_metrics_v1.json "
                    "--embedding-backend local-bge"
                ),
            }
        ),
        "agent_outputs": {
            "path": str(args.agent_jsonl.resolve()),
            "row_count": agent_count,
        },
    }
    atomic_write(args.output, payload)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_markdown(payload), encoding="utf-8")
    print(json.dumps({
        "output": str(args.output.resolve()),
        "report": str(args.report.resolve()),
        "ragas_jsonl": str(args.ragas_jsonl.resolve()),
        "agent_jsonl": str(args.agent_jsonl.resolve()),
        "retrieval_mrr": retrieval["aggregate"]["mrr"],
        "function_exact_status_accuracy": function_quality["exact_status_accuracy"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
