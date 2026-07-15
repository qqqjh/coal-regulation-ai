"""Migrate legacy Chroma collections to the Milvus vector store.

Usage:
    python tools/migrate_chroma_to_milvus.py
    python tools/migrate_chroma_to_milvus.py --kb-id 3 --clear --probe-query "瓦斯报警浓度"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from langchain.schema import Document as LangChainDocument  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.vector_store import vector_store_service  # noqa: E402


def _iter_kb_collections(kb_id: Optional[int] = None) -> Iterable[tuple[int, str]]:
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("chromadb is required to read legacy Chroma data") from exc

    client = chromadb.PersistentClient(path=str(settings.CHROMA_DB_DIR))
    for collection in client.list_collections():
        name = collection.name
        match = re.fullmatch(r"kb_(\d+)", name)
        if not match:
            continue
        current_kb_id = int(match.group(1))
        if kb_id is not None and current_kb_id != kb_id:
            continue
        yield current_kb_id, name


def _load_chroma_documents(collection_name: str) -> Tuple[List[LangChainDocument], Optional[List[List[float]]]]:
    import chromadb

    client = chromadb.PersistentClient(path=str(settings.CHROMA_DB_DIR))
    collection = client.get_collection(collection_name)
    result = collection.get(include=["documents", "metadatas", "embeddings"])
    docs = result.get("documents") or []
    metadatas = result.get("metadatas") or []
    embeddings = result.get("embeddings")

    documents: List[LangChainDocument] = []
    vectors: List[List[float]] = []
    for i, (content, metadata) in enumerate(zip(docs, metadatas)):
        if not content:
            continue
        documents.append(LangChainDocument(page_content=content, metadata=metadata or {}))
        if embeddings is not None:
            vectors.append([float(x) for x in embeddings[i]])

    if embeddings is not None and len(vectors) == len(documents):
        return documents, vectors
    return documents, None


def migrate(kb_id: Optional[int], clear: bool, drop_collection: bool, probe_query: Optional[str]) -> int:
    total = 0
    collections = list(_iter_kb_collections(kb_id))
    if not collections:
        print("No legacy Chroma kb_* collections found.")
        return 0

    if drop_collection:
        client = vector_store_service._get_client()
        if client.has_collection(vector_store_service.collection_name):
            client.drop_collection(vector_store_service.collection_name)
            vector_store_service._collection_ready = False
            vector_store_service._collection_dim = None
            print(f"Dropped Milvus collection: {vector_store_service.collection_name}")

    for current_kb_id, collection_name in collections:
        docs, vectors = _load_chroma_documents(collection_name)
        if clear:
            deleted = vector_store_service.delete_knowledge_base(current_kb_id)
            print(f"[kb_{current_kb_id}] clear existing Milvus data: {deleted}")

        if vectors is not None:
            print(f"[kb_{current_kb_id}] using existing Chroma embeddings ({len(vectors)} vectors)")
        else:
            print(f"[kb_{current_kb_id}] Chroma embeddings unavailable; regenerating embeddings")
        added = vector_store_service.add_documents(docs, current_kb_id, vectors=vectors)
        total += added
        print(f"[kb_{current_kb_id}] {collection_name}: migrated {added}/{len(docs)} chunks")

        if probe_query:
            hits = vector_store_service.search(probe_query, kb_id=current_kb_id, k=3)
            print(f"[kb_{current_kb_id}] probe hits: {len(hits)}")
            for i, hit in enumerate(hits, 1):
                filename = hit.metadata.get("filename", "unknown")
                score = hit.metadata.get("score", 0)
                preview = hit.page_content.replace("\n", " ")[:120]
                print(f"  {i}. score={score:.4f} file={filename} text={preview}")

    print(f"Done. Total migrated chunks: {total}")
    return total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-id", type=int, default=None, help="Only migrate one kb_{id} collection")
    parser.add_argument("--clear", action="store_true", help="Delete matching Milvus data before importing")
    parser.add_argument("--drop-collection", action="store_true", help="Drop the whole Milvus collection before importing")
    parser.add_argument("--probe-query", default=None, help="Run a Top3 probe query after each KB migration")
    args = parser.parse_args()
    migrate(args.kb_id, args.clear, args.drop_collection, args.probe_query)


if __name__ == "__main__":
    main()
