# RAG 原子主张、Top100 召回与补库流程 v1

本流程以现有 v9 待审 chunk 为父级审查单元，不覆盖现有黄金集和线上检索代码。

50 个父 chunk 始终是黄金评估单元。脚本提取的 `claims` 只是父 chunk 内部用于改善召回的检索 query 单元，不是新增的黄金样本，也不能未经人工确认直接作为 claim-level 指标分母。

## 新增文件

- `rag_eval/config/gold_scope_overrides_v1.json`  
  首轮评估口径覆盖项。所有结论仍需领域人员确认。
- `rag_eval/scripts/prepare_atomic_gold_v1.py`  
  清理评估口径，并从固定 v9 chunk 中提取待确认的原子审查主张。
- `rag_eval/scripts/retrieve_atomic_claims_v1.py`  
  对每个原子主张分别执行 BM25、可选 Dense Top100，完整合并后可选统一 rerank。
- `rag_eval/scripts/build_kb_gap_registry_v1.py`  
  生成知识库缺失规则审计与补库登记表。
- `rag_eval/scripts/build_atomic_claim_review_html_v1.py`  
  生成逐父 chunk 的内部检索 query 与排除单元检查报告。
- `rag_eval/scripts/merge_claim_evidence_to_parent_v1.py`  
  将多个检索单元的候选证据去重、覆盖优先排序并合并回父 chunk。

## 第一步：生成清理后的原子主张集

```powershell
python rag_eval/scripts/prepare_atomic_gold_v1.py
```

输出：

```text
rag_eval/data/gold_atomic_claims_v9_v1.json
```

关键规则：

- v9 父 chunk 的内容与位置不变。
- 自动拆出的 claim 均标记为 `needs_human_confirmation`。
- 每个 claim 均标记为 `purpose=retrieval_query_only`、`is_gold_evaluation_unit=false`。
- 标题、公式、仪器清单、单位换算、描述性上下文等进入 `excluded_units`，不参与检索。
- 自动 claim 按 `high`、`medium`、`low` 标注检索优先级；目录、公式、无明确要求的清单行通常为 `low`。
- 严格 Recall 默认只纳入“适合检索且已有确认目标证据”的样本。
- 合规但尚无目标规则的样本不会被直接计为检索失败。
- `likely_not_in_kb`、`needs_kb_audit`、`known_retrieval_miss` 被明确区分。

人工确认时，需要为每个 claim：

1. 删除不是审查主张的说明、公式、目录或标题。
2. 合并被误拆的同一要求。
3. 拆开仍包含多个要求的 claim。
4. 将父级确认规则分配到对应 claim 的 `target_evidence_ids`。
5. 将 `claim_review_status` 改为 `confirmed`。

生成检查 HTML：

```powershell
python rag_eval/scripts/build_atomic_claim_review_html_v1.py
```

输出：

```text
rag_eval/reports/atomic_claim_review_v9_v1.html
```

## 第二步：先运行无外部依赖的 BM25 Top100 基线

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py --top-k 100 --final-top-k 15
```

输出：

```text
rag_eval/data/atomic_retrieval_results_v9_v1.json
```

此时：

- 每个原子主张独立检索。
- 默认只检索 `high`、`medium` 主张，排除低价值目录和表格清单行。
- BM25 先取 Top100。
- 安装了 `jieba` 时使用与现有项目一致的 jieba 分词；未安装时使用字符级回退，并在输出配置中明确记录，二者结果不能直接混合比较。
- 最终候选池不会因为凑满 15 条而提前停止。
- 输出的 `source_top_k` 保留 Dense、BM25 各自完整 Top100 的 ID、排名和分数。
- 输出的 `union_reranked_audit` 保留完整合并候选的 RRF 与 rerank 排名。
- 输出分别保留 Dense、BM25、最终排序的首个目标证据排名和 Recall@5/10/20/50/100。
- 默认不把父 chunk 的证据自动视为每个原子 claim 的正确证据，避免产生虚假的 claim 级 Recall。

尚未完成 claim 级证据分配时，如需临时观察父级证据能否被原子 query 找到，可显式运行：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py --allow-parent-targets
```

