---
name: escalation-queue
description: 升级队列与标注飞轮：审查链争议项的存取、裁决、人工标注导出
script:
---

# 升级队列与标注飞轮

## 数据位置
SQLite：`data/review_queue_v9.db`（WAL 模式，引擎并行写入安全）

## 升级类型
| 类型 | 含义 | 典型处理 |
|------|------|----------|
| disagreement | 初审判违规、核验删光、数值工具支持初审 | 用 numeric_compare_tool 交叉验证 |
| numeric_conflict | LLM 判违规但数值工具判全部合规 | 大概率方向误判，核对后改合规 |
| low_confidence | 最终"不确定" | 改写查询词重新检索补证据 |
| error | 调用异常 | 查看 get_chunk_review，必要时转人工 |
| counter_check | 初审判合规但反向数值核验疑似超限（漏报嫌疑） | 核对配对场景是否一致，宽松属实则改不合规 |

## 处理方式
```
python main_agent_v9.py --process-queue            # 主智能体逐项裁决（独立上下文）
python main_agent_v9.py --process-queue --max-items 5
python main_agent_v9.py --queue-stats              # 查看状态
```
- 每项最多重试 3 次，超限转 dead（dead-letter 需人工）
- 裁决自动沉淀到 annotations 表（数据飞轮）

## 标注导出（扩充黄金评估集）
```python
from review_queue import ReviewQueue
q = ReviewQueue("data/review_queue_v9.db")
q.export_gold_cases("rag_eval/data/flywheel_gold_cases.json")          # 仅人工确认
q.export_gold_cases("path.json", sources=("human","frontend","agent")) # 含智能体裁决
```
前端人工确认/驳回时应调用 `q.add_annotation(..., source="frontend")` 入库。
