---
name: chunk-rule-doc
description: 规则文档（法规/规程/标准的 MinerU JSON）按章节切分，产出知识库 chunks
script: chapter_based_chunking_v6.py
---

# 规则文档切分入库

## 何时使用
用户上传了**规则文档**（法规、安全规程、国家/行业标准、实施细则等），
需要把它加入规则知识库时。

## 前置条件
- 文档已经过 MinerU 解析为 JSON，文件名格式 `MinerU_<文档名>__*.json`
- JSON 文件放在 `new_docs/rule_docs_json/` 目录下

## 用法
```
python chapter_based_chunking_v6.py
```
无命令行参数；脚本自动处理 `new_docs/rule_docs_json/` 下全部 `MinerU_*.json`。

## 输出
- `chunks_visualization/chunks_v6_<时间戳>.json` — 切分结果
- `chunks_visualization/chunks_v6_latest.json` — 稳定版（审查引擎自动加载这个）
- `chunks_visualization/chunks_report_v6_<时间戳>.html` — 可视化报告

## 注意
- 切分后**无需手动向量化**：审查引擎 hybrid_rag_review_v9.py 启动时检测到
  chunks_v6_latest.json 内容变化会自动重建 BGE 索引（按内容哈希缓存）。
- 突出矿井专用条款会在引擎加载时按章节结构自动打 outburst_only 标签并
  在非突出矿井审查中被过滤，无需人工标注。
