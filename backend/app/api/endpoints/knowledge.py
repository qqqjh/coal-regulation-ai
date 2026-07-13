from fastapi import APIRouter, UploadFile, File, HTTPException, Depends, Form
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from typing import List
from app.services.vector_store import vector_store_service
from app.services.monitor_service import monitor_service
from app.models.schemas import DocumentInfo
from app.models.database import KnowledgeBase, Document
from app.db.database import get_db
import os
import uuid
from datetime import datetime
from app.core.config import settings
import time

router = APIRouter()


def _normalize_user_id(user_id: str | int | None) -> str:
    value = str(user_id or "guest").strip()
    return value[:100] or "guest"


def _is_admin(user_id: str | int | None = None, role: str | None = None) -> bool:
    return role == "admin" or _normalize_user_id(user_id) in {"admin", "1"}


def _serialize_kb(kb: KnowledgeBase) -> dict:
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "created_at": kb.created_at.isoformat() + 'Z',
        "document_count": len(kb.documents),
        "total_size": sum(doc.file_size for doc in kb.documents if doc.file_size),
        "updated_at": max(
            (doc.indexed_time or doc.upload_time for doc in kb.documents if doc.indexed_time or doc.upload_time),
            default=kb.created_at
        ).isoformat() + 'Z',
        "owner_user_id": kb.owner_user_id,
        "owner_name": kb.owner_name,
        "owner_role": kb.owner_role,
        "visibility": kb.visibility,
    }


async def _get_visible_kb(
    db: AsyncSession,
    kb_id: int,
    user_id: str | int | None = None,
    role: str | None = None,
) -> KnowledgeBase:
    result = await db.execute(
        select(KnowledgeBase)
        .options(selectinload(KnowledgeBase.documents))
        .where(KnowledgeBase.id == kb_id)
    )
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if _is_admin(user_id, role):
        return kb
    if kb.owner_user_id != _normalize_user_id(user_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


async def _get_visible_doc(
    db: AsyncSession,
    doc_id: int,
    user_id: str | int | None = None,
    role: str | None = None,
) -> Document:
    result = await db.execute(select(Document).where(Document.id == doc_id))
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    await _get_visible_kb(db, doc.kb_id, user_id, role)
    return doc


@router.post("/bases")
async def create_knowledge_base(
    name: str = Form(...),
    description: str = Form(None),
    user_id: str = Form("guest"),
    role: str = Form("user"),
    user_name: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    """创建知识库"""
    owner_id = "admin" if _is_admin(user_id, role) else _normalize_user_id(user_id)
    kb = KnowledgeBase(
        name=name,
        description=description,
        owner_user_id=owner_id,
        owner_name=user_name or ("管理员" if owner_id == "admin" else owner_id),
        owner_role="admin" if _is_admin(user_id, role) else "user",
        visibility="private",
        created_at=datetime.utcnow()
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)

    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "owner_user_id": kb.owner_user_id,
        "created_at": kb.created_at.isoformat()
    }

@router.get("/bases")
async def list_knowledge_bases(
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db),
):
    """获取知识库列表"""
    stmt = (
        select(KnowledgeBase)
        .options(selectinload(KnowledgeBase.documents))
        .order_by(KnowledgeBase.created_at.desc())
    )
    if not _is_admin(user_id, role):
        stmt = stmt.where(KnowledgeBase.owner_user_id == _normalize_user_id(user_id))
    result = await db.execute(
        stmt
    )
    kbs = result.scalars().all()

    return [_serialize_kb(kb) for kb in kbs]

@router.get("/bases/{kb_id}")
async def get_knowledge_base(
    kb_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """获取知识库详情"""
    kb = await _get_visible_kb(db, kb_id, user_id, role)

    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "created_at": kb.created_at.isoformat(),
        "document_count": len(kb.documents),
        "owner_user_id": kb.owner_user_id,
    }

@router.put("/bases/{kb_id}")
async def update_knowledge_base(
    kb_id: int,
    name: str = Form(...),
    description: str = Form(...),
    user_id: str = Form("guest"),
    role: str = Form("user"),
    db: AsyncSession = Depends(get_db)
):
    """更新知识库信息"""
    kb = await _get_visible_kb(db, kb_id, user_id, role)

    kb.name = name
    kb.description = description
    await db.commit()
    await db.refresh(kb)

    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "created_at": kb.created_at.isoformat()
    }

