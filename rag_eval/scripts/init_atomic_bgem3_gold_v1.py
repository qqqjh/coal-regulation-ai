"""Initialize a resumable gold set for BGE-M3 atomic-claim parent evidence."""

from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARENT = PROJECT_ROOT / "rag_eval" / "data" / "parent_chunk_evidence_local_bgem3_grouped_v1.json"
DEFAULT_ATOMIC_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
DEFAULT_BGE_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json"
DEFAULT_CHECKPOINTS = PROJECT_ROOT / "rag_eval" / "data" / "atomic_bgem3_gold_checkpoints_v1"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "atomic_bgem3_gold_annotations_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "atomic_bgem3_gold_annotations_review_v1.html"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def prior_maps(case: dict[str, Any], item_key: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    exact = {}
    groups = {}
    for item in case.get(item_key, []):
        exact[item["chunk_id"]] = item
        group_id = item.get("evidence_group_id", "")
        if group_id:
            groups[group_id] = item
    return exact, groups


def migrate_annotation(
    evidence: dict[str, Any],
    old_atomic_exact: dict[str, dict[str, Any]],
    bge_exact: dict[str, dict[str, Any]],
    bge_groups: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    proposals = []
    if evidence["chunk_id"] in old_atomic_exact:
        proposals.append(("old_atomic_exact_chunk", old_atomic_exact[evidence["chunk_id"]]))
    if evidence["chunk_id"] in bge_exact:
        proposals.append(("parent_bgem3_exact_chunk", bge_exact[evidence["chunk_id"]]))
    group_id = evidence.get("evidence_group_id", "")
    if group_id and group_id in bge_groups:
        proposals.append(("parent_bgem3_evidence_group", bge_groups[group_id]))

    labels = {item.get("label", "unlabeled") for _source, item in proposals}
    conflict = len(labels) > 1
    chosen_source, chosen = proposals[0] if proposals and not conflict else ("unlabeled", {})
    note = chosen.get("note", "")
    if conflict:
        note = "已有黄金标注冲突：" + "；".join(
            f"{source}={item.get('label')}" for source, item in proposals
        )

    return {
        **evidence,
        "label": chosen.get("label", "unlabeled") if not conflict else "unlabeled",
        "confidence": chosen.get("confidence", "unlabeled") if not conflict else "unlabeled",
        "note": note,
        "annotation_source": chosen_source,
        "migration_proposals": [
            {
                "source": source,
                "chunk_id": item.get("chunk_id", ""),
                "label": item.get("label", "unlabeled"),
            }
            for source, item in proposals
        ],
        "migration_conflict": conflict,
    }


def initialize_case(
    case: dict[str, Any],
    old_atomic: dict[str, Any],
    bge: dict[str, Any],
) -> dict[str, Any]:
    old_exact, _old_groups = prior_maps(old_atomic, "evidence_annotations")
    bge_exact, bge_groups = prior_maps(bge, "candidate_annotations")
    annotations = [
        migrate_annotation(evidence, old_exact, bge_exact, bge_groups)
        for evidence in case["final_evidence"]
    ]
    unlabeled = sum(item["label"] == "unlabeled" for item in annotations)
    return {
        "schema": "atomic_bgem3_gold_checkpoint_v1",
        "case_id": case["case_id"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "completed" if not unlabeled else "in_progress",
        "human_review_status": "pending_domain_expert_confirmation",
        "pending": case["pending"],
        "retrieval_claims": case["retrieval_claims"],
        "final_label": old_atomic.get("final_label", "unlabeled"),
        "final_confidence": old_atomic.get("final_confidence", "unlabeled"),
        "final_reason": old_atomic.get("final_reason", ""),
        "missing_correct_evidence": old_atomic.get("missing_correct_evidence", False),
        "not_eval_suitable": old_atomic.get("not_eval_suitable", False),
        "review_flags": list(old_atomic.get("review_flags", []))
        + (["BGE原子主张候选变化后，需重新确认missing_correct_evidence"] if annotations else []),
        "evidence_count": len(annotations),
        "labeled_evidence_count": len(annotations) - unlabeled,
        "evidence_annotations": annotations,
    }


def build_summary(checkpoint_dir: Path) -> dict[str, Any]:
    cases = [load(path) for path in sorted(checkpoint_dir.glob("*.json"))]
    labels = Counter(
        item["label"]
        for case in cases
        for item in case["evidence_annotations"]
    )
    sources = Counter(
        item["annotation_source"]
        for case in cases
        for item in case["evidence_annotations"]
    )
    return {
        "schema": "atomic_bgem3_gold_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "annotation_method": "Existing same-case gold migration followed by checkpointed manual review.",
        "checkpoint_directory": str(checkpoint_dir.resolve()),
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "total_evidence_count": sum(case["evidence_count"] for case in cases),
        "labeled_evidence_count": sum(case["labeled_evidence_count"] for case in cases),
        "evidence_label_counts": dict(labels),
        "annotation_source_counts": dict(sources),
        "migration_conflict_count": sum(
            item["migration_conflict"]
            for case in cases
            for item in case["evidence_annotations"]
        ),
        "cases": cases,
    }


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render(payload: dict[str, Any]) -> str:
    case_cards = []
    for case in payload["cases"]:
        evidence_cards = []
        for item in case["evidence_annotations"]:
            evidence_cards.append(
                f"""<article class="evidence {esc(item['label'])}" data-label="{esc(item['label'])}">
                <header><b>#{item['parent_rank']} {esc(item['chunk_id'])}</b>
                <span>{esc(item['label'])} · {esc(item['annotation_source'])}</span></header>
                <p>覆盖主张 {item['coverage_count']} 个 · 最佳主张排名 Top{item['best_claim_rank']}
                {f" · 证据组 {esc(item['evidence_group_id'])}" if item.get('evidence_group_id') else ""}</p>
                {f"<p class='conflict'>{esc(item['note'])}</p>" if item.get('migration_conflict') else ""}
                <details><summary>规则全文</summary><pre>{esc(item['content'])}</pre></details></article>"""
            )
        case_cards.append(
            f"""<section class="case"><header><div><h2>{esc(case['case_id'])}</h2>
            <p>{esc(case['pending'].get('doc_name', ''))}</p></div>
            <b>{case['labeled_evidence_count']}/{case['evidence_count']} 已继承标注</b></header>
            <details><summary>待审 chunk 原文</summary><pre>{esc(case['pending'].get('content', ''))}</pre></details>
            <div class="evidence-list">{''.join(evidence_cards) or '<p>无检索候选</p>'}</div></section>"""
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <title>BGE-M3 原子主张黄金标注初始集</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f3f5f4;color:#17241e;font-family:"Microsoft YaHei",sans-serif}}
    .top{{position:sticky;top:0;z-index:4;background:#173a2c;color:#fff;padding:16px 3vw}}h1,h2{{margin:0}}
    .top p{{margin:6px 0}}button{{margin:5px 5px 0 0;padding:6px 10px}}main{{width:min(1450px,96vw);margin:18px auto}}
    .case{{background:#fff;border:1px solid #ccd5cf;margin-bottom:16px}}.case>header{{display:flex;justify-content:space-between;padding:13px;border-bottom:1px solid #dbe1dd}}
    .case>details{{margin:10px}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;line-height:1.6}}
    .evidence-list{{padding:10px}}.evidence{{border-left:5px solid #8b9690;background:#f8faf9;padding:10px;margin-bottom:9px}}
    .evidence.useful{{border-color:#14754e}}.evidence.useless{{border-color:#a55449}}.evidence.uncertain{{border-color:#c38b20}}.evidence.unlabeled{{border-color:#596c80}}
    .evidence header{{display:flex;justify-content:space-between;gap:10px}}.evidence p{{color:#58675f}}.conflict{{color:#a2352c!important;font-weight:700}}
    </style></head><body><div class="top"><h1>BGE-M3 + 原子主张黄金标注初始集</h1>
    <p>已继承 {payload['labeled_evidence_count']}/{payload['total_evidence_count']}；冲突 {payload['migration_conflict_count']}；标签 {esc(payload['evidence_label_counts'])}</p>
    <button onclick="filter('all')">全部</button><button onclick="filter('unlabeled')">仅未标注</button>
    <button onclick="filter('useful')">仅 useful</button><button onclick="filter('uncertain')">仅 uncertain</button></div>
    <main>{''.join(case_cards)}</main><script>
    function filter(v){{document.querySelectorAll('.evidence').forEach(x=>x.style.display=v==='all'||x.dataset.label===v?'':'none')}}
    </script></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--old-atomic-gold", type=Path, default=DEFAULT_ATOMIC_GOLD)
    parser.add_argument("--bge-gold", type=Path, default=DEFAULT_BGE_GOLD)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    parent = load(args.parent)
    old_cases = {case["case_id"]: case for case in load(args.old_atomic_gold)["cases"]}
    bge_cases = {case["case_id"]: case for case in load(args.bge_gold)["cases"]}
    created = skipped = 0
    for case in parent["cases"]:
        path = args.checkpoint_dir / f"{case['case_id']}.json"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue
        atomic_write(path, initialize_case(case, old_cases[case["case_id"]], bge_cases[case["case_id"]]))
        created += 1

    payload = build_summary(args.checkpoint_dir)
    atomic_write(args.output, payload)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(render(payload), encoding="utf-8")
    print(f"created={created} skipped={skipped}")
    print(json.dumps({key: payload[key] for key in (
        "case_count", "completed_case_count", "total_evidence_count",
        "labeled_evidence_count", "evidence_label_counts",
        "annotation_source_counts", "migration_conflict_count",
    )}, ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.html.resolve())


if __name__ == "__main__":
    main()
