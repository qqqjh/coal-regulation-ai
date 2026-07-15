"""Merge claim-level retrieval evidence back into the 50 parent pending chunks.

Selection policy:
- every claim contributes its TopN candidates to the parent merge pool;
- duplicate regulation chunks are merged and record all covered claims;
- final parent evidence first covers as many claims as possible;
- remaining slots prefer evidence covering more claims and ranking higher;
- parent chunks with no retrieval claims remain in the output with an empty list.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLAIMS = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
DEFAULT_RETRIEVAL = PROJECT_ROOT / "rag_eval" / "data" / "atomic_retrieval_results_v9_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "parent_chunk_evidence_v9_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "parent_chunk_evidence_review_v9_v1.html"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def claim_lookup(claim_dataset: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {
        claim["claim_id"]: {
            "case_id": case["case_id"],
            "claim_text": claim["claim_text"],
            "retrieval_query": claim["retrieval_query"],
            "retrieval_priority": claim["retrieval_priority"],
        }
        for case in claim_dataset["cases"]
        for claim in case["claims"]
    }


def aggregate_candidates(
    claim_results: Sequence[Dict[str, Any]],
    claims: Dict[str, Dict[str, Any]],
    per_claim_pool: int,
) -> List[Dict[str, Any]]:
    evidence: Dict[str, Dict[str, Any]] = {}
    for result in claim_results:
        claim_id = result["claim_id"]
        for candidate in result["candidates"][:per_claim_pool]:
            chunk_id = candidate["chunk_id"]
            evidence_group_id = candidate.get("evidence_group_id", "")
            aggregation_key = evidence_group_id or chunk_id
            record = evidence.setdefault(
                aggregation_key,
                {
                    "chunk_id": chunk_id,
                    "evidence_group_id": evidence_group_id,
                    "group_member_chunk_ids": [],
                    "doc_name": candidate.get("doc_name", ""),
                    "chapter": candidate.get("chapter", ""),
                    "section": candidate.get("section", ""),
                    "article": candidate.get("article", ""),
                    "content": candidate.get("content", ""),
                    "representative_rerank_score": float(candidate["rerank_score"]),
                    "claim_hits": [],
                },
            )
            if chunk_id not in record["group_member_chunk_ids"]:
                record["group_member_chunk_ids"].append(chunk_id)
            if float(candidate["rerank_score"]) > record["representative_rerank_score"]:
                record.update(
                    {
                        "chunk_id": chunk_id,
                        "doc_name": candidate.get("doc_name", ""),
                        "chapter": candidate.get("chapter", ""),
                        "section": candidate.get("section", ""),
                        "article": candidate.get("article", ""),
                        "content": candidate.get("content", ""),
                        "representative_rerank_score": float(candidate["rerank_score"]),
                    }
                )
            record["claim_hits"].append({
                "claim_id": claim_id,
                "claim_text": claims[claim_id]["claim_text"],
                "claim_priority": claims[claim_id]["retrieval_priority"],
                "claim_rank": candidate["final_rank"],
                "rerank_score": float(candidate["rerank_score"]),
                "rrf_score": float(candidate["rrf_score"]),
                "retrieval_details": candidate["retrieval_details"],
            })

    aggregated = []
    for record in evidence.values():
        ranks = [hit["claim_rank"] for hit in record["claim_hits"]]
        rerank_scores = [hit["rerank_score"] for hit in record["claim_hits"]]
        record["covered_claim_ids"] = list(dict.fromkeys(hit["claim_id"] for hit in record["claim_hits"]))
        record["coverage_count"] = len(record["covered_claim_ids"])
        record["best_claim_rank"] = min(ranks)
        record["reciprocal_rank_sum"] = sum(1.0 / rank for rank in ranks)
        record["best_rerank_score"] = max(rerank_scores)
        record["mean_rerank_score"] = mean(rerank_scores)
        aggregated.append(record)
    aggregated.sort(key=sort_key, reverse=True)
    return aggregated


def sort_key(candidate: Dict[str, Any]) -> tuple:
    return (
        candidate["coverage_count"],
        candidate["reciprocal_rank_sum"],
        candidate["best_rerank_score"],
        -candidate["best_claim_rank"],
    )


def select_parent_evidence(
    candidates: Sequence[Dict[str, Any]],
    claim_ids: Sequence[str],
    final_top_k: int,
) -> List[Dict[str, Any]]:
    remaining = list(candidates)
    selected: List[Dict[str, Any]] = []
    uncovered = set(claim_ids)

    # Coverage-first greedy selection: choose evidence that covers the most
    # currently uncovered claims, then break ties by aggregate rank quality.
    while uncovered and remaining and len(selected) < final_top_k:
        best = max(
            remaining,
            key=lambda item: (
                len(uncovered & set(item["covered_claim_ids"])),
                *sort_key(item),
            ),
        )
        if not (uncovered & set(best["covered_claim_ids"])):
            break
        selected.append(best)
        remaining.remove(best)
        uncovered -= set(best["covered_claim_ids"])

    for candidate in sorted(remaining, key=sort_key, reverse=True):
        if len(selected) >= final_top_k:
            break
        selected.append(candidate)

    for rank, candidate in enumerate(selected, start=1):
        candidate["parent_rank"] = rank
        candidate["selection_reason"] = (
            f"覆盖{candidate['coverage_count']}个检索单元；"
            f"最佳单元排名Top{candidate['best_claim_rank']}；"
            f"倒数排名和{candidate['reciprocal_rank_sum']:.4f}"
        )
    return selected


def build_parent_cases(
    claim_dataset: Dict[str, Any],
    retrieval: Dict[str, Any],
    per_claim_pool: int,
    final_top_k: int,
) -> List[Dict[str, Any]]:
    claims = claim_lookup(claim_dataset)
    results_by_case: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for result in retrieval["results"]:
        results_by_case[result["case_id"]].append(result)

    parent_cases = []
    for case in claim_dataset["cases"]:
        case_id = case["case_id"]
        claim_results = results_by_case.get(case_id, [])
        claim_ids = [result["claim_id"] for result in claim_results]
        candidates = aggregate_candidates(claim_results, claims, per_claim_pool)
        selected = select_parent_evidence(candidates, claim_ids, final_top_k)
        covered_claim_ids = {
            claim_id for evidence in selected for claim_id in evidence["covered_claim_ids"]
        }
        parent_cases.append({
            "case_id": case_id,
            "evaluation_task_type": case["evaluation_task_type"],
            "target_evidence_status": case["target_evidence_status"],
            "pending": case["pending"],
            "retrieval_claim_count": len(claim_ids),
            "retrieval_claims": [claims[claim_id] | {"claim_id": claim_id} for claim_id in claim_ids],
            "merge_pool_candidate_count": len(candidates),
            "final_evidence_count": len(selected),
            "covered_claim_count": len(covered_claim_ids),
            "uncovered_claim_ids": [claim_id for claim_id in claim_ids if claim_id not in covered_claim_ids],
            "final_evidence": selected,
        })
    return parent_cases


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render_html(payload: Dict[str, Any]) -> str:
    cards = []
    for case in payload["cases"]:
        evidence_html = []
        for evidence in case["final_evidence"]:
            hits = "".join(
                f"<li><b>{esc(hit['claim_id'])}</b> Top{hit['claim_rank']}：{esc(hit['claim_text'])}</li>"
                for hit in evidence["claim_hits"]
            )
            evidence_html.append(
                f"""
                <article class="evidence">
                  <div class="evidence-head">
                    <b>#{evidence['parent_rank']} {esc(evidence['chunk_id'])}</b>
                    <span>覆盖 {evidence['coverage_count']} 个单元{f" · 证据组 {esc(evidence['evidence_group_id'])}" if evidence.get('evidence_group_id') else ""}</span>
                  </div>
                  <p>{esc(evidence['selection_reason'])}</p>
                  <details><summary>覆盖的检索单元</summary><ul>{hits}</ul></details>
                  <details><summary>法规全文</summary><pre>{esc(evidence['content'])}</pre></details>
                </article>
                """
            )
        cards.append(
            f"""
            <section class="case" data-search="{esc((case['case_id'] + ' ' + case['pending'].get('content', '')).lower())}">
              <header>
                <div><h2>{esc(case['case_id'])}</h2><p>{esc(case['pending'].get('doc_name'))}</p></div>
                <div class="stats">检索单元 {case['retrieval_claim_count']} · 合并池 {case['merge_pool_candidate_count']} · 最终证据 {case['final_evidence_count']} · 已覆盖 {case['covered_claim_count']}</div>
              </header>
              <details><summary>父 chunk 原文</summary><pre>{esc(case['pending'].get('content'))}</pre></details>
              <div class="evidence-list">{''.join(evidence_html) or '<p class="empty">无父级候选证据</p>'}</div>
            </section>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>父 chunk 合并证据检查</title>