@router.delete("/bases/{kb_id}")
async def delete_knowledge_base(
    kb_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """删除知识库"""
    kb = await _get_visible_kb(db, kb_id, user_id, role)

    # 1. 删除该知识库下所有文档的物理文件
    deleted_files = 0
    for doc in kb.documents:
        if os.path.exists(doc.file_path):
            try:
                os.remove(doc.file_path)
                deleted_files += 1
                print(f"已删除文件: {doc.file_path}")
            except Exception as e:
                print(f"删除文件 {doc.file_path} 时出错: {str(e)}")

    # 2. 删除该知识库的所有向量数据（删除整个collection）
    try:
        vector_store_service.delete_knowledge_base(kb_id)
        print(f"已删除知识库 {kb_id} 的向量collection")
    except Exception as e:
        print(f"删除向量collection时出错: {str(e)}")

    # 3. 删除数据库记录（会级联删除所有文档记录）
    await db.delete(kb)
    await db.commit()

    return {
        "message": "知识库已删除",
        "id": kb_id,
        "deleted_files": deleted_files,
        "deleted_documents": len(kb.documents)
    }

@router.post("/bases/{kb_id}/documents")
async def upload_document(
    kb_id: int,
    file: UploadFile = File(...),
    user_id: str = Form("guest"),
    role: str = Form("user"),
    db: AsyncSession = Depends(get_db)
):
    """上传文档到知识库"""
    start_time = time.time()

    try:
        # 检查知识库是否存在
        await _get_visible_kb(db, kb_id, user_id, role)

        # 处理文件名冲突：如果文件已存在，添加数字后缀
        original_filename = file.filename
        filename = original_filename
        file_path = settings.UPLOAD_DIR / filename
        counter = 1

        # 如果文件名已存在，添加 (1), (2) 等后缀
        while os.path.exists(file_path):
            name, ext = os.path.splitext(original_filename)
            filename = f"{name}({counter}){ext}"
            file_path = settings.UPLOAD_DIR / filename
            counter += 1

        # 先创建文档记录以获取 doc_id
        doc = Document(
            kb_id=kb_id,
            name=filename,  # 使用实际保存的文件名（可能带后缀）
            file_path=str(file_path),
            file_type=filename.split('.')[-1].lower(),
            file_size=0,  # 暂时设为0，处理后更新
            status="processing",
            upload_time=datetime.utcnow()
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)

        # 处理文件并向量化，传入 doc_id 和 kb_id
        result_msg = await vector_store_service.process_file(file, filename, doc.id, kb_id)

        # 获取文件大小并更新文档记录
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
        doc.file_size = file_size
        success = str(result_msg).startswith("Success")
        doc.status = "indexed" if success else "failed"
        doc.indexed_time = datetime.utcnow() if success else None
        await db.commit()
        await db.refresh(doc)

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="knowledge",
            operation="upload_document",
            status="success",
            latency=time.time() - start_time
        )

        return {
            "id": doc.id,
            "filename": filename,
            "status": doc.status,
            "message": result_msg
        }

    except HTTPException:
        raise
    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="knowledge",
            operation="upload_document",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/bases/{kb_id}/documents")
