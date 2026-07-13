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
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from docx import Document

from app.core.config import settings
from app.db.database import get_db
from app.services.monitor_service import monitor_service
from app.services.review_kb_permission_service import assert_kb_allowed

# 引入仓库根的共享模块（review_queue / docx_adapter）
_PROJECT_ROOT = str(settings.PROJECT_ROOT)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
from review_queue import ReviewQueue  # noqa: E402
import docx_adapter  # noqa: E402
from app.services.mongo_service import mongo_service

router = APIRouter()

_queue = ReviewQueue(settings.V9_QUEUE_DB)
_WORK_DIR = settings.PROJECT_ROOT / "data" / "v9_web_work"
_PREPROCESS_DIR = _WORK_DIR / "preprocess"
_PREPROCESS_DIR.mkdir(parents=True, exist_ok=True)
_FEEDBACK_SNAPSHOT_DIR = _WORK_DIR / "feedback_snapshots"
_FEEDBACK_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _sync_job_to_mongo(job_id: str, include_issues: bool = False) -> None:
    """Best-effort MongoDB mirror. SQLite remains the worker queue source of truth."""
    try:
        job = _queue.get_job(job_id)
        if not job:
            return
        mongo_service.upsert_review_job(job)
        if include_issues:
            mongo_service.replace_review_issues(job_id, _queue.list_issues(job_id))
    except Exception as exc:
        print(f"[mongo] sync job failed: {exc}")


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


def _feedback_snapshot_path(job_id: str, issue_id: int) -> Path:
    return _FEEDBACK_SNAPSHOT_DIR / f"{job_id}_issue_{int(issue_id)}_before.docx"


def _feedback_snapshot_meta_path(job_id: str, issue_id: int) -> Path:
    return _FEEDBACK_SNAPSHOT_DIR / f"{job_id}_issue_{int(issue_id)}_before.json"


def _refresh_working_document_views(job_id: str, work: Path) -> None:
    parsed = docx_adapter.parse_docx(work)
    paragraphs_path = _WORK_DIR / f"{job_id}_paragraphs.json"
    paragraphs_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    _queue.update_job(job_id, paragraphs_path=str(paragraphs_path))
    try:
        (_WORK_DIR / f"{job_id}_preview.pdf").unlink(missing_ok=True)
    except Exception:
        pass


