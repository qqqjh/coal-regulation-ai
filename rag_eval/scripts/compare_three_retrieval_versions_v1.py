"""Compare retrieval metrics for the initial, atomic-claim, and grouped BGE versions."""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "three_retrieval_versions_metrics_v1.json"
OUTPUT_MD = PROJECT_ROOT / "思路修改" / "三版检索指标对比_v1.md"
KS = (1, 3, 5, 10, 15)

VERSIONS = (
    {
        "key": "initial",
        "name": "第一版混合检索",
        "method": "text-embedding-v3 Dense Top5 + BM25 Top5 + RRF Hybrid Top5 + 自定义Reranker",
        "path": PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json",
        "items_key": "candidate_annotations",
        "rank_key": "rank",
    },
    {
        "key": "atomic",
        "name": "原子主张级检索",
        "method": "父chunk拆分原子主张，各主张独立检索后合并为父级证据候选",
        "path": PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json",
        "items_key": "evidence_annotations",
        "rank_key": "parent_rank",
    },
    {
        "key": "grouped_bge",
        "name": "当前BGE-M3证据组版",
        "method": "BGE-M3 Dense + Learned Sparse + RRF + BGE-Reranker + 证据组去重补位",
        "path": PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json",
        "items_key": "candidate_annotations",
        "rank_key": "rank",
    },
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def dcg(values: list[int]) -> float:
    return sum(value / math.log2(rank + 1) for rank, value in enumerate(values, start=1))


def evaluate_version(config: dict[str, Any]) -> dict[str, Any]:
    source = load_json(config["path"])
    all_cases = []
    for case in source["cases"]:
        ranked = sorted(case[config["items_key"]], key=lambda item: item[config["rank_key"]])
        relevance = [int(item["label"] == "useful") for item in ranked]
        useful_count = sum(relevance)
        if not useful_count:
            continue
        first_rank = relevance.index(1) + 1
        row = {
            "candidate_count": len(ranked),
            "useful_count": useful_count,
            "mrr": 1.0 / first_rank,
        }
        for k in KS:
            top = relevance[:k]
            hits = sum(top)
            row[f"hit@{k}"] = float(hits > 0)
            row[f"recall@{k}"] = hits / useful_count
            row[f"precision@{k}"] = hits / k
            ideal = dcg([1] * min(k, useful_count))
            row[f"ndcg@{k}"] = dcg(top) / ideal if ideal else 0.0
        all_cases.append(row)

    total_candidates = sum(len(case[config["items_key"]]) for case in source["cases"])
    candidate_counts = [len(case[config["items_key"]]) for case in source["cases"]]
    result = {
        "key": config["key"],
        "name": config["name"],
        "method": config["method"],
        "source": str(config["path"].resolve()),
        "case_count": len(source["cases"]),
        "evaluated_case_count": len(all_cases),
        "excluded_zero_gold_case_count": len(source["cases"]) - len(all_cases),
        "useful_case_coverage": len(all_cases) / len(source["cases"]),
        "total_useful_evidence_count": sum(row["useful_count"] for row in all_cases),
        "total_candidate_count": total_candidates,
        "average_candidates_per_case": mean(candidate_counts),
        "minimum_candidates_per_case": min(candidate_counts),
        "maximum_candidates_per_case": max(candidate_counts),
        "mrr": mean(row["mrr"] for row in all_cases),
    }
    for metric in ("hit", "recall", "precision", "ndcg"):
        result[f"{metric}_at"] = {
            str(k): mean(row[f"{metric}@{k}"] for row in all_cases) for k in KS
        }
    return result


def fmt(value: float) -> str:
    return f"{value:.4f}"


def build_markdown(results: list[dict[str, Any]]) -> str:
    lines = [
        "# 三版检索指标对比",
        "",
        "统一口径：以父待审 chunk 为评价单位；仅统计至少包含一条 `useful` 黄金证据的案例；"
        "每一版使用其自身人工标注候选中的 `useful` 作为黄金证据。",
        "",
        "注意：三版黄金候选池不同，因此该表适合比较各版本候选排序和有效证据覆盖情况，"
        "不能视为完全独立、严格同源的模型排行榜。",
        "",
        "## 数据规模",
        "",
        "| 版本 | 总案例 | 有useful案例 | 覆盖率 | useful证据数 | 总候选 | 平均候选/案例 | 候选范围 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        lines.append(
            f"| {item['name']} | {item['case_count']} | {item['evaluated_case_count']} | "
            f"{item['useful_case_coverage']:.2%} | {item['total_useful_evidence_count']} | "
            f"{item['total_candidate_count']} | "
            f"{item['average_candidates_per_case']:.2f} | "
            f"{item['minimum_candidates_per_case']}～{item['maximum_candidates_per_case']} |"
        )

    lines.extend(
        [
            "",
            "## 三版核心指标对比",
            "",
            "| 指标 | 第一版混合检索 | 原子主张级检索 | 当前BGE-M3证据组版 |",
            "|---|---:|---:|---:|",
        ]
    )
    metrics = [
        ("MRR", lambda item: item["mrr"]),
        ("Hit@1", lambda item: item["hit_at"]["1"]),
        ("Hit@3", lambda item: item["hit_at"]["3"]),
        ("Hit@5", lambda item: item["hit_at"]["5"]),
        ("Hit@10", lambda item: item["hit_at"]["10"]),
        ("Hit@15", lambda item: item["hit_at"]["15"]),
        ("Recall@5", lambda item: item["recall_at"]["5"]),
        ("Recall@10", lambda item: item["recall_at"]["10"]),
        ("Recall@15", lambda item: item["recall_at"]["15"]),
        ("NDCG@15", lambda item: item["ndcg_at"]["15"]),
    ]
    for label, getter in metrics:
        lines.append("| " + label + " | " + " | ".join(fmt(getter(item)) for item in results) + " |")

    for item in results:
        lines.extend(
            [
                "",
                f"## {item['name']}",
                "",
                item["method"],
                "",
                f"仅统计至少存在一条 useful 黄金证据的 **{item['evaluated_case_count']} 个可评价案例**，"
                f"排除 {item['excluded_zero_gold_case_count']} 个没有 useful 黄金证据的案例。",
                "",
                "| 指标 | 数值 |",
                "|---|---:|",
            ]
        )
        for label, getter in metrics:
            lines.append(f"| {label} | {fmt(getter(item))} |")

    lines.extend(
        [
            "",
            "## 解读限制",
            "",
            "- 第一版和当前 BGE-M3 版均固定为每个案例 Top15；原子主张版合并后的父级候选数量为 0～25 条。",
            "- 第一版与当前版的 `Recall@15=1.0`，因为 useful 黄金证据来源于其自身 Top15 标注，"
            "这只能验证候选内排序，不能证明召回了知识库中全部正确规则。",
            "- 原子主张版存在超过 15 条的父级证据候选，因此 `Recall@15` 可以反映 useful 证据是否集中在前15名。",
            "- 三版可评价案例分别为不同子集。后续进行严格模型对比时，应建立独立于候选池的统一证据组黄金集。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> None:
    results = [evaluate_version(config) for config in VERSIONS]
    payload = {
        "schema": "coal_rag_three_retrieval_versions_metrics_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "evaluation_definition_zh": "父待审chunk级；仅统计至少一条useful黄金证据的案例；各版本按自身候选人工标签评价。",
        "versions": results,
    }
    atomic_write(OUTPUT_JSON, payload)
    OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_MD.write_text(build_markdown(results), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(OUTPUT_JSON.resolve())
    print(OUTPUT_MD.resolve())


if __name__ == "__main__":
    main()
