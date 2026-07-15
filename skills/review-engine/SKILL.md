---
name: review-engine
description: v9 审查引擎：合规性/错别字/重复性三链并行审查待审文档，产出 JSON 结果与 HTML 报告
script: hybrid_rag_review_v9.py
---

# 审查引擎 v9

## 何时使用
待审文档已切分（存在 pending_doc_chunks_v9_*.json）后，执行完整审查。

## 流水线（引擎内部自动完成，无需干预）
1. Phase A：BGE-M3 本地混合检索（dense+sparse RRF）+ bge-reranker-v2-m3 重排，
   整篇文档批量预计算
2. Phase B 三并行：合规链（初审→数值核验工具→二次核验→立场分类）‖ 错别字链 ‖
   文档级重复性链，chunk 级并发
3. 分歧/数值矛盾/低置信/异常自动写入升级队列（data/review_queue_v9.db）

## 用法
```
python hybrid_rag_review_v9.py [--doc-filter 004,006] [--max-docs 1]
                               [--mine-type non_outburst|outburst]
                               [--concurrency 4] [--no-report]
```
- `--doc-filter`：按文档名子串过滤待审文档（逗号分隔）
- `--max-docs`：最多审核 N 篇，0=全部（默认 1，防止误跑全量）
- `--mine-type`：矿井类型，决定突出矿井专用条款是否参与检索（默认 non_outburst）

## 输出
- `review_results/review_result_v9_incremental*.json` — 增量结果（断点续跑用）
- `review_results/review_result_v9_<时间戳>.json` — 最终结果
- `review_results/review_report_v9_<时间戳>.html` — HTML 报告
- 升级队列待处理项 → 运行 `python main_agent_v9.py --process-queue` 裁决

## 环境要求
- Python 环境：D:\Anaconda\envs\langchain0.3（含 FlagEmbedding、torch+CUDA）
- 环境变量 DASHSCOPE_API_KEY（LLM 审查用；检索不需要 API）
- 本地模型：models/bge-m3、models/bge-reranker-v2-m3