def _rollback_applied_feedback(issue: dict) -> dict:
    job_id = issue.get("job_id")
    issue_id = int(issue["id"])
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在，无法回滚文档")

    work = _WORK_DIR / f"{job_id}_working.docx"
    if not work.exists():
        source = Path(job.get("docx_path") or "")
        if source.exists():
            shutil.copy2(source, work)
        else:
            raise HTTPException(404, "工作副本文档不存在，无法回滚")

    snapshot_path = _feedback_snapshot_path(job_id, issue_id)
    snapshot_meta_path = _feedback_snapshot_meta_path(job_id, issue_id)
    meta = {}
    if snapshot_meta_path.exists():
        try:
            meta = json.loads(snapshot_meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    block_indices = meta.get("block_indices") or issue.get("block_indices") or []
    original_text = meta.get("original_text") or issue.get("original_text") or ""
    new_text = meta.get("new_text") or ""
    suggestion = meta.get("suggestion") or issue.get("suggestion") or ""

    rolled_back_by = ""
    if new_text and original_text:
        doc = Document(str(work))
        if docx_adapter.apply_rewrite_at_blocks(
            doc, block_indices, new_text, original_text, mark_color=None
        ):
            doc.save(str(work))
            rolled_back_by = "reverse_rewrite"

    if not rolled_back_by and suggestion:
        doc = Document(str(work))
        if docx_adapter.revert_suggestion_at_blocks(
            doc, block_indices, suggestion, mark_color=None
        ):
            doc.save(str(work))
            rolled_back_by = "reverse_suggestion"

    if not rolled_back_by:
        if not snapshot_path.exists():
            raise HTTPException(409, "该已改写裁决缺少回滚快照，无法安全撤回")
        shutil.copy2(snapshot_path, work)
        rolled_back_by = "snapshot"

    _refresh_working_document_views(job_id, work)
    return {"rolled_back": True, "rollback_method": rolled_back_by}


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


def _convert_pdf_to_docx(source: Path, target: Path) -> None:
    """用 Microsoft Word 将 PDF 转成可审查的 .docx 副本。"""
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
                print(f"PDF 转 DOCX SaveAs2 返回异常但 .docx 已生成，继续处理: {exc}")
            else:
                raise RuntimeError(f"PDF 转 Word 失败，请先手动转成 .docx 后再上传：{exc}") from exc
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError("PDF 转 Word 失败：未生成有效 .docx 文件")
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
    if not file.filename.lower().endswith((".docx", ".doc", ".pdf")):
        raise HTTPException(400, "只支持 Word/PDF 文档（.docx/.doc/.pdf）")

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
    elif original_suffix == ".doc":
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
    else:
        try:
            _convert_pdf_to_docx(original_path, docx_path)
            shutil.copy2(original_path, pdf_path)
            steps.append({"name": "convert_pdf_to_docx", "status": "done", "message": "PDF 已转换为可审查 .docx 副本"})
            steps.append({"name": "use_original_pdf_preview", "status": "done", "message": "已使用原始 PDF 作为预览"})
        except Exception as exc:
            meta = {
                "preprocess_id": preprocess_id,
                "doc_name": file.filename,
                "original_path": str(original_path),
                "docx_path": "",
                "pdf_path": str(original_path),
                "docx_ready": False,
                "pdf_ready": original_path.exists() and original_path.stat().st_size > 0,
                "pdf_error": "",
                "status": "failed",
                "error": str(exc),
                "steps": steps + [{"name": "convert_pdf_to_docx", "status": "failed", "message": str(exc)}],
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            _save_preprocess_meta(meta)
            raise HTTPException(500, str(exc)) from exc

    if original_suffix == ".pdf":
        pdf_error = ""
    else:
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


def _create_job_from_preprocess(
    preprocess_id: str,
    mine_type: str = "non_outburst",
    user_id: str = "guest",
    kb_id: int | None = None,
) -> dict:
    meta = _load_preprocess_meta(preprocess_id)
    if not meta.get("docx_ready") or not meta.get("docx_path"):
        raise HTTPException(400, "预处理未完成，不能启动审查")
    docx_path = Path(meta["docx_path"])
    if not docx_path.exists():
        raise HTTPException(404, "预处理后的 .docx 文件不存在")

    job_id = uuid.uuid4().hex[:12]
    _queue.create_job(
        job_id,
        doc_name=meta["doc_name"],
        docx_path=str(docx_path),
        mine_type=mine_type,
        user_id=(user_id or "guest")[:80],
        kb_id=kb_id,
    )

    pdf_path = Path(meta["pdf_path"]) if meta.get("pdf_path") else None
    if pdf_path and pdf_path.exists() and pdf_path.stat().st_size > 0:
        shutil.copy2(pdf_path, _WORK_DIR / f"{job_id}_preview.pdf")

    _sync_job_to_mongo(job_id)

    return {
        "job_id": job_id,
        "doc_name": meta["doc_name"],
        "status": "pending",
        "kb_id": kb_id,
        "preprocess": meta,
    }


@router.post("/preprocess")
async def preprocess(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    """上传 Word 并完成审查前预处理：保存原件、统一为 .docx、尽量生成 PDF 预览。"""
    started = time.perf_counter()
    try:
        result = await _preprocess_upload_file(file)
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="preprocess",
            status="success",
            latency=time.perf_counter() - started,
            metadata={"filename": file.filename, "preprocess_id": result.get("preprocess_id")},
        )
        return result
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="preprocess",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"filename": file.filename},
        )
        raise


