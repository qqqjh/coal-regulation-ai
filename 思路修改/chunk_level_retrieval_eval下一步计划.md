# Chunk-Level Retrieval Eval 下一步计划

## 1. 当前结论

本项目不应把 RAG 评测主线设计成普通的“问题 -> 检索 -> 回答”形式。真实流程是：

```text
待审文档切块 -> 用待审 chunk 检索法规 chunk -> reviewer 判断合规性
```

因此第一阶段应固定为 **chunk-level retrieval eval**：

- 输入单位：待审文档 chunk。
- 检索目标：应命中的法规 chunk。
- 评测目标：待审 chunk 是否能召回正确法规依据，以及召回证据是否能支撑后续审查。

普通 QA-style RAGAS 可以作为补充展示，但不应作为主评测，因为它不符合当前系统的真实输入形态。

## 2. 是否要固定待审 chunk 切分方法

需要固定。

如果不固定待审 chunk 方法，后续 Recall@k、MRR、Context Precision 等指标无法解释。因为指标变化可能来自：

- 待审 chunk 切分变了；
- 法规库 chunk 切分变了；
- embedding 模型变了；
- top_k 变了；
- rerank 或 parent-child retrieval 变了；
- reviewer prompt 变了。

第一版 baseline 应先固定这些变量：

| 变量 | 第一版建议 |
|---|---|
| 待审文档切分方法 | 固定当前最可靠版本，例如 `pending_doc_chunking_v7.py` 及其对应输出 JSON；如果生产仍使用其他版本，先确认实际版本 |
| 法规库切分方法 | 固定为当前法规库 chunk 版本，例如 `chapter_based_chunking_v4.py` 及其输出 |
| 检索输入 | 固定为 `section_path + chunk_role_note + chunk content`，或先固定为 `section_path + chunk content` |
| 候选数量 | 固定 top_k，例如 10 或 15 |
| 检索器 | 第一版可先用当前脚本里的 BM25，后续再接 embedding / hybrid |
| 标注方式 | 每个待审 chunk 标注哪些法规 chunk 是 useful / useless / uncertain |

后续如果要比较不同待审 chunker，应该单独做“切块方法消融实验”，而不是混入主检索评测。

## 3. 待审 chunk 的上下文问题

用户给出的例子：

```text
三、防火红线管理
1、采煤机外喷雾不开或开后无水、效果不好，强行割煤。
2、未制定专项措施，采煤机滚筒截割石头。
3、未制定专项措施，采煤机滚筒割锚杆。
4、对高冒区不检查瓦斯、瓦斯浓度达到0.8%未制定专项措施进行割煤作业。
5、采煤机滚筒截割小梁、逼帮板。
```

这个例子本身不是典型的“切块切坏了”。原因：

- 标题“三、防火红线管理”仍在 chunk 中；
- 编号条目完整；
- 表达的是一组红线违规示例。

真正的问题是 reviewer 或检索输入没有理解 chunk 的语义角色。模型可能把“禁止发生的违规场景”误读成“待审制度允许这样做”，从而产生误判。

这个问题更准确地叫：

```text
结构上下文 / 语义角色 / chunk role metadata 不足
```

不建议简单通过加大 chunk 长度解决。加大 chunk 会带来新问题：

- 一个 chunk 混入多个审查点；
- 检索 query 主题变散；
- reviewer 更容易抓局部句子误判；
- 评测时很难定位失败原因。

更合适的方案是：**chunk 保持相对原子化，但强制带父级标题和语义角色说明。**

## 4. 待审 chunk 建议数据结构

后续待审 chunk 建议扩展为：

```json
{
  "chunk_id": "004::chunk_001",
  "doc_name": "待审文档名称",
  "chunk_no": 1,
  "content": "1、采煤机外喷雾不开或开后无水、效果不好，强行割煤。...",
  "section_path": [
    "三、防火红线管理"
  ],
  "chunk_role": "redline_violation_examples",
  "role_confidence": 0.9,
  "semantic_note": "本段为红线违规行为清单，条目描述的是禁止发生的违规场景，不是允许性操作要求。",
  "page_range": "12-13",
  "chunk_level": "section",
  "source_span": {
    "page_start": 12,
    "page_end": 13,
    "text_anchor": "采煤机外喷雾不开或开后无水"
  }
}
```

注意：`chunk_role` 只辅助 reviewer 正确理解上下文，不能直接决定合规结论。

## 5. 语义角色如何生成

建议采用：

```text
规则提取候选 -> 固定枚举角色分类 -> 低置信度再用 LLM 兜底
```

不要让模型自由生成角色名称。角色应固定枚举，便于评测、统计和复现。

第一版角色枚举建议：

