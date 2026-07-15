# 待审 Chunk 字符阈值固定说明

v5 = 基础待审切块 + 数值/OCR 清洗 + 标题碎片合并 + 大 chunk 二次切分
v6 = 想在 v5 上加表格解析，但当前代码没有真正接入主流程，是中间稿
v7 = 在 v6 思路上真正接入表格，并新增结构优先二次切分


## 1. 当前问题

做 chunk-level retrieval eval 时，待审文档的 chunk 切分方法需要先固定。否则后续检索指标变化无法解释：

- 可能是检索器变好了；
- 可能是法规库 chunk 变了；
- 也可能只是待审 chunk 的长度和边界变了。

因此第一版评测不应频繁改待审 chunk 阈值，而应先固定一套 baseline。

## 2. 当前代码中的阈值

当前 `pending_doc_chunking_v7.py` 中的核心阈值如下：

```python
LARGE_CHUNK_THRESHOLD = 3000
SECOND_LEVEL_SPLIT_THRESHOLD = 2200
MIN_CHUNK_THRESHOLD = 50
FIRST_LEVEL_MIN_BODY_CHARS = 80
SECOND_LEVEL_MIN_BODY_CHARS = 60
```

含义如下：

| 阈值                                    | 含义                                                  |
| --------------------------------------- | ----------------------------------------------------- |
| `MIN_CHUNK_THRESHOLD = 50`            | 小于 50 字符的 chunk 认为可能是纯标题或碎片，优先合并 |
| `FIRST_LEVEL_MIN_BODY_CHARS = 80`     | 一级结构条目至少约 80 字符，才更适合作为有效子 chunk  |
| `SECOND_LEVEL_MIN_BODY_CHARS = 60`    | 二级编号条目至少约 60 字符，才更适合作为有效子 chunk  |
| `SECOND_LEVEL_SPLIT_THRESHOLD = 2200` | 超过 2200 字符时，优先尝试按二级结构继续切分          |
| `LARGE_CHUNK_THRESHOLD = 3000`        | 超过 3000 字符时，认为是大 chunk，需要触发拆分逻辑    |

注意：这不是要求所有 chunk 都必须在 50-3000 字符之间，而是：

```text
< 50     尽量合并
> 2200   尝试结构化二次切分
> 3000   视为大 chunk，重点拆分或复查
```

## 3. 最新 v7 输出分布

基于最新输出：

```text
chunks_visualization/pending_doc_chunks_v7_20260525_200108.json
```

统计结果：

| 指标           | 数值 |
| -------------- | ---: |
| chunk 数量     |  955 |
| 最小字符数     |   43 |
| 最大字符数     | 4106 |
| 平均字符数     |  517 |
| 中位数         |  356 |
| P80            |  740 |
| P90            | 1200 |
| P95            | 1717 |
| 小于 50 字符   |    3 |
| 小于 100 字符  |   78 |
| 大于 2200 字符 |   11 |
| 大于 3000 字符 |    5 |

这个分布说明：

- 当前大部分 chunk 并不长；
- 95% 的 chunk 在 1717 字符以内；
- 真正超过 3000 字符的只有少量样本；
- 短 chunk 数量不算少，但小于 50 字符的碎片很少。

因此当前阈值可以作为第一版评测 baseline。

## 4. 第一版建议固定的字符区间解释

建议在评测中按以下区间分析，而不是强制每个 chunk 都落入同一区间：

| 区间          | 解释                | 处理建议                       |
| ------------- | ------------------- | ------------------------------ |
| `0-50`      | 疑似碎片或标题-only | 应优先合并或人工复查           |
| `50-100`    | 短 chunk            | 不一定错误，但要看是否缺上下文 |
| `100-1500`  | 正常 chunk          | 第一版最理想的主力区间         |
| `1500-2200` | 偏长但可接受        | 观察检索是否变散               |
| `2200-3000` | 长 chunk            | 需要重点看 Recall 和候选噪声   |
| `>3000`     | 异常长 chunk        | 优先复查为什么没有被结构切开   |

推荐第一版 baseline 描述为：

