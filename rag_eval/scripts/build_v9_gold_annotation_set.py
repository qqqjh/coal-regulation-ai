"""
Build the v9-based 50-case gold annotation candidate set.

Selection rules:
- high/medium mapping cases keep Top1;
- the first three low-confidence cases keep Top3;
- the fourth low-confidence case keeps Top1;
- duplicate v9 chunks merge their source cases;
- reduce unique pending chunks to 50 by removing the most similar lower-priority
  random samples, while protecting comparison cases and explicitly kept Top3s.

Each final pending chunk receives 15 unique regulation candidates pooled from:
- Dense Top5
- BM25 Top5
- Hybrid RRF Top5
The pool is filled from wider Top20 results when the three Top5 lists overlap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from build_annotation_app import (  # noqa: E402
    BM25Retriever,
    build_keyword_hint,
    flatten_kb,
    normalize_text,
    pending_quote,
    write_html,
)
from hybrid_rag_review_v8 import HybridRAGReviewerV8  # noqa: E402


DEFAULT_MAPPING = PROJECT_ROOT / "rag_eval" / "data" / "v5_to_v9_case_mappings.json"
DEFAULT_V9 = PROJECT_ROOT / "chunks_visualization" / "pending_doc_chunks_v9_20260528_204330.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_annotation_cases_v9.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "gold_annotation_app_v9.html"
DEFAULT_AUDIT = PROJECT_ROOT / "rag_eval" / "data" / "gold_annotation_selection_audit_v9.json"

KEEP_TOP3_LOW = {
    "cmp_006_chunk089",
    "cmp_006_chunk104",
    "cmp_006_chunk030",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def compact_text(text: Any) -> str:
    text = normalize_text(text).lower()
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9.%≥≤<>]+", text))


def grams(text: str, n: int = 3) -> Set[str]:
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def text_similarity(left: str, right: str) -> float:
    left_grams = grams(compact_text(left))
    right_grams = grams(compact_text(right))
    if not left_grams or not right_grams:
        return 0.0
    return 2 * len(left_grams & right_grams) / (len(left_grams) + len(right_grams))


def v9_key(doc_name: str, chunk_no: int) -> str:
    return f"{doc_name}::chunk{chunk_no}"


def expand_and_merge(mapping: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    merged: Dict[str, Dict[str, Any]] = {}
    expansion_audit = []
    for case in mapping["cases"]:
        keep_count = 3 if case["case_id"] in KEEP_TOP3_LOW else 1
        for candidate_rank, candidate in enumerate(case["v9_candidates"][:keep_count], start=1):
            key = v9_key(candidate["v9_doc_name"], candidate["v9_chunk_no"])
            expansion_audit.append({
                "source_case_id": case["case_id"],
                "source_confidence": case["recommended_confidence"],
                "kept_candidate_rank": candidate_rank,
                "v9_key": key,
            })
            if key not in merged:
                merged[key] = {
                    "v9_key": key,
                    "doc_name": candidate["v9_doc_name"],
                    "chunk_no": candidate["v9_chunk_no"],
                    "chunk_index": candidate["v9_chunk_index"],
                    "pending_chunk": candidate,
                    "source_cases": [],
                    "topics": [],
                    "source_notes": [],
                    "protected": False,
                }
            record = merged[key]
            record["source_cases"].append(case["case_id"])
            if case.get("topic") and case["topic"] not in record["topics"]:
                record["topics"].append(case["topic"])
            if case.get("source_note") and case["source_note"] not in record["source_notes"]:
                record["source_notes"].append(case["source_note"])
            record["protected"] = (
                record["protected"]
                or case["case_id"] in KEEP_TOP3_LOW
                or case.get("sample_source") == "comparison_v5"
            )
    return list(merged.values()), expansion_audit


def selection_priority(record: Dict[str, Any]) -> Tuple[int, int, int]:
    comparison_count = sum(case_id.startswith("cmp_") for case_id in record["source_cases"])
    explicit_top3 = sum(case_id in KEEP_TOP3_LOW for case_id in record["source_cases"])
    return (
        1000 if explicit_top3 else 0,
        comparison_count,
        len(record["source_cases"]),
    )


def reduce_to_target(records: List[Dict[str, Any]], target: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    kept = list(records)
    removed = []
    while len(kept) > target:
        pairs = []
        for i in range(len(kept)):
            for j in range(i + 1, len(kept)):
                if kept[i]["doc_name"] != kept[j]["doc_name"]:
                    continue
                similarity = text_similarity(
                    kept[i]["pending_chunk"]["content"],
                    kept[j]["pending_chunk"]["content"],
                )
                pairs.append((similarity, i, j))
        pairs.sort(reverse=True)

        chosen = None
        for similarity, i, j in pairs:
            left, right = kept[i], kept[j]
            left_priority = selection_priority(left)
            right_priority = selection_priority(right)
            if left_priority == right_priority and left["protected"] and right["protected"]:
                continue
            remove_idx = i if left_priority < right_priority else j
            keep_idx = j if remove_idx == i else i
            chosen = (similarity, remove_idx, keep_idx)
            break
        if chosen is None:
            raise RuntimeError("Unable to reduce cases without removing equally protected records")

        similarity, remove_idx, keep_idx = chosen
        removed_record = kept[remove_idx]
        retained_record = kept[keep_idx]
        removed.append({
            "removed_v9_key": removed_record["v9_key"],
            "removed_source_cases": removed_record["source_cases"],
            "retained_similar_v9_key": retained_record["v9_key"],
            "retained_source_cases": retained_record["source_cases"],
            "similarity": round(similarity, 6),
            "reason": "Removed lower-priority similar pending chunk while preserving comparison and explicit Top3 cases.",
        })
        del kept[remove_idx]
    return kept, removed


class ThreeWayRetriever:
    def __init__(self, reviewer: HybridRAGReviewerV8, kb_chunks: List[Dict[str, Any]]):
        self.reviewer = reviewer
        self.kb_chunks = kb_chunks
        self.by_id = {chunk["chunk_id"]: chunk for chunk in kb_chunks}
        # hybrid_rag_review_v8 builds the Chroma collection with sequential
        # vector IDs (kb_0, kb_1, ...), while annotation IDs are stable
        # document-name IDs. Keep an explicit bridge between the two.
        self.by_vector_id = {f"kb_{idx}": chunk for idx, chunk in enumerate(kb_chunks)}
        self.bm25 = BM25Retriever(kb_chunks)

    def dense_search(self, embedding: Sequence[float], top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        results = self.reviewer.collection.query(query_embeddings=[embedding], n_results=top_k)
        matched = []
        for idx, chunk_id in enumerate(results["ids"][0]):
            chunk = self.by_vector_id.get(chunk_id) or self.by_id.get(chunk_id)
            if chunk is None:
                continue
            distance = results["distances"][0][idx] if results.get("distances") else 0.0
            matched.append((chunk, 1 - float(distance)))
        return matched

    @staticmethod
    def hybrid_search(
        dense: List[Tuple[Dict[str, Any], float]],
        bm25: List[Tuple[Dict[str, Any], float, str]],
        top_k: int,
        vector_weight: float = 0.5,
    ) -> List[Tuple[Dict[str, Any], float]]:
        scores: Dict[str, float] = defaultdict(float)
        chunks: Dict[str, Dict[str, Any]] = {}
        for rank, (chunk, _score) in enumerate(dense):
            chunks[chunk["chunk_id"]] = chunk
            scores[chunk["chunk_id"]] += vector_weight / (rank + 60)
        for rank, (chunk, _score, _source) in enumerate(bm25):
            chunks[chunk["chunk_id"]] = chunk
            scores[chunk["chunk_id"]] += (1 - vector_weight) / (rank + 60)
        ordered = sorted(scores, key=scores.get, reverse=True)[:top_k]
        return [(chunks[chunk_id], scores[chunk_id]) for chunk_id in ordered]

    def candidate_pool(self, query: str, embedding: Sequence[float], target: int = 15) -> List[Dict[str, Any]]:
        dense = self.dense_search(embedding, top_k=20)
        bm25 = self.bm25.search(query, top_k=20)
        hybrid = self.hybrid_search(dense, bm25, top_k=20)

        details: Dict[str, Dict[str, Any]] = {}
        ordered_ids: List[str] = []

        def add(group: str, rows: Sequence[Any], limit: int, rank_offset: int = 0) -> None:
            for rank, row in enumerate(rows[:limit], start=1):
                chunk, score = row[0], float(row[1])
                cid = chunk["chunk_id"]
                if cid not in details:
                    details[cid] = {"chunk": chunk, "sources": {}, "first_group": group}
                    ordered_ids.append(cid)
                details[cid]["sources"][group] = {"rank": rank + rank_offset, "score": round(score, 6)}

        add("dense", dense, 5)
        add("bm25", bm25, 5)
        add("hybrid", hybrid, 5)

        # Fill overlaps to 15 unique candidates from the wider Top20 lists.
        cursor = 5
        while len(ordered_ids) < target and cursor < 20:
            add("dense", dense[cursor:cursor + 1], 1, cursor)
            add("bm25", bm25[cursor:cursor + 1], 1, cursor)
            add("hybrid", hybrid[cursor:cursor + 1], 1, cursor)
            cursor += 1

        candidates = []
        for rank, cid in enumerate(ordered_ids[:target], start=1):
            item = details[cid]
            chunk = item["chunk"]
            sources = item["sources"]
            candidates.append({
                "rank": rank,
                "chunk_id": cid,
                "doc_name": chunk.get("doc_name", ""),
                "kb_chunk_index": chunk.get("kb_chunk_index"),
                "chapter": chunk.get("chapter", ""),
                "section": chunk.get("section", ""),
                "article": chunk.get("article", ""),
                "page_range": chunk.get("page_range", ""),
                "chunk_level": chunk.get("chunk_level", ""),
                "score": max(source["score"] for source in sources.values()),
                "source": "+".join(sources.keys()),
                "retrieval_details": sources,
                "reason": (
                    "候选来源: "
                    + "；".join(
                        f"{name} Top{detail['rank']} score={detail['score']}"
                        for name, detail in sources.items()
                    )
                    + "；共同关键词: "
                    + "、".join(build_keyword_hint(query, chunk.get("content", "")))
                ),
                "content": chunk.get("content", ""),
                "anchor_quote": pending_quote(chunk.get("content", ""), 100),
                "label": "unlabeled",
                "note": "",
            })
        return candidates


def build_case(record: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    pending = record["pending_chunk"]
    topics = record["topics"]
    return {
        "case_id": f"v9_{record['doc_name'].strip()[:3]}_chunk{record['chunk_no']:03d}",
        "sample_source": "v5_to_v9_mapped_and_deduplicated",
        "topic": "；".join(topics),
        "source_note": " | ".join(record["source_notes"]),
        "source_case_ids": record["source_cases"],
        "pending": {
            "doc_name": record["doc_name"],
            "chunk_no": record["chunk_no"],
            "chunk_index": record["chunk_index"],
            "chapter": pending.get("chapter", ""),
            "section": pending.get("section", ""),
            "article": pending.get("article", ""),
            "page_range": pending.get("page_range", ""),
            "chunk_level": pending.get("chunk_level", ""),
            "split_strategy": pending.get("split_strategy", ""),
            "quote": pending_quote(pending.get("content", "")),
            "content": pending.get("content", ""),
        },
        "final_label": "unlabeled",
        "missing_correct_evidence": False,
        "not_eval_suitable": False,
        "human_note": "",
        "candidates": candidates,
    }


def bm25_candidates(retriever: BM25Retriever, query: str, target: int = 15) -> List[Dict[str, Any]]:
    candidates = []
    for rank, (chunk, score, source) in enumerate(retriever.search(query, top_k=target), start=1):
        candidates.append({
            "rank": rank,
            "chunk_id": chunk["chunk_id"],
            "doc_name": chunk.get("doc_name", ""),
            "kb_chunk_index": chunk.get("kb_chunk_index"),
            "chapter": chunk.get("chapter", ""),
            "section": chunk.get("section", ""),
            "article": chunk.get("article", ""),
            "page_range": chunk.get("page_range", ""),
            "chunk_level": chunk.get("chunk_level", ""),
            "score": round(float(score), 6),
            "source": source,
            "retrieval_details": {"bm25": {"rank": rank, "score": round(float(score), 6)}},
            "reason": (
                f"候选来源: bm25；Top{rank}；得分: {float(score):.6f}；共同关键词: "
                + "、".join(build_keyword_hint(query, chunk.get("content", "")))
            ),
            "content": chunk.get("content", ""),
            "anchor_quote": pending_quote(chunk.get("content", ""), 100),
            "label": "unlabeled",
            "note": "",
        })
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the v9 50-case gold annotation candidate set.")
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--v9", type=Path, default=DEFAULT_V9)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--target-cases", type=int, default=50)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument(
        "--retrieval-mode",
        choices=("bm25", "three-way"),
        default="bm25",
        help="Use local BM25 only, or external-query-embedding Dense+BM25+Hybrid retrieval.",
    )
    args = parser.parse_args()

    mapping = load_json(args.mapping)
    records, expansion_audit = expand_and_merge(mapping)
    selected, removed = reduce_to_target(records, args.target_cases)
    selected.sort(key=lambda item: (item["doc_name"], item["chunk_no"]))

    kb_chunks = flatten_kb(load_json(args.kb))
    local_bm25 = BM25Retriever(kb_chunks)
    cases = []
    if args.retrieval_mode == "three-way":
        reviewer = HybridRAGReviewerV8()
        reviewer.load_knowledge_base(str(args.kb.relative_to(PROJECT_ROOT)))
        reviewer.load_or_build_index()
        retriever = ThreeWayRetriever(reviewer, kb_chunks)
        queries = [record["pending_chunk"]["content"] for record in selected]
        print(f"Generating embeddings for {len(queries)} selected pending chunks...")
        embeddings = reviewer.get_embeddings(queries)
    else:
        retriever = None
        embeddings = [None] * len(selected)

    for idx, (record, embedding) in enumerate(zip(selected, embeddings), start=1):
        print(f"  candidates {idx}/{len(selected)}: {record['v9_key']}")
        query = record["pending_chunk"]["content"]
        if args.retrieval_mode == "three-way":
            candidates = retriever.candidate_pool(query, embedding, target=15)
        else:
            candidates = bm25_candidates(local_bm25, query, target=15)
        cases.append(build_case(record, candidates))

    output_data = {
        "schema": "coal_rag_gold_annotation_cases_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "case_count": len(cases),
        "kb_source": str(args.kb.resolve()),
        "kb_sha256": hashlib.sha256(args.kb.read_bytes()).hexdigest(),
        "selection_rules": {
            "high_medium": "keep Top1",
            "low_first_three": sorted(KEEP_TOP3_LOW),
            "low_fourth": "keep Top1",
            "similarity_reduction": "merge identical v9 chunks, then remove lower-priority similar random samples to 50",
            "retrieval_candidates": (
                "15 unique candidates pooled from Dense Top5 + BM25 Top5 + Hybrid RRF Top5, filled from Top20 overlaps"
                if args.retrieval_mode == "three-way"
                else "Local BM25 Top15; Dense and Hybrid candidates pending explicit external embedding authorization"
            ),
        },
        "retrieval_mode": args.retrieval_mode,
        "retrieval_text_strategy": "retrieval_text fallback content",
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # Reuse the existing annotation UI; use a separate storage key.
    write_html(cases, args.html)
    html_text = args.html.read_text(encoding="utf-8").replace(
        "coal_rag_eval_annotations_v1",
        "coal_rag_gold_annotations_v9_v1",
    )
    args.html.write_text(html_text, encoding="utf-8")

    audit = {
        "schema": "coal_rag_gold_annotation_selection_audit_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "expanded_mapping_count": len(expansion_audit),
        "unique_before_similarity_reduction": len(records),
        "final_count": len(selected),
        "expansion": expansion_audit,
        "removed_by_similarity": removed,
        "selected": [
            {
                "v9_key": record["v9_key"],
                "source_cases": record["source_cases"],
                "topics": record["topics"],
                "protected": record["protected"],
            }
            for record in selected
        ],
    }
    args.audit.parent.mkdir(parents=True, exist_ok=True)
    with args.audit.open("w", encoding="utf-8") as f:
        json.dump(audit, f, ensure_ascii=False, indent=2)

    print(f"Expanded mappings: {len(expansion_audit)}")
    print(f"Unique v9 pending chunks before reduction: {len(records)}")
    print(f"Final cases: {len(cases)}")
    print(f"Cases JSON: {args.output}")
    print(f"Annotation HTML: {args.html}")
    print(f"Selection audit: {args.audit}")


if __name__ == "__main__":
    main()
