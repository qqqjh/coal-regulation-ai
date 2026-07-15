import json
import os
import shutil
import sys
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from langchain.schema import Document as LangChainDocument
from langchain_community.document_loaders import TextLoader
from langchain_openai import OpenAIEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings

_project_root = str(settings.PROJECT_ROOT)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from rule_applicability import (  # noqa: E402
    APPLICABILITY_AUTO,
    APPLICABILITY_GENERAL,
    APPLICABILITY_OUTBURST_ONLY,
    ensure_rule_applicability,
    normalize_applicability_mode,
    with_rule_applicability,
)


class VectorStoreService:
    """Milvus-backed vector store for Web knowledge-base RAG.

    Milvus deployment modes share the same MilvusClient API. The only thing
    that changes is settings.MILVUS_URI.
    """

    OUTPUT_FIELDS = [
        "text",
        "tenant_id",
        "kb_id",
        "doc_id",
        "filename",
        "doc_name",
        "source_type",
        "visibility",
        "chunk_index",
        "page",
        "page_range",
        "chapter",
        "section",
        "chunking_strategy",
        "indexed_at",
        "metadata_json",
    ]

    def __init__(self):
        self.embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
        )
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200,
        )
        self._client = None
        self._collection_ready = False
        self._collection_dim: Optional[int] = None

    @property
    def collection_name(self) -> str:
        return f"{settings.MILVUS_COLLECTION_PREFIX}_chunks"

    def _get_client(self):
        if settings.VECTOR_BACKEND.lower() != "milvus":
            raise RuntimeError(f"Unsupported VECTOR_BACKEND: {settings.VECTOR_BACKEND}")

        if self._client is None:
            try:
                from pymilvus import MilvusClient
            except ImportError as exc:
                raise RuntimeError(
                    "Milvus backend is enabled but pymilvus is not installed. "
                    "Run: pip install 'pymilvus>=2.6.0,<3.0.0'"
                ) from exc

            uri = str(settings.MILVUS_URI)
            if uri.endswith(".db"):
                Path(uri).parent.mkdir(parents=True, exist_ok=True)
            self._client = MilvusClient(uri=uri, token=settings.MILVUS_TOKEN or None)
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
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("tenant_id", DataType.VARCHAR, max_length=64)
        schema.add_field("kb_id", DataType.INT64)
        schema.add_field("doc_id", DataType.INT64)
        schema.add_field("filename", DataType.VARCHAR, max_length=512)
        schema.add_field("doc_name", DataType.VARCHAR, max_length=512)
        schema.add_field("source_type", DataType.VARCHAR, max_length=64)
        schema.add_field("visibility", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_index", DataType.INT64)
        schema.add_field("page", DataType.INT64)
        schema.add_field("page_range", DataType.VARCHAR, max_length=128)
        schema.add_field("chapter", DataType.VARCHAR, max_length=512)
        schema.add_field("section", DataType.VARCHAR, max_length=512)
        schema.add_field("chunking_strategy", DataType.VARCHAR, max_length=128)
        schema.add_field("indexed_at", DataType.VARCHAR, max_length=64)
        schema.add_field("metadata_json", DataType.VARCHAR, max_length=65535)

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

    def _embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        return self.embeddings.embed_documents(texts)

    def _embed_query(self, query: str) -> List[float]:
        return self.embeddings.embed_query(query)

    @staticmethod
    def _str(value, max_len: int = 512) -> str:
        if value is None:
            return ""
        return str(value)[:max_len]

    @staticmethod
    def _int(value, default: int = 0) -> int:
        try:
            if value is None or value == "":
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def _metadata_to_entity(
        self,
        *,
        text: str,
        vector: List[float],
        metadata: Dict,
        kb_id: int,
        fallback_chunk_index: int,
    ) -> Dict:
        doc_id = self._int(metadata.get("doc_id"), 0)
        chunk_index = self._int(
            metadata.get("chunk_index", metadata.get("chunk_id", fallback_chunk_index)),
            fallback_chunk_index,
        )
        tenant_id = self._str(metadata.get("tenant_id") or settings.DEFAULT_TENANT_ID, 64)
        filename = self._str(metadata.get("filename"), 512)
        indexed_at = datetime.utcnow().isoformat()
        content_hash = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()[:12]
        entity_id = f"{tenant_id}_{kb_id}_{doc_id}_{chunk_index}_{content_hash}"

        full_metadata = {
            **metadata,
            "tenant_id": tenant_id,
            "kb_id": int(kb_id),
            "doc_id": doc_id,
            "chunk_index": chunk_index,
            "filename": filename,
            "indexed_at": indexed_at,
        }

        return {
            "id": self._str(entity_id, 128),
            "vector": vector,
            "text": self._str(text, 65535),
            "tenant_id": tenant_id,
            "kb_id": int(kb_id),
            "doc_id": doc_id,
            "filename": filename,
            "doc_name": self._str(metadata.get("doc_name") or filename, 512),
            "source_type": self._str(metadata.get("source_type") or "regulation", 64),
            "visibility": self._str(metadata.get("visibility") or "internal", 64),
            "chunk_index": chunk_index,
            "page": self._int(metadata.get("page"), 0),
            "page_range": self._str(metadata.get("page_range"), 128),
            "chapter": self._str(metadata.get("chapter"), 512),
            "section": self._str(metadata.get("section"), 512),
            "chunking_strategy": self._str(metadata.get("chunking_strategy"), 128),
            "indexed_at": indexed_at,
            "metadata_json": self._str(
                json.dumps(full_metadata, ensure_ascii=False, default=str),
                65535,
            ),
        }

    def _build_filter(
        self,
        kb_id: int,
        *,
        doc_id: Optional[int] = None,
        user_scope: Optional[Dict] = None,
    ) -> Optional[str]:
        tenant_id = settings.DEFAULT_TENANT_ID
        allowed_doc_ids = None

        if user_scope:
            tenant_id = user_scope.get("tenant_id") or tenant_id
            allowed_kb_ids = user_scope.get("allowed_kb_ids")
            if allowed_kb_ids is not None and int(kb_id) not in {int(x) for x in allowed_kb_ids}:
                return None
            allowed_doc_ids = user_scope.get("allowed_doc_ids")

        parts = [
            f"tenant_id == {self._quote(tenant_id)}",
            f"kb_id == {int(kb_id)}",
        ]
        if doc_id is not None:
            parts.append(f"doc_id == {int(doc_id)}")
        elif allowed_doc_ids is not None:
            allowed = [int(x) for x in allowed_doc_ids]
            if not allowed:
                return None
            parts.append("doc_id in [" + ", ".join(str(x) for x in allowed) + "]")
        return " and ".join(parts)

    def _row_to_document(self, row: Dict, score: Optional[float] = None) -> LangChainDocument:
        entity = row.get("entity", row)
        metadata = {}
        raw_metadata = entity.get("metadata_json")
        if raw_metadata:
            try:
                metadata = json.loads(raw_metadata)
            except json.JSONDecodeError:
                metadata = {}

        for field in self.OUTPUT_FIELDS:
            if field in {"text", "metadata_json"}:
                continue
            if field in entity and field not in metadata:
                metadata[field] = entity[field]
        if score is not None:
            metadata["score"] = float(score)

        return LangChainDocument(
            page_content=entity.get("text") or "",
            metadata=metadata,
        )

    def _ensure_project_root_importable(self) -> None:
        root = str(settings.PROJECT_ROOT)
        if root not in sys.path:
            sys.path.insert(0, root)

    def add_documents(
        self,
        documents: Iterable[LangChainDocument],
        kb_id: int,
        vectors: Optional[List[List[float]]] = None,
    ) -> int:
        docs = [doc for doc in documents if doc.page_content and doc.page_content.strip()]
        if not docs:
            return 0

        texts = [doc.page_content.strip() for doc in docs]
        if vectors is None:
            vectors = self._embed_documents(texts)
        if len(vectors) != len(docs):
            raise ValueError(f"向量数量与文档数量不一致: vectors={len(vectors)}, docs={len(docs)}")
        self._ensure_collection(len(vectors[0]))

        entities = [
            self._metadata_to_entity(
                text=text,
                vector=vector,
                metadata=doc.metadata or {},
                kb_id=kb_id,
                fallback_chunk_index=i,
            )
            for i, (doc, text, vector) in enumerate(zip(docs, texts, vectors))
        ]

        client = self._get_client()
        client.insert(collection_name=self.collection_name, data=entities)
        try:
            client.flush(collection_name=self.collection_name)
        except Exception:
            pass
        return len(entities)

    def _add_rule_chunks_to_vector_store(
        self,
        chunks_by_doc: Dict[str, List[Dict]],
        *,
        filename: str,
        file_path: Path,
        doc_id: Optional[int],
        kb_id: Optional[int],
        mineru_json_path: str,
        applicability_mode: str = APPLICABILITY_AUTO,
    ) -> int:
        if kb_id is None:
            raise ValueError("规则文档向量化需要 kb_id")

        documents: List[LangChainDocument] = []
        for doc_name, chunks in chunks_by_doc.items():
            for i, chunk in enumerate(chunks):
                chunk = with_rule_applicability(
                    chunk, doc_name, applicability_mode
                )
                content = str(chunk.get("retrieval_text") or chunk.get("content") or "").strip()
                if not content:
                    continue
                metadata = {
                    "tenant_id": settings.DEFAULT_TENANT_ID,
                    "doc_id": int(doc_id or 0),
                    "kb_id": int(kb_id or 0),
                    "filename": filename,
                    "doc_name": doc_name,
                    "chunk_index": i,
                    "chunk_id": i,
                    "source_type": "regulation",
                    "visibility": "internal",
                    "part": chunk.get("part", ""),
                    "chapter": chunk.get("chapter", ""),
                    "section": chunk.get("section", ""),
                    "article": chunk.get("article", ""),
                    "page_range": chunk.get("page_range", ""),
                    "chunk_type": chunk.get("chunk_type", chunk.get("chunk_level", "content")),
                    "semantic_role": chunk.get("semantic_role", ""),
                    "canonical_rule_id": chunk.get("canonical_rule_id", ""),
                    "retrievable": bool(chunk.get("retrievable", True)),
                    "applicability": chunk["applicability"],
                    "applicability_mode": chunk["applicability_mode"],
                    "applicability_reason": chunk["applicability_reason"],
                    "applicability_matches": chunk["applicability_matches"],
                    "char_count": int(chunk.get("char_count") or len(content)),
                    "upload_time": str(os.path.getmtime(file_path)),
                    "mineru_json_path": mineru_json_path,
                    "chunking_strategy": "mineru_chapter_v6",
                }
                documents.append(LangChainDocument(page_content=content, metadata=metadata))

        return self.add_documents(documents, kb_id)

    def _process_rule_file_with_mineru(
        self,
        file_path: Path,
        filename: str,
        doc_id: Optional[int],
        kb_id: Optional[int],
        applicability_mode: str = APPLICABILITY_AUTO,
    ) -> str:
        self._ensure_project_root_importable()
        from chapter_based_chunking_v6 import chunk_rule_json_files_v6
        from mineru_adapter import convert_document_to_mineru_json

        result = convert_document_to_mineru_json(
            file_path,
            doc_kind="rule",
            api_url=settings.MINERU_API_URL,
            timeout=7200,
        )
        artifact_dir = (
            settings.DATA_DIR
            / "knowledge_chunks"
            / f"kb_{int(kb_id or 0)}"
            / f"doc_{int(doc_id or 0)}"
        )
        all_rule_chunks = chunk_rule_json_files_v6(
            json_files=[Path(result.project_json_path)],
            output_dir=artifact_dir,
        )
        doc_name = Path(result.project_json_path).stem.replace("MinerU_", "").split("__")[0]
        chunks_by_doc = {doc_name: all_rule_chunks.get(doc_name, [])}
        if not chunks_by_doc[doc_name]:
            raise ValueError(f"规则文档切分结果为空: {result.project_json_path}")
        added = self._add_rule_chunks_to_vector_store(
            chunks_by_doc,
            filename=filename,
            file_path=file_path,
            doc_id=doc_id,
            kb_id=kb_id,
            mineru_json_path=result.project_json_path,
            applicability_mode=applicability_mode,
        )
        return (
            f"Success: MinerU rule chunks indexed ({added}); "
            f"applicability={applicability_mode}"
        )

    async def process_file(
        self,
        file,
        filename: str,
        doc_id: Optional[int] = None,
        kb_id: Optional[int] = None,
        original_filename: Optional[str] = None,
        applicability_mode: str = APPLICABILITY_AUTO,
    ) -> str:
        if kb_id is None:
            raise ValueError("文档向量化需要 kb_id")

        applicability_mode = normalize_applicability_mode(applicability_mode)
        file_path = settings.UPLOAD_DIR / filename
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        ext = filename.split(".")[-1].lower()
        if ext in {"pdf", "docx", "doc"}:
            return self._process_rule_file_with_mineru(
                file_path,
                filename,
                doc_id,
                kb_id,
                applicability_mode=applicability_mode,
            )
        if ext != "txt":
            return "Unsupported file format"

        loader = TextLoader(str(file_path), encoding="utf-8")
        documents = loader.load()
        splits = self.text_splitter.split_documents(documents)
        for i, split in enumerate(splits):
            split.metadata["tenant_id"] = settings.DEFAULT_TENANT_ID
            split.metadata["doc_id"] = int(doc_id or 0)
            split.metadata["kb_id"] = int(kb_id)
            split.metadata["filename"] = original_filename or filename
            split.metadata["doc_name"] = original_filename or filename
            split.metadata["chunk_index"] = i
            split.metadata["source_type"] = "regulation"
            split.metadata["visibility"] = "internal"
            split.metadata["upload_time"] = str(os.path.getmtime(file_path))
            split.metadata["chunking_strategy"] = "text_recursive"
            tagged = with_rule_applicability(
                split.metadata,
                original_filename or filename,
                applicability_mode,
            )
            split.metadata.update(tagged)

        added = self.add_documents(splits, kb_id)
        return f"Success: text chunks indexed ({added})"

    def search(
        self,
        query: str,
        kb_id: int = None,
        k: int = 4,
        user_scope: Optional[Dict] = None,
    ) -> List[LangChainDocument]:
        if kb_id is None:
            kb_id = settings.DEFAULT_KB_ID
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return []

        expr = self._build_filter(kb_id, user_scope=user_scope)
        if expr is None:
            return []

        query_vector = self._embed_query(query)
        results = client.search(
            collection_name=self.collection_name,
            data=[query_vector],
            anns_field="vector",
            filter=expr,
            limit=k,
            output_fields=self.OUTPUT_FIELDS,
        )

        docs = []
        for hit in (results[0] if results else []):
            entity = hit.get("entity", {})
            if self._int(entity.get("kb_id")) != int(kb_id):
                continue
            docs.append(self._row_to_document(hit, score=hit.get("distance")))
        return docs

    def _query_all_rows(
        self,
        expr: str,
        *,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict]:
        """读取某个知识库的全部规则块，供 v10 本地 BGE 索引使用。"""
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return []
        fields = output_fields or ["id", *self.OUTPUT_FIELDS]
        iterator = None
        try:
            iterator = client.query_iterator(
                collection_name=self.collection_name,
                batch_size=1000,
                filter=expr,
                output_fields=fields,
            )
            rows: List[Dict] = []
            while True:
                batch = iterator.next()
                if not batch:
                    break
                rows.extend(batch)
            return rows
        except (AttributeError, TypeError, NotImplementedError):
            # Milvus Lite/旧版客户端可能没有 query_iterator。
            return client.query(
                collection_name=self.collection_name,
                filter=expr,
                output_fields=fields,
                limit=10000,
            )
        finally:
            if iterator is not None:
                try:
                    iterator.close()
                except Exception:
                    pass

    def get_knowledge_base_review_chunks(
        self,
        kb_id: int,
        *,
        doc_id: Optional[int] = None,
    ) -> List[Dict]:
        """按 kb_id 返回可直接交给 v10 审查引擎的规则块。"""
        expr = self._build_filter(kb_id, doc_id=doc_id)
        if expr is None:
            return []
        rows = self._query_all_rows(expr)
        chunks: List[Dict] = []
        for row in rows:
            document = self._row_to_document(row)
            metadata = ensure_rule_applicability(
                document.metadata,
                str(document.metadata.get("doc_name") or document.metadata.get("filename") or ""),
            )
            if metadata.get("retrievable", True) is False:
                continue
            chunks.append(
                {
                    **metadata,
                    "id": str(row.get("id") or ""),
                    "content": document.page_content,
                    "retrieval_text": document.page_content,
                    "doc_name": str(
                        metadata.get("doc_name") or metadata.get("filename") or ""
                    ),
                }
            )
        return sorted(
            chunks,
            key=lambda item: (
                self._int(item.get("doc_id")),
                self._int(item.get("chunk_index")),
            ),
        )

    def get_applicability_summary(
        self,
        kb_id: int,
        *,
        doc_id: Optional[int] = None,
    ) -> Dict[str, int]:
        chunks = self.get_knowledge_base_review_chunks(kb_id, doc_id=doc_id)
        general = sum(
            1 for chunk in chunks if chunk.get("applicability") == APPLICABILITY_GENERAL
        )
        outburst_only = sum(
            1
            for chunk in chunks
            if chunk.get("applicability") == APPLICABILITY_OUTBURST_ONLY
        )
        return {
            "total": len(chunks),
            "general": general,
            "outburst_only": outburst_only,
        }

    def get_applicability_summaries(self, kb_id: int) -> Dict[int, Dict[str, int]]:
        """一次读取整个知识库并按文档汇总，避免列表页逐文档扫描 Milvus。"""
        summaries: Dict[int, Dict[str, int]] = {}
        for chunk in self.get_knowledge_base_review_chunks(kb_id):
            doc_id = self._int(chunk.get("doc_id"))
            summary = summaries.setdefault(
                doc_id,
                {"total": 0, "general": 0, "outburst_only": 0},
            )
            summary["total"] += 1
            applicability = chunk.get("applicability")
            if applicability in {APPLICABILITY_GENERAL, APPLICABILITY_OUTBURST_ONLY}:
                summary[applicability] += 1
        return summaries

    def get_stats(self, kb_id: int = None):
        client = self._get_client()
        exists = client.has_collection(self.collection_name)
        return {
            "backend": "milvus",
            "uri": str(settings.MILVUS_URI),
            "collection_name": self.collection_name,
            "collection_exists": exists,
            "kb_id": kb_id,
        }

    def get_document_chunks(self, doc_id: int, kb_id: int, limit: int = 20):
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return [], 0

        expr = self._build_filter(kb_id, doc_id=doc_id)
        if expr is None:
            return [], 0

        rows = client.query(
            collection_name=self.collection_name,
            filter=expr,
            output_fields=self.OUTPUT_FIELDS,
            limit=max(int(limit), 1),
        )
        count_rows = client.query(
            collection_name=self.collection_name,
            filter=expr,
            output_fields=["id"],
            limit=10000,
        )
        rows = sorted(rows, key=lambda row: self._int(row.get("chunk_index")))
        chunks = []
        for i, row in enumerate(rows):
            metadata = self._row_to_document(row).metadata
            metadata = ensure_rule_applicability(
                metadata,
                str(metadata.get("doc_name") or metadata.get("filename") or ""),
            )
            chunks.append({
                "id": i + 1,
                "content": row.get("text") or "",
                "metadata": metadata,
                "page": metadata.get("page", i + 1),
            })
        return chunks, len(count_rows)

    def delete_document(self, doc_id: int, kb_id: int) -> int:
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return 0

        expr = self._build_filter(kb_id, doc_id=doc_id)
        if expr is None:
            return 0

        rows = client.query(
            collection_name=self.collection_name,
            filter=expr,
            output_fields=["id"],
            limit=10000,
        )
        ids = [row["id"] for row in rows]
        if not ids:
            return 0
        client.delete(collection_name=self.collection_name, ids=ids)
        try:
            client.flush(collection_name=self.collection_name)
        except Exception:
            pass
        return len(ids)

    def delete_knowledge_base(self, kb_id: int) -> bool:
        client = self._get_client()
        if not client.has_collection(self.collection_name):
            return True

        expr = self._build_filter(kb_id)
        if expr is None:
            return True
        client.delete(collection_name=self.collection_name, filter=expr)
        try:
            client.flush(collection_name=self.collection_name)
        except Exception:
            pass
        return True

    def _get_collection(self, kb_id: int):
        raise RuntimeError(
            "Chroma collection access has been removed. Use "
            "vector_store_service.search(..., kb_id=...) instead."
        )


vector_store_service = VectorStoreService()


def cleanup_orphan_segments():
    """Compatibility hook used by app startup.

    Chroma needed segment-folder cleanup. Milvus Standalone is service-managed,
    so there is nothing to remove here.
    """

    if settings.VECTOR_BACKEND.lower() == "milvus":
        print("Milvus backend enabled; skip Chroma orphan segment cleanup.")
        return
