"""Analyze where known useful evidence is lost in the BGE-M3 atomic pipeline."""

from __future__ import annotations

import argparse
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from merge_claim_evidence_to_parent_v1 import build_parent_cases


ROOT = Path(__file__).resolve().parents[2]
CLAIMS = ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
RETRIEVAL = ROOT / "rag_eval" / "data" / "atomic_retrieval_local_bgem3_grouped_v1.json"
INITIAL = ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
ATOMIC = ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
BGE_PARENT = ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json"
GROUPS = ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
OUTPUT = ROOT / "rag_eval" / "data" / "atomic_bgem3_merge_pool_analysis_v1.json"
HTML = ROOT / "rag_eval" / "reports" / "atomic_bgem3_merge_pool_analysis_v1.html"
POOLS = (1, 3, 5, 10, 15)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def build(claims_path: Path = CLAIMS, retrieval_path: Path = RETRIEVAL) -> dict[str, Any]:
    chunk_to_group = load(GROUPS)["chunk_to_group"]

    def key(item: dict[str, Any]) -> str:
        return item.get("evidence_group_id") or chunk_to_group.get(item["chunk_id"]) or item["chunk_id"]

    def useful(items: list[dict[str, Any]]) -> set[str]:
        return {key(item) for item in items if item.get("label") == "useful"}

    initial = {case["case_id"]: case for case in load(INITIAL)["cases"]}
    atomic = {case["case_id"]: case for case in load(ATOMIC)["cases"]}
    bge = {case["case_id"]: case for case in load(BGE_PARENT)["cases"]}
    references = {
        "initial": {
            case_id: useful(case["candidate_annotations"])
            for case_id, case in initial.items()
        },
        "old_atomic": {
            case_id: useful(case["evidence_annotations"])
            for case_id, case in atomic.items()
        },
        "bge_parent": {
            case_id: useful(case["candidate_annotations"])
            for case_id, case in bge.items()
        },
    }
    known = {
        case_id: (
            references["initial"][case_id]
            | references["old_atomic"][case_id]
            | references["bge_parent"][case_id]
        )
        for case_id in initial
    }
    references["union"] = known
    old_loss_cases = {
        case_id
        for case_id, case in initial.items()
        if useful(case["candidate_annotations"]) and not useful(atomic[case_id]["evidence_annotations"])
    }

    claims = load(claims_path)
    retrieval = load(retrieval_path)
    claim_results_by_case: dict[str, list[dict[str, Any]]] = {}
    for result in retrieval["results"]:
        claim_results_by_case.setdefault(result["case_id"], []).append(result)

    claim_recovered = {}
    source_recovered = {}
    min_ranks = {}
    for case_id, known_keys in known.items():
        ranks: dict[str, int] = {}
        source_keys = set()
        for result in claim_results_by_case.get(case_id, []):
            source_keys.update(
                key(candidate)
                for route in ("dense", "sparse")
                for candidate in result["source_top_k"][route]
            )
            for candidate in result["candidates"]:
                evidence_key = key(candidate)
                ranks[evidence_key] = min(ranks.get(evidence_key, 999), candidate["final_rank"])
        min_ranks[case_id] = ranks
        claim_recovered[case_id] = known_keys & set(ranks)
        source_recovered[case_id] = known_keys & source_keys

    pool_rows = []
    pool_cases: dict[int, dict[str, dict[str, Any]]] = {}
    for pool in POOLS:
        cases = build_parent_cases(claims, retrieval, pool, 25)
        by_case = {case["case_id"]: case for case in cases}
        pool_cases[pool] = by_case
        recovered_by_case = {
            case_id: known[case_id] & {key(item) for item in case["final_evidence"]}
            for case_id, case in by_case.items()
        }
        pool_rows.append(
            {
                "per_claim_pool": pool,
                "total_final_candidates": sum(case["final_evidence_count"] for case in cases),
                "known_useful_recovered": sum(len(items) for items in recovered_by_case.values()),
                "known_useful_recovery_rate": (
                    sum(len(items) for items in recovered_by_case.values())
                    / sum(len(items) for items in known.values())
                ),
                "cases_with_known_useful_recovered": sum(bool(items) for items in recovered_by_case.values()),
                "old_loss_cases_recovered": sum(
                    bool(recovered_by_case[case_id]) for case_id in old_loss_cases
                ),
            }
        )

    case_rows = []
    for case_id, known_keys in known.items():
        ranks = min_ranks[case_id]
        case_rows.append(
            {
                "case_id": case_id,
                "pending": initial[case_id]["pending"],
                "retrieval_claim_count": len(claim_results_by_case.get(case_id, [])),
                "known_useful_count": len(known_keys),
                "known_useful_in_claim_top15": len(claim_recovered[case_id]),
                "known_useful_absent_from_claim_top15": len(known_keys - claim_recovered[case_id]),
                "known_useful_min_rank_buckets": {
                    "top1": sum(ranks.get(item) == 1 for item in known_keys),
                    "top2_3": sum(2 <= ranks.get(item, 999) <= 3 for item in known_keys),
                    "top4_5": sum(4 <= ranks.get(item, 999) <= 5 for item in known_keys),
                    "top6_10": sum(6 <= ranks.get(item, 999) <= 10 for item in known_keys),
                    "top11_15": sum(11 <= ranks.get(item, 999) <= 15 for item in known_keys),
                    "absent": sum(item not in ranks for item in known_keys),
                },
                "pool_recovered": {
                    str(pool): len(
                        known_keys & {key(item) for item in pool_cases[pool][case_id]["final_evidence"]}
                    )
                    for pool in POOLS
                },
                "old_atomic_complete_loss": case_id in old_loss_cases,
            }
        )

    total_known = sum(len(items) for items in known.values())
    reference_results = {}
    for name, reference in references.items():
        total = sum(len(items) for items in reference.values())
        reference_results[name] = {
            "useful_count": total,
            "source_dense_sparse_top100_union_recovered": sum(
                len(reference[case_id] & source_recovered[case_id])
                for case_id in reference
            ),
            "claim_top15_recovered": sum(
                len(reference[case_id] & claim_recovered[case_id])
                for case_id in reference
            ),
            "pool_recovered": {
                str(pool): sum(
                    len(
                        reference[case_id]
                        & {key(item) for item in pool_cases[pool][case_id]["final_evidence"]}
                    )
                    for case_id in reference
                )
                for pool in POOLS
            },
        }
    return {
        "schema": "atomic_bgem3_merge_pool_analysis_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "claims_source": str(claims_path.resolve()),
        "retrieval_source": str(retrieval_path.resolve()),
        "definition_zh": "以三套既有黄金标注的useful并集作为已知useful，按证据组归一，定位BGE原子主张管线的召回损失位置。",
        "summary": {
            "known_useful_count": total_known,
            "cases_with_known_useful": sum(bool(items) for items in known.values()),
            "cases_without_retrieval_claims": sum(not claim_results_by_case.get(case_id) for case_id in known),
            "known_useful_in_cases_without_retrieval_claims": sum(
                len(known[case_id]) for case_id in known if not claim_results_by_case.get(case_id)
            ),
            "known_useful_recovered_by_any_claim_top15": sum(len(items) for items in claim_recovered.values()),
            "known_useful_absent_from_all_claim_top15": sum(
                len(known[case_id] - claim_recovered[case_id]) for case_id in known
            ),
            "known_useful_recovered_by_dense_sparse_top100_union": sum(
                len(items) for items in source_recovered.values()
            ),
            "known_useful_absent_from_dense_sparse_top100_union": sum(
                len(known[case_id] - source_recovered[case_id]) for case_id in known
            ),
            "old_atomic_complete_loss_case_count": len(old_loss_cases),
            "pool_results": pool_rows,
            "reference_results": reference_results,
        },
        "cases": case_rows,
    }


