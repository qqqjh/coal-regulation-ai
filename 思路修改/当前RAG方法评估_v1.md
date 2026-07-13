# 当前 RAG 方法评估

生成时间：`2026-06-16T11:27:17`

## 方法口径

- 当前离线黄金集：`atomic_codex_v2_focused_top3_gold_annotations.json`。
- 评价单位：待审父 chunk。
- 证据归一化：优先使用 `evidence_group_id`，没有证据组时使用 `chunk_id`。
- 只在至少有一条 `useful` 黄金证据的案例上计算 MRR/Hit/Recall/NDCG。
- `v9_actual_agent_refs` 才是根据 v9 主智能体/多智能体审查结果统计的现有方法指标。
- `gold_pool_candidate_ordering` 仅保留为对照：它评估黄金候选池内部排序，不代表 v9 实际输出。

## v9 实际智能体引用指标

- 总案例：50
- 可评价案例：44
- 排除无 useful 黄金证据案例：6
- useful 黄金证据总数：167
- 有实际引用的案例：50
- 平均实际引用数：14.64
- 来源状态：`{'full_retrieved_kb_refs': 50}`

| 指标 | 数值 |
|---|---:|
| MRR | 0.4199 |
| Hit@1 | 0.2955 |
| Hit@3 | 0.4545 |
| Hit@5 | 0.6136 |
| Hit@10 | 0.7500 |
| Hit@15 | 0.7955 |
| Recall@1 | 0.0734 |
| Recall@3 | 0.1718 |
| Recall@5 | 0.2737 |
| Recall@10 | 0.4114 |
| Recall@15 | 0.4966 |
| Precision@1 | 0.2955 |
| Precision@3 | 0.2348 |
| Precision@5 | 0.2136 |
| Precision@10 | 0.1614 |
| Precision@15 | 0.1303 |
| NDCG@1 | 0.2955 |
| NDCG@3 | 0.2547 |
| NDCG@5 | 0.2764 |
| NDCG@10 | 0.3213 |
| NDCG@15 | 0.3563 |

说明：当前历史 v9 结果没有保存完整检索 TopK，仅保存了最终报告展示/采用的 `kb_refs`。
因此这张表反映“智能体实际引用证据”的质量；后续重新运行 v9 后，会使用新增的 `retrieved_kb_refs` 评估完整检索结果。

## 黄金候选池排序指标（对照）

- 总案例：50
- 可评价案例：44
- 排除无 useful 黄金证据案例：6
- useful 黄金证据总数：167
- 有候选证据的案例：48
- 平均候选数：10.96
- `missing_correct_evidence` 案例：35

| 指标 | 数值 |
|---|---:|
| MRR | 0.5903 |
| Hit@1 | 0.4091 |
| Hit@3 | 0.7727 |
| Hit@5 | 0.8864 |
| Hit@10 | 0.9773 |
| Hit@15 | 0.9773 |
| Recall@1 | 0.1236 |
| Recall@3 | 0.3930 |
| Recall@5 | 0.6175 |
| Recall@10 | 0.8058 |
| Recall@15 | 0.9596 |
| Precision@1 | 0.4091 |
| Precision@3 | 0.4015 |
| Precision@5 | 0.4091 |
| Precision@10 | 0.2886 |
| Precision@15 | 0.2455 |
| NDCG@1 | 0.4091 |
| NDCG@3 | 0.4441 |
| NDCG@5 | 0.5336 |
| NDCG@10 | 0.6039 |
| NDCG@15 | 0.6730 |

## RAGAS 指标

RAGAS 需要同一批样本的 `user_input`、`retrieved_contexts`、`response`、`reference`。
当前数据骨架中，`retrieved_contexts` 来自 v9 实际结果的法规引用，`reference_contexts` 来自人工黄金 useful 证据；目前还缺少由现有审查链生成并落盘的 `response`。
参考：RAGAS 官方文档的 RAG 示例也是先收集 `response` 和 `retrieved_contexts`，再构造 `EvaluationDataset` 并调用 `evaluate(...)`。

| RAGAS指标 | 本项目解释 | 当前状态 |
|---|---|---|
| Faithfulness | 回答是否被检索法规支撑 | 待跑审查链生成 response 后计算 |
| Answer / Response Relevancy | 回答是否针对待审chunk | 待生成 response 后计算 |
| Context Precision | 排名前面的法规是否更相关 | 已有检索近似指标，RAGAS版待接LLM judge |
| Context Recall | 黄金证据是否被上下文覆盖 | 已有 Recall@k，RAGAS版待接 reference_contexts |
| Factual Correctness | 合规结论和事实是否与参考一致 | 待生成 response 后计算 |

已生成 RAGAS 数据骨架：`D:\work\project\coal-regulation-ai\rag_eval\data\current_rag_method_ragas_dataset_skeleton_v1.jsonl`。
RAGAS 运行脚本：`rag_eval/scripts/run_ragas_current_method_v1.py`。

## 时间指标

| 时间指标 | 均值(s) | 中位数(s) | P95(s) | 最大(s) |
|---|---:|---:|---:|---:|
| phase_a_avg_per_chunk | 1.548 | 1.536 | 1.623 | 1.623 |
| chunk_total | 5.788 | 3.116 | 20.320 | 66.878 |
| generation_like_llm | 6.506 | 4.041 | 19.174 | 49.580 |
| compliance_review | 3.467 | 2.834 | 5.534 | 28.363 |
| numeric_total | 0.756 | 0.000 | 6.220 | 23.793 |
| verification | 0.434 | 0.000 | 4.294 | 21.699 |
| kb_classification | 0.084 | 0.000 | 0.000 | 4.971 |
| typo | 2.521 | 0.931 | 11.395 | 34.852 |
| repetition_doc_total | 78.383 | 78.250 | 90.190 | 90.190 |

## 结论

- 检索指标可以先用于比较候选排序质量，但当前黄金证据来自候选池内部标注，不能等价为全知识库召回上限。
- v9 实际引用指标偏低，主要因为旧结果只落盘了报告展示/采用的 `kb_refs`，合规块的大量检索证据没有保存；需要重新跑 v9 并使用 `retrieved_kb_refs` 才能得到完整检索 TopK 指标。
- 35 个 `missing_correct_evidence` 案例说明 Top3 原子主张候选仍有正确证据缺失风险，后续应重点看这些样本。
- RAGAS 应放在完整审查输出之后，用来评估最终回答质量；它不替代 Hit/Recall/MRR/NDCG。

## 参考

- RAGAS GitHub: https://github.com/vibrantlabsai/ragas
- RAGAS simple RAG evaluation docs: https://docs.ragas.io/en/stable/getstarted/rag_eval/
