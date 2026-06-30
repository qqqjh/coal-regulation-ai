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
import shutil
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
_PREPROCESS_DIR = _WORK_DIR / "preprocess"
_PREPROCESS_DIR.mkdir(parents=True, exist_ok=True)


def _preprocess_meta_path(preprocess_id: str) -> Path:
    if not preprocess_id or not preprocess_id.isalnum():
        raise HTTPException(400, "预处理ID无效")
    return _PREPROCESS_DIR / f"{preprocess_id}.json"


def _load_preprocess_meta(preprocess_id: str) -> dict:
    meta_path = _preprocess_meta_path(preprocess_id)
    if not meta_path.exists():
        raise HTTPException(404, "预处理结果不存在或已清理")
    with meta_path.open(encoding="utf-8") as f:
        return json.load(f)


def _save_preprocess_meta(meta: dict) -> None:
    meta_path = _preprocess_meta_path(meta["preprocess_id"])
    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _convert_doc_to_docx(source: Path, target: Path) -> None:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(source.resolve()), ReadOnly=True, AddToRecentFiles=False)
        try:
            doc.SaveAs2(str(target.resolve()), FileFormat=16)
        except Exception as exc:
            if target.exists() and target.stat().st_size > 0:
                print(f"Word SaveAs2 返回异常但 .docx 已生成，继续处理: {exc}")
            else:
                raise RuntimeError(
                    f"Word COM 转换 .doc 失败，请先手动另存为 .docx 后再上传：{exc}"
                ) from exc
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("Word COM 转换 .doc 失败：未生成有效 .docx 文件")
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _try_build_pdf(docx_path: Path, pdf_path: Path) -> str:
    """生成 PDF 预览缓存。返回空字符串表示成功，非空为可展示错误。"""
    try:
        import importlib.util

        wm_spec = importlib.util.spec_from_file_location(
            "word_modifier", settings.PROJECT_ROOT / "word_modifier.py"
        )
        wm = importlib.util.module_from_spec(wm_spec)
        wm_spec.loader.exec_module(wm)
        pdf_bytes = wm.docx_to_pdf_bytes(docx_path)
        pdf_path.write_bytes(pdf_bytes)
        if not pdf_path.exists() or pdf_path.stat().st_size == 0:
            return "PDF 预览生成失败：未生成有效 PDF 文件"
        return ""
    except Exception as exc:
        return f"PDF 预览生成失败：{str(exc)[:500]}"