def render(payload: dict[str, Any]) -> str:
    rows = payload["summary"]["pool_results"]
    table = "".join(
        f"""<tr><td>Top{row['per_claim_pool']}</td><td>{row['total_final_candidates']}</td>
        <td>{row['known_useful_recovered']}</td><td>{row['known_useful_recovery_rate']:.1%}</td>
        <td>{row['cases_with_known_useful_recovered']}</td><td>{row['old_loss_cases_recovered']}</td></tr>"""
        for row in rows
    )
    reference_table = "".join(
        f"""<tr><td>{esc(name)}</td><td>{row['useful_count']}</td>
        <td>{row['source_dense_sparse_top100_union_recovered']}</td>
        <td>{row['claim_top15_recovered']}</td>
        <td>{row['pool_recovered']['3']}</td><td>{row['pool_recovered']['10']}</td><td>{row['pool_recovered']['15']}</td></tr>"""
        for name, row in payload["summary"]["reference_results"].items()
    )
    cards = []
    for case in sorted(
        payload["cases"],
        key=lambda item: (
            not item["old_atomic_complete_loss"],
            -item["known_useful_absent_from_claim_top15"],
            -item["known_useful_count"],
        ),
    ):
        bucket = case["known_useful_min_rank_buckets"]
        pool_text = " · ".join(f"Top{pool}: {count}" for pool, count in case["pool_recovered"].items())
        cards.append(
            f"""<article><header><b>{esc(case['case_id'])}</b>
            <span>{'旧原子完全丢失' if case['old_atomic_complete_loss'] else ''}</span></header>
            <p>原子主张 {case['retrieval_claim_count']} · 已知 useful {case['known_useful_count']} · 主张Top15命中 {case['known_useful_in_claim_top15']} · 完全未命中 {case['known_useful_absent_from_claim_top15']}</p>
            <p>最小排名分布：{esc(bucket)}</p><p>不同父级合并池恢复：{esc(pool_text)}</p>
            <details><summary>待审 chunk 原文</summary><pre>{esc(case['pending'].get('content', ''))}</pre></details></article>"""
        )
    summary = payload["summary"]
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>BGE原子合并池漏斗分析</title><style>
    body{{margin:0;background:#f3f5f4;color:#18251f;font-family:"Microsoft YaHei",sans-serif}}.top{{background:#17392c;color:#fff;padding:18px 4vw}}main{{width:min(1350px,94vw);margin:18px auto}}
    table{{border-collapse:collapse;width:100%;background:#fff;margin-bottom:18px}}th,td{{border:1px solid #ccd5cf;padding:9px;text-align:left}}article{{background:#fff;border-left:5px solid #536e61;padding:12px;margin-bottom:10px}}article header{{display:flex;justify-content:space-between}}article p{{color:#52645a}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;line-height:1.6}}
    </style></head><body><div class="top"><h1>BGE-M3 原子主张父级合并池漏斗分析</h1>
    <p>已知 useful {summary['known_useful_count']}；任一主张 Top15 命中 {summary['known_useful_recovered_by_any_claim_top15']}；完全未进入主张 Top15 {summary['known_useful_absent_from_all_claim_top15']}。</p></div>
    <main><table><thead><tr><th>每主张进入合并池</th><th>父级候选总数</th><th>恢复已知 useful</th><th>恢复率</th><th>覆盖待审块</th><th>恢复旧原子完全丢失块</th></tr></thead><tbody>{table}</tbody></table>
    <table><thead><tr><th>参照黄金集</th><th>useful</th><th>Dense/Sparse Top100并集命中</th><th>主张Top15命中</th><th>父级Top3池</th><th>父级Top10池</th><th>父级Top15池</th></tr></thead>
    <tbody>{reference_table}</tbody></table>
    {''.join(cards)}</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claims", type=Path, default=CLAIMS)
    parser.add_argument("--retrieval", type=Path, default=RETRIEVAL)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--html", type=Path, default=HTML)
    args = parser.parse_args()
    payload = build(args.claims, args.retrieval)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.html.write_text(render(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.html.resolve())


if __name__ == "__main__":
    main()