@router.post("/start/{preprocess_id}")
async def start_preprocessed(
    preprocess_id: str,
    mine_type: str = "non_outburst",
    user_id: str = "guest",
    kb_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """基于预处理结果创建审查任务。"""
    started = time.perf_counter()
    try:
        await assert_kb_allowed(db, user_id, kb_id)
        result = _create_job_from_preprocess(preprocess_id, mine_type=mine_type, user_id=user_id, kb_id=kb_id)
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="start_review",
            status="success",
            latency=time.perf_counter() - started,
            metadata={
                "preprocess_id": preprocess_id,
                "job_id": result.get("job_id"),
                "mine_type": mine_type,
                "user_id": user_id,
                "kb_id": kb_id,
            },
        )
        return result
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="start_review",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"preprocess_id": preprocess_id, "mine_type": mine_type, "user_id": user_id, "kb_id": kb_id},
        )
        raise


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    mine_type: str = "non_outburst",
    user_id: str = "guest",
    kb_id: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """兼容旧调用：上传后先预处理，再创建审查任务。新前端应使用 /preprocess + /start。"""
    started = time.perf_counter()
    try:
        await assert_kb_allowed(db, user_id, kb_id)
        meta = await _preprocess_upload_file(file)
        result = _create_job_from_preprocess(meta["preprocess_id"], mine_type=mine_type, user_id=user_id, kb_id=kb_id)
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="upload_and_start",
            status="success",
            latency=time.perf_counter() - started,
            metadata={
                "filename": file.filename,
                "preprocess_id": meta.get("preprocess_id"),
                "job_id": result.get("job_id"),
                "mine_type": mine_type,
                "user_id": user_id,
                "kb_id": kb_id,
            },
        )
        return result
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="upload_and_start",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"filename": file.filename, "mine_type": mine_type, "user_id": user_id, "kb_id": kb_id},
        )
        raise


@router.get("/jobs")
async def jobs(user_id: str = "guest", limit: int = 30, scope: str = "mine"):
    """列出当前用户的待审文档历史。MongoDB 可用时读长期镜像，不可用时退回 SQLite。"""
    query_user = None if scope == "all" else (user_id or "guest")[:80]
    rows = _queue.list_jobs(user_id=query_user, limit=limit)
    mongo_service.upsert_review_jobs(rows)
    mongo_items = None if scope == "all" else mongo_service.list_review_jobs(user_id=query_user or "guest", limit=limit)
    source_rows = mongo_items if mongo_items is not None else rows
    items = []
    for row in source_rows:
        item = dict(row)
        try:
            item["timings"] = json.loads(item.get("timings") or "{}")
        except json.JSONDecodeError:
            item["timings"] = {}
        item["issue_count"] = int(item.get("issue_count") or 0)
        item["decided_count"] = int(item.get("decided_count") or 0)
        items.append(item)
    return {"items": items}


@router.get("/storage-health")
async def storage_health():
    """查看当前业务存储分层接入状态。"""
    return {
        "sqlite_queue": {"available": settings.V9_QUEUE_DB.exists(), "path": str(settings.V9_QUEUE_DB)},
        "mongodb": mongo_service.health(),
        "redis": {"configured": bool(settings.REDIS_URL), "url": settings.REDIS_URL, "used_by_v9": False},
        "milvus": {"configured": bool(settings.MILVUS_URI), "uri": settings.MILVUS_URI},
    }