```python
ROLE_LABELS = [
    "requirement_clause",          # 正向要求/制度要求
    "prohibition_clause",          # 禁止性条款
    "redline_violation_examples",  # 红线违规示例清单
    "penalty_clause",              # 处罚/考核
    "procedure_step",              # 操作流程
    "inspection_standard",         # 检查标准
    "definition_clause",           # 定义说明
    "reference_clause",            # 引用依据
    "unknown"
]
```

第一版可以先做规则，不必立即接 LLM：

| 触发信号 | 可能角色 |
|---|---|
| 标题含“红线”“红线管理” | `redline_violation_examples` |
| 标题或正文含“严禁”“禁止”“不得” | `prohibition_clause` |
| 标题含“处罚”“考核”“责任追究” | `penalty_clause` |
| 标题含“操作流程”“作业程序”“步骤” | `procedure_step` |
| 标题含“检查标准”“验收标准” | `inspection_standard` |
| 标题含“定义”“术语” | `definition_clause` |
| 标题含“依据”“引用” | `reference_clause` |
| 正文大量出现“必须”“应当”“应”“要求” | `requirement_clause` |

对“红线管理”类 chunk，应生成固定说明：

```text
【语义角色】红线违规行为清单
【解释】以下条目描述禁止发生的违规场景，不是允许性操作要求。
```

## 6. 检索输入应如何拼接

第一版建议不要只用 `content` 做 query，而是统一构造检索文本：

```text
【章节路径】三、防火红线管理
【语义角色】红线违规行为清单；以下条目描述禁止发生的违规场景，不是允许性操作要求。
【待审内容】
1、采煤机外喷雾不开或开后无水、效果不好，强行割煤。
2、未制定专项措施，采煤机滚筒截割石头。
...
```

这样做的目的不是让角色替代检索，而是避免检索和审查阶段丢失语义边界。

后续可以做两个检索输入版本对比：

| 版本 | query 构造 |
|---|---|
| A | `content` |
| B | `section_path + content` |
| C | `section_path + semantic_note + content` |

但第一版 baseline 应先固定其中一个，不要多变量同时变。

## 7. 标注集应该怎么设计

第一版标注集建议仍以待审 chunk 为中心，每条 case 结构如下：

```json
{
  "case_id": "004_chunk_001",
  "pending": {
    "doc_name": "待审文档",
    "chunk_no": 1,
    "section_path": ["三、防火红线管理"],
    "chunk_role": "redline_violation_examples",
    "semantic_note": "本段为红线违规行为清单，条目描述的是禁止发生的违规场景，不是允许性操作要求。",
    "content": "..."
  },
  "candidates": [
    {
      "chunk_id": "煤矿安全规程::chunk429",
      "rank": 1,
      "score": 123.4,
      "content": "...",
      "label": "unlabeled"
    }
  ],
  "gold_refs": [],
  "final_label": "unlabeled",
  "failure_type": ""
}
```

人工标注时至少需要标：

| 字段 | 说明 |
|---|---|
| candidate label | 候选法规 chunk 是否有用：`useful / useless / uncertain` |
| missing_correct_evidence | top_k 中是否缺失真正应该命中的法规 |
| not_eval_suitable | 这个待审 chunk 是否不适合用于评测 |
| final_label | 后续可扩展为最终合规结果：`compliant / non_compliant / uncertain / unlabeled` |
| failure_type | 检索失败原因：`pending_chunk_context / kb_chunking / retrieval / ocr_or_extraction / reviewer / not_failure` |

## 8. 第一阶段指标

先不要把指标做得太复杂，优先能解释问题。

建议第一阶段输出：

| 指标 | 含义 |
|---|---|
| Recall@k | 标注为 useful 的法规 chunk 是否出现在 top_k |
| MRR | 第一个 useful 法规 chunk 排名是否靠前 |
| Useful Precision@k | top_k 中 useful 比例，衡量噪声 |
| Missing Evidence Rate | 人工勾选 `missing_correct_evidence` 的比例 |
| Role Error Rate | 因 chunk 角色误解导致的失败比例 |
| Context Error Rate | 因缺少章节路径/上下文导致的失败比例 |

第二阶段再加入最终 reviewer 的准确率、误报率、漏报率和三分类结果。

## 9. 现有代码需要修改的点

### 9.1 修改待审 chunker

目标文件候选：

- `pending_doc_chunking_v7.py`
- 如果当前生产仍使用旧版本，则先确认实际使用的是哪一个输出 JSON。

建议新增：

```python
def build_section_path(chunk):
    ...

def classify_chunk_role(section_path, content, metadata):
    ...

def build_semantic_note(chunk_role):
    ...
```

输出 chunk 时新增字段：

- `section_path`
- `chunk_role`
- `role_confidence`
- `semantic_note`
- `source_span`，如果已有页码或文本锚点，尽量保留

### 9.2 修改评测样本生成脚本

目标文件：

- `rag_eval/scripts/build_annotation_app.py`