该结果只能用于诊断，不能作为最终 claim 级黄金指标。

仅调试前几个 claim：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py --max-claims 5
```

## 第三步：增加 Dense Top100

Dense 路径复用现有 `hybrid_rag_review_v8.py` 的 DashScope embedding 与 Chroma 索引。

运行前需要确保当前 Python 环境已安装项目依赖，并已设置：

```powershell
$env:DASHSCOPE_API_KEY = "..."
```

运行：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py --dense --top-k 100 --final-top-k 15
```

Dense Top100 与 BM25 Top100 会先完整合并去重，再进行最终排序。所有原始排名保存在 `retrieval_details` 中。

Dense 请求具备网络故障保护：

- DashScope embedding 请求默认最多重试 8 次。
- 重试等待时间按指数退避增长，最长等待 60 秒。
- 每完成一个检索单元立即原子保存输出检查点。
- 若进程中断或网络持续失败，重新运行相同配置会自动跳过已完成检索单元，从失败位置继续。
- 若更换模型、TopK、是否启用 Dense 等配置，旧输出不会被误用于续跑。

如需明确忽略已有检查点并重新开始，可在子脚本运行时使用：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py --no-resume
```

## 第四步：统一重排

### bge-reranker

安装对应依赖后，可以使用 CrossEncoder 或 FlagEmbedding 后端：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py `
  --dense `
  --top-k 100 `
  --reranker cross-encoder `
  --reranker-model BAAI/bge-reranker-v2-m3
```

或：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py `
  --dense `
  --top-k 100 `
  --reranker flagembedding `
  --reranker-model BAAI/bge-reranker-v2-m3
```

### Qwen3-Reranker

脚本已提供独立 `qwen3` 后端，使用 Qwen3-Reranker 的 yes/no 相关性评分方式，不把它当成普通 CrossEncoder：

```powershell
python rag_eval/scripts/retrieve_atomic_claims_v1.py `
  --dense `
  --top-k 100 `
  --reranker qwen3 `
  --reranker-model Qwen/Qwen3-Reranker-0.6B
