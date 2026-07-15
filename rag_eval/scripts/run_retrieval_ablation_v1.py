"""6.1 检索效果消融：在同一黄金集上对比四档检索方案。

四档（每档只比上一档多一个组件，便于看清每一步的增益）：
  1. dense          稠密检索基线（BGE-M3 dense，按余弦排序）
  2. hybrid         稠密 + 学习稀疏 + RRF(k=60) 融合
  3. hybrid_rerank  混合召回 + bge-reranker-v2-m3 重排
  4. parent_multi   父块整体 query + 编号项/关键句片段 query 多路召回后重排去重（v9 生产方案）

所有变体在同一黄金集、同一候选上限(TopK=15)、同一评价口径下，报告
覆盖率/Hit@K/Recall@K/MRR/NDCG@K 与平均检索耗时，指标口径完全一致。

证据归一化与黄金集口径直接复用 evaluate_current_rag_method_v1.py，
确保与已有 6.1 单方案报告可比。

必须在 BGE 环境运行：
  D:\\Anaconda\\envs\\langchain0.3\\python.exe rag_eval/scripts/run_retrieval_ablation_v1.py
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Callable

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (str(PROJECT_ROOT), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# 复用已验证的黄金集口径 / 证据归一化 / 指标聚合
from evaluate_current_rag_method_v1 import (  # noqa: E402
    DEFAULT_GOLD,
    DEFAULT_KB,
    DEFAULT_GROUPS,
    KS,
    aggregate_case_rows,
    build_runtime_kb_id_map,
    evaluate_ranked_ids,
    load_chunk_to_group,
    load_json,
    normalize_review_refs,
)

OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "retrieval_ablation_v1.json"
OUTPUT_MD = PROJECT_ROOT / "rag_eval" / "reports" / "retrieval_ablation_v1.md"
MAX_K = max(KS)

VARIANTS = [
    ("dense", "稠密检索基线", "BGE-M3 dense 余弦排序"),
    ("hybrid", "混合检索", "dense + learned sparse + RRF(k=60)"),
    ("hybrid_rerank", "混合检索+重排", "混合召回 + bge-reranker-v2-m3"),
    ("parent_multi", "父块保底+片段补充", "整块query + 片段query 多路召回重排去重（v9生产）"),
]


def build_engine(mine_type: str):
    import hybrid_rag_review_v9 as eng

    reviewer = eng.HybridRAGReviewerV9(mine_type=mine_type)
    reviewer.load_knowledge_base()
    reviewer.build_index()
    return reviewer, eng


def retrieve_all_variants(reviewer, eng, content: str) -> dict[str, dict[str, Any]]:
    """对一个待审 chunk 跑四档检索，返回 {variant: {ranked_chunk_ids, elapsed_sec}}。

    encode 只做一次（整块一次、片段一次），各变体按其真实需要的 encode + search 计时，
    保证耗时口径公平：dense/hybrid/hybrid_rerank 只算整块 encode，parent_multi 额外算片段 encode。
    """
    out: dict[str, dict[str, Any]] = {}

    t0 = time.perf_counter()
    full_dense_arr, full_lex_list = reviewer.retriever.encode([content])
    t_query_encode = time.perf_counter() - t0
    full_dense, full_lex = full_dense_arr[0], full_lex_list[0]

    segments = eng.split_chunk_segments(content)
    if segments:
        t0 = time.perf_counter()
        seg_dense, seg_lex = reviewer.retriever.encode(segments)
        t_seg_encode = time.perf_counter() - t0
    else:
        seg_dense, seg_lex, t_seg_encode = [], [], 0.0

    n_docs = len(reviewer.kb_chunks)

    # 1) dense
    t0 = time.perf_counter()
    dense = eng._dense_search(full_dense, reviewer.kb_dense, MAX_K)
    out["dense"] = {
        "ranked_chunk_ids": [f"kb_{idx}" for idx, _ in dense],
        "elapsed_sec": t_query_encode + (time.perf_counter() - t0),
    }

    # 2) hybrid (dense + sparse + RRF)
    t0 = time.perf_counter()
    pool = max(30, eng.RERANK_POOL_PER_CHANNEL * 3)
    d = eng._dense_search(full_dense, reviewer.kb_dense, pool)
    s = eng._sparse_search(full_lex, reviewer.kb_postings, n_docs, pool)
    rrf = eng._rrf_hybrid(d, s, MAX_K)
    out["hybrid"] = {
        "ranked_chunk_ids": [f"kb_{idx}" for idx, _ in rrf],
        "elapsed_sec": t_query_encode + (time.perf_counter() - t0),
    }

    # 3) hybrid + rerank（关阈值，纯排序消融）
    t0 = time.perf_counter()
    indices = reviewer._recall_union(full_dense, full_lex, eng.RERANK_POOL_PER_CHANNEL)
    if indices:
        scores = reviewer.retriever.rerank(content, [reviewer.kb_texts[i] for i in indices])
        ranked = sorted(zip(indices, scores), key=lambda x: x[1], reverse=True)[:MAX_K]
        ids3 = [f"kb_{idx}" for idx, _ in ranked]
    else:
        ids3 = []
    out["hybrid_rerank"] = {
        "ranked_chunk_ids": ids3,
        "elapsed_sec": t_query_encode + (time.perf_counter() - t0),
    }

    # 4) parent + multi-query（整块 + 片段，关阈值；对应 search_chunk_multi）
    t0 = time.perf_counter()
    pairs: list[tuple[str, int]] = []
    for idx in reviewer._recall_union(full_dense, full_lex, eng.RERANK_POOL_PER_CHANNEL):
        pairs.append((content, idx))
    for s_i, segment in enumerate(segments):
        for idx in reviewer._recall_union(seg_dense[s_i], seg_lex[s_i], eng.SEGMENT_POOL_PER_CHANNEL):
            pairs.append((segment, idx))
    if pairs:
        unique_pairs = list(dict.fromkeys(pairs))
        scores = reviewer.retriever.rerank_pairs(
            [[q, reviewer.kb_texts[idx]] for q, idx in unique_pairs]
        )
        best: dict[int, float] = {}
        for (_q, idx), score in zip(unique_pairs, scores):
            if score > best.get(idx, -1.0):
                best[idx] = score
        ranked4 = sorted(best.items(), key=lambda x: x[1], reverse=True)[:MAX_K]
        ids4 = [f"kb_{idx}" for idx, _ in ranked4]
    else:
        ids4 = []
    out["parent_multi"] = {
        "ranked_chunk_ids": ids4,
        "elapsed_sec": t_query_encode + t_seg_encode + (time.perf_counter() - t0),
    }
    return out


def fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_markdown(payload: dict[str, Any]) -> str:
    rows = payload["variants"]
    lines = [
        "# 6.1 检索效果消融对比",
        "",
        f"生成时间：`{payload['generated_at']}`",
        f"黄金集：`{payload['gold']}`",
        f"可评价案例：{payload['evaluated_case_count']} / 总案例 {payload['case_count']}"
        f"（仅在至少有一条 useful 黄金证据的案例上计算）",
        f"候选上限 TopK = {MAX_K}，重排变体已关闭分数阈值以做纯排序消融。",
        "",
        "## 总览（同一指标口径）",
        "",
        "| 检索方案 | 主要设置 | 覆盖率(Hit@15) | Hit@5 | Recall@5 | MRR | NDCG@10 | 平均耗时/ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, name, setting in VARIANTS:
        agg = rows[key]["aggregate"]
        lines.append(
            f"| {name} | {setting} | {fmt(agg['hit_at']['15'])} | {fmt(agg['hit_at']['5'])} | "
            f"{fmt(agg['recall_at']['5'])} | {fmt(agg['mrr'])} | {fmt(agg['ndcg_at']['10'])} | "
            f"{rows[key]['avg_retrieval_ms']:.1f} |"
        )

    def metric_table(title: str, metric: str) -> list[str]:
        block = ["", f"## {title}", "", "| 检索方案 | " + " | ".join(f"@{k}" for k in KS) + " |",
                 "|---|" + "---:|" * len(KS)]
        for key, name, _setting in VARIANTS:
            agg = rows[key]["aggregate"]
            cells = " | ".join(fmt(agg[f"{metric}_at"][str(k)]) for k in KS)
            block.append(f"| {name} | {cells} |")
        return block

    lines += metric_table("Hit@K", "hit")
    lines += metric_table("Recall@K", "recall")
    lines += metric_table("NDCG@K", "ndcg")
    lines += metric_table("Precision@K", "precision")
    lines += [
        "",
        "## 说明",
        "",
        "- 四档为递进消融：每档只比上一档多一个组件（+稀疏RRF / +重排 / +多查询），可直接读出每步增益。",
        "- 证据归一化、黄金集口径与 `evaluate_current_rag_method_v1.py` 完全一致，可与 6.1 单方案报告对照。",
        "- 重排两档关闭了生产用的 `RERANK_SCORE_THRESHOLD`，只比排序能力；生产环境该阈值会牺牲少量召回换精度。",
        "- 耗时为单 query 端到端（含其所需 encode）。parent_multi 额外包含片段编码与多路重排，故耗时最高。",
        "",
        f"运行命令：`python rag_eval/scripts/run_retrieval_ablation_v1.py`（需 BGE 环境）",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--mine-type", default="non_outburst", choices=["non_outburst", "outburst"])
    parser.add_argument("--limit", type=int, default=0, help="只跑前N个案例（调试用，0=全部）")
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--report", type=Path, default=OUTPUT_MD)
    args = parser.parse_args()

    gold = load_json(args.gold)
    cases = gold["cases"]
    if args.limit > 0:
        cases = cases[: args.limit]

    print(f"加载引擎（mine_type={args.mine_type}）...")
    reviewer, eng = build_engine(args.mine_type)
    runtime_kb_id_map = build_runtime_kb_id_map(args.kb, args.mine_type)
    chunk_to_group = load_chunk_to_group(args.groups)
    print(f"  engine kb_chunks={len(reviewer.kb_chunks)} | runtime_kb_id_map={len(runtime_kb_id_map)}")
    if len(reviewer.kb_chunks) != len(runtime_kb_id_map):
        print("  ⚠️ engine 与 runtime_kb_id_map 数量不一致，映射可能错位，请检查 KB/过滤口径。")

    rows_per_variant: dict[str, list[dict[str, Any]]] = {k: [] for k, _, _ in VARIANTS}
    times_per_variant: dict[str, list[float]] = {k: [] for k, _, _ in VARIANTS}

    for n, case in enumerate(cases, start=1):
        content = case["pending"]["content"]
        variant_out = retrieve_all_variants(reviewer, eng, content)
        for key, _name, _setting in VARIANTS:
            ranked_chunk_ids = variant_out[key]["ranked_chunk_ids"]
            refs = [{"chunk_id": cid} for cid in ranked_chunk_ids]
            ranked_ids = normalize_review_refs(refs, runtime_kb_id_map, chunk_to_group)
            row = evaluate_ranked_ids(
                case=case,
                ranked_ids=ranked_ids,
                candidate_count=len(refs),
                source_status=key,
            )
            rows_per_variant[key].append(row)
            times_per_variant[key].append(variant_out[key]["elapsed_sec"])
        if n % 10 == 0:
            print(f"  已处理 {n}/{len(cases)} 个案例")

    variants_payload: dict[str, Any] = {}
    evaluated = case_total = 0
    for key, _name, _setting in VARIANTS:
        result = aggregate_case_rows(rows_per_variant[key])
        result["avg_retrieval_ms"] = mean(times_per_variant[key]) * 1000 if times_per_variant[key] else 0.0
        result["cases"] = result["cases"]  # keep detail
        variants_payload[key] = result
        evaluated = result["aggregate"]["evaluated_case_count"]
        case_total = result["aggregate"]["case_count"]

    payload = {
        "schema": "coal_retrieval_ablation_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "gold": str(args.gold.resolve()),
        "mine_type": args.mine_type,
        "topk": MAX_K,
        "case_count": case_total,
        "evaluated_case_count": evaluated,
        "variants": variants_payload,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_markdown(payload), encoding="utf-8")

    print("\n=== 检索消融总览（评价案例 {} 个）===".format(evaluated))
    for key, name, _setting in VARIANTS:
        agg = variants_payload[key]["aggregate"]
        print(f"  {name:14s} Hit@5={fmt(agg['hit_at']['5'])} Recall@5={fmt(agg['recall_at']['5'])} "
              f"MRR={fmt(agg['mrr'])} NDCG@10={fmt(agg['ndcg_at']['10'])} "
              f"avg={variants_payload[key]['avg_retrieval_ms']:.1f}ms")
    print(args.output.resolve())
    print(args.report.resolve())


if __name__ == "__main__":
    main()
