"""Retrieve atomic claims with local BGE-M3 dense/sparse and BGE reranker."""

from __future__ import annotations

import argparse
import gc
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from retrieve_parent_chunks_local_bgem3_v1 import (
    build_sparse_postings,
    dense_search,
    flatten_kb,
    retrieval_text,
    rrf_hybrid,
    source_audit,
    sparse_search,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLAIMS = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_EMBEDDING_MODEL = PROJECT_ROOT / "models" / "bge-m3"
DEFAULT_RERANKER_MODEL = PROJECT_ROOT / "models" / "bge-reranker-v2-m3"
DEFAULT_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "atomic_retrieval_local_bgem3_grouped_v1.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def selected_claims(dataset: dict[str, Any], priorities: set[str]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (case, claim)
        for case in dataset["cases"]
        for claim in case["claims"]
        if claim.get("retrieval_priority", "medium") in priorities
    ]


def rrf_screen(
    dense: list[tuple[int, float]],
    sparse: list[tuple[int, float]],
    top_k: int,
) -> list[dict[str, Any]]:
    dense_details = {index: {"rank": rank, "score": score} for rank, (index, score) in enumerate(dense, 1)}
    sparse_details = {index: {"rank": rank, "score": score} for rank, (index, score) in enumerate(sparse, 1)}
    hybrid = rrf_hybrid(dense, sparse, top_k)
    rows = []
    for index, score in hybrid:
        details = {}
        if index in dense_details:
            details["dense"] = dense_details[index]
        if index in sparse_details:
            details["sparse"] = sparse_details[index]
        rows.append(
            {
                "document_index": index,
                "rrf_score": float(score),
                "retrieval_details": details,
            }
        )
    return rows


