"""Generate parent-chunk candidates with local BGE-M3 dense/sparse retrieval.

Pipeline:
- BGE-M3 dense TopK
- BGE-M3 learned sparse TopK
- RRF hybrid TopK
- bge-reranker-v2-m3 reranking over the three-way TopK union

The fixed 50 pending chunks are read from the existing gold case file. Project
text never leaves the local machine.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = PROJECT_ROOT / "rag_eval" / "data" / "gold_annotation_cases_v9.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_EMBEDDING_MODEL = PROJECT_ROOT / "models" / "bge-m3"
DEFAULT_RERANKER_MODEL = PROJECT_ROOT / "models" / "bge-reranker-v2-m3"
DEFAULT_EVIDENCE_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "parent_candidates_local_bgem3_v6_grouped_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "parent_candidates_local_bgem3_v6_grouped_v1.html"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retrieval_text(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("retrieval_text") or chunk.get("content", ""))


def flatten_kb(kb_data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for doc_name, rows in kb_data.items():
        for index, row in enumerate(rows, start=1):
            if row.get("retrievable", True) is False:
                continue
            item = dict(row)
            item["doc_name"] = doc_name
            item["kb_chunk_index"] = index - 1
            item["chunk_id"] = f"{doc_name}::chunk{index}"
            chunks.append(item)
    return chunks


def top_indices(scores: np.ndarray, top_k: int) -> List[int]:
    if scores.size == 0:
        return []
    limit = min(top_k, scores.size)
    selected = np.argpartition(scores, -limit)[-limit:]
    return selected[np.argsort(scores[selected])[::-1]].tolist()


def dense_search(query_vector: np.ndarray, document_vectors: np.ndarray, top_k: int) -> List[Tuple[int, float]]:
    scores = document_vectors @ query_vector
    return [(index, float(scores[index])) for index in top_indices(scores, top_k)]


def build_sparse_postings(
    document_weights: Sequence[Dict[str, float]],
) -> Dict[str, List[Tuple[int, float]]]:
    postings: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for document_index, weights in enumerate(document_weights):
        for token_id, weight in weights.items():
            postings[str(token_id)].append((document_index, float(weight)))
    return postings


def sparse_search(
    query_weights: Dict[str, float],
    postings: Dict[str, List[Tuple[int, float]]],
    document_count: int,
    top_k: int,
) -> List[Tuple[int, float]]:
    scores = np.zeros(document_count, dtype=np.float32)
    for token_id, query_weight in query_weights.items():
        for document_index, document_weight in postings.get(str(token_id), ()):
            scores[document_index] += float(query_weight) * document_weight
    return [
        (index, float(scores[index]))
        for index in top_indices(scores, top_k)
        if scores[index] > 0
    ]


def rrf_hybrid(
    dense: Sequence[Tuple[int, float]],
    sparse: Sequence[Tuple[int, float]],
    top_k: int,
    rrf_k: int = 60,
) -> List[Tuple[int, float]]:
    scores: Dict[int, float] = defaultdict(float)
    for rows in (dense, sparse):
        for rank, (document_index, _score) in enumerate(rows, start=1):
            scores[document_index] += 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=scores.get, reverse=True)[:top_k]
    return [(document_index, scores[document_index]) for document_index in ordered]


def source_audit(rows: Sequence[Tuple[int, float]], kb_chunks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "rank": rank,
            "chunk_id": kb_chunks[index]["chunk_id"],
            "score": float(score),
        }
        for rank, (index, score) in enumerate(rows, start=1)
    ]


def build_union(
    dense: Sequence[Tuple[int, float]],
    sparse: Sequence[Tuple[int, float]],
    hybrid: Sequence[Tuple[int, float]],
) -> List[Dict[str, Any]]:
    union: Dict[int, Dict[str, Any]] = {}
    for source, rows in (("dense", dense), ("sparse", sparse), ("hybrid", hybrid)):
        for rank, (document_index, score) in enumerate(rows, start=1):
            record = union.setdefault(document_index, {"document_index": document_index, "retrieval_details": {}})
            record["retrieval_details"][source] = {"rank": rank, "score": float(score)}
    return list(union.values())


def candidate_payload(
    record: Dict[str, Any],
    chunk: Dict[str, Any],
    final_rank: int,
    rerank_score: float,
) -> Dict[str, Any]:
    details = record["retrieval_details"]
    return {
        "rank": final_rank,
        "chunk_id": chunk["chunk_id"],
        "doc_name": chunk.get("doc_name", ""),
        "kb_chunk_index": chunk.get("kb_chunk_index"),
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "article": chunk.get("article", ""),
        "page_range": chunk.get("page_range", ""),
        "chunk_level": chunk.get("chunk_level", ""),
        "canonical_rule_id": chunk.get("canonical_rule_id", ""),
        "evidence_group_id": record.get("evidence_group_id", ""),
        "score": float(rerank_score),
        "rerank_score": float(rerank_score),
        "source": "+".join(details),
        "retrieval_details": details,
        "reason": "；".join(
            f"{source} Top{detail['rank']} score={detail['score']:.6f}"
            for source, detail in details.items()
        ) + f"；reranker score={rerank_score:.6f}",
        "content": chunk.get("content", ""),
        "retrieval_text": retrieval_text(chunk),
        "anchor_quote": str(chunk.get("content", ""))[:100],
        "label": "unlabeled",
        "note": "",
    }


def apply_evidence_group_dedup(
    ranked_union: Sequence[Dict[str, Any]],
    kb_chunks: Sequence[Dict[str, Any]],
    chunk_to_group: Dict[str, str],
    final_top_k: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept: List[Dict[str, Any]] = []
    suppressed: List[Dict[str, Any]] = []
    selected_group_members: Dict[str, Dict[str, Any]] = {}
    for original_rank, item in enumerate(ranked_union, start=1):
        chunk = kb_chunks[item["document_index"]]
        group_id = chunk_to_group.get(chunk["chunk_id"], "")
        item["evidence_group_id"] = group_id
        if group_id and group_id in selected_group_members:
            selected = selected_group_members[group_id]
            suppressed.append(
                {
                    "original_rerank_rank": original_rank,
                    "chunk_id": chunk["chunk_id"],
                    "rerank_score": item["rerank_score"],
                    "evidence_group_id": group_id,
                    "kept_chunk_id": selected["chunk_id"],
                    "kept_rerank_score": selected["rerank_score"],
                }
            )
            continue
        kept.append(item)
        if group_id:
            selected_group_members[group_id] = {
                "chunk_id": chunk["chunk_id"],
                "rerank_score": item["rerank_score"],
            }
        if len(kept) >= final_top_k:
            break
    return kept, suppressed


def render_html(cases: Sequence[Dict[str, Any]], output: Path, configuration: Dict[str, Any]) -> None:
    import html

    output.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Local BGE-M3 Parent Retrieval Review</title>",
        "<style>",
        "body{font-family:'Microsoft YaHei',Arial;margin:20px;background:#f5f6f7;color:#1f2933}",
        ".summary,.case{background:white;border:1px solid #d9dee3;margin-bottom:18px;padding:16px}",
        ".case h2{margin:0 0 8px;font-size:20px}.meta{color:#657482;margin:4px 0}",
        "details{margin:12px 0}.candidate{border-top:1px solid #e1e5e8;padding:14px 0}",
        ".candidate h3{margin:0 0 6px;font-size:16px}.badge{display:inline-block;background:#e8f0f5;padding:2px 6px;margin-right:5px}",
        "pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;background:#fafbfc;padding:12px;border-left:3px solid #3478a5}",
        "</style></head><body>",
        "<section class='summary'><h1>本地 BGE-M3 父 Chunk 检索候选</h1>",
        f"<pre>{html.escape(json.dumps(configuration, ensure_ascii=False, indent=2))}</pre></section>",
    ]
    for case in cases:
        pending = case["pending"]
        source_top_k = case["source_top_k"]
        parts.append(
            f"<section class='case'><h2>{html.escape(case['case_id'])}</h2>"
            f"<div class='meta'>{html.escape(pending.get('doc_name', ''))} · Chunk #{pending.get('chunk_no', '')}</div>"
            f"<div class='meta'>Dense Top5: {html.escape('、'.join(x['chunk_id'] for x in source_top_k['dense'][:5]))}</div>"
            f"<div class='meta'>Sparse Top5: {html.escape('、'.join(x['chunk_id'] for x in source_top_k['sparse'][:5]))}</div>"
            f"<div class='meta'>Hybrid Top5: {html.escape('、'.join(x['chunk_id'] for x in source_top_k['hybrid'][:5]))}</div>"
            f"<details><summary>待审 chunk 全文</summary><pre>{html.escape(pending.get('content', ''))}</pre></details>"
        )
        for candidate in case["candidates"]:
            details = candidate["retrieval_details"]
            badges = "".join(
                f"<span class='badge'>{html.escape(source)} Top{detail['rank']}</span>"
                for source, detail in details.items()
            )
            parts.append(
                f"<article class='candidate'><h3>#{candidate['rank']} {html.escape(candidate['chunk_id'])}</h3>"
                f"<div>{badges}<span class='badge'>rerank {candidate['rerank_score']:.6f}</span>"
                f"<span class='badge'>证据组 {html.escape(candidate.get('evidence_group_id') or '无')}</span></div>"
                f"<pre>{html.escape(candidate['content'])}</pre></article>"
            )
        if case.get("suppressed_same_evidence_group"):
            parts.append(
                "<details><summary>同证据组被抑制候选</summary><pre>"
                + html.escape(
                    json.dumps(case["suppressed_same_evidence_group"], ensure_ascii=False, indent=2)
                )
                + "</pre></details>"
            )
        parts.append("</section>")
    parts.append("</body></html>")
    output.write_text("".join(parts), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--embedding-model", type=Path, default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--reranker-model", type=Path, default=DEFAULT_RERANKER_MODEL)
    parser.add_argument("--evidence-groups", type=Path, default=DEFAULT_EVIDENCE_GROUPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--source-top-k", type=int, default=20)
    parser.add_argument("--final-top-k", type=int, default=15)
    parser.add_argument("--embedding-batch-size", type=int, default=8)
    parser.add_argument("--reranker-batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=2048)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.embedding_model.is_dir():
        raise FileNotFoundError(f"BGE-M3 embedding model not found: {args.embedding_model}")
    if not args.reranker_model.is_dir():
        raise FileNotFoundError(f"Reranker model not found: {args.reranker_model}")

    from FlagEmbedding import BGEM3FlagModel, FlagReranker
    import torch

    source_cases = load_json(args.cases)["cases"]
    kb_chunks = flatten_kb(load_json(args.kb))
    evidence_groups = load_json(args.evidence_groups)
    chunk_to_group = evidence_groups["chunk_to_group"]
    kb_texts = [retrieval_text(chunk) for chunk in kb_chunks]
    query_texts = [case["pending"]["content"] for case in source_cases]

    print(f"Knowledge-base chunks: {len(kb_chunks)}", flush=True)
    print(f"Pending parent chunks: {len(query_texts)}", flush=True)
    print(f"Loading BGE-M3: {args.embedding_model}", flush=True)
    embedding_model = BGEM3FlagModel(
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
    print("Encoding knowledge-base chunks...", flush=True)
    kb_encoded = embedding_model.encode(
        kb_texts,
        batch_size=args.embedding_batch_size,
        max_length=args.max_length,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )
    print("Encoding pending chunks...", flush=True)
    query_encoded = embedding_model.encode(
        query_texts,
        batch_size=args.embedding_batch_size,
        max_length=args.max_length,
        return_dense=True,
        return_sparse=True,
        return_colbert_vecs=False,
    )
    document_vectors = np.asarray(kb_encoded["dense_vecs"], dtype=np.float32)
    query_vectors = np.asarray(query_encoded["dense_vecs"], dtype=np.float32)
    document_sparse = kb_encoded["lexical_weights"]
    query_sparse = query_encoded["lexical_weights"]
    postings = build_sparse_postings(document_sparse)

    retrieval_cases = []
    for index, source_case in enumerate(source_cases):
        dense = dense_search(query_vectors[index], document_vectors, args.source_top_k)
        sparse = sparse_search(query_sparse[index], postings, len(kb_chunks), args.source_top_k)
        hybrid = rrf_hybrid(dense, sparse, args.source_top_k)
        retrieval_cases.append({
            "source_case": source_case,
            "dense": dense,
            "sparse": sparse,
            "hybrid": hybrid,
            "union": build_union(dense, sparse, hybrid),
        })

    del embedding_model, kb_encoded, query_encoded, document_vectors, query_vectors
    gc.collect()
    torch.cuda.empty_cache()

    print(f"Loading reranker: {args.reranker_model}", flush=True)
    reranker = FlagReranker(str(args.reranker_model.resolve()), use_fp16=True)
    output_cases = []
    for case_index, retrieval_case in enumerate(retrieval_cases, start=1):
        source_case = retrieval_case["source_case"]
        query = source_case["pending"]["content"]
        union = retrieval_case["union"]
        pairs = [[query, retrieval_text(kb_chunks[item["document_index"]])] for item in union]
        scores = reranker.compute_score(
            pairs,
            batch_size=args.reranker_batch_size,
            max_length=args.max_length,
            normalize=True,
        )
        if not isinstance(scores, (list, tuple, np.ndarray)):
            scores = [scores]
        for item, score in zip(union, scores):
            item["rerank_score"] = float(score)
        union.sort(key=lambda item: item["rerank_score"], reverse=True)
        deduplicated, suppressed = apply_evidence_group_dedup(
            union, kb_chunks, chunk_to_group, args.final_top_k
        )
        candidates = [
            candidate_payload(item, kb_chunks[item["document_index"]], rank, item["rerank_score"])
            for rank, item in enumerate(deduplicated, start=1)
        ]
        output_case = {key: value for key, value in source_case.items() if key != "candidates"}
        output_case["source_top_k"] = {
            "dense": source_audit(retrieval_case["dense"], kb_chunks),
            "sparse": source_audit(retrieval_case["sparse"], kb_chunks),
            "hybrid": source_audit(retrieval_case["hybrid"], kb_chunks),
        }
        output_case["candidate_count_before_rerank_cut"] = len(union)
        output_case["candidate_count_after_evidence_group_dedup"] = len(deduplicated)
        output_case["suppressed_same_evidence_group"] = suppressed
        output_case["candidates"] = candidates
        output_cases.append(output_case)
        print(f"Reranked {case_index}/{len(retrieval_cases)}: {source_case['case_id']}", flush=True)

    configuration = {
        "embedding_model": str(args.embedding_model.resolve()),
        "embedding_mode": "BGE-M3 dense + learned sparse",
        "hybrid": "RRF dense+sparse, rrf_k=60",
        "reranker_model": str(args.reranker_model.resolve()),
        "kb_source": str(args.kb.resolve()),
        "kb_sha256": file_sha256(args.kb),
        "pending_source": str(args.cases.resolve()),
        "evidence_groups_source": str(args.evidence_groups.resolve()),
        "evidence_group_count": evidence_groups["group_count"],
        "evidence_group_dedup_stage": "after reranker, before final_top_k; refill from lower rerank ranks",
        "source_top_k": args.source_top_k,
        "final_top_k": args.final_top_k,
        "max_length": args.max_length,
        "retrieval_text_strategy": "retrieval_text fallback content",
        "project_text_external_transfer": False,
    }
    payload = {
        "schema": "coal_rag_parent_candidates_local_bgem3_v6_grouped_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "case_count": len(output_cases),
        "configuration": configuration,
        "cases": output_cases,
    }
    write_json(args.output, payload)
    render_html(output_cases, args.html, configuration)
    print(f"Written JSON: {args.output}", flush=True)
    print(f"Written HTML: {args.html}", flush=True)


if __name__ == "__main__":
    main()
