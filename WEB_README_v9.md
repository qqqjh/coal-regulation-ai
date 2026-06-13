# v9 前后端联调说明

把 v9 审查引擎接入了现有的 React + FastAPI 系统。前端「审查」页改造成三栏：
顶部上传 + 主智能体状态条 / 中间 Word 段落展示 / 左侧实时问题（点击定位高亮，
可接受/驳回/改写）。人工反馈写入数据飞轮最高层，主智能体直接改写 Word 对应段落。

## 架构（三进程）

```
React 前端 (vite :3000)
   ↕ HTTP + SSE  (/api/v9/*, vite 代理到 :8000)
FastAPI 后端 (:8000, backend/app/api/endpoints/v9_review.py)
   ↕ SQLite 任务库  (data/review_queue_v9.db: jobs / issues / annotations)
v9 worker (常驻, BGE 模型只加载一次, v9_worker.py)
   → docx_adapter(段落索引) → hybrid_rag_review_v9 引擎 → 写问题(带段落索引)
   → 人工反馈 → 主智能体生成替换文本 → 改写工作副本 docx
```

后端和 worker **都在 langchain0.3 环境**运行（该环境同时具备 fastapi 与 BGE/torch）。
后端不直接 import 引擎（避免每次重载模型），靠任务库与 worker 解耦。

## 启动（三个终端）

```bash
# 1) v9 worker（常驻，首次加载 BGE 约 1 分钟）
D:/Anaconda/envs/langchain0.3/python.exe v9_worker.py

# 2) 后端
cd backend
D:/Anaconda/envs/langchain0.3/python.exe main.py        # 或 uvicorn main:app --port 8000

# 3) 前端
npm run dev                                              # http://localhost:3000 → 「审查」页
```

环境变量 `DASHSCOPE_API_KEY` 需在 worker 与后端环境中可见（已在 backend/.env）。

## 接口（/api/v9）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /upload?mine_type= | 上传 docx，建任务（worker 异步执行） |
| GET | /status/{job_id} | 任务状态快照 |
| GET | /document/{job_id} | 段落模型（中间面板；改写后会刷新） |
| GET | /stream/{job_id} | SSE：实时推进度 + 新问题 |
| GET | /issues/{job_id}?after_id= | 一次性拉问题（兜底/轮询） |
| POST | /feedback | {issue_id, action: accept/reject/custom, text} |
| GET | /feedback-status/{issue_id} | 反馈处理结果（前端轮询改写是否完成） |
| GET | /download/{job_id} | 下载主智能体改写后的工作副本 |

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
2. **复杂排版**：中间面板按段落 + 表格渲染，图片/合并单元格等会简化显示。
3. **重复性**：文档级重复在全部块审完后一次性产出（非逐块流式）。
4. worker 当前单文档串行处理 jobs；多用户并发需加多 worker 或任务并行。
