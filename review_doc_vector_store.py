"""待审文档向量长期保存层。

v9 审查在 Phase A 已经生成待审 chunks 的 BGE dense 向量 `q_dense`。
本模块负责把这些向量写入独立 Milvus collection，供后续历史待审文档检索、
跨文档重复性审查和权限过滤使用。
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = PROJECT_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import settings  # noqa: E402


def _utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _str(value: Any, max_len: int = 512) -> str:
    if value is None:
        return ""
    return str(value)[:max_len]


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class ReviewDocVectorStore:
    OUTPUT_FIELDS = [
        "id",
        "text",
        "tenant_id",
        "user_id",
        "org_id",
        "job_id",
        "doc_name",
        "mine_type",
        "chunk_index",
        "page_range",
        "chapter",
        "section",
        "article",
        "chunk_level",
        "source_blocks_json",
        "metadata_json",
        "indexed_at",
    ]

    def __init__(self):
        self._client = None
        self._collection_ready = False
        self._collection_dim: Optional[int] = None

    @property
    def collection_name(self) -> str:
        return f"{settings.MILVUS_COLLECTION_PREFIX}_review_doc_chunks"

    def _get_client(self):
        if self._client is None:
            from pymilvus import MilvusClient

            self._client = MilvusClient(
                uri=str(settings.MILVUS_URI),
                token=settings.MILVUS_TOKEN or None,
            )
        return self._client

    def _ensure_collection(self, dim: int) -> None:
        client = self._get_client()
        if self._collection_ready and self._collection_dim == dim:
            return
        if client.has_collection(self.collection_name):
            self._collection_ready = True
            self._collection_dim = dim
            return

        from pymilvus import DataType, MilvusClient

        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=160)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
        schema.add_field("user_id", DataType.VARCHAR, max_length=128)
        schema.add_field("org_id", DataType.VARCHAR, max_length=128)
        schema.add_field("job_id", DataType.VARCHAR, max_length=128)
        schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
        schema.add_field("mine_type", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("page_range", DataType.VARCHAR, max_length=128)
        schema.add_field("chapter", DataType.VARCHAR, max_length=512)
        schema.add_field("section", DataType.VARCHAR, max_length=512)
        schema.add_field("article", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_level", DataType.VARCHAR, max_length=128)
        schema.add_field("source_blocks_json", DataType.VARCHAR, max_length=4096)
        schema.add_field("metadata_json", DataType.VARCHAR, max_length=65535)
        schema.add_field("indexed_at", DataType.VARCHAR, max_length=64)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        client.create_collection(
            collection_name=self.collection_name,
            schema=schema,
            index_params=index_params,
        )
        self._collection_ready = True
        self._collection_dim = dim

    def _entity_id(self, job_id: str, chunk_index: int, content: str) -> str:
        content_hash = hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return _str(f"{job_id}_{chunk_index}_{content_hash}", 160)

    def _entity(
        self,
        *,
        job: Dict[str, Any],
        chunk: Dict[str, Any],
        chunk_index: int,
        vector: List[float],
        indexed_at: str,
    ) -> Dict[str, Any]:
        content = str(chunk.get("content") or "").strip()
        job_id = str(job.get("job_id") or "")
        user_id = str(job.get("user_id") or "guest")
        tenant_id = str(job.get("tenant_id") or settings.DEFAULT_TENANT_ID)
        org_id = str(job.get("org_id") or tenant_id)
        source_blocks = chunk.get("source_blocks") or []
        metadata = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "org_id": org_id,
            "job_id": job_id,
            "doc_name": job.get("doc_name", ""),
            "mine_type": job.get("mine_type", "non_outburst"),
            "chunk_index": chunk_index,
            "chapter": chunk.get("chapter", ""),
            "section": chunk.get("section", ""),
            "article": chunk.get("article", ""),
            "page_range": chunk.get("page_range", ""),
            "chunk_level": chunk.get("chunk_level", ""),
            "source_blocks": source_blocks,
            "char_count": int(chunk.get("char_count") or len(content)),
            "indexed_at": indexed_at,
        }
        return {
            "id": self._entity_id(job_id, chunk_index, content),
            "vector": vector,
            "text": _str(content, 65535),
            "tenant_id": _str(tenant_id, 64),
            "user_id": _str(user_id, 128),
            "org_id": _str(org_id, 128),
            "job_id": _str(job_id, 128),
            "doc_name": _str(job.get("doc_name", ""), 512),
            "mine_type": _str(job.get("mine_type", "non_outburst"), 64),
            "chunk_index": int(chunk_index),
            "page_range": _str(chunk.get("page_range", ""), 128),
            "chapter": _str(chunk.get("chapter", ""), 512),
            "section": _str(chunk.get("section", ""), 512),
            "article": _str(chunk.get("article", ""), 512),
            "chunk_level": _str(chunk.get("chunk_level", ""), 128),
            "source_blocks_json": _str(json.dumps(source_blocks, ensure_ascii=False), 4096),
            "metadata_json": _str(json.dumps(metadata, ensure_ascii=False, default=str), 65535),
            "indexed_at": indexed_at,
        }

    def upsert_job_chunks(
        self,
        *,
        job: Dict[str, Any],
        chunks: List[Dict[str, Any]],
        dense_vecs: np.ndarray,
    ) -> List[Dict[str, Any]]:
        if dense_vecs is None:
            return []
        vectors = np.asarray(dense_vecs, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] < len(chunks):
            raise ValueError(f"待审向量维度异常: vectors={vectors.shape}, chunks={len(chunks)}")
        if not chunks:
            return []

        self._ensure_collection(int(vectors.shape[1]))
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise ValueError("job_id 为空，不能保存待审文档向量")

        client = self._get_client()
        if client.has_collection(self.collection_name):
            try:
                client.delete(
                    collection_name=self.collection_name,
                    filter=f"job_id == {_quote(job_id)}",
                )
            except Exception:
                pass

        indexed_at = _utc_now()
        entities: List[Dict[str, Any]] = []
        for i, chunk in enumerate(chunks):
            content = str(chunk.get("content") or "").strip()
            if not content:
                continue
            entities.append(
                self._entity(
                    job=job,
                    chunk=chunk,
                    chunk_index=i,
                    vector=[float(x) for x in vectors[i].tolist()],
                    indexed_at=indexed_at,
                )
            )

        if not entities:
            return []

        client.insert(collection_name=self.collection_name, data=entities)
        try:
            client.flush(collection_name=self.collection_name)
        except Exception:
            pass
        return [
            {
                "milvus_id": entity["id"],
                "job_id": entity["job_id"],
                "chunk_index": entity["chunk_index"],
                "content": entity["text"],
                "metadata": json.loads(entity["metadata_json"] or "{}"),
            }
            for entity in entities
        ]

    def count_job_chunks(self, job_id: str) -> int:
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return 0
        rows = client.query(
            collection_name=self.collection_name,
            filter=f"job_id == {_quote(job_id)}",
            output_fields=["id"],
            limit=10000,
        )
        return len(rows)

    def search_similar_chunks(
        self,
        *,
        job_id: str,
        chunk_index: int,
        user_id: str = "guest",
        top_k: int = 5,
        include_same_job: bool = False,
    ) -> List[Dict[str, Any]]:
        """用当前待审 chunk 向量检索历史待审 chunks。

        默认只在同一用户范围内查，并排除当前 job，避免把自己查出来。
        """
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return []

        current_rows = client.query(
            collection_name=self.collection_name,
            filter=f"job_id == {_quote(job_id)} and chunk_index == {int(chunk_index)}",
            output_fields=["id", "vector", "text", "metadata_json"],
            limit=1,
        )
        if not current_rows:
            return []
        current = current_rows[0]
        vector = current.get("vector")
        if vector is None:
            return []

        parts = []
        if user_id:
            parts.append(f"user_id == {_quote(user_id)}")
        if not include_same_job:
            parts.append(f"job_id != {_quote(job_id)}")
        expr = " and ".join(parts) if parts else None

        results = client.search(
            collection_name=self.collection_name,
            data=[vector],
            anns_field="vector",
            filter=expr,
            limit=max(1, min(int(top_k), 50)),
            output_fields=self.OUTPUT_FIELDS,
        )

        items: List[Dict[str, Any]] = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity", {})
            metadata = {}
            if entity.get("metadata_json"):
                try:
                    metadata = json.loads(entity.get("metadata_json") or "{}")
                except json.JSONDecodeError:
                    metadata = {}
            source_blocks = []
            if entity.get("source_blocks_json"):
                try:
                    source_blocks = json.loads(entity.get("source_blocks_json") or "[]")
                except json.JSONDecodeError:
                    source_blocks = []
            items.append({
                "milvus_id": entity.get("id", ""),
                "score": float(hit.get("distance", 0.0)),
                "job_id": entity.get("job_id", ""),
                "doc_name": entity.get("doc_name", ""),
                "chunk_index": int(entity.get("chunk_index") or 0),
                "content": entity.get("text", ""),
                "chapter": entity.get("chapter", ""),
                "section": entity.get("section", ""),
                "article": entity.get("article", ""),
                "page_range": entity.get("page_range", ""),
                "source_blocks": source_blocks,
                "metadata": metadata,
            })
        return items


review_doc_vector_store = ReviewDocVectorStore()
