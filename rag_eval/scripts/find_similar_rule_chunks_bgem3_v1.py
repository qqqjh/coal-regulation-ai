"""Find semantically similar rule chunks with local BGE-M3 dense embeddings.

This script is a candidate-discovery tool. High cosine similarity does not
automatically mean that two rules are duplicates: applicability, authority,
numeric thresholds, and exceptions still require human review.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KB = PROJECT_ROOT / "chunks_visualization" / "chunks_v6_latest.json"
DEFAULT_MODEL = PROJECT_ROOT / "models" / "bge-m3"
DEFAULT_CACHE = PROJECT_ROOT / "rag_eval" / "data" / "rule_similarity_bgem3_embeddings_v6.npz"
DEFAULT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "similar_rule_pairs_bgem3_v6.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "similar_rule_pairs_bgem3_v6.html"

NUMBER_PATTERN = re.compile(r"(?<![\w.])(?:[<>≥≤±~～-]?\s*)?\d+(?:\.\d+)?\s*(?:%|m|mm|cm|MPa|kPa|kN|N|h|min|s|°|人|组|次)?", re.I)
NEGATION_TERMS = ("不得", "严禁", "禁止", "不应", "不准", "必须", "应当", "可以", "可")
ROLE_ALLOWLIST = {"article_rule", "rule", "table_rule"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    temp.replace(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def flatten_kb(kb_data: Dict[str, List[Dict[str, Any]]], include_non_rules: bool) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for doc_name, rows in kb_data.items():
        for index, row in enumerate(rows, start=1):
            if row.get("retrievable", True) is False:
                continue
            if not include_non_rules and row.get("semantic_role") not in ROLE_ALLOWLIST:
                continue
            item = dict(row)
            item["doc_name"] = doc_name
            item["chunk_id"] = f"{doc_name}::chunk{index}"
            item["chunk_no"] = index
            item["retrieval_text"] = str(row.get("retrieval_text") or row.get("content", ""))
            chunks.append(item)
    return chunks


def extract_numbers(text: str) -> List[str]:
    return sorted({re.sub(r"\s+", "", match.group(0)) for match in NUMBER_PATTERN.finditer(text or "")})


def extract_negations(text: str) -> List[str]:
    return [term for term in NEGATION_TERMS if term in (text or "")]


def exact_text_key(text: str) -> str:
    normalized = compact(text)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def encode_chunks(
    chunks: Sequence[Dict[str, Any]],
    model_path: Path,
    cache_path: Path,
    kb_hash: str,
    batch_size: int,
    max_length: int,
    rebuild_cache: bool,
) -> np.ndarray:
    expected_ids = np.asarray([chunk["canonical_rule_id"] for chunk in chunks])
    if cache_path.exists() and not rebuild_cache:
        cached = np.load(cache_path, allow_pickle=False)
        if (
            str(cached["kb_sha256"].item()) == kb_hash
            and np.array_equal(cached["canonical_rule_ids"], expected_ids)
        ):
            print(f"Using embedding cache: {cache_path}", flush=True)
            return np.asarray(cached["embeddings"], dtype=np.float32)

    if not model_path.is_dir():
        raise FileNotFoundError(f"BGE-M3 model not found: {model_path}")

    from FlagEmbedding import BGEM3FlagModel

    print(f"Loading BGE-M3: {model_path}", flush=True)
    model = BGEM3FlagModel(
        str(model_path.resolve()),
        normalize_embeddings=True,
        use_fp16=True,
        devices="cuda",
        batch_size=batch_size,
        passage_max_length=max_length,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    print(f"Encoding {len(chunks)} rule chunks...", flush=True)
    encoded = model.encode(
        [chunk["retrieval_text"] for chunk in chunks],
        batch_size=batch_size,
        max_length=max_length,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    embeddings = np.asarray(encoded["dense_vecs"], dtype=np.float32)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        cache_path,
        kb_sha256=np.asarray(kb_hash),
        canonical_rule_ids=expected_ids,
        embeddings=embeddings,
    )
    print(f"Written embedding cache: {cache_path}", flush=True)
    return embeddings


def build_pairs(
    chunks: Sequence[Dict[str, Any]],
    embeddings: np.ndarray,
    threshold: float,
    same_document: bool,
    max_pairs: int,
) -> List[Dict[str, Any]]:
    similarities = embeddings @ embeddings.T
    pairs: List[Dict[str, Any]] = []
    for left_index in range(len(chunks)):
        left = chunks[left_index]
        for right_index in range(left_index + 1, len(chunks)):
            right = chunks[right_index]
            if not same_document and left["doc_name"] == right["doc_name"]:
                continue
            score = float(similarities[left_index, right_index])
            if score < threshold:
                continue
            left_numbers = extract_numbers(left["content"])
            right_numbers = extract_numbers(right["content"])
            left_negations = extract_negations(left["content"])
            right_negations = extract_negations(right["content"])
            exact_match = exact_text_key(left["content"]) == exact_text_key(right["content"])
            pairs.append({
                "pair_id": f"pair_{len(pairs) + 1:04d}",
                "cosine_similarity": score,
                "exact_text_match": exact_match,
                "numeric_difference": left_numbers != right_numbers,
                "negation_difference": left_negations != right_negations,
                "shared_numbers": sorted(set(left_numbers) & set(right_numbers)),
                "left_only_numbers": sorted(set(left_numbers) - set(right_numbers)),
                "right_only_numbers": sorted(set(right_numbers) - set(left_numbers)),
                "left": pair_chunk_payload(left, left_negations),
                "right": pair_chunk_payload(right, right_negations),
            })
    pairs.sort(key=lambda pair: pair["cosine_similarity"], reverse=True)
    return pairs[:max_pairs] if max_pairs > 0 else pairs


def pair_chunk_payload(chunk: Dict[str, Any], negations: List[str]) -> Dict[str, Any]:
    return {
        "chunk_id": chunk["chunk_id"],
        "canonical_rule_id": chunk.get("canonical_rule_id", ""),
        "doc_name": chunk["doc_name"],
        "chunk_no": chunk["chunk_no"],
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "article": chunk.get("article", ""),
        "page_range": chunk.get("page_range", ""),
        "semantic_role": chunk.get("semantic_role", ""),
        "numbers": extract_numbers(chunk.get("content", "")),
        "negation_terms": negations,
        "content": chunk.get("content", ""),
        "retrieval_text": chunk.get("retrieval_text", ""),
    }


def render_html(pairs: Sequence[Dict[str, Any]], output: Path, metadata: Dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cards = []
    for index, pair in enumerate(pairs, start=1):
        flags = []
        if pair["exact_text_match"]:
            flags.append("<span class='flag exact'>正文完全相同</span>")
        if pair["numeric_difference"]:
            flags.append("<span class='flag warning'>数值存在差异</span>")
        if pair["negation_difference"]:
            flags.append("<span class='flag danger'>规范词存在差异</span>")
        cards.append(
            f"<section class='pair' data-score='{pair['cosine_similarity']:.6f}' "
            f"data-risk='{int(pair['numeric_difference'] or pair['negation_difference'])}'>"
            f"<header><div><b>#{index}</b> 余弦相似度 <strong>{pair['cosine_similarity']:.4f}</strong></div>"
            f"<div>{''.join(flags)}</div></header>"
            f"<div class='comparison'>{chunk_card(pair['left'], 'A')}{chunk_card(pair['right'], 'B')}</div>"
            f"<details><summary>数值差异</summary><pre>{html.escape(json.dumps({k: pair[k] for k in ('shared_numbers','left_only_numbers','right_only_numbers')}, ensure_ascii=False, indent=2))}</pre></details>"
            "</section>"
        )

    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BGE-M3 相似规则检查</title>
<style>
body{{font-family:"Microsoft YaHei",Arial;margin:18px;background:#f4f6f7;color:#20272d}}
.summary,.pair{{background:#fff;border:1px solid #ccd3d8;margin-bottom:14px;padding:14px}}
.toolbar{{position:sticky;top:0;background:#f4f6f7;padding:10px 0;z-index:2}}
.toolbar input{{width:90px;padding:5px}}button{{padding:6px 10px;margin-left:6px}}
header{{display:flex;justify-content:space-between;gap:10px;align-items:center;border-bottom:1px solid #dde2e5;padding-bottom:10px}}
.comparison{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}}
.rule{{min-width:0;border-left:4px solid #2e718f;padding:10px;background:#fafbfc}}
.meta{{color:#657681;font-size:13px;overflow-wrap:anywhere;margin:4px 0}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font-family:"Microsoft YaHei",Arial;line-height:1.65}}
.flag{{display:inline-block;padding:3px 7px;margin-left:5px;font-size:12px;border:1px solid #adb8bf}}
.exact{{border-color:#2c805c;color:#176144}}.warning{{border-color:#ca8a20;color:#89590b}}.danger{{border-color:#c64b45;color:#8e2824}}
.hidden{{display:none}} @media(max-width:900px){{.comparison{{grid-template-columns:1fr}}}}
</style></head><body>
<section class="summary"><h1>BGE-M3 跨文档相似规则检查</h1>
<pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre>
<p>高相似度仅表示应人工检查。不要直接删除；重点核对适用范围、规范效力、数值阈值、否定词和例外条件。</p></section>
<div class="toolbar">最低显示相似度 <input id="threshold" type="number" min="0" max="1" step="0.01" value="{metadata['threshold']:.2f}">
<label><input id="riskOnly" type="checkbox"> 仅显示数值/规范词差异</label><button onclick="applyFilter()">筛选</button>
<span id="visibleCount"></span></div>
{''.join(cards)}
<script>
function applyFilter(){{
 const threshold=parseFloat(document.getElementById('threshold').value||0);
 const riskOnly=document.getElementById('riskOnly').checked; let visible=0;
 document.querySelectorAll('.pair').forEach(card=>{{
   const show=parseFloat(card.dataset.score)>=threshold && (!riskOnly || card.dataset.risk==='1');
   card.classList.toggle('hidden',!show); if(show) visible++;
 }});
 document.getElementById('visibleCount').textContent='显示 '+visible+' / {len(pairs)}+' 对';
}}
applyFilter();
</script></body></html>"""
    output.write_text(page, encoding="utf-8")


