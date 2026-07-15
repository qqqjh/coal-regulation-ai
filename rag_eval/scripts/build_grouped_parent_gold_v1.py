"""Build a grouped parent-chunk gold set from rerank-deduplicated candidates."""

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
DEFAULT_CANDIDATES = (
    PROJECT_ROOT / "rag_eval" / "data" / "parent_candidates_local_bgem3_v6_grouped_v1.json"
)
DEFAULT_OLD_GOLD = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_v1.json"
DEFAULT_GROUPS = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "parent_bgem3_annotations_grouped_review_v1.html"


# Only candidates newly introduced by refill require new manual judgments.
NEW_CANDIDATE_DECISIONS = {
    ("v9_004_chunk131", "煤矿安全规程::chunk715"): (
        "useful",
        "直接规定输送机转载点等地点必须安装喷雾装置或除尘器并在作业时降尘，直接覆盖待审块的转载点喷雾要求。",
    ),
    ("v9_004_chunk153", "煤矿安全规程::chunk109"): (
        "useless",
        "该条针对建井期间井筒掘砌的监测监控和通信系统形成要求，不直接约束待审块中的矿压监测探头安装维护作业。",
    ),
    ("v9_004_chunk153", "煤矿安全规程::chunk331"): (
        "useless",
        "该条针对钻孔放水、排水能力和水量水压监测，与矿压监测系统安装维护的核心审查事项不同。",
    ),
    ("v9_006_chunk053", "《防治煤与瓦斯突出细则》::chunk88"): (
        "useful",
        "直接规定钻屑瓦斯解吸指标K1、h2的测定方法和参考临界值，可用于核验待审块自行采用的K1等瓦斯参数阈值。",
    ),
    ("v9_006_chunk074", "煤矿安全规程::chunk141"): (
        "useless",
        "该条规范滚筒式采煤机操作，未直接覆盖超前探钻机施工、起吊或打钻安全要求。",
    ),
    ("v9_006_chunk075", "《防治煤与瓦斯突出细则》::chunk32"): (
        "uncertain",
        "该条规范防突措施钻孔施工中的传感器、喷孔顶钻防护和大直径钻孔措施；与待审块打钻安全有关，但是否属于防突措施钻孔需结合现场用途确认。",
    ),
    ("v9_006_chunk076", "煤矿安全规程::chunk573"): (
        "uncertain",
        "该条要求钻孔、爆破作业编制设计和安全技术措施并经批准，与待审块超前探钻孔作业有关，但未直接规定掉钻、打捞和封孔挂牌处置。",
    ),
    ("v9_006_chunk091", "煤矿安全规程::chunk280"): (
        "useless",
        "该条针对发现井下火灾后的报告、撤人和灭火处置，不直接核验待审块的自救器和避难硐室设置要求。",
    ),
    ("v9_006_chunk091", "煤矿安全规程::chunk248"): (
        "useless",
        "该条针对井下动火作业审批和防火措施，不直接核验自救器、避难硐室或紧急避险设施要求。",
    ),
}

LABEL_PRIORITY = {"unlabeled": 0, "useless": 1, "uncertain": 2, "useful": 3}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def group_label(items: list[dict[str, Any]]) -> str:
    return max((item["label"] for item in items), key=LABEL_PRIORITY.get)