<style>
*{{box-sizing:border-box}} body{{margin:0;background:#f4f6f8;color:#18222c;font-family:"Microsoft YaHei",sans-serif}}
.top{{position:sticky;top:0;z-index:2;background:#fff;border-bottom:1px solid #cbd4dc;padding:18px 24px}} h1{{margin:0 0 6px;font-size:24px}} .top p{{margin:4px 0;color:#52616f}}
input{{margin-top:10px;width:min(680px,100%);padding:10px;border:1px solid #aab6c2;border-radius:4px}}
main{{max-width:1300px;margin:auto;padding:20px}} .case{{background:#fff;border:1px solid #ccd4dc;margin-bottom:18px}}
.case>header{{display:flex;justify-content:space-between;gap:16px;padding:16px;border-bottom:1px solid #dce2e8}} h2{{margin:0;font-size:18px}} header p{{margin:5px 0;color:#52616f}} .stats{{font-size:13px;color:#385064}}
.case>details{{padding:12px 16px;border-bottom:1px solid #dce2e8}} summary{{cursor:pointer;font-weight:700}} pre{{white-space:pre-wrap;line-height:1.65;font-family:inherit}}
.evidence-list{{padding:16px}} .evidence{{border-left:4px solid #247a52;background:#f7f9fa;padding:12px;margin-bottom:12px}} .evidence-head{{display:flex;justify-content:space-between;gap:12px}}
.evidence p{{color:#52616f;font-size:13px}} li{{margin:6px 0;line-height:1.5}} .empty{{color:#7b8791}}
@media(max-width:800px){{.case>header,.evidence-head{{display:block}}}}
</style></head><body>
<div class="top"><h1>父 chunk 合并证据检查</h1>
<p>每个检索单元 Top{payload['configuration']['per_claim_pool']} 进入合并池；相同法规去重；父 chunk 最终最多保留 {payload['configuration']['final_top_k']} 条。</p>
<input id="search" placeholder="搜索 case_id 或父 chunk 内容"></div>
<main>{''.join(cards)}</main>
<script>const q=document.getElementById('search');q.addEventListener('input',()=>{{const v=q.value.trim().toLowerCase();document.querySelectorAll('.case').forEach(x=>x.style.display=!v||x.dataset.search.includes(v)?'':'none')}});</script>
</body></html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--retrieval", type=Path, default=DEFAULT_RETRIEVAL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--per-claim-pool", type=int, default=3)
    parser.add_argument("--final-top-k", type=int, default=25)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    claims = load_json(args.claims)
    retrieval = load_json(args.retrieval)
    cases = build_parent_cases(claims, retrieval, args.per_claim_pool, args.final_top_k)
    payload = {
        "schema": "coal_rag_parent_chunk_evidence_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claims_source": str(args.claims.resolve()),
        "retrieval_source": str(args.retrieval.resolve()),
        "configuration": {
            "per_claim_pool": args.per_claim_pool,
            "final_top_k": args.final_top_k,
            "selection": "coverage-first greedy, then coverage/rank aggregate",
            "source_reranker": retrieval.get("configuration", {}).get("reranker", "unknown"),
            "warning": (
                "Parent aggregation cannot correct irrelevant claim-level candidates. "
                "Coverage count is reliable only after a validated reranker."
            ),
        },
        "summary": {
            "parent_case_count": len(cases),
            "cases_with_retrieval_claims": sum(case["retrieval_claim_count"] > 0 for case in cases),
            "cases_without_retrieval_claims": sum(case["retrieval_claim_count"] == 0 for case in cases),
            "total_retrieval_claims": sum(case["retrieval_claim_count"] for case in cases),
            "total_final_evidence": sum(case["final_evidence_count"] for case in cases),
            "fully_covered_parent_cases_with_claims": sum(
                case["retrieval_claim_count"] > 0 and not case["uncovered_claim_ids"]
                for case in cases
            ),
        },
        "cases": cases,
    }
    write_json(args.output, payload)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    with args.html.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_html(payload))
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"Written: {args.output}")
    print(f"Written: {args.html}")


if __name__ == "__main__":
    main()
