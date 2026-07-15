# RAG 评估与检索技术选型实施计划

## 1. 最终目标

需要解决两个不同问题：

1. 选择更合适的 RAG 技术组合：
   - Embedding：`BAAI/bge-m3`、`Qwen3-Embedding` 等。
   - 向量库：Milvus、Qdrant。
   - 召回：Dense、BM25、Dense + BM25 混合检索。
   - 重排：无重排、`bge-reranker`、`Qwen3-Reranker`。
2. 评估完整规程审查系统：
   - 待审 chunk 切分是否合适。
   - 正确法规是否被召回。
   - reviewer 是否基于法规作出正确结论。
   - 提示词修改是否真正提升效果。

这两个问题不能一次性混合测试。应先建立稳定黄金集，再逐层做消融实验。

## 2. 推荐的整体评估分层

```text
原始待审文档定位锚点
        ↓
当前待审 chunk 映射
        ↓
法规检索评估
        ↓
混合检索与重排评估
        ↓
reviewer 合规判断评估
        ↓
RAGAS / 自定义 LLM 评估
```

### 第一层：检索评估

只回答：

> 给定一个待审 chunk，正确法规 chunk 是否被召回并排在前面？

这一层不运行 reviewer，不评估提示词。

### 第二层：审查结果评估

固定检索结果后，回答：

> reviewer 是否正确判断合规性、识别问题并引用正确法规？

这一层才比较 reviewer prompt。

### 第三层：RAGAS 与领域评价

RAGAS 作为补充评估框架，用于评估：

- 检索上下文是否相关。
- 回答是否忠实于检索法规。
- 审查结论是否满足煤矿规程审查要求。

RAGAS 不替代 Recall、MRR、nDCG、准确率、F1 等传统指标。

## 3. 先建立与 chunk 版本解耦的黄金集

当前已有：

- `rag_eval/data/annotation_cases_v1.json`
- 50 个样本：
  - 三版本不合规对比来源：30 个。
  - 分层随机样本：20 个。
- 每个样本已有 15 个 BM25 候选，但尚未人工标注。
- 这些样本基于旧 `v5` chunk 编号，不能直接作为当前 `v9` 的稳定标识。

### 3.1 黄金样本不能只保存 chunk 编号

`chunk75` 只在某个切分版本中有效。后续切分变化后，编号会失效。

黄金样本应保存原始文档定位锚点：

```json
{
  "case_id": "case_004_001",
  "doc_id": "004",
  "source_page_range": "105-135",
  "anchor_quote": "将永久支护锚索作为起吊点",
  "issue_description": "永久支护锚索被用作起吊点",
  "expected_label": "non_compliant",
  "source_type": "three_version_comparison",
  "gold_regulation_chunk_ids": [],
  "gold_reason": "",
  "chunk_mappings": {
    "v5": [],
    "v9": []
  }
}
```

稳定身份应是：

```text
原始文档 + 页码范围 + 原文锚点 + 问题描述
```

而不是某一版的 chunk 编号。

### 3.2 将旧 v5 样本映射到当前 v9

自动映射流程：

1. 读取旧样本的：
   - 文档编号。
   - 页码范围。
   - 原 chunk 内容。
   - 问题描述。
2. 在相同文档的 `v9` chunks 中，先按页码范围筛选候选。
3. 计算旧内容与 v9 chunk 的文本重合度：
   - 锚点短语匹配。
   - 字符 n-gram 相似度。
   - 内容包含关系。
4. 为每个旧样本输出最相似的 3 个 v9 chunk。
5. 人工确认最终映射。

映射必须允许：

- 一个旧 chunk 对应多个 v9 chunk。
- 多个旧问题对应同一个 v9 chunk。

如果多个问题最终对应同一个 v9 chunk，检索评估时应合并为一个 query case，并合并其正确法规依据，避免同一查询被重复计权。

## 4. 50 个样本应该如何组成

50 个样本适合作为第一版 pilot benchmark，但不足以作为最终结论。

第一版建议组成：

| 类型 | 数量 | 说明 |
|---|---:|---|
| 已人工确认的真实不合规 | 30 | 从三版本对比中迁移，但需排除错误历史标注 |
| 合规且存在明确支持法规 | 15 | 可用于评估支持性法规召回 |
| 无明确适用法规或不适合判断 | 5 | 用于评估系统是否会强行召回、产生误判 |

选择合规样本时不要完全随机，应按以下维度分层：

- 三个待审文档均有覆盖。
- 不同主题：瓦斯、顶板、机电、运输、防火等。
- 不同 chunk 长度。
- 不同 `chunk_level`。
- 正向要求、禁止条款、红线案例、事故案例等不同语义角色。

需要单独标记 OCR 错误。OCR 错误与法规违规不是同一种任务，不能混入普通检索失败统计。

## 5. 黄金法规依据如何标注

你提出的候选池：

```text
Dense Top5 + BM25 Top5 + Hybrid Top5
```

