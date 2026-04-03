from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.database import get_db
from app.services.document_review_service import document_review_service
from app.core.config import settings
import os
import uuid
from datetime import datetime
from docx import Document
import io

router = APIRouter()

@router.post("/review")
async def review_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db)
):
    """上传Word文档进行审查"""

    # 验证文件类型
    if not file.filename.endswith(('.docx', '.doc')):
        raise HTTPException(status_code=400, detail="只支持Word文档格式(.docx, .doc)")

    try:
        # 读取文件内容
        content = await file.read()

        # 解析Word文档
        doc = Document(io.BytesIO(content))

        # 提取文本内容
        full_text = []
        for para in doc.paragraphs:
            if para.text.strip():
                full_text.append(para.text)

        document_content = '\n'.join(full_text)

        if not document_content.strip():
            raise HTTPException(status_code=400, detail="文档内容为空")

        # 保存原始文件
        file_id = str(uuid.uuid4())
        original_filename = f"{file_id}_{file.filename}"
        file_path = settings.UPLOAD_DIR / original_filename

        with open(file_path, 'wb') as f:
            f.write(content)

        # 执行多智能体审查
        review_result = await document_review_service.review_document(document_content)

        # 返回审查结果
        return {
            "file_id": file_id,
            "filename": file.filename,
            "original_path": str(file_path),
            "total_changes": review_result["total_changes"],
            "typo_count": review_result["typo_count"],
            "fluency_count": review_result["fluency_count"],
            "duplicate_count": review_result["duplicate_count"],
            "compliance_count": review_result["compliance_count"],
            "changes": review_result["all_changes"],
            "original_content": review_result["original_content"],
            "revised_content": review_result["revised_content"],
            "errors": review_result["errors"],
            "timestamp": datetime.utcnow().isoformat()
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文档审查失败: {str(e)}")

@router.post("/apply-changes")
async def apply_changes(
    file_id: str,
    accepted_change_ids: list[int],
    db: AsyncSession = Depends(get_db)
):
    """应用选定的修改建议"""

    # TODO: 实现修改应用逻辑
    # 1. 根据file_id找到原始文件
    # 2. 根据accepted_change_ids应用修改
    # 3. 生成新的Word文档

    return {
        "message": "修改已应用",
        "file_id": file_id,
        "applied_count": len(accepted_change_ids)
    }

@router.get("/download/{file_id}")
async def download_revised_document(
    file_id: str,
    db: AsyncSession = Depends(get_db)
):
    """下载修改后的文档"""

    # TODO: 实现文档下载逻辑
    # 1. 根据file_id找到修改后的文件
    # 2. 返回文件下载

    file_path = settings.UPLOAD_DIR / f"{file_id}_revised.docx"

    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=file_path,
        filename=f"revised_{file_id}.docx",
        media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    )
