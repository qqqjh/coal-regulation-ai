# BGE-M3 + 原子主张黄金标注研究

## 1. 本次研究目的

验证以下观察是否成立：

> 原子主张检索与第一版相比，useful 总数接近，但有 useful 的待审 chunk 数量明显减少，说明 useful 更集中。

结论是：**“更集中”现象确实存在，但当前主要由召回覆盖下降造成，不能直接解释为检索质量提高。**

## 2. 三版方法

### 第一版

- 待审父 chunk 直接作为查询。
- Dense Top5 + BM25 Top5 + Hybrid RRF Top5。
- 去重后补足 15 条候选。

### 旧原子主张版

- 将待审父 chunk 拆成原子主张。
- 每条原子主张使用 `text-embedding-v3 Dense + BM25 + RRF + bge-reranker-v2-m3`。
- 每条主张 Top3 进入父 chunk 合并池。

### 本次 BGE 原子主张版

- 规则库：`chunks_visualization/chunks_v6_latest.json`，共 1001 个规则 chunk。
- 查询：354 条高/中优先级原子主张，覆盖 41 个待审 chunk。
- 9 个待审 chunk 没有高/中优先级原子主张。
- 每条主张：
  - BGE-M3 Dense Top100；
  - BGE-M3 Sparse Top100；
  - RRF 筛选 Top50；
  - `bge-reranker-v2-m3` 重排；
  - 同证据组去重；
  - 保留 Top15。
- 父 chunk 合并时再次按证据组去重。

## 3. 第一版与旧原子版的“集中”现象

| 指标 | 第一版 | 旧原子版 |
|---|---:|---:|
| 候选总数 | 750 | 434 |
| useful 总数 | 52 | 46 |
| 有 useful 的待审 chunk 数 | 28 | 19 |
| useful / 全部候选 | 6.93% | 10.60% |
| 每个有 useful 待审块的平均 useful 数 | 1.86 | 2.42 |

逐 chunk 变化：

| 类型 | 待审 chunk 数 |
|---|---:|
| 完全丢失 useful | 12 |
| 首次获得 useful | 3 |
| useful 增加 | 6 |
| useful 减少 | 4 |
| 数量不变 | 25 |

因此，旧原子版的 useful 确实更集中，但同时出现了明显的父 chunk 覆盖下降。

## 4. BGE 原子主张版结果

当前采用“每条主张 Top3 进入父级合并池”时：

| 指标 | 数值 |
|---|---:|
| 父级候选总数 | 470 |
| 有检索主张的待审 chunk | 41 |
| 无检索主张的待审 chunk | 9 |
| 可从已有黄金标注可靠继承标签 | 130 |
| 标签冲突，需重新判断 | 2 |
| 尚未标注 | 340 |

当前不应直接把这 470 条全部人工标注为最终黄金集，因为候选生成阶段已经暴露出较严重的召回损失。

## 5. 已知 useful 召回漏斗

为了在新候选尚未全部标注时进行诊断，将以下三套已有人工标注中的 useful 按同一待审 chunk、同一证据组取并集：

- 第一版 useful；
- 旧原子主张版 useful；
- BGE 父 chunk 直接检索版 useful。

并集共有 189 条已知 useful。它不是最终统一黄金集，但可以作为召回下界诊断集。

| 阶段 | 命中已知 useful | 相对 189 条 |
|---|---:|---:|
| BGE-M3 Dense/Sparse Top100 并集 | 122 | 64.6% |
| 任一原子主张重排 Top15 | 65 | 34.4% |
| 每主张 Top3 后合并到父 chunk | 23 | 12.2% |

主要损失位置：

1. **原子主张缺失**：9 个待审 chunk 没有检索主张，其中存在 25 条已知 useful。
2. **原子查询语义收窄**：67 条已知 useful 连 Dense/Sparse Top100 并集都没有进入；另有 57 条进入源召回但未进入主张重排 Top15。
3. **父级 Top3 合并池过严**：主张 Top15 已有 65 条已知 useful，父级 Top3 合并后只剩 23 条。

## 6. 不同父级合并池大小

父 chunk 最终最多保留 25 条候选。

| 每条主张进入合并池 | 父级候选总数 | 恢复已知 useful | 恢复率 | 覆盖待审 chunk |
|---|---:|---:|---:|---:|
| Top1 | 176 | 9 | 4.8% | 7 |
| Top3 | 470 | 23 | 12.2% | 15 |
| Top5 | 709 | 29 | 15.3% | 17 |
| Top10 | 946 | 42 | 22.2% | 23 |
| Top15 | 1014 | 47 | 24.9% | 26 |

