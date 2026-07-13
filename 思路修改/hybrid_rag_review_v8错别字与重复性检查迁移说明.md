# hybrid_rag_review_v8 错别字与重复性检查迁移说明

## 修改范围

- 新版本文件：`D:\work\project\coal-regulation-ai\hybrid_rag_review_v8.py`
- 保留旧版本：`D:\work\project\coal-regulation-ai\hybrid_rag_review_v7.py`
- 参考实现：
  - `D:\work\project\coal-regulation-lzy\coal-regulation-ai-backend\typo_check_agent.py`
  - `D:\work\project\coal-regulation-lzy\coal-regulation-ai-backend\redundancy_check_agent.py`

本次只迁移错别字检查和重复性检查。合规审查、法规检索、结果核验和相关性分类继续使用本项目原有流程。

## 错别字检查修改

v7 只检查前 800 字，只识别高确定性的汉字形近错误，单次调用失败后直接返回。

v8 按参考版本改为：

1. 检查完整 chunk，不再截断到前 800 字。
2. 长度小于 10 字的 chunk 不检查。
3. 检查错别字、专业术语错误和明显用词错误。
4. 明确排除数字、单位、公式、格式、行业简称和 OCR 噪声。
5. 单次检查最多重试 3 次。
6. 高置信度结果直接保留；中、低置信度结果调用 LLM 二次核验。
7. 保留参考版本字段：`wrong_char`、`correct_char`、`context`、`reason`、`confidence`。
8. 同时补充 `original`、`suggestion` 字段，以兼容本项目现有 HTML 报告和结果统计。

## 重复性检查修改

v7 对每个 chunk 单独使用 BM25 找 Top 3 相似 chunk，再调用 LLM 判断，容易漏掉语义相似但词面不同的重复内容，也会重复检查 A-B 和 B-A。

v8 按参考版本改为文档级检查：

1. 长度小于 40 字的 chunk 不参与重复检测。
2. 一次性为整篇文档的有效 chunk 获取 `text-embedding-v3` 向量。
3. 计算全文档 chunk 两两余弦相似度。
4. 只将相似度不低于 `0.90` 的候选对送给 LLM。
5. 候选对按相似度从高到低验证。
6. 每一对 chunk 只验证一次，不再重复检查 A-B 和 B-A。
7. LLM 只判断“重复”或“正常”。
8. 确认重复后，将同一重复对映射回两个相关 chunk，兼容本项目原有逐 chunk 报告结构。
9. 结果保留相似度、说明以及两个 chunk 的完整内容。

## 调度变化

- 文档开始审核时，同时启动三条主链：
  - 文档级重复性检查链。
  - 逐 chunk 合规审查链。
  - 逐 chunk 错别字检查链。
- 重复性检查在独立后台线程中处理整篇文档。
- 每个 chunk 内部继续并行执行合规审查链和错别字检查链。
- 全部 chunk 完成后，等待文档级重复性检查结束，并按 `chunk_index` 将重复结果回填到对应 chunk。
- 不再构建待审文档 BM25 重复检测索引。

因此，三并行不是对同一个 chunk 分别运行三个检查，而是：

```text
文档级重复性检查
    ||
逐 chunk 合规审查链
    ||
逐 chunk 错别字检查链
```

重复性检查必须读取整篇文档的全部 chunk，所以仍保持文档级检测方式。

## 报告变化

- HTML 报告版本和结果文件名更新为 `v8`。
- 错别字条目新增显示判断依据和置信度。
- 重复条目新增显示向量相似度。
- 自动待审 chunk 文件发现顺序更新为 `v9 -> v8 -> v7 -> ...`。

## 运行方式

```powershell
python hybrid_rag_review_v8.py
```

运行前仍需配置：

```powershell
$env:DASHSCOPE_API_KEY="你的 API Key"
```

## 需要注意

- 错别字检查改为全文检查，单次调用 token 数量和耗时会高于 v7。
- 重复性检查会计算文档内全部有效 chunk 对的相似度。向量阶段调用次数较少，但候选对数量取决于 `0.90` 阈值和文档内容。
- 为完全保持参考版本行为，重复性 LLM 验证时每个 chunk 仍只发送前 600 字，但最终结果中保存完整 chunk 内容。
