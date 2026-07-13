"""Retrieve Dense/BM25 TopK for atomic claims, merge, and optionally rerank.

The default run is a dependency-light BM25 baseline. Dense retrieval reuses the
existing DashScope + Chroma implementation only when --dense is enabled.
Optional reranking backends are loaded lazily:
- sentence-transformers CrossEncoder, suitable for bge-reranker models;
- FlagEmbedding FlagReranker.
- Qwen3-Reranker through its yes/no causal-language-model scoring interface.

All TopK source ranks are retained for recall diagnosis. Final candidates are
not truncated until after union and optional reranking.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_CLAIMS = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "atomic_retrieval_results_v9_v1.json"

TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+|\d+(?:\.\d+)?|[%‰≤≥<>±]+")
try:
    import jieba  # type: ignore
except ImportError:
    jieba = None


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any, max_retries: int = 12) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())

    for attempt in range(1, max_retries + 1):
        try:
            temp_path.replace(path)
            return
        except PermissionError:
            if attempt >= max_retries:
                raise
            delay = min(0.25 * attempt, 2.0)
            print(
                f"Output file is temporarily locked; retrying save "
                f"({attempt}/{max_retries}) in {delay:.2f}s...",
                flush=True,
            )
            time.sleep(delay)


def tokenize(text: str) -> List[str]:
    if jieba is not None:
        return [token.strip().lower() for token in jieba.cut(text or "") if token.strip()]
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def retrieval_text(chunk: Dict[str, Any]) -> str:
    return str(chunk.get("retrieval_text") or chunk.get("content", ""))


def flatten_kb(kb_data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    chunks = []
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


def build_document_local_chunk_identity(
    chunks: Sequence[Dict[str, Any]],
) -> Dict[str, Tuple[int, str]]:
    """Map global storage IDs to stable document-local chunk identities."""
    document_counts: Dict[str, int] = defaultdict(int)
    identities: Dict[str, Tuple[int, str]] = {}
    for chunk in chunks:
        global_id = str(chunk["id"])
        doc_name = str(chunk.get("doc_name", ""))
        local_index = document_counts[doc_name]
        document_counts[doc_name] += 1
        if global_id in identities:
            raise ValueError(f"Duplicate global knowledge-base chunk ID: {global_id}")
        identities[global_id] = (local_index, f"{doc_name}::chunk{local_index + 1}")
    return identities


class SimpleBM25:
    def __init__(self, chunks: Sequence[Dict[str, Any]], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.docs = [tokenize(retrieval_text(chunk)) for chunk in chunks]
        self.doc_lengths = [len(doc) for doc in self.docs]
        self.avgdl = sum(self.doc_lengths) / max(len(self.doc_lengths), 1)
        self.term_frequencies = [Counter(doc) for doc in self.docs]
        document_frequency = Counter()
        for doc in self.docs:
            document_frequency.update(set(doc))
        total = len(self.docs)
        self.idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in document_frequency.items()
        }

    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        query_terms = tokenize(query)
        scored = []
        for index, frequencies in enumerate(self.term_frequencies):
            score = 0.0
            length = self.doc_lengths[index]
            norm = self.k1 * (1 - self.b + self.b * length / max(self.avgdl, 1))
            for term in query_terms:
                frequency = frequencies.get(term, 0)
                if frequency:
                    score += self.idf.get(term, 0.0) * frequency * (self.k1 + 1) / (frequency + norm)
            if score > 0:
                scored.append((self.chunks[index], score))
        scored.sort(key=lambda row: row[1], reverse=True)
        return scored[:top_k]


class ExistingDenseRetriever:
    def __init__(self, kb_path: Path, max_retries: int = 8, retry_base_seconds: float = 2.0):
        try:
            from hybrid_rag_review_v8 import HybridRAGReviewerV8
        except ImportError as exc:
            raise RuntimeError(
                "Dense retrieval requires the existing project dependencies: "
                "dashscope, chromadb, rank_bm25, jieba, and openai."
            ) from exc
        self.reviewer = HybridRAGReviewerV8()
        self.reviewer.load_knowledge_base(str(kb_path))
        self.reviewer.load_or_build_index()
        self.chunk_identity_by_global_id = build_document_local_chunk_identity(
            self.reviewer.kb_chunks
        )
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds

    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float]]:
        last_error: BaseException | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                rows = self.reviewer.vector_search(query, k=top_k)
                break
            except Exception as exc:  # noqa: BLE001 - keep pipeline resumable across network/client errors.
                last_error = exc
                if attempt >= self.max_retries:
                    raise
                delay = min(self.retry_base_seconds * (2 ** (attempt - 1)), 60.0)
                print(
                    f"Dense retrieval failed on attempt {attempt}/{self.max_retries}: {exc}. "
                    f"Retrying in {delay:.1f}s...",
                    flush=True,
                )
                time.sleep(delay)
        else:
            raise RuntimeError("Dense retrieval failed without raising an exception") from last_error

        normalized = []
        for chunk, score in rows:
            global_id = str(chunk["id"])
            if global_id not in self.chunk_identity_by_global_id:
                raise KeyError(f"Dense result references unknown knowledge-base chunk ID: {global_id}")
            local_index, chunk_id = self.chunk_identity_by_global_id[global_id]
            item = dict(chunk)
            item["kb_chunk_index"] = local_index
            item["chunk_id"] = chunk_id
            normalized.append((item, float(score)))
        return normalized


class OptionalReranker:
    def __init__(self, backend: str, model: str, batch_size: int = 8, max_length: int = 4096):
        self.backend = backend
        self.model_name = self._validate_model_reference(model) if backend != "none" else model
        model = self.model_name
        self.batch_size = batch_size
        self.max_length = max_length
        if backend == "none":
            self.model = None
        elif backend == "cross-encoder":
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RuntimeError("Install sentence-transformers to use --reranker cross-encoder") from exc
            self.model = CrossEncoder(model, trust_remote_code=True)
        elif backend == "flagembedding":
            try:
                from FlagEmbedding import FlagReranker
            except ImportError as exc:
                raise RuntimeError("Install FlagEmbedding to use --reranker flagembedding") from exc
            self.model = FlagReranker(model, use_fp16=True)
        elif backend == "qwen3":
            try:
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer
            except ImportError as exc:
                raise RuntimeError("Install torch and transformers to use --reranker qwen3") from exc
            self.torch = torch
            self.tokenizer = AutoTokenizer.from_pretrained(model, padding_side="left", trust_remote_code=True)
            self.model = AutoModelForCausalLM.from_pretrained(
                model,
                torch_dtype="auto",
                device_map="auto",
                trust_remote_code=True,
            ).eval()
            self.false_id = self.tokenizer.convert_tokens_to_ids("no")
            self.true_id = self.tokenizer.convert_tokens_to_ids("yes")
            self.qwen_prefix = (
                "<|im_start|>system\n"
                "Judge whether the Document meets the requirements based on the Query. "
                "The answer can only be yes or no.<|im_end|>\n"
                "<|im_start|>user\n"
            )
            self.qwen_suffix = (
                "<|im_end|>\n<|im_start|>assistant\n"
                "<think>\n\n</think>\n\n"
            )
        else:
            raise ValueError(f"Unsupported reranker backend: {backend}")

    @staticmethod
    def _validate_model_reference(model: str) -> str:
        model_path = Path(model).expanduser()
        looks_like_local_path = model_path.is_absolute() or "\\" in model or model.startswith(".")
        if not looks_like_local_path:
            return model
        if not model_path.is_dir():
            raise FileNotFoundError(
                f"Local reranker directory does not exist: {model_path}. "
                "Use a complete local directory or the Hub model ID BAAI/bge-reranker-v2-m3."
            )
        required = ("config.json", "model.safetensors")
        missing = [name for name in required if not (model_path / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"Local reranker directory is incomplete: {model_path}; missing: {', '.join(missing)}"
            )
        return str(model_path.resolve())

    def score(self, query: str, candidates: Sequence[Dict[str, Any]]) -> List[float]:
        if self.model is None:
            return [float(candidate["rrf_score"]) for candidate in candidates]
        pairs = [[query, retrieval_text(candidate)] for candidate in candidates]
        if self.backend == "cross-encoder":
            scores = self.model.predict(pairs)
        elif self.backend == "flagembedding":
            scores = self.model.compute_score(pairs, normalize=True)
        else:
            scores = self._score_qwen3(pairs)
        if not isinstance(scores, (list, tuple)):
            scores = [scores]
        return [float(score) for score in scores]

    def _score_qwen3(self, pairs: Sequence[Sequence[str]]) -> List[float]:
        scores: List[float] = []
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start:start + self.batch_size]
            prompts = [
                f"{self.qwen_prefix}<Query>: {query}\n<Document>: {document}\n{self.qwen_suffix}"
                for query, document in batch
            ]
            encoded = self.tokenizer(
                prompts,
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            ).to(self.model.device)
            with self.torch.no_grad():
                logits = self.model(**encoded).logits[:, -1, [self.false_id, self.true_id]]
                probabilities = self.torch.nn.functional.softmax(logits, dim=-1)[:, 1]
            scores.extend(probabilities.detach().float().cpu().tolist())
        return scores


def merge_results(
    dense: Sequence[Tuple[Dict[str, Any], float]],
    bm25: Sequence[Tuple[Dict[str, Any], float]],
    rrf_k: int = 60,
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for source, rows in (("dense", dense), ("bm25", bm25)):
        for rank, (chunk, score) in enumerate(rows, start=1):
            chunk_id = chunk["chunk_id"]
            record = merged.setdefault(
                chunk_id,
                {
                    "chunk_id": chunk_id,
                    "doc_name": chunk.get("doc_name", ""),
                    "chapter": chunk.get("chapter", ""),
                    "section": chunk.get("section", ""),
                    "article": chunk.get("article", ""),
                    "content": chunk.get("content", ""),
                    "retrieval_text": retrieval_text(chunk),
                    "retrieval_details": {},
                    "rrf_score": 0.0,
                },
            )
            record["retrieval_details"][source] = {"rank": rank, "score": float(score)}
            record["rrf_score"] += 1.0 / (rrf_k + rank)
    return sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)


def evaluate_ranks(candidates: Sequence[Dict[str, Any]], targets: Sequence[str]) -> Dict[str, Any]:
    target_set = set(targets)
    target_ranks = [
        index for index, candidate in enumerate(candidates, start=1)
        if candidate["chunk_id"] in target_set
    ]
    return {
        "target_count": len(target_set),
        "found_target_count": len(target_ranks),
        "first_target_rank": min(target_ranks) if target_ranks else None,
        "recall_at": {
            str(k): len([rank for rank in target_ranks if rank <= k]) / len(target_set)
            if target_set else None
            for k in (5, 10, 20, 50, 100)
        },
    }


def source_audit(rows: Sequence[Tuple[Dict[str, Any], float]]) -> List[Dict[str, Any]]:
    return [
        {"rank": rank, "chunk_id": chunk["chunk_id"], "score": float(score)}
        for rank, (chunk, score) in enumerate(rows, start=1)
    ]


def union_audit(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "rank": rank,
            "chunk_id": row["chunk_id"],
            "rrf_score": float(row["rrf_score"]),
            "rerank_score": float(row["rerank_score"]),
            "retrieval_details": row["retrieval_details"],
        }
        for rank, row in enumerate(rows, start=1)
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--final-top-k", type=int, default=15)
    parser.add_argument("--dense", action="store_true", help="Enable existing DashScope + Chroma dense retrieval")
    parser.add_argument("--dense-retries", type=int, default=8)
    parser.add_argument("--dense-retry-base-seconds", type=float, default=2.0)
    parser.add_argument("--reranker", choices=("none", "cross-encoder", "flagembedding", "qwen3"), default="none")
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3")
    parser.add_argument("--reranker-batch-size", type=int, default=8)
    parser.add_argument("--reranker-max-length", type=int, default=4096)
    parser.add_argument("--strict-only", action="store_true")
    parser.add_argument(
        "--priorities",
        nargs="+",
        choices=("high", "medium", "low"),
        default=("high", "medium"),
        help="Atomic claim priorities to retrieve; default excludes low-value table/context rows",
    )
    parser.add_argument(
        "--allow-parent-targets",
        action="store_true",
        help="Use parent-case evidence as provisional claim targets; disabled by default to avoid false claim-level gold",
    )
    parser.add_argument("--max-claims", type=int, default=0, help="Debug limit; 0 means all claims")
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing compatible output and start over")
    return parser.parse_args()


def build_configuration(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "bm25_tokenizer": "jieba" if jieba is not None else "character_fallback",
        "dense_enabled": args.dense,
        "dense_chunk_id_strategy": "document_local_v1" if args.dense else "",
        "retrieval_text_strategy": "parent_headings_plus_content_v1",
        "bm25_top_k": args.top_k,
        "dense_top_k": args.top_k if args.dense else 0,
        "dense_retries": args.dense_retries,
        "dense_retry_base_seconds": args.dense_retry_base_seconds,
        "reranker": args.reranker,
        "reranker_model": args.reranker_model if args.reranker != "none" else "",
        "reranker_batch_size": args.reranker_batch_size,
        "reranker_max_length": args.reranker_max_length,
        "final_top_k": args.final_top_k,
        "claim_priorities": list(args.priorities),
        "allow_parent_targets": args.allow_parent_targets,
    }


def make_payload(
    args: argparse.Namespace,
    configuration: Dict[str, Any],
    results: Sequence[Dict[str, Any]],
    errors: Sequence[Dict[str, Any]],
    status: str,
) -> Dict[str, Any]:
    return {
        "schema": "coal_rag_atomic_retrieval_results_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
        "claims_source": str(args.claims.resolve()),
        "kb_source": str(args.kb.resolve()),
        "configuration": configuration,
        "claim_result_count": len(results),
        "error_count": len(errors),
        "errors": list(errors),
        "results": list(results),
    }


def load_resume_state(
    output: Path,
    args: argparse.Namespace,
    configuration: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    if args.no_resume or not output.exists():
        return [], []
    try:
        payload = load_json(output)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not read existing output for resume: {exc}. Starting fresh.", flush=True)
        return [], []

    compatible = (
        payload.get("schema") == "coal_rag_atomic_retrieval_results_v9_v1"
        and payload.get("claims_source") == str(args.claims.resolve())
        and payload.get("kb_source") == str(args.kb.resolve())
        and payload.get("configuration") == configuration
    )
    if not compatible:
        print("Existing output is not compatible with current configuration; starting fresh.", flush=True)
        return [], []

    results = payload.get("results", [])
    completed_claim_ids = {row.get("claim_id") for row in results}
    errors = [
        error for error in payload.get("errors", [])
        if error.get("claim_id") not in completed_claim_ids
    ]
    removed_error_count = len(payload.get("errors", [])) - len(errors)
    if removed_error_count:
        print(
            f"Removed {removed_error_count} stale error(s) for already completed claims.",
            flush=True,
        )
    print(f"Resuming from existing output: {len(results)} completed claim(s), {len(errors)} error(s).", flush=True)
    return results, errors


def iter_selected_claims(dataset: Dict[str, Any], args: argparse.Namespace) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    selected = []
    for case in dataset["cases"]:
        if args.strict_only and not case.get("include_in_strict_recall"):
            continue
        for claim in case["claims"]:
            if claim.get("retrieval_priority", "medium") not in args.priorities:
                continue
            selected.append((case, claim))
            if args.max_claims and len(selected) >= args.max_claims:
                return selected
    return selected


def main() -> None:
    args = parse_args()
    dataset = load_json(args.claims)
    kb_chunks = flatten_kb(load_json(args.kb))
    bm25 = SimpleBM25(kb_chunks)
    configuration = build_configuration(args)
    results, errors = load_resume_state(args.output, args, configuration)
    completed_claim_ids = {row["claim_id"] for row in results}

    dense = ExistingDenseRetriever(
        args.kb,
        max_retries=args.dense_retries,
        retry_base_seconds=args.dense_retry_base_seconds,
    ) if args.dense else None
    reranker = OptionalReranker(
        args.reranker,
        args.reranker_model,
        batch_size=args.reranker_batch_size,
        max_length=args.reranker_max_length,
    )

    selected_claims = iter_selected_claims(dataset, args)
    total_claims = len(selected_claims)
    if completed_claim_ids:
        print(f"Skipping {len(completed_claim_ids)} already completed claim(s).", flush=True)

    for index, (case, claim) in enumerate(selected_claims, start=1):
        if claim["claim_id"] in completed_claim_ids:
            continue
        print(f"[{index}/{total_claims}] Processing {claim['claim_id']}", flush=True)
        try:
            query = claim["retrieval_query"]
            dense_rows = dense.search(query, args.top_k) if dense else []
            bm25_rows = bm25.search(query, args.top_k)
            merged = merge_results(dense_rows, bm25_rows)
            scores = reranker.score(query, merged)
            for candidate, score in zip(merged, scores):
                candidate["rerank_score"] = score
            merged.sort(key=lambda candidate: candidate["rerank_score"], reverse=True)
            for rank, candidate in enumerate(merged, start=1):
                candidate["final_rank"] = rank
            targets = claim.get("target_evidence_ids", [])
            if not targets and args.allow_parent_targets:
                targets = claim.get("parent_target_evidence_ids", [])
            results.append({
                "case_id": case["case_id"],
                "claim_id": claim["claim_id"],
                "evaluation_task_type": case["evaluation_task_type"],
                "include_in_strict_recall": case["include_in_strict_recall"],
                "query": query,
                "target_evidence_ids": targets,
                "metrics": {
                    "dense": evaluate_ranks(
                        [{"chunk_id": chunk["chunk_id"]} for chunk, _score in dense_rows],
                        targets,
                    ),
                    "bm25": evaluate_ranks(
                        [{"chunk_id": chunk["chunk_id"]} for chunk, _score in bm25_rows],
                        targets,
                    ),
                    "final": evaluate_ranks(merged, targets),
                },
                "candidate_count_before_final_cut": len(merged),
                "source_top_k": {
                    "dense": source_audit(dense_rows),
                    "bm25": source_audit(bm25_rows),
                },
                "union_reranked_audit": union_audit(merged),
                "candidates": merged[: args.final_top_k],
            })
            completed_claim_ids.add(claim["claim_id"])
            write_json(args.output, make_payload(args, configuration, results, errors, "running"))
        except Exception as exc:  # noqa: BLE001
            error = {
                "case_id": case["case_id"],
                "claim_id": claim["claim_id"],
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            errors.append(error)
            try:
                write_json(args.output, make_payload(args, configuration, results, errors, "failed"))
                print(f"Failed at {claim['claim_id']}. Progress was saved to {args.output}", flush=True)
            except Exception as save_exc:  # noqa: BLE001
                print(
                    f"Failed at {claim['claim_id']}, and checkpoint save also failed: {save_exc}. "
                    f"Temporary checkpoint retained beside {args.output}.",
                    flush=True,
                )
            raise

    write_json(args.output, make_payload(args, configuration, results, errors, "complete"))
    print(f"Claims processed: {len(results)}")
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
