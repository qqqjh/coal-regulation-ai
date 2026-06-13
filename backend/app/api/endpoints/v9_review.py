"""v9 审查 Web 接口 —— 连接前端与 v9 worker

数据流：
  前端 upload → 这里建 job(pending) → v9_worker 取走审查 → issues 表
  前端 SSE /stream → 这里轮询 jobs+issues 增量推给前端
  前端点问题反馈 → /feedback → 写飞轮(human 最高层) + 置 issue 反馈 → worker 改 Word
  前端 /document → 返回段落模型（中间面板，改写后会刷新）
  前端 /download → 下载主智能体改写后的工作副本

注意：本模块通过 SQLite 任务库与 worker 通信，不直接 import v9 引擎
（引擎在 langchain0.3 环境，依赖不同）。
"""
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import settings

# 引入仓库根的共享模块（review_queue / docx_adapter）
_PROJECT_ROOT = str(settings.PROJECT_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from review_queue import ReviewQueue  # noqa: E402

router = APIRouter()

_queue = ReviewQueue(settings.V9_QUEUE_DB)
_WORK_DIR = settings.PROJECT_ROOT / "data" / "v9_web_work"


@router.post("/upload")
async def upload(file: UploadFile = File(...), mine_type: str = "non_outburst"):
    """上传 Word 待审文档，创建审查任务（worker 异步执行）。"""
    if not file.filename.lower().endswith((".docx", ".doc")):
        raise HTTPException(400, "只支持 Word 文档（.docx/.doc）")
    job_id = uuid.uuid4().hex[:12]
    dest = settings.UPLOAD_DIR / f"v9_{job_id}_{file.filename}"
    content = await file.read()
    dest.write_bytes(content)
    _queue.create_job(job_id, doc_name=file.filename, docx_path=str(dest),
                      mine_type=mine_type)
    return {"job_id": job_id, "doc_name": file.filename, "status": "pending"}


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@router.get("/document/{job_id}")
async def document(job_id: str):
    """返回段落模型（中间面板渲染；主智能体改写后会刷新此文件）。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    para_path = job.get("paragraphs_path")
    if not para_path or not Path(para_path).exists():
        return {"blocks": [], "n_blocks": 0, "status": job["status"]}
    with open(para_path, encoding="utf-8") as f:
        parsed = json.load(f)
    parsed["status"] = job["status"]
    return parsed


@router.get("/issues/{job_id}")
async def issues(job_id: str, after_id: int = 0):
    """一次性拉取问题（非流式，供轮询/兜底）。"""
    return {"issues": _queue.list_issues(job_id, after_id=after_id)}


@router.get("/stream/{job_id}")
async def stream(job_id: str):
    """SSE：实时推送任务进度与新问题。"""
    async def gen():
        last_issue_id = 0
        last_sig = None
        idle = 0
        while True:
            job = _queue.get_job(job_id)
            if not job:
                yield f"data: {json.dumps({'type': 'error', 'msg': '任务不存在'})}\n\n"
                return
            sig = (job["status"], job["progress"], job.get("agent_status"))
            if sig != last_sig:
                last_sig = sig
                payload = {"type": "status", "job": {
                    "status": job["status"], "progress": job["progress"],
                    "n_chunks": job["n_chunks"], "n_done": job["n_done"],
                    "agent_status": job.get("agent_status"),
                    "paragraphs_ready": bool(job.get("paragraphs_path")),
                }}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            new_issues = _queue.list_issues(job_id, after_id=last_issue_id)
            for it in new_issues:
                last_issue_id = max(last_issue_id, it["id"])
                yield f"data: {json.dumps({'type': 'issue', 'issue': it}, ensure_ascii=False)}\n\n"

            if job["status"] in ("done", "failed") and not new_issues:
                yield f"data: {json.dumps({'type': 'end', 'status': job['status']}, ensure_ascii=False)}\n\n"
                return

            idle = idle + 1 if not new_issues and sig == last_sig else 0
            await asyncio.sleep(1.0)
            if idle > 600:  # 10 分钟无变化，断开（前端可重连）
                yield f"data: {json.dumps({'type': 'timeout'})}\n\n"
                return

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/feedback")
async def feedback(body: dict):
    """人工反馈 —— 数据飞轮最高层。

    body: {issue_id, action: accept|reject|custom, text?}
      accept  人工认可系统判定（问题属实，采纳系统建议）
      reject  人工驳回（系统误报，原文合规）
      custom  人工给出自己的修改意见（问题属实，按人工意见改）
    """
    issue_id = body.get("issue_id")
    action = body.get("action")
    text = body.get("text", "")
    if not issue_id or action not in ("accept", "reject", "custom"):
        raise HTTPException(400, "参数错误：需要 issue_id 与 action(accept/reject/custom)")

    issue = _queue.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题不存在")
    job = _queue.get_job(issue["job_id"])
    doc_name = job["doc_name"] if job else issue["job_id"]

    # 人工裁决写入飞轮（最高优先级，source=human）
    final_verdict = {"accept": issue["status"], "reject": "合规",
                     "custom": issue["status"]}[action]
    _queue.add_annotation(
        doc_name=doc_name,
        chunk_index=issue["chunk_index"],
        pending_content=issue.get("original_text", ""),
        final_verdict=final_verdict,
        source="human",
        kb_refs=[{"regulation": issue.get("regulation", "")}],
        model_verdict=issue.get("status", ""),
        reason=text or issue.get("reason", ""),
        extra={"action": action, "issue_id": issue_id,
               "issue_type": issue.get("issue_type", ""),
               "human_text": text},
    )

    # accept/custom → 交 worker 让主智能体改 Word；reject → 无需改写
    if action == "reject":
        _queue.set_issue_feedback(issue_id, "reject", text)
        _queue.finish_feedback(issue_id, applied=2, agent_note="人工驳回，原文保留")
    else:
        _queue.set_issue_feedback(issue_id, action, text)

    return {"ok": True, "issue_id": issue_id, "action": action,
            "flywheel": "human", "verdict": final_verdict}


@router.get("/feedback-status/{issue_id}")
async def feedback_status(issue_id: int):
    """查询某问题反馈处理结果（前端轮询改写是否完成）。"""
    issue = _queue.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题不存在")
    return {"issue_id": issue_id, "human_action": issue["human_action"],
            "agent_applied": issue["agent_applied"], "agent_note": issue.get("agent_note", "")}


@router.get("/flywheel")
async def flywheel_list(source: str = "", limit: int = 200):
    """列出数据飞轮记录（前端查看）。source 可选 human/agent。"""
    items = _queue.list_annotations(limit=limit, source=source or None)
    return {"counts": _queue.count_annotations(), "items": items}


@router.delete("/flywheel/{ann_id}")
async def flywheel_delete(ann_id: int):
    """删除一条飞轮记录。"""
    ok = _queue.delete_annotation(ann_id)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True, "deleted": ann_id}


@router.get("/download/{job_id}")
async def download(job_id: str):
    """下载主智能体改写后的工作副本（无改写则返回原始上传）。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    work = _WORK_DIR / f"{job_id}_working.docx"
    path = work if work.exists() else Path(job["docx_path"])
    if not path.exists():
        raise HTTPException(404, "文档不存在")
    return FileResponse(
        path=str(path), filename=f"审查后_{job['doc_name']}",
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