@router.get("/review-similar/{job_id}/{chunk_index}")
async def review_similar(
    job_id: str,
    chunk_index: int,
    user_id: str = "guest",
    top_k: int = 5,
):
    """检索当前用户历史待审文档中与指定 chunk 相似的片段。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    query_user = (user_id or job.get("user_id") or "guest")[:80]
    try:
        from review_doc_vector_store import review_doc_vector_store

        items = review_doc_vector_store.search_similar_chunks(
            job_id=job_id,
            chunk_index=int(chunk_index),
            user_id=query_user,
            top_k=top_k,
            include_same_job=False,
        )
    except Exception as exc:
        raise HTTPException(500, f"历史待审相似检索失败：{exc}") from exc
    return {"items": items, "job_id": job_id, "chunk_index": chunk_index, "user_id": query_user}


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
    _sync_job_to_mongo(job_id)
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


@router.post("/jobs/{job_id}/rerun")
async def rerun_job(job_id: str, user_id: str = "guest", db: AsyncSession = Depends(get_db)):
    """基于历史任务的原始审查副本重新创建一个 pending 任务。"""
    old = _queue.get_job(job_id)
    if not old:
        raise HTTPException(404, "任务不存在")
    kb_id = old.get("kb_id")
    await assert_kb_allowed(db, user_id, kb_id)
    source_docx = Path(old["docx_path"])
    if not source_docx.exists():
        raise HTTPException(404, "历史任务的审查副本文档不存在，无法重新审查")

    new_job_id = uuid.uuid4().hex[:12]
    _queue.create_job(
        new_job_id,
        doc_name=old.get("doc_name") or source_docx.name,
        docx_path=str(source_docx),
        mine_type=old.get("mine_type") or "non_outburst",
        user_id=(user_id or old.get("user_id") or "guest")[:80],
        kb_id=kb_id,
    )
    old_preview = _WORK_DIR / f"{job_id}_preview.pdf"
    if old_preview.exists() and old_preview.stat().st_size > 0:
        shutil.copy2(old_preview, _WORK_DIR / f"{new_job_id}_preview.pdf")
    _sync_job_to_mongo(new_job_id)
    return {
        "ok": True,
        "job_id": new_job_id,
        "doc_name": old.get("doc_name") or source_docx.name,
        "status": "pending",
        "kb_id": kb_id,
    }


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """删除历史待审任务。运行中的任务会先标记取消，再移出历史。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.get("status") not in ("done", "failed", "cancelled"):
        _queue.cancel_job(job_id, reason="用户删除任务")
    ok = _queue.delete_job(job_id)
    try:
        mongo_service.delete_review_job(job_id)
    except Exception as exc:
        print(f"[mongo] delete review job failed: {exc}")
    for suffix in ("_preview.pdf", "_paragraphs.json", "_working.docx"):
        try:
            (_WORK_DIR / f"{job_id}{suffix}").unlink(missing_ok=True)
        except Exception:
            pass
    return {"ok": ok, "deleted": job_id}


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
    items = _queue.list_issues(job_id, after_id=after_id)
    if after_id == 0:
        _sync_job_to_mongo(job_id, include_issues=True)
    return {"issues": items}


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
async def feedback(body: dict, db: AsyncSession = Depends(get_db)):
    """人工反馈 —— 数据飞轮最高层。

    body: {issue_id, action: accept|reject|custom, text?}
      accept  人工认可系统判定（问题属实，采纳系统建议）
      reject  人工驳回（系统误报，原文合规）
      custom  人工给出自己的修改意见（问题属实，按人工意见改）
    """
    started = time.perf_counter()
    issue_id = body.get("issue_id")
    action = body.get("action")
    text = body.get("text", "")
    try:
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

        _sync_job_to_mongo(issue["job_id"], include_issues=True)

        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="feedback",
            status="success",
            latency=time.perf_counter() - started,
            tokens=max(0, len(text) // 2),
            metadata={
                "issue_id": issue_id,
                "job_id": issue["job_id"],
                "action": action,
                "issue_type": issue.get("issue_type", ""),
            },
        )

        return {"ok": True, "issue_id": issue_id, "action": action,
                "flywheel": "human", "verdict": final_verdict}
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="feedback",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"issue_id": issue_id, "action": action},
        )
        raise


@router.get("/feedback-status/{issue_id}")
async def feedback_status(issue_id: int):
    """查询某问题反馈处理结果（前端轮询改写是否完成）。"""
    issue = _queue.get_issue(issue_id)
    if not issue:
        raise HTTPException(404, "问题不存在")
    return {"issue_id": issue_id, "human_action": issue["human_action"],
            "agent_applied": issue["agent_applied"], "agent_note": issue.get("agent_note", "")}


