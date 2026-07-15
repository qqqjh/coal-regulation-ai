import json
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


SERVICE_DIR = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.getenv("BGE_MODEL_ROOT", "/root/autodl-tmp/models"))
EMBEDDING_MODEL = Path(os.getenv("BGE_EMBEDDING_MODEL", str(MODEL_ROOT / "bge-m3")))
RERANKER_MODEL = Path(os.getenv("BGE_RERANKER_MODEL", str(MODEL_ROOT / "bge-reranker-v2-m3")))
INDEX_ROOT = Path(os.getenv("BGE_INDEX_ROOT", str(SERVICE_DIR / "indexes")))
DEVICE = os.getenv("BGE_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
MAX_LENGTH = int(os.getenv("BGE_MAX_LENGTH", "1024"))
BATCH_SIZE = int(os.getenv("BGE_BATCH_SIZE", "8"))


class Document(BaseModel):
    id: str
    text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IndexRequest(BaseModel):
    documents: list[Document] = Field(min_length=1)


class EmbedRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=128)
    return_dense: bool = True
    return_sparse: bool = False


class RerankRequest(BaseModel):
    query: str = Field(min_length=1)
    documents: list[Document] = Field(min_length=1, max_length=200)
    top_k: int = Field(default=10, ge=1, le=200)


class SearchRequest(BaseModel):
    index: str
    query: str = Field(min_length=1)
    recall_k: int = Field(default=20, ge=1, le=200)
    top_k: int = Field(default=5, ge=1, le=100)


