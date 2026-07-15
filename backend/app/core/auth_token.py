from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Annotated, Any, Dict

from fastapi import Header, HTTPException

from app.core.config import settings


TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _secret() -> bytes:
    secret = (settings.APP_AUTH_SECRET or "").encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("APP_AUTH_SECRET must contain at least 32 characters")
    return secret


def create_access_token(user_id: str, role: str) -> str:
    payload = {
        "sub": str(user_id),
        "role": str(role or "user"),
        "iat": int(time.time()),
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{signature}"


def decode_access_token(token: str) -> Dict[str, Any]:
    try:
        body, signature = token.split(".", 1)
        expected = _b64encode(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature mismatch")
        payload = json.loads(_b64decode(body))
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("token expired")
        if not payload.get("sub"):
            raise ValueError("missing subject")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期，请重新登录") from exc


def require_identity(
    authorization: Annotated[str | None, Header()] = None,
) -> Dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="请先登录后再管理模型 API")
    return decode_access_token(authorization.split(" ", 1)[1].strip())