async def list_documents(
    kb_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """获取知识库的文档列表"""
    # 检查知识库是否存在
    await _get_visible_kb(db, kb_id, user_id, role)

    # 获取文档列表
    result = await db.execute(
        select(Document)
        .where(Document.kb_id == kb_id)
        .order_by(Document.upload_time.desc())
    )
    docs = result.scalars().all()

    return [
        {
            "id": doc.id,
            "name": doc.name,
            "type": doc.file_type,
            "size": doc.file_size,
            "status": doc.status,
            "uploadTime": doc.upload_time.isoformat(),
            "indexedTime": doc.indexed_time.isoformat() if doc.indexed_time else None
        }
        for doc in docs
    ]

@router.get("/document-content/{doc_id}")
async def get_document_content(
    doc_id: int,
    limit: int = 20,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """获取文档内容（分块）- 从向量数据库检索真实内容"""
    doc = await _get_visible_doc(db, doc_id, user_id, role)

    # 使用文档ID和知识库ID从向量数据库获取文档分块和总数
    chunks, total_chunks = vector_store_service.get_document_chunks(doc.id, doc.kb_id, limit=limit)

    return {
        "id": doc.id,
        "name": doc.name,
        "type": doc.file_type,
        "size": doc.file_size,
        "uploadTime": doc.upload_time.isoformat(),
        "chunks": chunks,
        "total_chunks": total_chunks,
        "displayed_chunks": len(chunks)
    }

@router.get("/documents/{doc_id}")
async def get_document(
    doc_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """获取文档详情"""
    doc = await _get_visible_doc(db, doc_id, user_id, role)

    return {
        "id": doc.id,
        "name": doc.name,
        "type": doc.file_type,
        "size": doc.file_size,
        "status": doc.status,
        "uploadTime": doc.upload_time.isoformat(),
        "indexedTime": doc.indexed_time.isoformat() if doc.indexed_time else None,
        "kb_id": doc.kb_id
    }

@router.get("/documents/{doc_id}/download")
async def download_document(
    doc_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """下载文档原文件"""
    doc = await _get_visible_doc(db, doc_id, user_id, role)

    if not os.path.exists(doc.file_path):
        raise HTTPException(status_code=404, detail="文件不存在")

    return FileResponse(
        path=doc.file_path,
        filename=doc.name,
        media_type='application/octet-stream'
    )

@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db)
):
    """删除文档"""
    doc = await _get_visible_doc(db, doc_id, user_id, role)

    # 1. 删除向量数据库中的数据（使用文档ID和知识库ID）
    deleted_count = 0
    try:
        deleted_count = vector_store_service.delete_document(doc.id, doc.kb_id)
        print(f"从向量数据库中删除了 {deleted_count} 个文档块")
    except Exception as e:
        print(f"删除向量数据时出错: {str(e)}")
        # 继续执行，不阻止文件和数据库记录的删除

    # 2. 删除物理文件
    if os.path.exists(doc.file_path):
        os.remove(doc.file_path)

    # 3. 删除数据库记录
    await db.delete(doc)
    await db.commit()

    return {"message": "文档已删除", "id": doc_id, "deleted_chunks": deleted_count}

@router.get("/list", response_model=List[DocumentInfo])
async def list_all_documents(
    user_id: str = "guest",
    role: str = "user",
    db: AsyncSession = Depends(get_db),
):
    """获取所有文档列表（兼容旧接口）"""
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .order_by(Document.upload_time.desc())
    )
    if not _is_admin(user_id, role):
        stmt = stmt.where(KnowledgeBase.owner_user_id == _normalize_user_id(user_id))
    result = await db.execute(stmt)
    docs = result.scalars().all()

    return [
        DocumentInfo(
            id=str(doc.id),
            name=doc.name,
            type=doc.file_type,
            size=doc.file_size,
            uploadTime=doc.upload_time,
            status=doc.status
        )
        for doc in docs
    ]

@router.post("/upload")
async def upload_document_simple(
    file: UploadFile = File(...),
    user_id: str = Form("guest"),
    role: str = Form("user"),
    user_name: str = Form(None),
    db: AsyncSession = Depends(get_db)
):
    """上传文档（简化版，自动创建默认知识库）"""
    start_time = time.time()

    try:
        # 查找或创建默认知识库
        owner_id = "admin" if _is_admin(user_id, role) else _normalize_user_id(user_id)
        result = await db.execute(
            select(KnowledgeBase).where(
                KnowledgeBase.name == "默认知识库",
                KnowledgeBase.owner_user_id == owner_id,
            )
        )
        kb = result.scalar_one_or_none()

        if not kb:
            kb = KnowledgeBase(
                name="默认知识库",
                description="自动创建的默认知识库",
                owner_user_id=owner_id,
                owner_name=user_name or ("管理员" if owner_id == "admin" else owner_id),
                owner_role="admin" if _is_admin(user_id, role) else "user",
                visibility="private",
                created_at=datetime.utcnow()
            )
            db.add(kb)
            await db.commit()
            await db.refresh(kb)

        # 先创建文档记录，确保向量库元数据有 doc_id/kb_id
        file_id = str(uuid.uuid4())
        filename = f"{file_id}_{file.filename}"
        file_path = settings.UPLOAD_DIR / filename
        doc = Document(
            kb_id=kb.id,
            name=file.filename,
            file_path=str(file_path),
            file_type=file.filename.split('.')[-1].lower(),
            file_size=0,
            status="processing",
            upload_time=datetime.utcnow()
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)

        result_msg = await vector_store_service.process_file(
            file,
            filename,
            doc.id,
            kb.id,
            original_filename=file.filename,
        )

        # 获取文件大小
        file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

        # 更新文档记录
        success = str(result_msg).startswith("Success")
        doc.file_size = file_size
        doc.status = "indexed" if success else "failed"
        doc.indexed_time = datetime.utcnow() if success else None
        await db.commit()

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="knowledge",
            operation="upload_document",
            status="success",
            latency=time.time() - start_time
        )

        return {
            "filename": file.filename,
            "status": result_msg
        }

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="knowledge",
            operation="upload_document",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))
