---
name: word-review-web
description: v9 Web Word 审查、PDF 预览高亮、人工裁决与工作副本改写流程
script: v9_worker.py
---

# v9 Web Word 审查与改写

## 何时使用
用户通过前端“审查”页面上传 `.docx` / `.doc`，系统先进行文档预处理（保存原件、统一为 `.docx`、尽量生成 PDF 预览缓存），再创建审查任务。用户需要在页面中查看 Word/PDF 预览、定位审查问题、人工采纳/驳回/自定义改写，并下载改写后的 Word 工作副本。

## 关键文件
- 前端页面：`src/pages/Review/index.jsx`、`src/pages/Review/index.css`
- Web API：`backend/app/api/endpoints/v9_review.py`
- 常驻 worker：`v9_worker.py`
- 队列/飞轮库：`review_queue.py`
- Word 段落模型与定位：`docx_adapter.py`
- Word/PDF 工具：`word_modifier.py`
- LLM 编辑工具：`llm_doc_editor.py`

## 后端接口
- `POST /api/v9/preprocess`：上传 Word 并完成审查前预处理，返回 `preprocess_id`、`.docx` 就绪状态和 PDF 预览状态
- `POST /api/v9/start/{preprocess_id}?mine_type=non_outburst|outburst`：基于预处理结果创建审查任务
- `POST /api/v9/upload?mine_type=non_outburst|outburst`：上传 Word 并创建任务
- `GET /api/v9/stream/{job_id}`：SSE 推送任务状态与增量 issues
- `GET /api/v9/document/{job_id}`：返回段落模型 JSON
- `GET /api/v9/preview-pdf/{job_id}`：将原始/工作副本 Word 转 PDF 并缓存
- `POST /api/v9/feedback`：人工 `accept` / `reject` / `custom`
- `GET /api/v9/feedback-status/{issue_id}`：轮询改写结果
- `POST /api/v9/cancel/{job_id}`：取消未完成任务
- `GET /api/v9/download/{job_id}`：下载工作副本

## 运行
1. 启动 FastAPI 后端：`backend/main.py`
2. 启动前端：`npm run dev`
3. 启动常驻 worker：
   `D:\Anaconda\envs\langchain0.3\python.exe v9_worker.py`

## 注意
- PDF 预览依赖 Windows Word COM：需要本机安装 Microsoft Word，并安装 `pywin32`。
- worker 与前端通过 `data/review_queue_v9.db` 通信；不要手动删除运行中的队列库。
- 每个任务的工作副本和 PDF 缓存在 `data/v9_web_work/`。