def group_dedup(
    rows: list[dict[str, Any]],
    kb_chunks: list[dict[str, Any]],
    chunk_to_group: dict[str, str],
    final_top_k: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    selected = []
    suppressed = []
    used_groups: dict[str, str] = {}
    for original_rank, row in enumerate(rows, 1):
        chunk = kb_chunks[row["document_index"]]
        group_id = chunk_to_group.get(chunk["chunk_id"], "")
        if group_id and group_id in used_groups:
            suppressed.append(
                {
                    "original_rank": original_rank,
                    "chunk_id": chunk["chunk_id"],
                    "evidence_group_id": group_id,
                    "kept_chunk_id": used_groups[group_id],
                }
            )
            continue
        row["evidence_group_id"] = group_id
        selected.append(row)
        if group_id:
            used_groups[group_id] = chunk["chunk_id"]
        if len(selected) >= final_top_k:
            break
    return selected, suppressed


def candidate_payload(row: dict[str, Any], chunk: dict[str, Any], final_rank: int) -> dict[str, Any]:
    return {
        "final_rank": final_rank,
        "chunk_id": chunk["chunk_id"],
        "doc_name": chunk.get("doc_name", ""),
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "article": chunk.get("article", ""),
        "page_range": chunk.get("page_range", ""),
        "content": chunk.get("content", ""),
        "retrieval_text": retrieval_text(chunk),
        "evidence_group_id": row.get("evidence_group_id", ""),
        "rrf_score": row["rrf_score"],
        "rerank_score": row["rerank_score"],
        "retrieval_details": row["retrieval_details"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--embedding-model", type=Path, default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--reranker-model", type=Path, default=DEFAULT_RERANKER_MODEL)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--source-top-k", type=int, default=100)
    parser.add_argument("--rrf-top-k", type=int, default=50)
    parser.add_argument("--final-top-k", type=int, default=15)
    parser.add_argument("--embedding-batch-size", type=int, default=8)
    parser.add_argument("--reranker-batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--priorities", nargs="+", default=("high", "medium"))
    parser.add_argument("--query-field", default="retrieval_query")
    return parser.parse_args()


def configuration_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload = {}
    for key, value in vars(args).items():
        if isinstance(value, Path):
            payload[key] = str(value.resolve())
        elif isinstance(value, tuple):
            payload[key] = list(value)
        else:
            payload[key] = value
    payload.update(
        {
            "pipeline": (
                "BGE-M3 dense/sparse Top100 -> RRF Top50 -> "
                "BGE reranker -> evidence-group dedup Top15"
            )
        }
    )
    return payload


def main() -> None:
    args = parse_args()
    if not args.embedding_model.is_dir():
        raise FileNotFoundError(f"BGE-M3 embedding model not found: {args.embedding_model}")
    if not args.reranker_model.is_dir():
        raise FileNotFoundError(f"Reranker model not found: {args.reranker_model}")

    from FlagEmbedding import BGEM3FlagModel, FlagReranker
    import torch

    dataset = load_json(args.claims)
    claims = selected_claims(dataset, set(args.priorities))
    kb_chunks = flatten_kb(load_json(args.kb))
    chunk_to_group = load_json(args.groups)["chunk_to_group"]
    kb_texts = [retrieval_text(chunk) for chunk in kb_chunks]
    query_texts = [claim[args.query_field] for _case, claim in claims]

    print(f"KB chunks: {len(kb_chunks)}; atomic claims: {len(claims)}", flush=True)
    model = BGEM3FlagModel(
        str(args.embedding_model.resolve()),
        normalize_embeddings=True,
        use_fp16=True,
        devices="cuda",
        batch_size=args.embedding_batch_size,
        passage_max_length=args.max_length,
        query_max_length=args.max_length,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )
    print("Encoding KB and atomic claims...", flush=True)
    encode_options = {
        "batch_size": args.embedding_batch_size,
        "max_length": args.max_length,
        "return_dense": True,
        "return_sparse": True,
        "return_colbert_vecs": False,
    }
    kb_encoded = model.encode(kb_texts, **encode_options)
    query_encoded = model.encode(query_texts, **encode_options)
    document_vectors = np.asarray(kb_encoded["dense_vecs"], dtype=np.float32)
    query_vectors = np.asarray(query_encoded["dense_vecs"], dtype=np.float32)
    postings = build_sparse_postings(kb_encoded["lexical_weights"])
    query_sparse = query_encoded["lexical_weights"]

    retrieval_rows = []
    for index, (case, claim) in enumerate(claims):
        dense = dense_search(query_vectors[index], document_vectors, args.source_top_k)
        sparse = sparse_search(query_sparse[index], postings, len(kb_chunks), args.source_top_k)
        retrieval_rows.append(
            {
                "case": case,
                "claim": claim,
                "dense": dense,
                "sparse": sparse,
                "screened": rrf_screen(dense, sparse, args.rrf_top_k),
            }
        )
    del model, kb_encoded, query_encoded, document_vectors, query_vectors
    gc.collect()
    torch.cuda.empty_cache()

    reranker = FlagReranker(str(args.reranker_model.resolve()), use_fp16=True)
    results = []
    for index, row in enumerate(retrieval_rows, 1):
        query = row["claim"][args.query_field]
        screened = row["screened"]
        pairs = [[query, retrieval_text(kb_chunks[item["document_index"]])] for item in screened]
        scores = reranker.compute_score(
            pairs, batch_size=args.reranker_batch_size, max_length=args.max_length, normalize=True
        )
        if not isinstance(scores, (list, tuple, np.ndarray)):
            scores = [scores]
        for item, score in zip(screened, scores):
            item["rerank_score"] = float(score)
        screened.sort(key=lambda item: item["rerank_score"], reverse=True)
        selected, suppressed = group_dedup(screened, kb_chunks, chunk_to_group, args.final_top_k)
        results.append(
            {
                "case_id": row["case"]["case_id"],
                "claim_id": row["claim"]["claim_id"],
                "query": query,
                "source_top_k": {
                    "dense": source_audit(row["dense"], kb_chunks),
                    "sparse": source_audit(row["sparse"], kb_chunks),
                },
                "rrf_screen_count": len(screened),
                "suppressed_same_evidence_group": suppressed,
                "candidates": [
                    candidate_payload(item, kb_chunks[item["document_index"]], rank)
                    for rank, item in enumerate(selected, 1)
                ],
            }
        )
        if index % 10 == 0 or index == len(retrieval_rows):
            print(f"Reranked {index}/{len(retrieval_rows)}", flush=True)
            atomic_write(
                args.output,
                {
                    "schema": "coal_rag_atomic_retrieval_local_bgem3_grouped_v1",
                    "status": "running" if index < len(retrieval_rows) else "complete",
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "configuration": configuration_payload(args),
                    "claim_result_count": len(results),
                    "results": results,
                },
            )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
