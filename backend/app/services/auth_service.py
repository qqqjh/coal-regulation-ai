import hashlib
import secrets
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AppUser


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()


def _new_password_hash(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    return _hash_password(password, salt), salt


def serialize_user(user: AppUser) -> dict:
    return {
        "id": user.public_id,
        "username": user.username,
        "name": user.display_name,
        "role": user.role,
        "avatar": user.avatar,
    }


async def ensure_admin_user(db: AsyncSession) -> AppUser:
    result = await db.execute(select(AppUser).where(AppUser.username == "admin"))
    admin = result.scalar_one_or_none()
    if admin:
        return admin

    password_hash, salt = _new_password_hash("123456")
    admin = AppUser(
        public_id="admin",
        username="admin",
        display_name="管理员",
        role="admin",
        password_hash=password_hash,
        password_salt=salt,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def authenticate_user(db: AsyncSession, username: str, password: str) -> AppUser:
    await ensure_admin_user(db)
    normalized_username = (username or "").strip()
    result = await db.execute(select(AppUser).where(AppUser.username == normalized_username))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if user.password_hash != _hash_password(password or "", user.password_salt):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return user


async def register_user(
    db: AsyncSession,
    username: str,
    password: str,
    display_name: str | None = None,
) -> AppUser:
    await ensure_admin_user(db)
    normalized_username = (username or "").strip()
    if len(normalized_username) < 2:
        raise HTTPException(status_code=400, detail="用户名至少需要 2 个字符")
    if len(password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 个字符")
    if normalized_username.lower() == "admin":
        raise HTTPException(status_code=400, detail="admin 是系统管理员账号，不能注册")

    exists = await db.execute(select(AppUser).where(AppUser.username == normalized_username))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")

    public_id = normalized_username[:100]
    public_exists = await db.execute(select(AppUser).where(AppUser.public_id == public_id))
    if public_exists.scalar_one_or_none():
        public_id = f"user_{secrets.token_hex(4)}"

    password_hash, salt = _new_password_hash(password)
    user = AppUser(
        public_id=public_id,
        username=normalized_username,
        display_name=(display_name or normalized_username).strip()[:100],
        role="user",
        password_hash=password_hash,
        password_salt=salt,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user
