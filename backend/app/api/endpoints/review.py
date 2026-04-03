from fastapi import APIRouter, UploadFile, File, HTTPException, BackgroundTasks, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.schemas import ReviewRequest, ReviewResponse
from app.services.document_review_service import document_review_service
from app.services.document_modifier import document_modifier
from app.services.monitor_service import monitor_service
from app.models.database import ReviewTask
from app.db.database import get_db
import uuid
from datetime import datetime
import time
import shutil
from app.core.config import settings
from docx import Document
import io
import json
from pathlib import Path

router = APIRouter()

# 存储审查任务的进度（实际应用中应使用Redis或数据库）
review_tasks = {}

@router.post("/upload")
async def upload_for_review(
    file: UploadFile = File(...),
    kb_id: int = 3,  # 默认知识库ID为3
    db: AsyncSession = Depends(get_db)
):
    """上传Word文档进行审查"""
    start_time = time.time()

    try:
        # 验证文件类型
        if not file.filename.endswith(('.docx', '.doc')):
            raise HTTPException(status_code=400, detail="只支持Word文档格式(.docx, .doc)")

        # 生成任务ID
        task_id = str(uuid.uuid4())

        # 保存文件
        file_path = settings.UPLOAD_DIR / f"review_{task_id}_{file.filename}"
        content_bytes = await file.read()

        with open(file_path, "wb") as buffer:
            buffer.write(content_bytes)

        # 解析Word文档
        doc = Document(io.BytesIO(content_bytes))

        # 提取文本内容
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text)

        content = '\n'.join(full_text)

        if not content.strip():
            raise HTTPException(status_code=400, detail="文档内容为空")

        # 创建审查任务记录（保存知识库ID）
        task = ReviewTask(
            task_id=task_id,
            file_name=file.filename,
            file_path=str(file_path),
            status="pending",
            progress=0,
            kb_id=kb_id,  # 保存知识库ID
            created_at=datetime.utcnow()
        )
        db.add(task)
        await db.commit()

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="upload_document",
            status="success",
            latency=time.time() - start_time
        )

        return {
            "task_id": task_id,
            "filename": file.filename,
            "status": "pending",
            "kb_id": kb_id,
            "content_preview": content[:200] + "..." if len(content) > 200 else content,
            "message": "文档已上传，等待审查"
        }

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="upload_document",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/analyze/{task_id}")
async def analyze_document(
    task_id: str,
    db: AsyncSession = Depends(get_db)
):
    """开始审查文档（同步执行以确保完成）"""
    from sqlalchemy import select
    import logging

    logger = logging.getLogger(__name__)
    start_time = time.time()

    # 查找任务
    result = await db.execute(
        select(ReviewTask).where(ReviewTask.task_id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == "processing":
        return {"message": "任务正在处理中", "task_id": task_id}

    # 更新任务状态
    task.status = "processing"
    task.progress = 0
    await db.commit()

    try:
        logger.info(f"开始处理审查任务: {task_id}")
        logger.info(f"文件: {task.file_name}")

        # 读取Word文档内容
        doc = Document(task.file_path)
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text)

        content = '\n'.join(full_text)
        logger.info(f"文档内容长度: {len(content)} 字符")

        # 执行四个智能体审查（传递知识库ID）
        logger.info(f"开始执行多智能体审查... 使用知识库ID: {task.kb_id}")
        review_result = await document_review_service.review_document(content, kb_id=task.kb_id or 3)
        logger.info(f"审查完成! 总修改数: {review_result.get('total_changes', 0)}")

        # 更新任务结果
        task.status = "completed"
        task.progress = 100
        task.result = json.dumps(review_result, ensure_ascii=False)
        task.completed_at = datetime.utcnow()
        await db.commit()

        logger.info(f"任务结果已保存到数据库")

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="analyze_document",
            status="success",
            latency=time.time() - start_time,
            tokens=1500
        )

        return {
            "message": "审查任务已完成",
            "task_id": task_id,
            "status": "completed",
            "total_changes": review_result.get('total_changes', 0)
        }

    except Exception as e:
        logger.error(f"处理任务失败: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())

        task.status = "failed"
        task.error_msg = str(e)
        await db.commit()

        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="analyze_document",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )

        raise HTTPException(status_code=500, detail=f"分析失败: {str(e)}")

