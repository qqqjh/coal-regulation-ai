"""MongoDB 长期业务数据镜像层。

当前 v9 worker 仍以 SQLite `review_queue_v9.db` 作为进程间队列；
MongoDB 先承接长期可追溯数据：审查任务、问题清单、人工反馈镜像。
这样可以渐进迁移，不阻塞现有前端和 worker。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from app.core.config import settings


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _loads_json(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class MongoService:
    def __init__(self):
        self._client = None
        self._db = None
        self._last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(settings.ENABLE_MONGODB)

    def _connect(self):
        if not self.enabled:
            self._last_error = "MongoDB disabled by ENABLE_MONGODB=false"
            return None
        if self._db is not None:
            return self._db
        try:
            from pymongo import ASCENDING, DESCENDING, MongoClient

            self._client = MongoClient(
                settings.MONGODB_URI,
                serverSelectionTimeoutMS=settings.MONGODB_SERVER_SELECTION_TIMEOUT_MS,
            )
            self._client.admin.command("ping")
            self._db = self._client[settings.MONGODB_DB]
            self._ensure_indexes(ASCENDING, DESCENDING)
            self._last_error = ""
            return self._db
        except Exception as exc:
            self._client = None
            self._db = None
            self._last_error = str(exc)
            return None

    def _ensure_indexes(self, asc, desc) -> None:
        db = self._db
        db.review_jobs.create_index([("user_id", asc), ("sqlite_id", desc)])
        db.review_jobs.create_index([("status", asc), ("updated_at", desc)])
        db.review_jobs.create_index([("doc_name", asc)])
        db.review_issues.create_index([("job_id", asc), ("issue_id", asc)], unique=True)
        db.review_issues.create_index([("job_id", asc), ("human_action", asc)])
        db.review_chunks.create_index([("job_id", asc), ("chunk_index", asc)], unique=True)
        db.review_chunks.create_index([("user_id", asc), ("job_id", asc)])
        db.review_chunks.create_index([("milvus_id", asc)], unique=True)
        db.audit_logs.create_index([("created_at", desc)])

    def health(self) -> Dict[str, Any]:
        db = self._connect()
        return {
            "enabled": self.enabled,
            "available": db is not None,
            "database": settings.MONGODB_DB,
            "error": self._last_error,
        }

    def upsert_review_job(self, job: Dict[str, Any]) -> bool:
        db = self._connect()
        if db is None or not job:
            return False
        job_id = job.get("job_id")
        if not job_id:
            return False
        doc = {
            "_id": job_id,
            "job_id": job_id,
            "sqlite_id": job.get("id"),
            "user_id": job.get("user_id") or "guest",
            "doc_name": job.get("doc_name", ""),
            "docx_path": job.get("docx_path", ""),
            "paragraphs_path": job.get("paragraphs_path", ""),
            "mine_type": job.get("mine_type", "non_outburst"),
            "status": job.get("status", ""),
            "progress": int(job.get("progress") or 0),
            "n_chunks": int(job.get("n_chunks") or 0),
            "n_done": int(job.get("n_done") or 0),
            "agent_stage": job.get("agent_stage", ""),
            "phase_done": int(job.get("phase_done") or 0),
            "phase_total": int(job.get("phase_total") or 0),
            "timings": _loads_json(job.get("timings"), {}),
            "agent_status": job.get("agent_status", ""),
            "error": job.get("error", ""),
            "issue_count": int(job.get("issue_count") or 0),
            "decided_count": int(job.get("decided_count") or 0),
            "created_at": job.get("created_at", ""),
            "updated_at": job.get("updated_at", ""),
            "mirrored_at": _now_iso(),
        }
        db.review_jobs.update_one(
            {"_id": job_id},
            {"$set": doc, "$setOnInsert": {"first_mirrored_at": _now_iso()}},
            upsert=True,
        )
        return True

    def upsert_review_jobs(self, jobs: Iterable[Dict[str, Any]]) -> int:
        count = 0
        for job in jobs:
            if self.upsert_review_job(job):
                count += 1
        return count

    def replace_review_issues(self, job_id: str, issues: List[Dict[str, Any]]) -> bool:
        db = self._connect()
        if db is None or not job_id:
            return False
        db.review_issues.delete_many({"job_id": job_id})
        docs = []
        for issue in issues:
            issue_id = issue.get("id")
            docs.append({
                "_id": f"{job_id}:{issue_id}",
                "job_id": job_id,
                "issue_id": issue_id,
                "chunk_index": issue.get("chunk_index"),
                "block_indices": issue.get("block_indices") or [],
                "issue_type": issue.get("issue_type", ""),
                "status": issue.get("status", ""),
                "title": issue.get("title", ""),
                "original_text": issue.get("original_text", ""),
                "suggestion": issue.get("suggestion", ""),
                "regulation": issue.get("regulation", ""),
                "reason": issue.get("reason", ""),
                "detail": issue.get("detail") or {},
                "escalation_type": issue.get("escalation_type", ""),
                "human_action": issue.get("human_action", "pending"),
                "human_text": issue.get("human_text", ""),
                "agent_applied": issue.get("agent_applied", 0),
                "agent_note": issue.get("agent_note", ""),
                "created_at": issue.get("created_at", ""),
                "updated_at": issue.get("updated_at", ""),
                "mirrored_at": _now_iso(),
            })
        if docs:
            db.review_issues.insert_many(docs, ordered=False)
        return True

    def replace_review_chunks(
        self,
        *,
        job: Dict[str, Any],
        vector_records: List[Dict[str, Any]],
    ) -> bool:
        db = self._connect()
        if db is None or not job:
            return False
        job_id = job.get("job_id")
        if not job_id:
            return False
        db.review_chunks.delete_many({"job_id": job_id})
        docs = []
        for record in vector_records:
            metadata = record.get("metadata") or {}
            chunk_index = int(record.get("chunk_index") or metadata.get("chunk_index") or 0)
            content = record.get("content") or ""
            docs.append({
                "_id": f"{job_id}:{chunk_index}",
                "job_id": job_id,
                "chunk_index": chunk_index,
                "milvus_id": record.get("milvus_id", ""),
                "user_id": job.get("user_id") or metadata.get("user_id") or "guest",
                "tenant_id": metadata.get("tenant_id") or "default",
                "org_id": metadata.get("org_id") or metadata.get("tenant_id") or "default",
                "doc_name": job.get("doc_name") or metadata.get("doc_name", ""),
                "mine_type": job.get("mine_type") or metadata.get("mine_type", "non_outburst"),
                "chapter": metadata.get("chapter", ""),
                "section": metadata.get("section", ""),
                "article": metadata.get("article", ""),
                "page_range": metadata.get("page_range", ""),
                "chunk_level": metadata.get("chunk_level", ""),
                "source_blocks": metadata.get("source_blocks") or [],
                "char_count": metadata.get("char_count") or len(content),
                "content": content,
                "indexed_at": metadata.get("indexed_at") or _now_iso(),
                "mirrored_at": _now_iso(),
            })
        if docs:
            db.review_chunks.insert_many(docs, ordered=False)
        return True

    def list_review_jobs(self, user_id: str = "guest", limit: int = 30) -> Optional[List[Dict[str, Any]]]:
        db = self._connect()
        if db is None:
            return None
        limit = max(1, min(int(limit), 200))
        cursor = (
            db.review_jobs
            .find({"user_id": user_id or "guest"}, {"_id": 0})
            .sort([("sqlite_id", -1), ("updated_at", -1)])
            .limit(limit)
        )
        return list(cursor)

    def delete_review_job(self, job_id: str) -> bool:
        db = self._connect()
        if db is None or not job_id:
            return False
        db.review_issues.delete_many({"job_id": job_id})
        db.review_chunks.delete_many({"job_id": job_id})
        db.review_jobs.delete_one({"job_id": job_id})
        return True


mongo_service = MongoService()