可以作为人工标注起点，但不能直接视为完整 gold。

原因：如果三个检索器都漏掉正确法规，人工只能看到错误候选，最终会错误地认为不存在正确法规。

### 推荐候选池

第一轮候选生成建议：

```text
BM25 Top10
+ BGE-M3 Dense Top10
+ Qwen3-Embedding Dense Top10
+ Hybrid Top10
+ Reranker Top10
→ 按法规 chunk ID 去重
```

标注界面还必须支持：

- 在完整法规库中关键词搜索。
- 按法规名称、章节、条款过滤。
- 手工添加候选之外的正确法规 chunk。
- 标记“正确法规存在，但当前法规切分不合适”。

每个法规候选建议标注：

```text
2 = directly_relevant：可直接支撑审查结论
1 = partially_relevant：有关联，但不足以单独支撑结论
0 = irrelevant：无关
? = uncertain：需要进一步确认
```

每个待审样本还需要标注：

- `expected_label`：`compliant / non_compliant / uncertain / not_suitable`
- `gold_regulation_chunk_ids`
- `gold_reason`
- `missing_gold_evidence`
- `failure_type`

## 6. 实验顺序：一次只改变一个变量

不要直接测试所有组合的笛卡尔积。应按以下顺序逐层筛选。

### 阶段 A：固定当前基线

固定：

- 待审切分：`pending_doc_chunking_v9.py`
- 法规切分：当前 `chunks_v4`
- Query 构造：先使用纯 `content`
- Top K：统一测试 `5 / 10 / 20`
- BM25 参数：固定
- 融合策略：当前 RRF，`k=60`

记录当前基线：

```text
text-embedding-v3 + Chroma + BM25 + RRF + 无独立 reranker
```

### 阶段 B：比较 Embedding 模型

只替换 Embedding：

```text
text-embedding-v3
BAAI/bge-m3
Qwen3-Embedding
```

这一阶段建议使用 NumPy 精确余弦检索作为统一检索器，不使用 Milvus/Qdrant 的近似索引。

原因：先排除向量数据库索引误差，才能确认差异确实来自 Embedding 模型。

注意：

- 固定模型具体版本。
- 固定向量维度。
- 固定 query/document prompt 或 instruction。
- BGE-M3 第一轮只比较 dense 向量；其 sparse 能力另做实验，否则变量不唯一。

### 阶段 C：比较向量库

选定一个 Embedding 后，再比较：

```text
Chroma
Milvus
Qdrant
```

三者必须写入完全相同的预生成向量。

向量库比较重点不是语义质量，而是：

- 相对于 NumPy 精确检索的 ANN Recall@K。
- 查询延迟 P50 / P95。
- 索引构建耗时。
- 磁盘占用。
- 部署和维护复杂度。
- 过滤、混合检索、扩展能力。

如果三个库都接近精确检索结果，最终选择应以工程需求为主，不应只依据小规模 50 样本上的微小排名差异。

### 阶段 D：比较混合检索

固定 Embedding 和向量库，比较：

```text
Dense only
BM25 only
Dense + BM25 + RRF
Dense + BM25 + Weighted Fusion
```

候选召回建议先取：

```text
Dense Top20 + BM25 Top20 → 融合后 Top20
```

不要在召回阶段过早裁剪到 Top5，否则 reranker 无法挽回漏召回。

### 阶段 E：比较 Reranker

固定前面选出的召回组合：

```text
Hybrid Top20 或 Top50
→ 无 reranker
→ bge-reranker
→ Qwen3-Reranker
→ 最终 Top5 / Top10
```

Reranker 主要优化前排精度，不能解决候选池没有正确法规的问题。

因此必须同时观察：

- rerank 前 Recall@20。
- rerank 后 Recall@5。
- MRR、nDCG@5。
- 延迟与成本。

### 阶段 F：比较待审 chunk 切分

检索方案稳定后，才比较不同待审切分方法。

因为黄金样本已经使用原文锚点定位，可以将同一批 case 映射到不同 chunker 输出：

```text
同一原文问题
→ v7 对应 chunk
→ v8 对应 chunk
→ v9 对应 chunk
→ 使用同一检索方案评估
```

同时记录：

- case 是否被切散。
- 一个 chunk 是否包含多个审查问题。
- 标题/父级上下文是否保留。
- chunk 是否过长导致 query 主题发散。

### 阶段 G：比较 reviewer prompt

只有在检索方案与 chunk 方法固定后，才比较提示词。

使用同一组检索法规上下文，比较：

- 当前 prompt。
- 加入章节路径。
- 加入语义角色说明。
- 修改阈值判断规则。
- 不同 reviewer 模型。

否则无法判断提升来自检索还是提示词。

## 7. 指标体系

### 7.1 检索指标

必须输出：