async def process_review_task(task_id: str):
    """处理审查任务（后台任务）"""
    from sqlalchemy import select
    import json
    from app.db.database import async_session
    import traceback
    import logging

    logger = logging.getLogger(__name__)
    start_time = time.time()

    # 创建新的数据库会话
    async with async_session() as db:
        try:
            logger.info(f"开始处理审查任务: {task_id}")

            # 查找任务
            result = await db.execute(
                select(ReviewTask).where(ReviewTask.task_id == task_id)
            )
            task = result.scalar_one_or_none()

            if not task:
                logger.error(f"任务不存在: {task_id}")
                return

            logger.info(f"找到任务: {task.file_name}")

            # 读取Word文档内容
            doc = Document(task.file_path)
            full_text = []
            for para in doc.paragraphs:
                if para.text.strip():
                    full_text.append(para.text)

            content = '\n'.join(full_text)
            logger.info(f"文档内容长度: {len(content)} 字符")

            # 执行四个智能体审查
            logger.info("开始执行多智能体审查...")
            review_result = await document_review_service.review_document(content)
            logger.info(f"审查完成! 总修改数: {review_result.get('total_changes', 0)}")

            # 更新任务结果
            task.status = "completed"
            task.progress = 100
            task.result = json.dumps(review_result, ensure_ascii=False)
            task.completed_at = datetime.utcnow()
            await db.commit()

            logger.info(f"任务结果已保存到数据库")

            # 记录监控日志
            await monitor_service.log_operation(
                db=db,
                module="review",
                operation="analyze_document",
                status="success",
                latency=time.time() - start_time,
                tokens=1500  # 估算token数
            )

        except Exception as e:
            logger.error(f"处理任务失败: {str(e)}")
            logger.error(traceback.format_exc())

            # 尝试更新任务状态为失败
            try:
                task.status = "failed"
                task.error_msg = str(e)
                await db.commit()
            except Exception as e2:
                logger.error(f"更新失败状态也失败了: {str(e2)}")

            try:
                await monitor_service.log_operation(
                    db=db,
                    module="review",
                    operation="analyze_document",
                    status="failed",
                    latency=time.time() - start_time,
                    error_msg=str(e)
                )
            except Exception as e3:
                logger.error(f"记录监控日志失败: {str(e3)}")

@router.get("/result/{task_id}")
async def get_review_result(
    task_id: str,
    db: AsyncSession = Depends(get_db)
):
    """获取审查结果"""
    from sqlalchemy import select
    import json

    result = await db.execute(
        select(ReviewTask).where(ReviewTask.task_id == task_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    response = {
        "task_id": task.task_id,
        "file_name": task.file_name,
        "status": task.status,
        "progress": task.progress,
        "current_agent": task.current_agent,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None
    }

    if task.status == "completed" and task.result:
        response["result"] = json.loads(task.result)

    if task.status == "failed" and task.error_msg:
        response["error"] = task.error_msg

    return response

@router.post("/apply-changes")
async def apply_changes(
    request: dict,
    db: AsyncSession = Depends(get_db)
):
    """应用选定的修改建议并生成修改后的文档"""
    from sqlalchemy import select

    file_id = request.get("file_id")
    accepted_change_ids = request.get("accepted_change_ids", [])

    if not file_id:
        raise HTTPException(status_code=400, detail="缺少file_id参数")

    # 查找任务
    result = await db.execute(
        select(ReviewTask).where(ReviewTask.task_id == file_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != "completed":
        raise HTTPException(status_code=400, detail="任务尚未完成")

    if not task.result:
        raise HTTPException(status_code=400, detail="任务结果不存在")

    try:
        # 解析审查结果
        review_result = json.loads(task.result)
        all_changes = review_result.get("all_changes", [])

        # 准备文件路径
        original_file_path = Path(task.file_path)
        output_file_path = settings.UPLOAD_DIR / f"{file_id}_revised.docx"

        # 应用修改
        apply_result = document_modifier.apply_changes_to_document(
            original_file_path=original_file_path,
            output_file_path=output_file_path,
            changes=all_changes,
            accepted_change_ids=accepted_change_ids
        )

        return {
            "message": "修改已成功应用",
            "file_id": file_id,
            "total_accepted": len(accepted_change_ids),
            "applied_count": apply_result["applied_count"],
            "failed_count": apply_result["failed_count"],
            "output_path": str(output_file_path),
            "download_url": f"/api/review/download/{file_id}"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"应用修改失败: {str(e)}")

@router.get("/download/{file_id}")
async def download_revised_document(
    file_id: str,
    db: AsyncSession = Depends(get_db)
):
    """下载修改后的文档"""
    from sqlalchemy import select

    # 查找任务
    result = await db.execute(
        select(ReviewTask).where(ReviewTask.task_id == file_id)
    )
    task = result.scalar_one_or_none()

    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 检查修改后的文件是否存在
    revised_file_path = settings.UPLOAD_DIR / f"{file_id}_revised.docx"

    if not revised_file_path.exists():
        raise HTTPException(status_code=404, detail="修改后的文档不存在，请先应用修改")

    # 返回文件下载
    return FileResponse(
        path=str(revised_file_path),
        filename=f"修改后_{task.file_name}",
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )

@router.post("/analyze", response_model=ReviewResponse)
async def review_document_sync(
    request: ReviewRequest,
    db: AsyncSession = Depends(get_db)
):
    """同步审查文档（简化版，用于快速测试）"""
    start_time = time.time()

    try:
        report = await review_agent_service.review_document(
            document_content=request.content,
            requirements=request.requirements or "符合《煤矿安全规程》"
        )

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="sync_review",
            status="success",
            latency=time.time() - start_time,
            tokens=1200
        )

        return ReviewResponse(
            original_text=request.content,
            suggestions=[issue["description"] for issue in report.get("issues", [])],
            revised_text=report.get("revised_content", "")
        )

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="review",
            operation="sync_review",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))
