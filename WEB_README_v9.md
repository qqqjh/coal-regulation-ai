# v9 前后端联调说明

把 v9 审查引擎接入了现有的 React + FastAPI 系统。前端「审查」页改造成三栏：
左侧实时问题 / 中间 PDF 高亮预览或 Word 段落视图 / 右侧问题详情与人工操作。
人工反馈写入数据飞轮最高层，主智能体直接改写 Word 工作副本。

## 架构（三进程）

```
React 前端 (vite :3000, react-pdf)
   ↕ HTTP + SSE  (/api/v9/*, vite 代理到 :8000)
FastAPI 后端 (:8000, backend/app/api/endpoints/v9_review.py)
   ↕ SQLite 任务库  (data/review_queue_v9.db: jobs / issues / annotations)
v9 worker (常驻, BGE 模型只加载一次, v9_worker.py)
   → docx_adapter(段落索引) → hybrid_rag_review_v9 引擎 → 写问题(带段落索引)
   → 人工反馈 → 主智能体生成替换文本 → 改写工作副本 docx
```

后端和 worker **都在 langchain0.3 环境**运行（该环境同时具备 fastapi、BGE/torch、pywin32）。
后端不直接 import 引擎（避免每次重载模型），靠任务库与 worker 解耦。
PDF 预览由后端通过 Microsoft Word COM 将 Word 工作副本转成缓存 PDF，因此后端运行环境需要 Windows + Word。
前端上传会先调用预处理接口，完成 `.docx` 规范化和 PDF 预览准备后，再创建审查任务。

## 启动（三个终端）

```powershell
# 1) v9 worker（常驻，首次加载 BGE 约 1 分钟）
D:/Anaconda/envs/langchain0.3/python.exe v9_worker.py

# 2) 后端
cd backend
D:/Anaconda/envs/langchain0.3/python.exe main.py        # 或 uvicorn main:app --port 8000

# 3) 前端
npm.cmd run dev                                          # http://localhost:3000 → 「审查」页
```

环境变量 `DASHSCOPE_API_KEY` 需在 worker 与后端环境中可见（已在 backend/.env）。

前端联调时如果不想自动处理历史升级队列，可以这样启动 worker：

```powershell
$env:V9_DISABLE_ESCALATION_LOOP="1"
D:/Anaconda/envs/langchain0.3/python.exe v9_worker.py
```

该开关只关闭主智能体升级队列线程，不影响前端上传任务和人工反馈改写 Word。

## 接口（/api/v9）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /preprocess | 上传 Word，保存原件，统一为 .docx，并尽量生成 PDF 预览缓存 |
| POST | /start/{preprocess_id}?mine_type= | 基于预处理结果创建审查任务 |
| POST | /upload?mine_type= | 兼容旧调用：内部执行 preprocess + start |
| GET | /status/{job_id} | 任务状态快照 |
| POST | /cancel/{job_id} | 取消未完成任务 |
| GET | /document/{job_id} | 段落模型（中间面板；改写后会刷新） |
| GET | /stream/{job_id} | SSE：实时推进度 + 新问题 |
| GET | /issues/{job_id}?after_id= | 一次性拉问题（兜底/轮询） |
| GET | /preview-pdf/{job_id}?refresh=0/1 | 将原始或工作副本 Word 转为 PDF 并返回缓存文件 |
| POST | /feedback | {issue_id, action: accept/reject/custom, text} |
| GET | /feedback-status/{issue_id} | 反馈处理结果（前端轮询改写是否完成） |
| GET | /download/{job_id} | 下载主智能体改写后的工作副本 |
| GET | /flywheel?source=&limit= | 查看人工反馈 / 飞轮样本 |
| DELETE | /flywheel/{ann_id} | 删除一条飞轮标注 |

## 数据飞轮（人工反馈最高层）

每次人工 accept/reject/custom 都在 `annotations` 表写一条 `source=human` 记录：
accept→采纳系统判定，reject→系统误报(合规)，custom→属实且给出人工改法。
导出扩充黄金集：
```python
from review_queue import ReviewQueue
ReviewQueue("data/review_queue_v9.db").export_gold_cases(
    "rag_eval/data/flywheel_gold_cases.json", sources=("human",))
```

## 已知限制（垂直链路阶段）

1. **矿井类型与索引**：索引在 worker 启动时按 non_outburst 构建并缓存；
   切到 outburst 任务不会重新纳入突出专用条款（需按矿井类型分别建索引，后续补）。
2. **复杂排版**：PDF 预览保留 Word 渲染效果；段落视图仍按段落 + 表格渲染，图片/合并单元格等会简化显示。
3. **重复性**：文档级重复在全部块审完后一次性产出（非逐块流式）。
4. worker 当前单文档串行处理 jobs；多用户并发需加多 worker 或任务并行。
5. **PDF 预览**：依赖后端机器可调用 Microsoft Word COM；没有 Word 时可使用段落视图和下载功能。