def build_payload(candidates: dict[str, Any], old_gold: dict[str, Any], groups: dict[str, Any]) -> dict[str, Any]:
    old_cases = {case["case_id"]: case for case in old_gold["cases"]}
    chunk_to_group = groups["chunk_to_group"]
    cases = []
    transfer_counts: Counter[str] = Counter()

    for source_case in candidates["cases"]:
        case_id = source_case["case_id"]
        old_case = old_cases[case_id]
        old_by_chunk = {item["chunk_id"]: item for item in old_case["candidate_annotations"]}
        old_by_group: dict[str, list[dict[str, Any]]] = {}
        for item in old_case["candidate_annotations"]:
            group_id = chunk_to_group.get(item["chunk_id"], "")
            if group_id:
                old_by_group.setdefault(group_id, []).append(item)

        annotations = []
        for candidate in source_case["candidates"]:
            chunk_id = candidate["chunk_id"]
            group_id = candidate.get("evidence_group_id", "")
            exact = old_by_chunk.get(chunk_id)
            group_items = old_by_group.get(group_id, []) if group_id else []
            if group_items:
                label = group_label(group_items)
                source = "migrated_evidence_group_aggregate"
                prior_labels = sorted({item["label"] for item in group_items})
                note = (
                    f"同证据组 {group_id} 按 useful > uncertain > useless 聚合旧黄金标签："
                    f"{'、'.join(prior_labels)}。保留规则由reranker排序决定。"
                )
            elif exact:
                label = exact["label"]
                source = "migrated_exact_chunk"
                note = exact["note"]
            else:
                label, note = NEW_CANDIDATE_DECISIONS[(case_id, chunk_id)]
                source = "codex_manual_refill_candidate_v1"
            transfer_counts[source] += 1
            annotations.append(
                {
                    **candidate,
                    "label": label,
                    "confidence": exact.get("confidence", "medium") if exact else "medium",
                    "note": note,
                    "annotation_source": source,
                    "evaluation_evidence_id": group_id or chunk_id,
                }
            )

        removed = []
        new_ids = {item["chunk_id"] for item in annotations}
        for item in old_case["candidate_annotations"]:
            if item["chunk_id"] in new_ids:
                continue
            removed.append(
                {
                    "chunk_id": item["chunk_id"],
                    "old_rank": item["rank"],
                    "old_label": item["label"],
                    "evidence_group_id": chunk_to_group.get(item["chunk_id"], ""),
                    "removal_reason": "same_evidence_group_suppressed_after_rerank",
                }
            )

        useful_evidence_ids = sorted(
            {item["evaluation_evidence_id"] for item in annotations if item["label"] == "useful"}
        )
        case = {
            **{key: value for key, value in old_case.items() if key != "candidate_annotations"},
            "schema": "coal_rag_parent_bgem3_grouped_gold_case_v1",
            "source_top_k": source_case["source_top_k"],
            "candidate_count_before_rerank_cut": source_case["candidate_count_before_rerank_cut"],
            "candidate_count_after_evidence_group_dedup": source_case[
                "candidate_count_after_evidence_group_dedup"
            ],
            "suppressed_same_evidence_group": source_case["suppressed_same_evidence_group"],
            "removed_old_candidate_annotations": removed,
            "gold_useful_evidence_ids": useful_evidence_ids,
            "candidate_annotations": annotations,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        cases.append(case)

    labels = Counter(item["label"] for case in cases for item in case["candidate_annotations"])
    return {
        "schema": "coal_rag_parent_bgem3_annotations_grouped_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "human_review_status": "pending_domain_expert_confirmation",
        "definition_zh": "权限过滤暂未实现；reranker后按证据组去重并从后续排名补位。同组任一成员在指标计算时使用相同evaluation_evidence_id。",
        "candidate_source": str(DEFAULT_CANDIDATES.resolve()),
        "old_gold_source": str(DEFAULT_OLD_GOLD.resolve()),
        "evidence_groups_source": str(DEFAULT_GROUPS.resolve()),
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "total_candidate_count": sum(len(case["candidate_annotations"]) for case in cases),
        "candidate_label_counts": dict(labels),
        "annotation_transfer_counts": dict(transfer_counts),
        "suppressed_candidate_count": sum(
            len(case["suppressed_same_evidence_group"]) for case in cases
        ),
        "refill_candidate_count": transfer_counts["codex_manual_refill_candidate_v1"],
        "cases": cases,
    }


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render_html(payload: dict[str, Any]) -> str:
    sections = []
    for case in payload["cases"]:
        candidates = []
        for item in case["candidate_annotations"]:
            candidates.append(
                f"""<article class="candidate {esc(item['label'])}">
                <h3>#{item['rank']} {esc(item['label'])} · {esc(item['chunk_id'])}</h3>
                <p class="meta">rerank={item['rerank_score']:.6f} · 证据组={esc(item.get('evidence_group_id') or '无')} · {esc(item['annotation_source'])}</p>
                <p>{esc(item['note'])}</p>
                <details><summary>规则全文</summary><pre>{esc(item['content'])}</pre></details></article>"""
            )
        suppressed = case["suppressed_same_evidence_group"]
        suppressed_html = (
            f"<details><summary>同证据组抑制记录（{len(suppressed)}）</summary>"
            f"<pre>{esc(json.dumps(suppressed, ensure_ascii=False, indent=2))}</pre></details>"
            if suppressed
            else ""
        )
        sections.append(
            f"""<section class="case"><h2>{esc(case['case_id'])}</h2>
            <p>{esc(case['final_reason'])}</p>{suppressed_html}
            <details><summary>待审 chunk</summary><pre>{esc(case['pending']['content'])}</pre></details>
            <div class="grid">{''.join(candidates)}</div></section>"""
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>证据组黄金标注复核</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f5f4;color:#1d2923;font-family:"Microsoft YaHei",sans-serif}}
    .top{{position:sticky;top:0;z-index:5;background:#19352a;color:#fff;padding:16px 4vw}}main{{width:min(1600px,94vw);margin:22px auto}}
    .case{{background:#fff;border-top:4px solid #19352a;padding:16px;margin-bottom:22px}}h2,h3{{margin:0}}p{{line-height:1.65}}
    details{{border:1px solid #d7ded9;margin-top:8px}}summary{{cursor:pointer;padding:8px;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;padding:10px;margin:0}}
    .grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}}.candidate{{border:1px solid #d7ded9;border-top:4px solid #8c9690;padding:10px;min-width:0}}
    .candidate.useful{{border-top-color:#187050}}.candidate.uncertain{{border-top-color:#b87920}}.candidate.useless{{border-top-color:#8c9690}}
    .candidate h3,.candidate p{{font-size:13px;overflow-wrap:anywhere}}.meta{{color:#627168}}@media(max-width:1000px){{.grid{{grid-template-columns:1fr}}}}</style></head>
    <body><div class="top"><h1>证据组去重后的黄金标注集</h1>
    <p>{payload['case_count']} 案例 · {payload['total_candidate_count']} 候选 · 抑制 {payload['suppressed_candidate_count']} 条 · 补位 {payload['refill_candidate_count']} 条</p></div>
    <main>{''.join(sections)}</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--old-gold", type=Path, default=DEFAULT_OLD_GOLD)
    parser.add_argument("--groups", type=Path, default=DEFAULT_GROUPS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args()

    payload = build_payload(load_json(args.candidates), load_json(args.old_gold), load_json(args.groups))
    atomic_write(args.output, payload)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(render_html(payload), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "case_count", "total_candidate_count", "candidate_label_counts",
        "annotation_transfer_counts", "suppressed_candidate_count", "refill_candidate_count"
    )}, ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.html.resolve())


if __name__ == "__main__":
    main()