```text
待审 chunk 采用 pending_doc_chunking_v7.py。
小于 50 字符的 chunk 视为过小并尽量合并。
超过 2200 字符的 chunk 尝试按二级结构切分。
超过 3000 字符的 chunk 视为大 chunk 并进入拆分/复查范围。
评测时按长度区间统计检索效果，而不单独调整阈值。
```

## 5. 为什么不建议直接缩小或放大阈值

### 5.1 不建议简单放大 chunk

放大 chunk 可以减少上下文缺失，但会带来：

- 一个 chunk 混入多个审查点；
- 检索 query 主题变散；
- top_k 召回噪声增加；
- reviewer 容易抓住局部句子误判；
- 失败原因更难定位。

像“防火红线管理”这类例子，核心问题不是 chunk 太短，而是模型没有理解该 chunk 是“违规示例清单”。这应通过 `section_path`、`chunk_role`、`semantic_note` 解决，而不是单纯加大 chunk。

### 5.2 不建议简单缩小 chunk

缩小 chunk 可以让检索主题更聚焦，但会带来：

- 标题和正文分离；
- 数值条件和适用场景分离；
- 表格标题、行列含义和数据分离；
- reviewer 缺少完整判断条件。

因此待审 chunk 应以“审查语义完整”为优先，而不是只追求字符数均匀。

## 6. 对 RAG Eval 的影响

第一版 chunk-level retrieval eval 中，建议把 chunk 长度作为分析字段加入 case：

```json
{
  "pending": {
    "chunk_no": 12,
    "char_count": 735,
    "length_bucket": "100-1500",
    "content": "..."
  }
}
```

建议统计：

- 不同长度区间的 Recall@k；
- 不同长度区间的 Useful Precision@k；
- `<100` chunk 是否更容易缺上下文；
- `>2200` chunk 是否更容易候选法规噪声过多；
- `>3000` chunk 是否集中在表格、长措施、结构识别失败等场景。

这样可以判断是否真的需要调整阈值，而不是凭感觉修改。

## 7. 下一步修改点

### 7.1 保持 `pending_doc_chunking_v7.py` 阈值不变

第一版评测先固定：

```python
MIN_CHUNK_THRESHOLD = 50
SECOND_LEVEL_SPLIT_THRESHOLD = 2200
LARGE_CHUNK_THRESHOLD = 3000
```

暂时不要为了单个误判例子改阈值。

### 7.2 在评测样本中记录长度桶

修改：

```text
rag_eval/scripts/build_annotation_app.py
```

生成 annotation case 时增加：

```python
def length_bucket(char_count):
    if char_count < 50:
        return "0-50"
    if char_count < 100:
        return "50-100"
    if char_count < 1500:
        return "100-1500"
    if char_count < 2200:
        return "1500-2200"
    if char_count < 3000:
        return "2200-3000"
    return ">3000"
```

并写入：

```json
{
  "char_count": 735,
  "length_bucket": "100-1500"
}
```

### 7.3 标注页面展示 chunk 长度

annotation app 页面里应展示：

- `char_count`
- `length_bucket`
- 是否属于 `<100` 或 `>2200` 的重点观察样本

这样人工标注时可以同步判断失败是否和 chunk 长度有关。

### 7.4 指标脚本按长度分组

后续新增评测脚本时，至少输出：

```text
Recall@k by length_bucket
Useful Precision@k by length_bucket
Missing Evidence Rate by length_bucket
```

如果发现：

- `<100` 的 missing evidence 很高，说明短 chunk 上下文不足；
- `>2200` 的 useful precision 很低，说明长 chunk 检索噪声高；
- `>3000` 集中失败，说明结构切分仍需优化。

再进入第二阶段调整 chunk 阈值。

## 8. 当前结论

第一版不建议再纠结“待审文档必须在多少字符之间”。更合适的固定方案是：

```text
理想区间：100-1500 字符
可接受区间：50-2200 字符
长 chunk 观察区间：2200-3000 字符
异常大 chunk：>3000 字符
异常小 chunk：<50 字符
```

当前 `pending_doc_chunking_v7.py` 的阈值可以先固定为 baseline。后续是否调整，应由 chunk-level retrieval eval 的分组指标决定。