def chunk_card(chunk: Dict[str, Any], label: str) -> str:
    return (
        f"<article class='rule'><h2>{label} · {html.escape(chunk['chunk_id'])}</h2>"
        f"<div class='meta'>稳定ID：{html.escape(chunk['canonical_rule_id'])}</div>"
        f"<div class='meta'>章：{html.escape(str(chunk['chapter']))}　节：{html.escape(str(chunk['section']))}　"
        f"条：{html.escape(str(chunk['article']))}　页码：{html.escape(str(chunk['page_range']))}</div>"
        f"<div class='meta'>规范词：{html.escape('、'.join(chunk['negation_terms']) or '无')}</div>"
        f"<details open><summary>规则全文</summary><pre>{html.escape(chunk['content'])}</pre></details>"
        f"<details><summary>实际检索文本</summary><pre>{html.escape(chunk['retrieval_text'])}</pre></details></article>"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb", type=Path, default=DEFAULT_KB)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--threshold", type=float, default=0.90)
    parser.add_argument("--max-pairs", type=int, default=1000)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--same-document", action="store_true")
    parser.add_argument("--include-non-rules", action="store_true")
    parser.add_argument("--rebuild-cache", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    kb_hash = sha256(args.kb)
    chunks = flatten_kb(load_json(args.kb), args.include_non_rules)
    print(f"Comparable chunks: {len(chunks)}", flush=True)
    embeddings = encode_chunks(
        chunks, args.model, args.cache, kb_hash, args.batch_size, args.max_length, args.rebuild_cache
    )
    pairs = build_pairs(chunks, embeddings, args.threshold, args.same_document, args.max_pairs)
    metadata = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "kb_source": str(args.kb.resolve()),
        "kb_sha256": kb_hash,
        "embedding_model": str(args.model.resolve()),
        "comparable_chunk_count": len(chunks),
        "cross_document_only": not args.same_document,
        "threshold": args.threshold,
        "pair_count": len(pairs),
        "exact_text_pair_count": sum(pair["exact_text_match"] for pair in pairs),
        "numeric_difference_pair_count": sum(pair["numeric_difference"] for pair in pairs),
        "negation_difference_pair_count": sum(pair["negation_difference"] for pair in pairs),
        "role_counts": dict(Counter(chunk.get("semantic_role") for chunk in chunks)),
        "warning": "High cosine similarity is a review candidate, not an automatic duplicate decision.",
    }
    atomic_write_json(args.json, {"schema": "similar_rule_pairs_bgem3_v6_v1", "metadata": metadata, "pairs": pairs})
    render_html(pairs, args.html, metadata)
    print(json.dumps(metadata, ensure_ascii=False, indent=2), flush=True)
    print(f"Written JSON: {args.json}", flush=True)
    print(f"Written HTML: {args.html}", flush=True)


if __name__ == "__main__":
    main()
