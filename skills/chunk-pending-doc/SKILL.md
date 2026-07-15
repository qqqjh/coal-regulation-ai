---
name: chunk-pending-doc
description: 待审文档（作业规程等 MinerU JSON）切分，产出待审 chunks
script: pending_doc_chunking_v9.py
---

# 待审文档切分

## 何时使用
用户上传了**待审文档**（采煤/掘进作业规程、安全技术措施等），
需要切分后送审查引擎时。

## 前置条件
- 文档已经过 MinerU 解析为 JSON
- JSON 文件放在 `new_docs/test_doc_json/` 目录下

## 用法
```
python pending_doc_chunking_v9.py
```
无命令行参数；脚本自动处理 `new_docs/test_doc_json/` 下全部 `*.json`。

## 输出
- `chunks_visualization/pending_doc_chunks_v9_<时间戳>.json` — 切分结果
  （审查引擎自动加载最新时间戳的这个文件）
- `chunks_visualization/pending_doc_chunks_v9_<时间戳>.html` — 可视化报告

## 下一步
切分完成后运行审查引擎（见 review-engine 技能）。