| 指标 | 用途 |
|---|---|
| Hit@K | Top K 中是否至少出现一个正确法规 |
| Recall@K | 所有 gold 法规中被召回的比例 |
| Precision@K | Top K 中相关法规的比例 |
| MRR | 第一个正确法规出现的位置 |
| nDCG@K | 支持分级相关性，并考虑排名 |
| MAP | 多个正确法规场景下的整体排序质量 |
| Missing Evidence Rate | 正确法规完全未进入候选池的比例 |

对于 `no applicable rule` 样本，单独统计：

- 无关法规误召回率。
- 系统是否能够拒绝强行匹配。

### 7.2 向量库工程指标

- 索引构建时间。
- 查询延迟 P50 / P95。
- 每秒查询量。
- ANN Recall@K，相对于 NumPy 精确检索。
- 内存、磁盘占用。
- 部署和维护复杂度。

### 7.3 reviewer 指标

- 合规三分类准确率。
- Macro-F1。
- 不合规 Precision / Recall / F1。
- 混淆矩阵。
- 问题级检出 Precision / Recall。
- 法规引用正确率。
- 无依据误判率。

### 7.4 RAGAS 与自定义指标

将待审 chunk 作为 `user_input`，召回法规作为 `retrieved_contexts`。

适合使用：

- Context Precision。
- Context Recall。
- Faithfulness。
- 自定义领域 Rubric：
  - 是否正确理解红线案例、事故案例等语义角色。
  - 是否正确处理阈值方向。
  - 是否引用了适用法规。
  - 是否在证据不足时输出不确定。

RAGAS 的 LLM 评分需要先抽样与人工评分对齐，不能直接视为真实准确率。

## 8. 开发集与保留测试集

50 个样本建议拆分为：

```text
开发集：35
保留测试集：15
```

- 开发集用于选择模型、融合权重、Top K 和 prompt。
- 保留测试集只在方案确定后运行，避免持续调参造成过拟合。
- 所有方案使用同一批 case，做逐 case 配对比较。
- 由于样本较少，报告中应给出 bootstrap 置信区间。

完成第一版流程后，建议将黄金集扩展到至少 150 至 300 个 case。

## 9. 推荐的数据目录

```text
rag_eval/
├─ data/
│  ├─ source_cases_v2.json
│  ├─ pending_chunk_mappings_v9.json
│  ├─ gold_cases_v2.json
│  └─ splits_v2.json
├─ scripts/
│  ├─ migrate_cases_to_v9.py
│  ├─ build_candidate_pool.py
│  ├─ build_annotation_app_v2.py
│  ├─ evaluate_retrieval.py
│  ├─ compare_embeddings.py
│  ├─ compare_vector_stores.py
│  ├─ compare_rerankers.py
│  └─ evaluate_reviewer.py
└─ reports/
   ├─ case_mapping_report.html
   ├─ retrieval_baseline.json
   ├─ retrieval_comparison.html
   └─ reviewer_comparison.html
```

## 10. 接下来按顺序做什么

### 第一步：清理和迁移现有 50 个样本

1. 保留现有 `annotation_cases_v1.json`，不要覆盖。
2. 从三版本对比和旧 v5 chunk 中提取原文锚点。
3. 自动映射到当前 `pending_doc_chunks_v9_20260528_204330.json`。
4. 生成人工映射确认 HTML。
5. 合并映射到同一个 v9 chunk 的重复 query。
6. 重新确认最终样本是否仍为约 50 个。

### 第二步：补齐黄金法规依据

1. 使用多路宽召回生成候选池，而不是只用 BM25。
2. 升级标注页面，使其支持搜索完整法规库并手工添加 gold。
3. 人工标注法规相关性、最终合规标签和判断原因。
4. 锁定 `gold_cases_v2.json`。

### 第三步：运行当前系统 baseline

评估：

```text
text-embedding-v3 + Chroma + BM25 + RRF
```

输出传统检索指标与当前 reviewer 指标。

### 第四步：按消融顺序做技术选型

```text
Embedding
→ 向量库
→ 混合检索策略
→ Reranker
→ 待审切分
→ Reviewer Prompt
```

每个阶段只保留表现更好的少数方案进入下一阶段。

## 11. 当前最应该避免的做法

- 不要先搭建多个向量库再开始标注。
- 不要把旧 chunk 编号直接当作黄金样本 ID。
- 不要只标注当前检索器返回的候选。
- 不要一次比较多个同时变化的技术组合。
- 不要只看 RAGAS 分数而忽略 Recall、MRR、F1 和人工错误分析。
- 不要用同一批 50 个样本不断调参后，再把它们当最终测试结果。

## 12. 当前建议决策

第一阶段暂不决定 Milvus 还是 Qdrant，也不决定最终 Embedding 和 Reranker。

当前唯一需要固定的是：

```text
当前待审基线切分：v9
当前法规切分：chunks_v4
第一版评测规模：现有 50 个 case
黄金样本身份：原始文档定位锚点，而非 chunk 编号
```

完成黄金集后，所有技术选择才有可靠依据。
