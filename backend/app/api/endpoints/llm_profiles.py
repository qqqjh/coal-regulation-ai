from __future__ import annotations

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.auth_token import require_identity
from app.services.llm_profile_service import llm_profile_service


router = APIRouter()


class LLMProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str = Field(min_length=8, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=1000)


def _raise_profile_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("")
async def list_profiles(identity: Dict[str, Any] = Depends(require_identity)):
    try:
        profiles = llm_profile_service.list_profiles(identity["sub"])
        active = next((profile for profile in profiles if profile.get("is_active")), None)
        return {"profiles": profiles, "active_profile": active}
    except Exception as exc:
        _raise_profile_error(exc)


@router.post("")
async def create_profile(
    payload: LLMProfilePayload,
    identity: Dict[str, Any] = Depends(require_identity),
):
    try:
        profile = llm_profile_service.save_profile(
            user_id=identity["sub"],
            name=payload.name,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
        )
        return {"ok": True, "profile": profile}
    except Exception as exc:
        _raise_profile_error(exc)


@router.put("/{profile_id}")
async def update_profile(
    profile_id: str,
    payload: LLMProfilePayload,
    identity: Dict[str, Any] = Depends(require_identity),
):
    try:
        profile = llm_profile_service.save_profile(
            user_id=identity["sub"],
            profile_id=profile_id,
            name=payload.name,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
        )
        return {"ok": True, "profile": profile}
    except Exception as exc:
        _raise_profile_error(exc)


@router.put("/{profile_id}/activate")
async def activate_profile(
    profile_id: str,
    identity: Dict[str, Any] = Depends(require_identity),
):
    try:
        profile = llm_profile_service.activate_profile(identity["sub"], profile_id)
        return {"ok": True, "active_profile": profile}
    except Exception as exc:
        _raise_profile_error(exc)


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: str,
    identity: Dict[str, Any] = Depends(require_identity),
):
    try:
        llm_profile_service.delete_profile(identity["sub"], profile_id)
        return {"ok": True, "deleted": profile_id}
    except Exception as exc:
        _raise_profile_error(exc)


@router.post("/{profile_id}/test")
async def test_profile(
    profile_id: str,
    identity: Dict[str, Any] = Depends(require_identity),
):
    try:
        result = await asyncio.to_thread(
            llm_profile_service.test_profile, identity["sub"], profile_id
        )
        return result
    except Exception as exc:
        _raise_profile_error(exc)
