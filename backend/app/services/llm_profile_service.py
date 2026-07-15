from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken
from openai import OpenAI

from app.core.config import settings
from app.services.mongo_service import mongo_service


SYSTEM_PROFILE_ID = "system-default"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LLMProfileService:
    """Encrypted, user-scoped OpenAI-compatible model profiles stored in MongoDB."""

    def _db(self):
        db = mongo_service._connect()
        if db is None:
            raise RuntimeError("MongoDB 不可用，无法管理模型配置")
        db.llm_profiles.create_index([("user_id", 1), ("updated_at", -1)])
        return db

    @staticmethod
    def _normalized_user(user_id: str) -> str:
        return str(user_id or "guest")[:100]

    @staticmethod
    def _validate_base_url(base_url: str) -> str:
        value = str(base_url or "").strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Base URL 必须是有效的 http:// 或 https:// 地址")
        if parsed.username or parsed.password:
            raise ValueError("Base URL 中不能包含用户名或密码")
        if parsed.query or parsed.fragment:
            raise ValueError("Base URL 中不能包含查询参数或锚点")
        return value

    @staticmethod
    def _validate_text(value: str, label: str, max_length: int) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{label}不能为空")
        if len(normalized) > max_length:
            raise ValueError(f"{label}不能超过 {max_length} 个字符")
        return normalized

    @staticmethod
    def _fernet() -> Fernet:
        key = str(settings.LLM_PROFILE_ENCRYPTION_KEY or "").strip()
        if not key:
            raise RuntimeError("服务器尚未配置 LLM_PROFILE_ENCRYPTION_KEY")
        try:
            return Fernet(key.encode("ascii"))
        except Exception as exc:
            raise RuntimeError("LLM_PROFILE_ENCRYPTION_KEY 格式无效") from exc

    def _encrypt_key(self, api_key: str) -> str:
        return self._fernet().encrypt(str(api_key or "").encode("utf-8")).decode("ascii")

    def _decrypt_key(self, encrypted: str) -> str:
        try:
            return self._fernet().decrypt(str(encrypted or "").encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError("模型 API Key 无法解密，请重新保存该配置") from exc

    @staticmethod
    def _masked_key(api_key: str) -> str:
        if not api_key:
            return "未设置（适用于无需鉴权的本地 API）"
        suffix = api_key[-4:] if len(api_key) >= 4 else api_key[-1:]
        return f"••••••••{suffix}"

    def _system_runtime_profile(self) -> Dict[str, Any]:
        api_key = settings.DASHSCOPE_API_KEY or settings.OPENAI_API_KEY
        return {
            "id": SYSTEM_PROFILE_ID,
            "name": "系统默认模型",
            "base_url": settings.DASHSCOPE_BASE_URL,
            "model": settings.DASHSCOPE_MODEL,
            "api_key": api_key,
            "is_system": True,
        }

    def _active_profile_id(self, user_id: str, db=None) -> str:
        # PyMongo Database deliberately rejects truth-value testing.  Keep an
        # explicitly supplied database instead of using ``or`` here.
        if db is None:
            db = self._db()
        doc = db.llm_model_settings.find_one({"_id": self._normalized_user(user_id)})
        return str((doc or {}).get("active_profile_id") or SYSTEM_PROFILE_ID)

    def _public_profile(self, runtime: Dict[str, Any], active_id: str) -> Dict[str, Any]:
        return {
            "id": runtime["id"],
            "name": runtime["name"],
            "base_url": runtime["base_url"],
            "model": runtime["model"],
            "api_key_masked": self._masked_key(runtime.get("api_key", "")),
            "has_api_key": bool(runtime.get("api_key")),
            "is_system": bool(runtime.get("is_system")),
            "is_active": runtime["id"] == active_id,
            "created_at": runtime.get("created_at", ""),
            "updated_at": runtime.get("updated_at", ""),
        }

    def list_profiles(self, user_id: str) -> List[Dict[str, Any]]:
        normalized_user = self._normalized_user(user_id)
        db = self._db()
        active_id = self._active_profile_id(normalized_user, db)
        profiles = [self._public_profile(self._system_runtime_profile(), active_id)]
        for doc in db.llm_profiles.find({"user_id": normalized_user}).sort("updated_at", -1):
            try:
                api_key = self._decrypt_key(doc.get("api_key_encrypted", ""))
            except Exception:
                api_key = ""
            runtime = {
                "id": str(doc["_id"]),
                "name": doc.get("name", ""),
                "base_url": doc.get("base_url", ""),
                "model": doc.get("model", ""),
                "api_key": api_key,
                "is_system": False,
                "created_at": doc.get("created_at", ""),
                "updated_at": doc.get("updated_at", ""),
            }
            profiles.append(self._public_profile(runtime, active_id))
        if not any(profile["is_active"] for profile in profiles):
            profiles[0]["is_active"] = True
        return profiles

    def save_profile(
        self,
        *,
        user_id: str,
        name: str,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        profile_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        normalized_user = self._normalized_user(user_id)
        normalized_name = self._validate_text(name, "配置名称", 80)
        normalized_url = self._validate_base_url(base_url)
        normalized_model = self._validate_text(model, "模型名称", 200)
        db = self._db()
        now = _now_iso()

        if profile_id:
            if profile_id == SYSTEM_PROFILE_ID:
                raise ValueError("系统默认模型不能编辑")
            existing = db.llm_profiles.find_one({"_id": profile_id, "user_id": normalized_user})
            if not existing:
                raise KeyError("模型配置不存在")
            encrypted_key = existing.get("api_key_encrypted", "")
            if api_key is not None and str(api_key).strip():
                encrypted_key = self._encrypt_key(str(api_key).strip())
            db.llm_profiles.update_one(
                {"_id": profile_id, "user_id": normalized_user},
                {"$set": {
                    "name": normalized_name,
                    "base_url": normalized_url,
                    "model": normalized_model,
                    "api_key_encrypted": encrypted_key,
                    "updated_at": now,
                }},
            )
            saved_id = profile_id
        else:
            saved_id = uuid.uuid4().hex[:16]
            db.llm_profiles.insert_one({
                "_id": saved_id,
                "user_id": normalized_user,
                "name": normalized_name,
                "base_url": normalized_url,
                "model": normalized_model,
                "api_key_encrypted": self._encrypt_key(str(api_key or "").strip()),
                "created_at": now,
                "updated_at": now,
            })

        return next(profile for profile in self.list_profiles(normalized_user) if profile["id"] == saved_id)

    def activate_profile(self, user_id: str, profile_id: str) -> Dict[str, Any]:
        normalized_user = self._normalized_user(user_id)
        db = self._db()
        if profile_id != SYSTEM_PROFILE_ID:
            exists = db.llm_profiles.find_one({"_id": profile_id, "user_id": normalized_user})
            if not exists:
                raise KeyError("模型配置不存在")
        db.llm_model_settings.update_one(
            {"_id": normalized_user},
            {"$set": {
                "user_id": normalized_user,
                "active_profile_id": profile_id,
                "updated_at": _now_iso(),
            }},
            upsert=True,
        )
        return self.get_active_public_profile(normalized_user)

    def delete_profile(self, user_id: str, profile_id: str) -> bool:
        if profile_id == SYSTEM_PROFILE_ID:
            raise ValueError("系统默认模型不能删除")
        normalized_user = self._normalized_user(user_id)
        db = self._db()
        result = db.llm_profiles.delete_one({"_id": profile_id, "user_id": normalized_user})
        if not result.deleted_count:
            raise KeyError("模型配置不存在")
        if self._active_profile_id(normalized_user, db) == profile_id:
            self.activate_profile(normalized_user, SYSTEM_PROFILE_ID)
        return True

    def get_active_runtime_profile(self, user_id: str) -> Dict[str, Any]:
        normalized_user = self._normalized_user(user_id)
        db = self._db()
        active_id = self._active_profile_id(normalized_user, db)
        if active_id == SYSTEM_PROFILE_ID:
            return self._system_runtime_profile()
        doc = db.llm_profiles.find_one({"_id": active_id, "user_id": normalized_user})
        if not doc:
            self.activate_profile(normalized_user, SYSTEM_PROFILE_ID)
            return self._system_runtime_profile()
        return {
            "id": str(doc["_id"]),
            "name": doc.get("name", ""),
            "base_url": doc.get("base_url", ""),
            "model": doc.get("model", ""),
            "api_key": self._decrypt_key(doc.get("api_key_encrypted", "")),
            "is_system": False,
        }

    def get_active_public_profile(self, user_id: str) -> Dict[str, Any]:
        runtime = self.get_active_runtime_profile(user_id)
        return self._public_profile(runtime, runtime["id"])

    def get_runtime_profile(self, user_id: str, profile_id: str) -> Dict[str, Any]:
        normalized_user = self._normalized_user(user_id)
        if profile_id == SYSTEM_PROFILE_ID:
            return self._system_runtime_profile()
        doc = self._db().llm_profiles.find_one({"_id": profile_id, "user_id": normalized_user})
        if not doc:
            raise KeyError("模型配置不存在")
        return {
            "id": str(doc["_id"]),
            "name": doc.get("name", ""),
            "base_url": doc.get("base_url", ""),
            "model": doc.get("model", ""),
            "api_key": self._decrypt_key(doc.get("api_key_encrypted", "")),
            "is_system": False,
        }

    def test_profile(self, user_id: str, profile_id: str) -> Dict[str, Any]:
        profile = self.get_runtime_profile(user_id, profile_id)
        started = time.perf_counter()
        client = OpenAI(
            api_key=profile.get("api_key") or "local-no-key",
            base_url=profile["base_url"],
            timeout=30,
            max_retries=0,
        )
        response = client.chat.completions.create(
            model=profile["model"],
            messages=[{"role": "user", "content": "Reply with OK."}],
            max_tokens=4,
            temperature=0,
        )
        content = (response.choices[0].message.content or "").strip()
        return {
            "ok": True,
            "profile_id": profile["id"],
            "model": profile["model"],
            "latency_seconds": round(time.perf_counter() - started, 2),
            "response_preview": content[:80],
        }


llm_profile_service = LLMProfileService()
