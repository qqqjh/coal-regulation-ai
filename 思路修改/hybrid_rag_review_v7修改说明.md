# hybrid_rag_review_v7 修改说明

## 修改目标

基于 `hybrid_rag_review_v5.py` 的审核思路，并复用 `hybrid_rag_review_v6.py` 已实现的错别字检查和重复性检查能力，生成 `hybrid_rag_review_v7.py`。

v7 的核心目标是调整 agent 执行顺序和并行方式：

1. 合规性审查 agent -> 结果核验 agent -> 检索相关性 agent。
2. 检索相关性 agent 只对最终审查为错误/不合规的待审 chunk 执行。
3. 上述合规链与错别字 agent、重复性 agent 三路并行执行。
4. 提示词增加“非突出矿井”适用性约束，避免把突出矿井专用条款误用于当前待审文档。

## v7 流程

每个待审 chunk 的处理流程如下：

```text
                 ┌─ 合规性审查 agent ─ 结果核验 agent ─ 若最终不合规，执行检索相关性 agent
待审 chunk + KB ─┼─ 错别字 agent
                 └─ 重复性 agent
```

说明：

- 合规性审查仍然需要先检索 KB 条款作为审核依据。
- 这里的“检索相关性 agent”对应代码中的 `classify_kb_chunks()`，它只负责给检索到的 KB 片段打“支持/反对/例外/无关”标签，用于前端展示。
- `classify_kb_chunks()` 不再在合规审查前执行，也不参与合规判断。
- 如果最终结果不是不合规，`kb_refs` 和 `kb_classifications` 不再写入该 chunk 的结果，减少前端无效展示数据。

## 主要代码改动

### 1. 新增 v7 文件

新增：

- `hybrid_rag_review_v7.py`

该文件保留 v5 的 RAG 合规审查主线，并复用 v6 中的：

- 文档内 BM25 自检索；
- 错别字检查 agent；
- 重复性检查 agent；
- 三任务并行调度结构；
- 增量保存机制；
- HTML 报告中的错别字/重复性展示。

### 2. 调整合规链顺序

`review_chunk_complete()` 改为：

```text
合规性审查 -> 结果核验 -> 检索相关性/立场分类
```

旧流程中 `classify_kb_chunks()` 会在初审前执行。v7 中它被移动到结果核验之后，并且只在 `review_result` 最终为不合规时调用。

### 3. 三路并行

`review_chunk_all_tasks()` 保持三路并行，但第一路不再是“分类+初审+核验”，而是完整合规链：

```text
合规链：合规性审查 -> 结果核验 -> 条件触发检索相关性
错别字检查
重复性检查
```

这三路通过 `ThreadPoolExecutor(max_workers=3)` 并行执行。

### 4. 前端/报告只展示问题 chunk

HTML 报告中只渲染以下任一条件命中的 chunk：

- 合规结果为不合规；
- 错别字检查发现问题；
- 重复性检查发现问题。

JSON 结果仍保留全量 chunk，便于追溯和调试。

### 5. 非突出矿井提示词约束

新增统一提示片段 `MINE_APPLICABILITY_NOTE`：

```text
本批待审对象按非突出矿井处理。
若参考法规片段明确限定为“突出矿井”“煤与瓦斯突出矿井”“突出煤层”“突出危险区域”等突出矿井专用场景：
- 不得直接作为非突出矿井待审内容的违规依据；
- 只有待审内容本身明确属于突出矿井/突出煤层/突出危险场景时，才可适用该条款；
- 若条款既包含突出矿井专用要求又包含通用要求，只能依据其中明确适用于所有矿井或一般场景的部分判断。
```

该约束已加入：

- `classify_kb_chunks()`；
- `review_chunk_with_llm()`；
- `verify_review_result()`。

## 输出文件

v7 运行后输出：

- `review_results/review_result_v7_*.json`
- `review_results/review_report_v7_*.html`
- `review_results/review_result_v7_incremental.json`

## 行为边界

- v7 不修改 `hybrid_rag_review_v5.py` 和 `hybrid_rag_review_v6.py`。
- 检索 KB 本身仍然发生在合规审查之前，因为合规审查需要法规依据。
- 检索相关性/立场分类 agent 不再为合规 chunk 运行，也不再为合规 chunk 生成前端展示数据。