从 Top10 放宽到 Top15，只多恢复 5 条已知 useful，却新增 68 条父级候选。该结果说明 Top10 的召回能力优于 Top3，但也显著增加了待标注候选数量。Top10 保留为历史对照实验，不再作为当前正式配置。

但即使使用 Top15，也只能恢复 47/189 条已知 useful，因此只调整父级合并池无法解决问题。

### 当前正式实验配置

- 使用 Codex v2 人工复核原子主张；
- 默认使用 `focused_query`；
- 每条主张 Top3 进入父级合并池；
- 父级按证据组去重后最终最多保留 Top25；
- `contextual_query` 暂时作为对照实验保留。

Top3 控制每条原子主张进入父级覆盖优先合并阶段的候选数量；父级最终候选仍最多保留 Top25。Top10 版本仅用于分析扩大合并池后的召回变化。

## 7. 对“useful 更集中”的判断

当前集中现象由两部分组成：

1. **正向集中**：一个待审 chunk 被拆成多个原子主张后，多个主张可能稳定命中同一批高度相关法规。
2. **负向集中**：没有主张、主张语义过窄、每主张 Top3 截断，导致不少待审 chunk 的正确证据完全消失。

现阶段负向集中影响较大。因此，不能用 useful 密度提高来证明原子主张法更好，必须同时观察：

- 有 useful 的待审 chunk 覆盖率；
- `missing_correct_evidence` 数量；
- 已知 useful Recall；
- 无原子主张的待审 chunk 数；
- 主张级召回到父级合并各阶段的漏斗。

## 8. 推荐下一版结构

不建议使用“纯原子主张检索”替代父 chunk 检索。建议改成双通道：

1. **父 chunk 直接检索作为保底通道**
   - BGE-M3 Dense + Sparse；
   - 保证没有原子主张的待审 chunk 仍有候选；
   - 保留父级整体语义和跨条款上下文。

2. **原子主张检索作为补充通道**
   - 用于发现父 chunk 查询容易稀释的具体数值、动作、对象和禁止性要求；
   - 不负责独立决定全部候选。

3. **合并与重排**
   - 合并父 chunk 直接检索候选和原子主张候选；
   - 按证据组去重；
   - 同时记录父查询得分、最佳原子主张得分、覆盖主张数；
   - 再生成固定数量候选用于黄金标注。

## 9. 黄金标注建议

当前 Top3 黄金集已从既有标注中安全迁移 301/548 条候选，剩余 247 条需要继续人工复核。

推荐顺序：

1. 实现“父 chunk 保底 + 原子主张补充”的双通道候选。
2. 使用现有三套黄金标注按相同待审 chunk 和证据组迁移已有标签。
3. 对冲突标签和新增候选进行人工复核。
4. 固定统一黄金证据组后，再比较：
   - 父 chunk 直接检索；
   - 纯原子主张检索；
   - 父 chunk + 原子主张双通道。

这样可避免每个检索版本只标注自己召回的候选，导致不同版本的黄金集不可直接比较。

## 10. 本次生成的文件

### 检索与黄金标注初始集

- `rag_eval/data/atomic_retrieval_local_bgem3_grouped_v1.json`
- `rag_eval/data/parent_chunk_evidence_local_bgem3_grouped_v1.json`
- `rag_eval/data/atomic_bgem3_gold_annotations_v1.json`
- `rag_eval/data/atomic_bgem3_gold_checkpoints_v1/`

### 可视化报告

- `rag_eval/reports/initial_vs_atomic_useful_comparison_v1.html`
- `rag_eval/reports/parent_chunk_evidence_local_bgem3_grouped_v1.html`
- `rag_eval/reports/atomic_bgem3_gold_annotations_review_v1.html`
- `rag_eval/reports/atomic_bgem3_diagnostic_v1.html`
- `rag_eval/reports/atomic_bgem3_merge_pool_analysis_v1.html`

### 统计数据

- `rag_eval/data/initial_vs_atomic_useful_comparison_v1.json`
- `rag_eval/data/atomic_bgem3_diagnostic_v1.json`
- `rag_eval/data/atomic_bgem3_merge_pool_analysis_v1.json`
