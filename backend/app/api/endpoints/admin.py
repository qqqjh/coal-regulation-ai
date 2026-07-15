from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.services import review_kb_permission_service as permission_service

router = APIRouter()


class ReviewKbPermissionItem(BaseModel):
    user_id: str
    user_name: str | None = None
    user_role: str | None = None
    kb_ids: list[int] = Field(default_factory=list)


class ReviewKbPermissionPayload(BaseModel):
    permissions: list[ReviewKbPermissionItem]


def require_admin(role: str) -> None:
    if role != "admin":
        raise HTTPException(status_code=403, detail="只有管理员可以访问审查知识库权限配置")


@router.get("/review-kb-permissions")
async def list_review_kb_permissions(
    role: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """管理员查看所有用户在审查时可使用的规程知识库。"""
    require_admin(role)
    users = await permission_service.list_configured_users(db)
    kbs = await permission_service.list_review_kbs(db)
    mapping = await permission_service.permission_map(db)
    all_kb_ids = [kb["id"] for kb in kbs]
    return {
        "users": [
            {
                **user,
                "configured": user["user_id"] in mapping,
                "kb_ids": mapping.get(user["user_id"], all_kb_ids),
            }
            for user in users
        ],
        "knowledge_bases": kbs,
    }


@router.put("/review-kb-permissions")
async def update_review_kb_permissions(
    payload: ReviewKbPermissionPayload,
    role: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """管理员保存用户审查知识库权限。保存后该用户严格按列表限制。"""
    require_admin(role)
    for item in payload.permissions:
        await permission_service.set_user_permissions(
            db,
            user_id=item.user_id,
            user_name=item.user_name,
            user_role=item.user_role,
            kb_ids=item.kb_ids,
        )
    await db.commit()
    return {"ok": True, "count": len(payload.permissions)}


@router.get("/review-kb-permissions/allowed")
async def list_allowed_review_kbs(
    user_id: str = "guest",
    db: AsyncSession = Depends(get_db),
):
    """当前用户读取自己可用于审查的规程知识库。"""
    kbs = await permission_service.list_allowed_review_kbs(db, user_id)
    return {
        "user_id": permission_service.normalize_user_id(user_id),
        "knowledge_bases": kbs,
        "kb_ids": [kb["id"] for kb in kbs],
    }
