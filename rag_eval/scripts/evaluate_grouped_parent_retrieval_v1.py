"""Evaluate parent retrieval with same-evidence-group normalization."""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json"
DEFAULT_RETRIEVAL = (
    PROJECT_ROOT / "rag_eval" / "data" / "parent_candidates_local_bgem3_v6_grouped_v1.json"
)
DEFAULT_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "grouped_parent_retrieval_metrics_v1.json"
KS = (1, 3, 5, 10, 15)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def evidence_id(candidate: dict[str, Any], chunk_to_group: dict[str, str]) -> str:
    return candidate.get("evidence_group_id") or chunk_to_group.get(candidate["chunk_id"]) or candidate["chunk_id"]


def unique_ranked_evidence_ids(
    candidates: Iterable[dict[str, Any]], chunk_to_group: dict[str, str]
) -> list[str]:
    result = []
    seen = set()
    for candidate in candidates:
        item = evidence_id(candidate, chunk_to_group)
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def dcg(binary_relevance: list[int]) -> float:
    return sum(value / math.log2(rank + 1) for rank, value in enumerate(binary_relevance, start=1))


def evaluate_case(retrieved: list[str], gold: set[str]) -> dict[str, Any]:
    first_relevant_rank = next((rank for rank, item in enumerate(retrieved, start=1) if item in gold), None)
    metrics: dict[str, Any] = {
        "gold_evidence_count": len(gold),
        "first_relevant_rank": first_relevant_rank,
        "reciprocal_rank": 1.0 / first_relevant_rank if first_relevant_rank else 0.0,
        "precision_at": {},
        "recall_at": {},
        "hit_at": {},
        "ndcg_at": {},
    }
    for k in KS:
        top = retrieved[:k]
        relevant_count = len(set(top) & gold)
        metrics["precision_at"][str(k)] = relevant_count / k
        metrics["recall_at"][str(k)] = relevant_count / len(gold) if gold else None
        metrics["hit_at"][str(k)] = float(relevant_count > 0)
        ideal = dcg([1] * min(k, len(gold)))
        actual = dcg([int(item in gold) for item in top])
        metrics["ndcg_at"][str(k)] = actual / ideal if ideal else None
    return metrics


def mean(values: Iterable[float | None]) -> float | None:
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    gold = load_json(args.gold)
    retrieval = load_json(args.retrieval)
    chunk_to_group = load_json(args.groups)["chunk_to_group"]
    gold_cases = {case["case_id"]: case for case in gold["cases"]}
    case_metrics = []
    for case in retrieval["cases"]:
        case_id = case["case_id"]
        retrieved_ids = unique_ranked_evidence_ids(case["candidates"], chunk_to_group)
        gold_ids = set(gold_cases[case_id]["gold_useful_evidence_ids"])
        case_metrics.append(
            {
                "case_id": case_id,
                "retrieved_evidence_ids": retrieved_ids,
                "gold_useful_evidence_ids": sorted(gold_ids),
                "metrics": evaluate_case(retrieved_ids, gold_ids),
            }
        )

    eligible_cases = [case for case in case_metrics if case["gold_useful_evidence_ids"]]
    aggregate: dict[str, Any] = {
        "case_count": len(case_metrics),
        "evaluated_case_count": len(eligible_cases),
        "excluded_zero_gold_case_count": len(case_metrics) - len(eligible_cases),
        "macro_population": "only cases with at least one useful gold evidence id",
        "mrr": mean(case["metrics"]["reciprocal_rank"] for case in eligible_cases),
    }
    for metric_name in ("precision_at", "recall_at", "hit_at", "ndcg_at"):
        aggregate[metric_name] = {
            str(k): mean(case["metrics"][metric_name][str(k)] for case in eligible_cases)
            for k in KS
        }

    payload = {
        "schema": "coal_rag_grouped_parent_retrieval_metrics_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "definition_zh": "检索结果和黄金证据均先归一化为evidence_group_id；同组多个chunk只计一次。",
        "gold_source": str(args.gold.resolve()),
        "retrieval_source": str(args.retrieval.resolve()),
        "evidence_groups_source": str(args.groups.resolve()),
        "aggregate": aggregate,
        "cases": case_metrics,
    }
    atomic_write(args.output, payload)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
