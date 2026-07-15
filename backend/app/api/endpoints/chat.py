from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from app.models.schemas import ChatRequest
from app.services.chat_service import chat_service
from app.services.monitor_service import monitor_service
from app.models.database import KnowledgeBase, Session as ChatSession, Message
from app.db.database import get_db
from datetime import datetime
import time
import json

router = APIRouter()


def _normalize_user_id(user_id: str | int | None) -> str:
    value = str(user_id or "guest").strip()
    return value[:100] or "guest"


def _is_admin(user_id: str | int | None = None, role: str | None = None) -> bool:
    return role == "admin" or _normalize_user_id(user_id) in {"admin", "1"}


async def _assert_kb_visible(db: AsyncSession, kb_id: int | None, user_id: str | None, role: str | None):
    if not kb_id:
        return
    result = await db.execute(select(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    kb = result.scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not _is_admin(user_id, role) and kb.owner_user_id != _normalize_user_id(user_id):
        raise HTTPException(status_code=404, detail="知识库不存在")

@router.post("/completions")
async def chat_completions(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db)
):
    """聊天补全（流式）"""
    start_time = time.time()

    if request.useRAG and request.kbId:
        await _assert_kb_visible(db, request.kbId, request.userId, request.role)

    # 将 Pydantic models 转换为 dict 用于 service
    msgs = [{"role": m.role, "content": m.content} for m in request.messages]

    async def generate_with_logging():
        total_content = ""
        token_count = 0

        try:
            async for chunk in chat_service.chat_stream(msgs, request.useRAG, request.kbId, request.model):
                total_content += chunk
                token_count += len(chunk) // 4  # 粗略估算
                yield chunk

            # 记录监控日志
            await monitor_service.log_operation(
                db=db,
                module="chat",
                operation="completions",
                status="success",
                tokens=token_count,
                latency=time.time() - start_time
            )

        except Exception as e:
            await monitor_service.log_operation(
                db=db,
                module="chat",
                operation="completions",
                status="failed",
                latency=time.time() - start_time,
                error_msg=str(e)
            )
            yield f"Error: {str(e)}"

    return StreamingResponse(
        generate_with_logging(),
        media_type="text/event-stream"
    )

@router.post("/sessions")
async def create_session(
    name: str,
    user_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """创建新会话"""
    session = ChatSession(
        name=name,
        user_id=user_id,
        created_at=datetime.utcnow()
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return {
        "id": session.id,
        "name": session.name,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat()
    }

@router.get("/sessions")
async def list_sessions(
    user_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """获取会话列表"""
    query = select(ChatSession).order_by(ChatSession.updated_at.desc())

    if user_id:
        query = query.where(ChatSession.user_id == user_id)

    result = await db.execute(query)
    sessions = result.scalars().all()

    return [
        {
            "id": s.id,
            "name": s.name,
            "created_at": s.created_at.isoformat(),
            "updated_at": s.updated_at.isoformat()
        }
        for s in sessions
    ]

@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    db: AsyncSession = Depends(get_db)
):
    """获取会话详情"""
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    return {
        "id": session.id,
        "name": session.name,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat()
    }

@router.put("/sessions/{session_id}")
async def update_session(
    session_id: int,
    name: str,
    db: AsyncSession = Depends(get_db)
):
    """更新会话（重命名）"""
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    session.name = name
    session.updated_at = datetime.utcnow()
    await db.commit()

    return {"message": "会话已更新", "id": session.id, "name": session.name}

@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: int,
    db: AsyncSession = Depends(get_db)
):
    """删除会话"""
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    await db.delete(session)
    await db.commit()

    return {"message": "会话已删除", "id": session_id}

@router.post("/sessions/{session_id}/messages")
async def add_message(
    session_id: int,
    role: str,
    content: str,
    db: AsyncSession = Depends(get_db)
):
    """添加消息到会话"""
    # 检查会话是否存在
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 创建消息
    message = Message(
        session_id=session_id,
        role=role,
        content=content,
        timestamp=datetime.utcnow(),
        tokens=len(content) // 4  # 粗略估算
    )
    db.add(message)

    # 更新会话时间
    session.updated_at = datetime.utcnow()

    await db.commit()
    await db.refresh(message)

    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "timestamp": message.timestamp.isoformat()
    }

@router.get("/sessions/{session_id}/messages")
async def get_messages(
    session_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db)
):
    """获取会话的消息历史"""
    # 检查会话是否存在
    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_id)
    )
    session = result.scalar_one_or_none()

    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 获取消息
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.timestamp.asc())
        .limit(limit)
    )
    messages = result.scalars().all()

    return [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "timestamp": m.timestamp.isoformat()
        }
        for m in messages
    ]

@router.delete("/sessions/{session_id}/messages")
async def clear_messages(
    session_id: int,
    db: AsyncSession = Depends(get_db)
):
    """清空会话的消息"""
    await db.execute(
        delete(Message).where(Message.session_id == session_id)
    )
    await db.commit()

    return {"message": "消息已清空", "session_id": session_id}