需要修改：

1. 读取 pending chunk 时保留 `section_path`、`chunk_role`、`semantic_note`。
2. 检索 query 不再只用 `content`，而是通过函数统一构造。
3. annotation case JSON 中加入 role/context 字段。
4. HTML 标注页面展示：
   - 章节路径；
   - 语义角色；
   - 语义说明；
   - 是否怀疑“上下文不足/角色误解”。
5. 导出 annotation JSON 时加入 `failure_type`。

建议新增函数：

```python
def build_pending_query(pending_chunk):
    parts = []
    if pending_chunk.get("section_path"):
        parts.append("【章节路径】" + " > ".join(pending_chunk["section_path"]))
    if pending_chunk.get("semantic_note"):
        parts.append("【语义角色】" + pending_chunk["semantic_note"])
    parts.append("【待审内容】\n" + pending_chunk.get("content", ""))
    return "\n".join(parts)
```

### 9.3 修复 `rag_eval` 脚本中的编码问题

当前 `rag_eval/scripts/build_annotation_app.py` 和已生成的 `annotation_cases_v1.json` 中能看到中文乱码，例如：

- `鍒樿嚧杩?`
- `涓夌増鏈?`
- `宸叉爣`

这会影响标注人员理解，也会影响关键词规则。

建议单独修复：

1. 确认源文件实际编码。
2. 修复 `DEFAULT_COMPARISON_MD` 路径。
3. 重新生成 `annotation_cases_v1.json`。
4. 重新生成 `annotation_app.html`。

如果乱码来自已经损坏的中间 JSON，则需要回到原始 MinerU 输出或原始 markdown 重新生成。

### 9.4 修改标注页面

目标文件由脚本生成：

- `rag_eval/reports/annotation_app.html`

不要直接手改 HTML，应该改生成脚本。

页面应新增：

- `chunk_role` 展示；
- `semantic_note` 展示；
- failure type 下拉框；
- “角色误解/上下文不足”勾选项；
- 查询文本预览，方便确认检索 query 到底用了哪些内容。

## 10. 建议执行顺序

### Step 1：确认固定基线

确认第一版固定：

- 待审 chunk JSON 路径；
- 法规 chunk JSON 路径；
- top_k；
- 检索器；
- 是否拼接 `section_path` 和 `semantic_note`。

产出：一段写入 README 或配置文件的 baseline 配置。

### Step 2：给待审 chunk 增加上下文字段

先做规则版，不接 LLM。

产出：

- 新版待审 chunk JSON；
- 每个 chunk 带 `section_path`、`chunk_role`、`semantic_note`。

### Step 3：改 annotation app 生成逻辑

让 `build_annotation_app.py` 使用新的 query 构造方式，并展示语义角色。

产出：

- 新的 `rag_eval/data/annotation_cases_v*.json`；
- 新的 `rag_eval/reports/annotation_app.html`。

### Step 4：人工标注 50-100 个 case

优先选：

- 之前误判过的 chunk；
- 含表格、数值、单位的 chunk；
- 红线/禁止/处罚类 chunk；
- 长 chunk；
- 检索 top_k 明显噪声多的 chunk。

产出：

- 标注导出的 JSON；
- 每个 case 的 candidate label 和失败类型。

### Step 5：计算第一版指标

新增一个指标脚本，例如：

- `rag_eval/scripts/evaluate_retrieval_annotations.py`

输出：

- Recall@5 / Recall@10 / Recall@15；
- MRR；
- Useful Precision@k；
- Missing Evidence Rate；
- 按 `chunk_role` 分组的失败率；
- 按 `failure_type` 分组的失败率。

### Step 6：再决定是否单独评测待审 chunker

如果失败样本中 `pending_chunk_context`、`role_error`、`ocr_or_extraction` 占比高，再单独比较不同待审 chunk 方法。

如果主要失败来自法规库切块或检索器，则暂时不要改待审 chunker，先优化法规库或检索策略。

## 11. 第一版不建议做的事

暂时不要：

- 一开始就全面上 RAGAS QA 评测；
- 同时改 chunker、embedding、top_k、reviewer prompt；
- 用 LLM 自由生成任意语义角色；
- 只靠关键词直接决定合规结论；
- 用扩大 chunk 尺寸来掩盖上下文缺失；
- 在没有人工金标的情况下宣称召回率提升。

## 12. 最小可行改造方案

如果只做最小版本，建议只做三件事：

1. 固定待审 chunk JSON 和法规 chunk JSON。
2. 在待审 chunk 上补充 `section_path`、`chunk_role`、`semantic_note`。
3. 修改 `build_annotation_app.py`，让标注 app 展示这些字段，并用统一 query 构造候选法规。

这样就能先把 chunk-level retrieval eval 跑起来，同时把“切块问题”和“角色理解问题”拆开看。

