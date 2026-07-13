"""Evaluate the current coal-regulation RAG method.

This script separates three layers:
1. retrieval quality metrics from the current evidence-level gold set;
2. available runtime metrics from the latest v9 review result JSON;
3. RAGAS readiness, including a JSONL dataset skeleton for later LLM judging.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top3_gold_annotations.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "current_rag_method_evaluation_v1.json"
OUTPUT_MD = PROJECT_ROOT / "思路修改" / "当前RAG方法评估_v1.md"
OUTPUT_RAGAS_JSONL = PROJECT_ROOT / "rag_eval" / "data" / "current_rag_method_ragas_dataset_skeleton_v1.jsonl"
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
        raise FileNotFoundError("未找到 review_results/review_result_v9_20*.json")
    return files[0]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def evidence_id(item: dict[str, Any]) -> str:
    return item.get("evidence_group_id") or item.get("chunk_id", "")


def tag_applicability(chunk: dict[str, Any], doc_name: str) -> str:
    if any(pattern in doc_name for pattern in OUTBURST_DOC_PATTERNS):
        return "outburst_only"
    struct_text = " ".join([
        str(chunk.get("part", "")),
        str(chunk.get("chapter", "")),
        str(chunk.get("section", "")),
        str(chunk.get("context_prefix", "") or ""),
        " ".join(chunk.get("parent_context") or []),
    ])
    if any(keyword in struct_text for keyword in OUTBURST_STRUCT_KEYWORDS):
        return "outburst_only"
    return "general"


def load_chunk_to_group(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = load_json(path)
    return dict(data.get("chunk_to_group") or {})


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


def dcg(values: list[int]) -> float:
    return sum(value / math.log2(rank + 1) for rank, value in enumerate(values, start=1))


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    ranked = sorted(case["evidence_annotations"], key=lambda item: item["parent_rank"])
    seen: set[str] = set()
    unique_ranked = []
    for item in ranked:
        eid = evidence_id(item)
        if eid in seen:
            continue
        seen.add(eid)
        unique_ranked.append(item)
    gold = {evidence_id(item) for item in unique_ranked if item["label"] == "useful"}
    relevance = [int(evidence_id(item) in gold) for item in unique_ranked]
    first_rank = next((i + 1 for i, value in enumerate(relevance) if value), None)
    row: dict[str, Any] = {
        "case_id": case["case_id"],
        "candidate_count": len(unique_ranked),
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


def evaluate_ranked_ids(
    case: dict[str, Any],
    ranked_ids: list[str],
    candidate_count: int,
    source_status: str,
) -> dict[str, Any]:
    gold = {
        evidence_id(item)
        for item in case["evidence_annotations"]
        if item["label"] == "useful"
    }
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
        "source_status": source_status,
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


def avg(values: Iterable[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return mean(valid) if valid else None


def aggregate_case_rows(case_rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in case_rows if row["gold_evidence_count"] > 0]
    aggregate: dict[str, Any] = {
        "case_count": len(case_rows),
        "evaluated_case_count": len(eligible),
        "excluded_zero_gold_case_count": len(case_rows) - len(eligible),
        "final_label_counts": dict(Counter(row["final_label"] for row in case_rows)),
        "missing_correct_evidence_case_count": sum(row["missing_correct_evidence"] for row in case_rows),
        "not_eval_suitable_case_count": sum(row["not_eval_suitable"] for row in case_rows),
        "total_gold_useful_evidence_count": sum(row["gold_evidence_count"] for row in case_rows),
        "average_candidates_per_case": mean(row["candidate_count"] for row in case_rows),
        "cases_with_candidates": sum(row["candidate_count"] > 0 for row in case_rows),
        "mrr": avg(row["mrr"] for row in eligible),
    }
    for metric in ("hit", "recall", "precision", "ndcg"):
        aggregate[f"{metric}_at"] = {
            str(k): avg(row[f"{metric}@{k}"] for row in eligible) for k in KS
        }
    return {"aggregate": aggregate, "cases": case_rows}


def aggregate_retrieval(gold: dict[str, Any]) -> dict[str, Any]:
    return aggregate_case_rows([evaluate_case(case) for case in gold["cases"]])


def iter_review_chunks(review: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for doc_name, chunks in review.items():
        if not isinstance(chunks, list):
            continue
        for item in chunks:
            row = dict(item)
            row["doc_name"] = doc_name
            yield row


def build_review_index(review: dict[str, Any]) -> dict[tuple[str, int], dict[str, Any]]:
    index = {}
    for row in iter_review_chunks(review):
        index[(row["doc_name"], int(row.get("chunk_index", -1)))] = row
    return index


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
        evaluation_id = chunk_to_group.get(stable_id) or stable_id
        if evaluation_id in seen:
            continue
        seen.add(evaluation_id)
        ranked_ids.append(evaluation_id)
    return ranked_ids


def aggregate_v9_actual_refs(
    gold: dict[str, Any],
    review_path: Path,
    kb_path: Path,
    groups_path: Path,
) -> dict[str, Any]:
    runtime_kb_id_map = build_runtime_kb_id_map(kb_path)
    chunk_to_group = load_chunk_to_group(groups_path)
    review = load_json(review_path)
    review_index = build_review_index(review)
    case_rows: list[dict[str, Any]] = []
    source_counter: Counter[str] = Counter()
    missing_review_rows = 0
    for case in gold["cases"]:
        pending = case["pending"]
        key = (pending.get("doc_name", ""), int(pending.get("chunk_index", -1)))
        review_row = review_index.get(key)
        if not review_row:
            missing_review_rows += 1
            refs = []
            source_status = "missing_review_row"
        elif review_row.get("retrieved_kb_refs"):
            refs = review_row.get("retrieved_kb_refs") or []
            source_status = "full_retrieved_kb_refs"
        else:
            refs = review_row.get("kb_refs") or []
            source_status = "displayed_or_used_kb_refs_only"
        source_counter[source_status] += 1
        ranked_ids = normalize_review_refs(refs, runtime_kb_id_map, chunk_to_group)
        case_rows.append(
            evaluate_ranked_ids(
                case=case,
                ranked_ids=ranked_ids,
                candidate_count=len(refs),
                source_status=source_status,
            )
        )
    result = aggregate_case_rows(case_rows)
    result["aggregate"]["source_status_counts"] = dict(source_counter)
    result["aggregate"]["missing_review_row_count"] = missing_review_rows
    result["aggregate"]["runtime_kb_id_count"] = len(runtime_kb_id_map)
    result["aggregate"]["definition_zh"] = (
        "从 v9 主智能体/多智能体审查结果中抽取实际法规引用；"
        "旧结果没有 full retrieved_kb_refs 时，回退使用报告展示的 kb_refs。"
    )
    return result


def summarize_runtime(review_path: Path) -> dict[str, Any]:
    if not review_path.exists():
        return {
            "status": "missing",
            "source": str(review_path.resolve()),
            "message": "未找到现有审查结果，需运行 v9 后采集时间指标。",
        }
    review = load_json(review_path)
    chunks = list(iter_review_chunks(review))
    timing_rows = [chunk.get("timings") or {} for chunk in chunks]
    has_timings = any(timing_rows)
    if not has_timings:
        return {
            "status": "not_recorded_in_existing_result",
            "source": str(review_path.resolve()),
            "chunk_count": len(chunks),
            "message": "该历史结果未落盘 timings；已在 hybrid_rag_review_v9.py 中补充后续运行字段。",
            "required_fields": [
                "phase_a_doc_total",
                "phase_a_avg_per_chunk",
                "chunk_total",
                "generation_like_llm",
                "compliance_review",
                "verification",
                "typo",
                "repetition_doc_total",
            ],
        }

    def values(key: str) -> list[float]:
        return [float(row[key]) for row in timing_rows if key in row]

    summary = {
        "status": "available",
        "source": str(review_path.resolve()),
        "chunk_count": len(chunks),
        "metrics": {},
    }
    for key in (
        "phase_a_avg_per_chunk",
        "chunk_total",
        "generation_like_llm",
        "compliance_review",
        "numeric_total",
        "verification",
        "kb_classification",
        "typo",
        "repetition_doc_total",
    ):
        vals = values(key)
        if vals:
            summary["metrics"][key] = {
                "count": len(vals),
                "mean_sec": mean(vals),
                "median_sec": median(vals),
                "p95_sec": sorted(vals)[max(0, math.ceil(len(vals) * 0.95) - 1)],
                "max_sec": max(vals),
            }
    return summary


def build_ragas_skeleton(
    gold: dict[str, Any],
    review_path: Path,
    output: Path,
) -> dict[str, Any]:
    review = load_json(review_path) if review_path.exists() else {}
    review_index = build_review_index(review) if isinstance(review, dict) else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for case in gold["cases"]:
            pending = case["pending"]
            review_row = review_index.get((pending.get("doc_name", ""), int(pending.get("chunk_index", -1))), {})
            refs = review_row.get("retrieved_kb_refs") or review_row.get("kb_refs") or []
            retrieved_contexts = [
                ref.get("content", "")
                for ref in refs
                if ref.get("content")
            ]
            reference_contexts = [
                item["content"]
                for item in sorted(case["evidence_annotations"], key=lambda x: x["parent_rank"])
                if item["label"] == "useful"
            ]
            row = {
                "case_id": case["case_id"],
                "user_input": case["pending"]["content"],
                "retrieved_contexts": retrieved_contexts,
                "response": "",
                "reference": f"{case.get('final_label', '')}：{case.get('final_reason', '')}",
                "reference_contexts": reference_contexts,
                "metadata": {
                    "doc_name": case["pending"].get("doc_name", ""),
                    "retrieved_context_count": len(retrieved_contexts),
                    "reference_context_count": len(reference_contexts),
                    "missing_correct_evidence": bool(case.get("missing_correct_evidence")),
                    "not_eval_suitable": bool(case.get("not_eval_suitable")),
                },
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return {
        "path": str(output.resolve()),
        "row_count": count,
        "status": "response_placeholder_empty",
        "retrieved_context_source": str(review_path.resolve()),
        "required_next_step": "用同一批待审chunk运行现有审查链，填充 response 后再执行 RAGAS。",
    }


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_markdown(payload: dict[str, Any]) -> str:
    retrieval = payload["retrieval"]["aggregate"]
    actual = payload["v9_actual_agent_refs"]["aggregate"]
    runtime = payload["runtime"]
    ragas = payload["ragas"]
    lines = [
        "# 当前 RAG 方法评估",
        "",
        f"生成时间：`{payload['generated_at']}`",
        "",
        "## 方法口径",
        "",
        "- 当前离线黄金集：`atomic_codex_v2_focused_top3_gold_annotations.json`。",
        "- 评价单位：待审父 chunk。",
        "- 证据归一化：优先使用 `evidence_group_id`，没有证据组时使用 `chunk_id`。",
        "- 只在至少有一条 `useful` 黄金证据的案例上计算 MRR/Hit/Recall/NDCG。",
        "- `v9_actual_agent_refs` 才是根据 v9 主智能体/多智能体审查结果统计的现有方法指标。",
        "- `gold_pool_candidate_ordering` 仅保留为对照：它评估黄金候选池内部排序，不代表 v9 实际输出。",
        "",
        "## v9 实际智能体引用指标",
        "",
        f"- 总案例：{actual['case_count']}",
        f"- 可评价案例：{actual['evaluated_case_count']}",
        f"- 排除无 useful 黄金证据案例：{actual['excluded_zero_gold_case_count']}",
        f"- useful 黄金证据总数：{actual['total_gold_useful_evidence_count']}",
        f"- 有实际引用的案例：{actual['cases_with_candidates']}",
        f"- 平均实际引用数：{actual['average_candidates_per_case']:.2f}",
        f"- 来源状态：`{actual['source_status_counts']}`",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| MRR | {fmt(actual['mrr'])} |",
    ]
    for k in KS:
        lines.append(f"| Hit@{k} | {fmt(actual['hit_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| Recall@{k} | {fmt(actual['recall_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| Precision@{k} | {fmt(actual['precision_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| NDCG@{k} | {fmt(actual['ndcg_at'][str(k)])} |")

    lines.extend([
        "",
        "说明：当前历史 v9 结果没有保存完整检索 TopK，仅保存了最终报告展示/采用的 `kb_refs`。",
        "因此这张表反映“智能体实际引用证据”的质量；后续重新运行 v9 后，会使用新增的 `retrieved_kb_refs` 评估完整检索结果。",
        "",
        "## 黄金候选池排序指标（对照）",
        "",
        f"- 总案例：{retrieval['case_count']}",
        f"- 可评价案例：{retrieval['evaluated_case_count']}",
        f"- 排除无 useful 黄金证据案例：{retrieval['excluded_zero_gold_case_count']}",
        f"- useful 黄金证据总数：{retrieval['total_gold_useful_evidence_count']}",
        f"- 有候选证据的案例：{retrieval['cases_with_candidates']}",
        f"- 平均候选数：{retrieval['average_candidates_per_case']:.2f}",
        f"- `missing_correct_evidence` 案例：{retrieval['missing_correct_evidence_case_count']}",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| MRR | {fmt(retrieval['mrr'])} |",
    ])
    for k in KS:
        lines.append(f"| Hit@{k} | {fmt(retrieval['hit_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| Recall@{k} | {fmt(retrieval['recall_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| Precision@{k} | {fmt(retrieval['precision_at'][str(k)])} |")
    for k in KS:
        lines.append(f"| NDCG@{k} | {fmt(retrieval['ndcg_at'][str(k)])} |")

    lines.extend([
        "",
        "## RAGAS 指标",
        "",
        "RAGAS 需要同一批样本的 `user_input`、`retrieved_contexts`、`response`、`reference`。",
        "当前数据骨架中，`retrieved_contexts` 来自 v9 实际结果的法规引用，`reference_contexts` 来自人工黄金 useful 证据；目前还缺少由现有审查链生成并落盘的 `response`。",
        "参考：RAGAS 官方文档的 RAG 示例也是先收集 `response` 和 `retrieved_contexts`，再构造 `EvaluationDataset` 并调用 `evaluate(...)`。",
        "",
        "| RAGAS指标 | 本项目解释 | 当前状态 |",
        "|---|---|---|",
        "| Faithfulness | 回答是否被检索法规支撑 | 待跑审查链生成 response 后计算 |",
        "| Answer / Response Relevancy | 回答是否针对待审chunk | 待生成 response 后计算 |",
        "| Context Precision | 排名前面的法规是否更相关 | 已有检索近似指标，RAGAS版待接LLM judge |",
        "| Context Recall | 黄金证据是否被上下文覆盖 | 已有 Recall@k，RAGAS版待接 reference_contexts |",
        "| Factual Correctness | 合规结论和事实是否与参考一致 | 待生成 response 后计算 |",
        "",
        f"已生成 RAGAS 数据骨架：`{ragas['path']}`。",
        "RAGAS 运行脚本：`rag_eval/scripts/run_ragas_current_method_v1.py`。",
        "",
        "## 时间指标",
        "",
    ])
    if runtime["status"] == "available":
        lines.extend(["| 时间指标 | 均值(s) | 中位数(s) | P95(s) | 最大(s) |", "|---|---:|---:|---:|---:|"])
        for key, row in runtime["metrics"].items():
            lines.append(
                f"| {key} | {row['mean_sec']:.3f} | {row['median_sec']:.3f} | "
                f"{row['p95_sec']:.3f} | {row['max_sec']:.3f} |"
            )
    else:
        lines.extend([
            f"当前历史结果状态：`{runtime['status']}`。",
            "",
            runtime["message"],
            "",
            "已在 `hybrid_rag_review_v9.py` 补充后续运行会落盘的时间字段：",
            "",
            "- `phase_a_doc_total`：文档级检索预计算总耗时",
            "- `phase_a_avg_per_chunk`：检索预计算平均到每个 chunk 的耗时",
            "- `chunk_total`：单 chunk 合规链 + 错别字链总耗时",
            "- `generation_like_llm`：LLM 生成/判断相关耗时合计",
            "- `compliance_review`、`numeric_total`、`verification`、`kb_classification`、`typo`",
            "- `repetition_doc_total`：文档级重复性检查耗时",
        ])
    lines.extend([
        "",
        "## 结论",
        "",
        "- 检索指标可以先用于比较候选排序质量，但当前黄金证据来自候选池内部标注，不能等价为全知识库召回上限。",
        "- v9 实际引用指标偏低，主要因为旧结果只落盘了报告展示/采用的 `kb_refs`，合规块的大量检索证据没有保存；需要重新跑 v9 并使用 `retrieved_kb_refs` 才能得到完整检索 TopK 指标。",
        "- 35 个 `missing_correct_evidence` 案例说明 Top3 原子主张候选仍有正确证据缺失风险，后续应重点看这些样本。",
        "- RAGAS 应放在完整审查输出之后，用来评估最终回答质量；它不替代 Hit/Recall/MRR/NDCG。",
        "",
        "## 参考",
        "",
        "- RAGAS GitHub: https://github.com/vibrantlabsai/ragas",
        "- RAGAS simple RAG evaluation docs: https://docs.ragas.io/en/stable/getstarted/rag_eval/",
    ])
    return "\n".join(lines) + "\n"


def main() -> None:
    gold = load_json(DEFAULT_GOLD)
    review_result = latest_review_result()
    retrieval_eval = aggregate_retrieval(gold)
    v9_actual_refs_eval = aggregate_v9_actual_refs(
        gold, review_result, DEFAULT_KB, DEFAULT_GROUPS
    )
    runtime = summarize_runtime(review_result)
    ragas = build_ragas_skeleton(gold, review_result, OUTPUT_RAGAS_JSONL)
    payload = {
        "schema": "coal_current_rag_method_evaluation_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sources": {
            "gold": str(DEFAULT_GOLD.resolve()),
            "review_result": str(review_result.resolve()),
            "kb": str(DEFAULT_KB.resolve()),
            "evidence_groups": str(DEFAULT_GROUPS.resolve()),
        },
        "v9_actual_agent_refs": v9_actual_refs_eval,
        "retrieval": retrieval_eval,
        "runtime": runtime,
        "ragas": ragas,
        "ragas_reference": {
            "repo": "https://github.com/vibrantlabsai/ragas",
            "planned_metrics": [
                "Faithfulness",
                "Response Relevancy",
                "Context Precision",
                "Context Recall",
                "Factual Correctness",
            ],
        },
    }
    atomic_write(OUTPUT_JSON, payload)
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(build_markdown(payload), encoding="utf-8")
    print(json.dumps(payload["v9_actual_agent_refs"]["aggregate"], ensure_ascii=False, indent=2))
    print(OUTPUT_JSON.resolve())
    print(OUTPUT_MD.resolve())
    print(OUTPUT_RAGAS_JSONL.resolve())


if __name__ == "__main__":
    main()