async def _preprocess_upload_file(file: UploadFile) -> dict:
    if not file.filename.lower().endswith((".docx", ".doc")):
        raise HTTPException(400, "只支持 Word 文档（.docx/.doc）")

    preprocess_id = uuid.uuid4().hex[:12]
    original_suffix = Path(file.filename).suffix.lower()
    original_path = _PREPROCESS_DIR / f"{preprocess_id}_original{original_suffix}"
    docx_path = _PREPROCESS_DIR / f"{preprocess_id}.docx"
    pdf_path = _PREPROCESS_DIR / f"{preprocess_id}.pdf"

    content = await file.read()
    original_path.write_bytes(content)
    if not original_path.exists() or original_path.stat().st_size == 0:
        raise HTTPException(400, "上传文件为空或保存失败")

    steps = []
    if original_suffix == ".docx":
        shutil.copy2(original_path, docx_path)
        steps.append({"name": "normalize_docx", "status": "done", "message": "已保存为标准审查副本"})
    else:
        try:
            _convert_doc_to_docx(original_path, docx_path)
            steps.append({"name": "convert_doc_to_docx", "status": "done", "message": ".doc 已转换为 .docx"})
        except Exception as exc:
            meta = {
                "preprocess_id": preprocess_id,
                "doc_name": file.filename,
                "original_path": str(original_path),
                "docx_path": "",
                "pdf_path": "",
                "docx_ready": False,
                "pdf_ready": False,
                "pdf_error": "",
                "status": "failed",
                "error": str(exc),
                "steps": steps + [{"name": "convert_doc_to_docx", "status": "failed", "message": str(exc)}],
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_preprocess_meta(meta)
            raise HTTPException(500, str(exc)) from exc

    pdf_error = _try_build_pdf(docx_path, pdf_path)
    steps.append({
        "name": "build_pdf_preview",
        "status": "warning" if pdf_error else "done",
        "message": pdf_error or "PDF 预览已生成",
    })
    meta = {
        "preprocess_id": preprocess_id,
        "doc_name": file.filename,
        "original_path": str(original_path),
        "docx_path": str(docx_path),
        "pdf_path": str(pdf_path) if pdf_path.exists() and pdf_path.stat().st_size > 0 else "",
        "docx_ready": True,
        "pdf_ready": bool(pdf_path.exists() and pdf_path.stat().st_size > 0),
        "pdf_error": pdf_error,
        "status": "ready",
        "error": "",
        "steps": steps,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_preprocess_meta(meta)
    return meta


def _create_job_from_preprocess(preprocess_id: str, mine_type: str = "non_outburst") -> dict:
    meta = _load_preprocess_meta(preprocess_id)
    if not meta.get("docx_ready") or not meta.get("docx_path"):
        raise HTTPException(400, "预处理未完成，不能启动审查")
    docx_path = Path(meta["docx_path"])
    if not docx_path.exists():
        raise HTTPException(404, "预处理后的 .docx 文件不存在")

    job_id = uuid.uuid4().hex[:12]
    _queue.create_job(job_id, doc_name=meta["doc_name"], docx_path=str(docx_path),
                      mine_type=mine_type)

    pdf_path = Path(meta["pdf_path"]) if meta.get("pdf_path") else None
    if pdf_path and pdf_path.exists() and pdf_path.stat().st_size > 0:
        shutil.copy2(pdf_path, _WORK_DIR / f"{job_id}_preview.pdf")

    return {
        "job_id": job_id,
        "doc_name": meta["doc_name"],
        "status": "pending",
        "preprocess": meta,
    }


@router.post("/preprocess")
async def preprocess(file: UploadFile = File(...)):
    """上传 Word 并完成审查前预处理：保存原件、统一为 .docx、尽量生成 PDF 预览。"""
    return await _preprocess_upload_file(file)


@router.post("/start/{preprocess_id}")
async def start_preprocessed(preprocess_id: str, mine_type: str = "non_outburst"):
    """基于预处理结果创建审查任务。"""
    return _create_job_from_preprocess(preprocess_id, mine_type=mine_type)


@router.post("/upload")
async def upload(file: UploadFile = File(...), mine_type: str = "non_outburst"):
    """兼容旧调用：上传后先预处理，再创建审查任务。新前端应使用 /preprocess + /start。"""
    meta = await _preprocess_upload_file(file)
    return _create_job_from_preprocess(meta["preprocess_id"], mine_type=mine_type)


@router.get("/status/{job_id}")
async def status(job_id: str):
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if isinstance(job.get("timings"), str):
        try:
            job["timings"] = json.loads(job.get("timings") or "{}")
        except json.JSONDecodeError:
            job["timings"] = {}
    return job


@router.post("/cancel/{job_id}")
async def cancel(job_id: str):
    """取消审查任务。pending 任务直接退出队列；运行中任务由 worker 协作停止。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job["status"] in ("done", "failed", "cancelled"):
        return {"ok": True, "job_id": job_id, "status": job["status"]}
    ok = _queue.cancel_job(job_id)
    return {"ok": ok, "job_id": job_id, "status": "cancelled"}


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
            sig = (
                job["status"], job["progress"], job.get("agent_status"),
                job.get("agent_stage"), job.get("phase_done"), job.get("phase_total"),
                job.get("timings"),
            )
            status_changed = sig != last_sig
            if status_changed:
                last_sig = sig
                try:
                    timings = json.loads(job.get("timings") or "{}")
                except json.JSONDecodeError:
                    timings = {}
                payload = {"type": "status", "job": {
                    "job_id": job["job_id"], "doc_name": job["doc_name"],
                    "status": job["status"], "progress": job["progress"],
                    "n_chunks": job["n_chunks"], "n_done": job["n_done"],
                    "agent_stage": job.get("agent_stage"),
                    "phase_done": job.get("phase_done"),
                    "phase_total": job.get("phase_total"),
                    "timings": timings,
                    "agent_status": job.get("agent_status"),
                    "paragraphs_ready": bool(job.get("paragraphs_path")),
                    "error": job.get("error"),
                }}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

            new_issues = _queue.list_issues(job_id, after_id=last_issue_id)
            for it in new_issues:
                last_issue_id = max(last_issue_id, it["id"])
                yield f"data: {json.dumps({'type': 'issue', 'issue': it}, ensure_ascii=False)}\n\n"

            if job["status"] in ("done", "failed", "cancelled") and not new_issues:
                yield f"data: {json.dumps({'type': 'end', 'status': job['status']}, ensure_ascii=False)}\n\n"
                return

            idle = idle + 1 if not new_issues and not status_changed else 0
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


@router.get("/preview-pdf/{job_id}")
async def preview_pdf(job_id: str, refresh: int = 0):
    """返回待审/工作副本的 PDF 预览，用于审查页面中栏展示。转换结果会落盘缓存。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")

    work = _WORK_DIR / f"{job_id}_working.docx"
    source = work if work.exists() else Path(job["docx_path"])
    if not source.exists():
        raise HTTPException(404, "文档不存在")

    cache_pdf = _WORK_DIR / f"{job_id}_preview.pdf"
    if (not refresh and cache_pdf.exists()
            and cache_pdf.stat().st_size > 0
            and cache_pdf.stat().st_mtime >= source.stat().st_mtime):
        return FileResponse(
            path=str(cache_pdf),
            media_type="application/pdf",
            headers={"Cache-Control": "no-cache"},
        )

    def _convert_to_cache() -> None:
        import importlib.util
        import tempfile
        import pythoncom
        import win32com.client

        wm_spec = importlib.util.spec_from_file_location(
            "word_modifier", settings.PROJECT_ROOT / "word_modifier.py"
        )
        wm = importlib.util.module_from_spec(wm_spec)
        wm_spec.loader.exec_module(wm)

        docx_path = source
        if source.suffix.lower() == ".doc":
            temp_docx = Path(tempfile.mktemp(suffix=".docx"))
            pythoncom.CoInitialize()
            word = None
            doc = None
            try:
                word = win32com.client.DispatchEx("Word.Application")
                word.Visible = False
                word.DisplayAlerts = 0
                doc = word.Documents.Open(str(source.resolve()), ReadOnly=True, AddToRecentFiles=False)
                try:
                    doc.SaveAs2(str(temp_docx.resolve()), FileFormat=16)
                except Exception as exc:
                    if temp_docx.exists() and temp_docx.stat().st_size > 0:
                        print(f"Word SaveAs2 返回异常但 .docx 已生成，继续生成 PDF: {exc}")
                    else:
                        raise RuntimeError(
                            f"Word COM 转换 .doc 失败，请先手动另存为 .docx 后再上传：{exc}"
                        ) from exc
                if not temp_docx.exists() or temp_docx.stat().st_size == 0:
                    raise RuntimeError("Word COM 转换 .doc 失败：未生成有效 .docx 文件")
            finally:
                if doc is not None:
                    try:
                        doc.Close(False)
                    except Exception:
                        pass
                if word is not None:
                    try:
                        word.Quit()
                    except Exception:
                        pass
                pythoncom.CoUninitialize()
            docx_path = temp_docx

        pdf_bytes = wm.docx_to_pdf_bytes(docx_path)
        tmp_pdf = cache_pdf.with_suffix(".tmp.pdf")
        tmp_pdf.write_bytes(pdf_bytes)
        tmp_pdf.replace(cache_pdf)

    try:
        await asyncio.get_event_loop().run_in_executor(None, _convert_to_cache)
    except Exception as exc:
        raise HTTPException(500, f"文档 PDF 预览生成失败：{exc}")

    return FileResponse(
        path=str(cache_pdf),
        media_type="application/pdf",
        headers={"Cache-Control": "no-cache"},
    )


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
