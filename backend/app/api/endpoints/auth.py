from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.core.auth_token import create_access_token
from app.services.auth_service import authenticate_user, register_user, serialize_user

router = APIRouter()


class LoginPayload(BaseModel):
    username: str
    password: str


class RegisterPayload(BaseModel):
    username: str
    password: str
    display_name: str | None = None


@router.post("/login")
async def login(payload: LoginPayload, db: AsyncSession = Depends(get_db)):
    user = await authenticate_user(db, payload.username, payload.password)
    serialized = serialize_user(user)
    return {
        "user": serialized,
        "access_token": create_access_token(serialized["id"], serialized["role"]),
        "token_type": "bearer",
    }


@router.post("/register")
async def register(payload: RegisterPayload, db: AsyncSession = Depends(get_db)):
    user = await register_user(
        db,
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
    )
    serialized = serialize_user(user)
    return {
        "user": serialized,
        "access_token": create_access_token(serialized["id"], serialized["role"]),
        "token_type": "bearer",
    }
