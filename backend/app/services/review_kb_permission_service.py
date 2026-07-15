from datetime import datetime
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.database import (
    AppUser,
    Document,
    KnowledgeBase,
    ReviewKbPermission,
    ReviewKbUserAccess,
)


DEFAULT_REVIEW_USERS = [
    {"user_id": "admin", "user_name": "管理员", "user_role": "admin"},
]


def normalize_user_id(user_id: str | int | None) -> str:
    value = str(user_id or "guest").strip()
    return value[:100] or "guest"


def is_admin_review_user(user_id: str | int | None) -> bool:
    return normalize_user_id(user_id) in {"admin", "1"}


def serialize_kb(kb: KnowledgeBase) -> dict:
    docs = getattr(kb, "documents", []) or []
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "document_count": len(docs),
        "documents": [
            {
                "id": doc.id,
                "name": doc.name,
                "status": doc.status,
                "chunk_count": int(doc.chunk_count or 0),
            }
            for doc in sorted(docs, key=lambda item: (item.name or "", item.id))
        ],
        "updated_at": (kb.updated_at or kb.created_at).isoformat() if (kb.updated_at or kb.created_at) else None,
    }


async def list_review_kbs(db: AsyncSession) -> list[dict]:
    result = await db.execute(
        select(KnowledgeBase)
        .options(selectinload(KnowledgeBase.documents))
        .order_by(KnowledgeBase.created_at.desc())
    )
    return [serialize_kb(kb) for kb in result.scalars().all()]


async def list_configured_users(db: AsyncSession) -> list[dict]:
    users_by_id = {u["user_id"]: dict(u) for u in DEFAULT_REVIEW_USERS}
    user_result = await db.execute(select(AppUser).order_by(AppUser.created_at.asc()))
    for user in user_result.scalars().all():
        users_by_id[user.public_id] = {
            "user_id": user.public_id,
            "user_name": user.display_name or user.username,
            "user_role": user.role or "user",
        }
    result = await db.execute(select(ReviewKbUserAccess).order_by(ReviewKbUserAccess.user_id.asc()))
    rows = result.scalars().all()
    for row in rows:
        users_by_id[row.user_id] = {
            "user_id": row.user_id,
            "user_name": row.user_name or row.user_id,
            "user_role": row.user_role or "user",
        }
    return list(users_by_id.values())


async def permission_map(db: AsyncSession) -> dict[str, list[int]]:
    access_result = await db.execute(select(ReviewKbUserAccess.user_id))
    mapping: dict[str, list[int]] = {row[0]: [] for row in access_result.all()}
    result = await db.execute(select(ReviewKbPermission).order_by(ReviewKbPermission.user_id.asc()))
    for row in result.scalars().all():
        mapping.setdefault(row.user_id, []).append(int(row.kb_id))
    return mapping


async def set_user_permissions(
    db: AsyncSession,
    user_id: str | int,
    kb_ids: Iterable[int],
    user_name: str | None = None,
    user_role: str | None = None,
) -> None:
    normalized_user_id = normalize_user_id(user_id)
    access_result = await db.execute(
        select(ReviewKbUserAccess).where(ReviewKbUserAccess.user_id == normalized_user_id)
    )
    access = access_result.scalar_one_or_none()
    if access is None:
        access = ReviewKbUserAccess(user_id=normalized_user_id)
        db.add(access)
    access.user_name = (user_name or normalized_user_id)[:100]
    access.user_role = (user_role or "user")[:50]
    access.updated_at = datetime.utcnow()

    cleaned_kb_ids = set()
    for kb_id in kb_ids:
        if kb_id is None or not str(kb_id).strip():
            continue
        cleaned_kb_ids.add(int(kb_id))
    cleaned_kb_ids = sorted(cleaned_kb_ids)
    await db.execute(delete(ReviewKbPermission).where(ReviewKbPermission.user_id == normalized_user_id))
    for kb_id in cleaned_kb_ids:
        db.add(ReviewKbPermission(user_id=normalized_user_id, kb_id=kb_id))


async def get_allowed_kb_ids(db: AsyncSession, user_id: str | int) -> list[int] | None:
    normalized_user_id = normalize_user_id(user_id)
    if is_admin_review_user(normalized_user_id):
        return None
    configured = await db.execute(
        select(ReviewKbUserAccess).where(ReviewKbUserAccess.user_id == normalized_user_id)
    )
    if configured.scalar_one_or_none() is None:
        return None
    result = await db.execute(
        select(ReviewKbPermission.kb_id).where(ReviewKbPermission.user_id == normalized_user_id)
    )
    return [int(row[0]) for row in result.all()]


async def list_allowed_review_kbs(db: AsyncSession, user_id: str | int) -> list[dict]:
    allowed_ids = await get_allowed_kb_ids(db, user_id)
    kbs = await list_review_kbs(db)
    if allowed_ids is None:
        return kbs
    allowed_set = set(allowed_ids)
    return [kb for kb in kbs if kb["id"] in allowed_set]


async def assert_kb_allowed(db: AsyncSession, user_id: str | int, kb_id: int | None) -> None:
    if is_admin_review_user(user_id):
        return
    if kb_id is None:
        return
    allowed_ids = await get_allowed_kb_ids(db, user_id)
    if allowed_ids is None:
        return
    if int(kb_id) not in set(allowed_ids):
        raise HTTPException(status_code=403, detail="当前用户无权使用该规程知识库进行审查")


async def resolve_allowed_reference_documents(
    db: AsyncSession,
    user_id: str | int,
    document_ids: Iterable[int],
) -> list[dict]:
    """Validate a task-scoped regulation selection and return a stable snapshot."""
    normalized_ids: list[int] = []
    seen = set()
    for value in document_ids:
        try:
            doc_id = int(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="参考规程文档 ID 无效")
        if doc_id <= 0 or doc_id in seen:
            continue
        seen.add(doc_id)
        normalized_ids.append(doc_id)
    if not normalized_ids:
        return []

    result = await db.execute(
        select(Document, KnowledgeBase.name)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(Document.id.in_(normalized_ids))
    )
    rows = {int(doc.id): (doc, kb_name) for doc, kb_name in result.all()}
    missing = [doc_id for doc_id in normalized_ids if doc_id not in rows]
    if missing:
        raise HTTPException(status_code=404, detail=f"参考规程不存在：{missing}")

    allowed_ids = await get_allowed_kb_ids(db, user_id)
    allowed_set = None if allowed_ids is None else {int(value) for value in allowed_ids}
    snapshots = []
    for doc_id in normalized_ids:
        doc, kb_name = rows[doc_id]
        if allowed_set is not None and int(doc.kb_id) not in allowed_set:
            raise HTTPException(status_code=403, detail=f"当前用户无权使用参考规程：{doc.name}")
        if str(doc.status or "").lower() != "indexed":
            raise HTTPException(status_code=400, detail=f"参考规程尚未完成索引：{doc.name}")
        snapshots.append({
            "id": int(doc.id),
            "kb_id": int(doc.kb_id),
            "kb_name": kb_name,
            "name": doc.name,
            "chunk_count": int(doc.chunk_count or 0),
        })
    return snapshots