class BGEModels:
    def __init__(self) -> None:
        self.embedder = None
        self.reranker = None
        self.lock = threading.Lock()

    def load(self) -> None:
        if not EMBEDDING_MODEL.is_dir():
            raise RuntimeError(f"Embedding model not found: {EMBEDDING_MODEL}")
        if not RERANKER_MODEL.is_dir():
            raise RuntimeError(f"Reranker model not found: {RERANKER_MODEL}")

        from FlagEmbedding import BGEM3FlagModel, FlagReranker

        use_fp16 = DEVICE.startswith("cuda")
        self.embedder = BGEM3FlagModel(
            str(EMBEDDING_MODEL),
            normalize_embeddings=True,
            use_fp16=use_fp16,
            device=DEVICE,
        )
        self.reranker = FlagReranker(str(RERANKER_MODEL), use_fp16=use_fp16)

    def encode(self, texts: list[str]) -> tuple[np.ndarray, list[dict[str, float]]]:
        with self.lock:
            result = self.embedder.encode(
                texts,
                batch_size=BATCH_SIZE,
                max_length=MAX_LENGTH,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
        dense = np.asarray(result["dense_vecs"], dtype=np.float32)
        sparse = [
            {str(token_id): float(weight) for token_id, weight in weights.items()}
            for weights in result["lexical_weights"]
        ]
        return dense, sparse

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        pairs = [[query, text] for text in documents]
        with self.lock:
            scores = self.reranker.compute_score(
                pairs,
                batch_size=BATCH_SIZE,
                max_length=MAX_LENGTH,
                normalize=True,
            )
        if not isinstance(scores, (list, tuple, np.ndarray)):
            scores = [scores]
        return [float(score) for score in scores]


class SearchIndex:
    def __init__(
        self,
        name: str,
        documents: list[dict[str, Any]],
        dense: np.ndarray,
        sparse: list[dict[str, float]],
    ) -> None:
        self.name = name
        self.documents = documents
        self.dense = np.asarray(dense, dtype=np.float32)
        self.sparse = sparse
        self.postings: dict[str, list[tuple[int, float]]] = {}
        for doc_index, weights in enumerate(sparse):
            for token_id, weight in weights.items():
                self.postings.setdefault(str(token_id), []).append((doc_index, float(weight)))

    def save(self) -> None:
        directory = INDEX_ROOT / self.name
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / "dense.npy", self.dense)
        (directory / "documents.json").write_text(
            json.dumps(self.documents, ensure_ascii=False), encoding="utf-8"
        )
        (directory / "sparse.json").write_text(
            json.dumps(self.sparse, ensure_ascii=False), encoding="utf-8"
        )

    @classmethod
    def load(cls, name: str) -> "SearchIndex":
        directory = INDEX_ROOT / name
        try:
            dense = np.load(directory / "dense.npy")
            documents = json.loads((directory / "documents.json").read_text(encoding="utf-8"))
            sparse = json.loads((directory / "sparse.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(name) from exc
        return cls(name, documents, dense, sparse)


models = BGEModels()
indexes: dict[str, SearchIndex] = {}


def validate_index_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise HTTPException(400, "index name may only contain letters, numbers, dot, underscore and hyphen")
    return name


def top_indices(scores: np.ndarray, limit: int) -> list[int]:
    if scores.size == 0:
        return []
    limit = min(limit, scores.size)
    if limit == scores.size:
        candidates = np.arange(scores.size)
    else:
        candidates = np.argpartition(scores, -limit)[-limit:]
    return candidates[np.argsort(scores[candidates])[::-1]].tolist()


def get_index(name: str) -> SearchIndex:
    validate_index_name(name)
    if name not in indexes:
        try:
            indexes[name] = SearchIndex.load(name)
        except KeyError as exc:
            raise HTTPException(404, f"index not found: {name}") from exc
    return indexes[name]


app = FastAPI(
    title="BGE-M3 Retrieval and Reranking Service",
    version="1.0.0",
    description="Dense+sparse recall with BGE-M3, followed by bge-reranker-v2-m3.",
)


@app.on_event("startup")
def startup() -> None:
    INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    models.load()


@app.get("/health")
def health() -> dict[str, Any]:
    gpu = None
    if torch.cuda.is_available():
        gpu = {
            "name": torch.cuda.get_device_name(0),
            "allocated_mb": round(torch.cuda.memory_allocated(0) / 1024**2, 1),
        }
    return {
        "status": "ready",
        "device": DEVICE,
        "embedding_model": str(EMBEDDING_MODEL),
        "reranker_model": str(RERANKER_MODEL),
        "indexes": sorted(indexes),
        "gpu": gpu,
    }


@app.post("/v1/embed")
def embed(request: EmbedRequest) -> dict[str, Any]:
    dense, sparse = models.encode(request.texts)
    response: dict[str, Any] = {"count": len(request.texts), "dimension": int(dense.shape[1])}
    if request.return_dense:
        response["dense"] = dense.tolist()
    if request.return_sparse:
        response["sparse"] = sparse
    return response


@app.post("/v1/rerank")
def rerank(request: RerankRequest) -> dict[str, Any]:
    scores = models.rerank(request.query, [document.text for document in request.documents])
    ranked = sorted(
        zip(request.documents, scores), key=lambda item: item[1], reverse=True
    )[: min(request.top_k, len(request.documents))]
    return {
        "query": request.query,
        "results": [
            {"rank": rank, "score": score, "document": document.model_dump()}
            for rank, (document, score) in enumerate(ranked, start=1)
        ],
    }


@app.put("/v1/indexes/{name}")
def create_index(name: str, request: IndexRequest) -> dict[str, Any]:
    validate_index_name(name)
    ids = [document.id for document in request.documents]
    if len(set(ids)) != len(ids):
        raise HTTPException(400, "document ids must be unique within an index")
    dense, sparse = models.encode([document.text for document in request.documents])
    index = SearchIndex(
        name,
        [document.model_dump() for document in request.documents],
        dense,
        sparse,
    )
    index.save()
    indexes[name] = index
    return {"index": name, "documents": len(index.documents), "dimension": int(index.dense.shape[1])}


@app.get("/v1/indexes")
def list_indexes() -> dict[str, Any]:
    names = {path.name for path in INDEX_ROOT.iterdir() if path.is_dir()}
    names.update(indexes)
    return {"indexes": sorted(names)}


@app.post("/v1/search")
def search(request: SearchRequest) -> dict[str, Any]:
    index = get_index(request.index)
    query_dense_batch, query_sparse_batch = models.encode([request.query])
    query_dense = query_dense_batch[0]
    query_sparse = query_sparse_batch[0]

    dense_scores = index.dense @ query_dense
    sparse_scores = np.zeros(len(index.documents), dtype=np.float32)
    for token_id, query_weight in query_sparse.items():
        for doc_index, doc_weight in index.postings.get(str(token_id), ()):
            sparse_scores[doc_index] += float(query_weight) * doc_weight

    recall_k = min(request.recall_k, len(index.documents))
    dense_ranked = top_indices(dense_scores, recall_k)
    sparse_ranked = top_indices(sparse_scores, recall_k)
    rrf_scores: dict[int, float] = {}
    for ranking in (dense_ranked, sparse_ranked):
        for rank, doc_index in enumerate(ranking, start=1):
            rrf_scores[doc_index] = rrf_scores.get(doc_index, 0.0) + 1.0 / (60 + rank)

    candidates = sorted(rrf_scores, key=rrf_scores.get, reverse=True)
    rerank_scores = models.rerank(
        request.query, [index.documents[doc_index]["text"] for doc_index in candidates]
    )
    ranked = sorted(
        zip(candidates, rerank_scores), key=lambda item: item[1], reverse=True
    )[: min(request.top_k, len(candidates))]

    return {
        "index": request.index,
        "query": request.query,
        "recall_candidates": len(candidates),
        "results": [
            {
                "rank": rank,
                "rerank_score": rerank_score,
                "dense_score": float(dense_scores[doc_index]),
                "sparse_score": float(sparse_scores[doc_index]),
                "rrf_score": float(rrf_scores[doc_index]),
                "document": index.documents[doc_index],
            }
            for rank, (doc_index, rerank_score) in enumerate(ranked, start=1)
        ],
    }