@router.post("/feedback/{issue_id}/revoke")
async def revoke_feedback(issue_id: int, db: AsyncSession = Depends(get_db)):
    """撤回人工裁决。已改入文档的裁决会先回滚 Word 工作副本。"""
    started = time.perf_counter()
    try:
        issue = _queue.get_issue(issue_id)
        if not issue:
            raise HTTPException(404, "问题不存在")
        if issue.get("human_action") == "pending":
            return {"ok": True, "issue_id": issue_id, "already_pending": True}
        if issue.get("agent_applied") == 3:
            raise HTTPException(409, "后台正在改写该问题，请等待完成或失败后再撤回")

        rollback_result = {}
        if issue.get("agent_applied") == 1:
            rollback_result = _rollback_applied_feedback(issue)

        ok = _queue.revoke_issue_feedback(issue_id)
        if not ok:
            raise HTTPException(404, "问题不存在")

        latest = _queue.get_issue(issue_id)
        _sync_job_to_mongo(issue["job_id"], include_issues=True)
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="revoke_feedback",
            status="success",
            latency=time.perf_counter() - started,
            metadata={
                "issue_id": issue_id,
                "job_id": issue.get("job_id"),
                "previous_action": issue.get("human_action"),
                "previous_agent_applied": issue.get("agent_applied"),
                **rollback_result,
            },
        )
        return {
            "ok": True,
            "issue_id": issue_id,
            "issue": latest,
            **rollback_result,
        }
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="revoke_feedback",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"issue_id": issue_id},
        )
        raise


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
async def preview_pdf(job_id: str, refresh: int = 0, rebuild: int = 0):
    """返回待审/工作副本的 PDF 预览，用于审查页面中栏展示。转换结果会落盘缓存。"""
    job = _queue.get_job(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")

    work = _WORK_DIR / f"{job_id}_working.docx"
    source = work if work.exists() else Path(job["docx_path"])
    if not source.exists():
        raise HTTPException(404, "文档不存在")

    cache_pdf = _WORK_DIR / f"{job_id}_preview.pdf"
    response_headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if (not rebuild and cache_pdf.exists()
            and cache_pdf.stat().st_size > 0
            and cache_pdf.stat().st_mtime >= source.stat().st_mtime):
        return FileResponse(
            path=str(cache_pdf),
            media_type="application/pdf",
            headers={
                **response_headers,
                "X-Source-MTime": str(source.stat().st_mtime),
                "X-Pdf-MTime": str(cache_pdf.stat().st_mtime),
            },
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
        if rebuild:
            try:
                cache_pdf.unlink(missing_ok=True)
            except Exception:
                pass
        tmp_pdf = _WORK_DIR / f"{job_id}_{uuid.uuid4().hex[:8]}.tmp.pdf"
        tmp_pdf.write_bytes(pdf_bytes)
        tmp_pdf.replace(cache_pdf)

    try:
        await asyncio.get_event_loop().run_in_executor(None, _convert_to_cache)
    except Exception as exc:
        raise HTTPException(500, f"文档 PDF 预览生成失败：{exc}")

    return FileResponse(
        path=str(cache_pdf),
        media_type="application/pdf",
        headers={
            **response_headers,
            "X-Source-MTime": str(source.stat().st_mtime),
            "X-Pdf-MTime": str(cache_pdf.stat().st_mtime),
        },
    )


@router.get("/download/{job_id}")
async def download(job_id: str, db: AsyncSession = Depends(get_db)):
    """下载主智能体改写后的工作副本（无改写则返回原始上传）。"""
    started = time.perf_counter()
    try:
        job = _queue.get_job(job_id)
        if not job:
            raise HTTPException(404, "任务不存在")
        work = _WORK_DIR / f"{job_id}_working.docx"
        path = work if work.exists() else Path(job["docx_path"])
        if not path.exists():
            raise HTTPException(404, "文档不存在")
        download_name = Path(job["doc_name"]).with_suffix(".docx").name
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="download",
            status="success",
            latency=time.perf_counter() - started,
            metadata={"job_id": job_id, "doc_name": job.get("doc_name", "")},
        )
        return FileResponse(
            path=str(path), filename=f"审查后_{download_name}",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as exc:
        await monitor_service.log_operation(
            db=db,
            module="v9_review",
            operation="download",
            status="failed",
            latency=time.perf_counter() - started,
            error_msg=str(exc),
            metadata={"job_id": job_id},
        )
        raise