```

首次运行需要下载模型，并需要足够的内存或显存。应使用同一 Top100 union 比较：

- Recall@5/10/20/50/100
- MRR
- nDCG
- 首个正确证据排名

重排只能改善已经进入 Top100 union 的证据顺序，不能解决知识库没有规则的问题。

## 第五步：将证据合并回父 chunk

```powershell
python rag_eval/scripts/merge_claim_evidence_to_parent_v1.py
```

输出：

```text
rag_eval/data/parent_chunk_evidence_v9_v1.json
rag_eval/reports/parent_chunk_evidence_review_v9_v1.html
```

默认策略：

1. 每个检索单元的 Top3 进入父级合并池。
2. 相同法规 chunk 去重，并记录它覆盖的所有检索单元。
3. 优先选择能够覆盖尚未覆盖检索单元的法规。
4. 剩余位置按覆盖单元数、单元内排名和 reranker 分数排序。
5. 每个父 chunk 最多保留 25 条证据。

当前生成结果基于 BM25、未使用 reranker。覆盖多个检索单元的通用法规可能被错误排高，因此当前父级结果用于检查合并逻辑，不应作为最终质量结果。完成 Dense + reranker 后，应重新执行本步骤。

## 第六步：生成知识库缺口登记

```powershell
python rag_eval/scripts/build_kb_gap_registry_v1.py
```

输出：

```text
rag_eval/data/kb_gap_registry_v1.json
```

每个缺口需要人工填写：

- 是否确认缺库；
- 需要补充的规则主题；
- 来源文件、版本和条款位置；
- 待补充规则正文；
- 审核人与决策。

注意：

- `known_retrieval_miss`：库中已有规则，应先修检索，不应重复补库。
- `likely_not_in_kb`：疑似缺库，但必须先检查原始规则来源。
- `needs_kb_audit`：目前无法判断，不能直接计为确认缺库。

## 推荐执行顺序

1. 运行 `prepare_atomic_gold_v1.py`。
2. 人工确认原子 claim 和 claim 级目标证据。
3. 运行 BM25 Top100 基线。
4. 运行 Dense + BM25 Top100 union。
5. 接入 bge-reranker，比较重排前后指标。
6. 接入 Qwen3-Reranker adapter，使用相同 union 做公平比较。
7. 运行 `merge_claim_evidence_to_parent_v1.py`，检查父 chunk 最终证据。
8. 运行 `build_kb_gap_registry_v1.py`，人工确认后再补库。
9. 补库后重新执行同一套检索评估。

## 一键顺序运行

流水线脚本：

```text
rag_eval/run_atomic_rag_pipeline_v1.py
rag_eval/run_atomic_rag_pipeline_v1.cmd
rag_eval/run_atomic_rag_pipeline_v1.ps1
```

推荐使用 Python 入口。默认运行 smoke 模式，只处理 3 个检索单元，用于验证 API、GPU、模型下载和完整流程：

```powershell
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py
```

smoke 成功后，运行完整 Dense + BM25 + BGE reranker + 父级合并 + 缺口登记：

```powershell
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py --mode full
```

脚本默认：

- 使用 `langchain0.3` Conda 环境；
- 检查 GPU 和 CUDA；
- 从 ModelScope 下载 `AI-ModelScope/bge-reranker-v2-m3`，不从 Hugging Face 下载；
- 默认魔搭缓存目录为 `D:\models\modelscope`；
- 将魔搭返回的完整本地模型目录传给 FlagEmbedding，并在子进程中开启 Hugging Face 离线模式；
- 使用已有 `gold_atomic_claims_v9_v1.json`，不覆盖人工修改；
- 执行 Dense + BM25 Top100；
- 使用 `BAAI/bge-reranker-v2-m3`；
- 合并证据回父 chunk；
- 生成检查 HTML 和知识库缺口登记。

只有需要重新执行自动检索单元提取并覆盖现有文件时，才使用：

```powershell
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py --mode full --regenerate-claims
```

其他常用选项：

```powershell
# 不调用Dense embedding API，只运行BM25与后续阶段
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py --mode full --skip-dense

# 不运行reranker
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py --mode full --reranker none

# 使用Qwen3-Reranker
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py `
  --mode full `
  --reranker qwen3 `
  --reranker-model Qwen/Qwen3-Reranker-0.6B
```

Dense 检索前需要在当前终端设置：

```powershell
$env:DASHSCOPE_API_KEY = "你的API_KEY"
```

指定其他魔搭缓存目录：

```powershell
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py `
  --modelscope-cache-dir E:\modelscope_models
```

指定其他魔搭模型 ID：

```powershell
conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py `
  --reranker-model AI-ModelScope/bge-reranker-v2-m3
```

### 单独下载魔搭模型并实时查看进度

推荐先使用专用下载脚本将模型下载到普通本地目录：

```powershell
conda run --no-capture-output -n langchain0.3 python rag_eval/download_reranker_modelscope.py
```

注意必须包含 `--no-capture-output`，否则 `conda run` 可能缓存终端输出，导致下载进度无法实时显示。

默认下载位置：

```text
D:\work\project\coal-regulation-ai\models\bge-reranker-v2-m3
```

默认临时缓存位置：

```text
D:\work\project\coal-regulation-ai\models\modelscope-cache
```

下载中断后，重新运行相同命令即可尝试续传。仅检查模型是否完整：

```powershell
conda run --no-capture-output -n langchain0.3 python rag_eval/download_reranker_modelscope.py --inspect-only
```

下载完成后使用本地模型运行 smoke：

```powershell
conda run --no-capture-output -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py `
  --model-provider local `
  --reranker-model D:\work\project\coal-regulation-ai\models\bge-reranker-v2-m3
```
