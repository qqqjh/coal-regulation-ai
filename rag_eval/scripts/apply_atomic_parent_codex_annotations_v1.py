# -*- coding: utf-8 -*-
"""Apply Codex manual labels to atomic parent evidence checkpoints.

The script writes each completed parent case atomically before moving to the
next case. Re-running it skips completed cases unless --reapply is passed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_checkpoints_v1"
DEFAULT_SUMMARY = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "atomic_parent_gold_annotations_review_v1.html"


def ranks(useful: tuple[int, ...] = (), uncertain: tuple[int, ...] = (), missing: bool | None = None) -> dict[str, Any]:
    return {"useful": set(useful), "uncertain": set(uncertain), "missing": missing}


# Decisions are based on the current parent-level atomic retrieval candidates.
# A rank not listed as useful/uncertain is manually judged useless for that case.
DECISIONS: dict[str, dict[str, Any]] = {
    "v9_004_chunk013": ranks(missing=True),
    "v9_004_chunk038": ranks(uncertain=(4, 5), missing=True),
    "v9_004_chunk060": ranks(useful=(1, 2), missing=True),
    "v9_004_chunk061": ranks(useful=(5,), uncertain=(1, 2, 3, 4), missing=False),
    "v9_004_chunk076": ranks(useful=(12,), uncertain=(2, 10, 11, 13), missing=True),
    "v9_004_chunk087": ranks(uncertain=(1, 3, 8, 11, 13), missing=True),
    "v9_004_chunk120": ranks(uncertain=(3, 6, 9), missing=True),
    "v9_004_chunk131": ranks(missing=True),
    "v9_004_chunk142": ranks(useful=(9,), uncertain=(14,), missing=True),
    "v9_004_chunk153": ranks(uncertain=(13,), missing=True),
    "v9_004_chunk159": ranks(useful=(4,), uncertain=(1, 5, 9, 10, 16, 17), missing=True),
    "v9_004_chunk161": ranks(missing=True),
    "v9_006_chunk003": ranks(missing=True),
    "v9_006_chunk008": ranks(uncertain=(3, 4, 5), missing=False),
    "v9_006_chunk023": ranks(missing=True),
    "v9_006_chunk024": ranks(missing=True),
    "v9_006_chunk026": ranks(missing=True),
    "v9_006_chunk030": ranks(missing=True),
    "v9_006_chunk040": ranks(useful=(2, 5, 8), uncertain=(1, 11, 12), missing=False),
    "v9_006_chunk046": ranks(uncertain=(12, 18, 20), missing=True),
    "v9_006_chunk048": ranks(missing=False),
    "v9_006_chunk050": ranks(uncertain=(6,), missing=True),
    "v9_006_chunk053": ranks(useful=(1, 3, 4), uncertain=(5, 6), missing=False),
    "v9_006_chunk074": ranks(useful=(7, 12), uncertain=(3, 6, 9, 10, 11), missing=True),
    "v9_006_chunk075": ranks(useful=(4, 7), uncertain=(3, 8), missing=True),
    "v9_006_chunk076": ranks(useful=(3,), uncertain=(5, 6), missing=True),
    "v9_006_chunk088": ranks(useful=(1, 2, 4), uncertain=(3,), missing=False),
    "v9_006_chunk091": ranks(useful=(1, 2), uncertain=(3, 4, 5), missing=False),
    "v9_006_chunk095": ranks(uncertain=(2, 3, 6), missing=True),
    "v9_006_chunk096": ranks(uncertain=(2, 7), missing=True),
    "v9_006_chunk097": ranks(uncertain=(2, 5, 6, 12), missing=True),
    "v9_006_chunk104": ranks(useful=(13,), uncertain=(3,), missing=False),
    "v9_006_chunk113": ranks(useful=(5,), missing=False),
    "v9_006_chunk118": ranks(uncertain=(4, 10), missing=True),
    "v9_006_chunk135": ranks(missing=True),
    "v9_006_chunk159": ranks(useful=(2, 3, 5, 8, 9, 13, 18, 19, 24), uncertain=(6, 7, 12, 15), missing=False),
    "v9_006_chunk161": ranks(uncertain=(8,), missing=True),
    "v9_066_chunk035": ranks(missing=True),
    "v9_066_chunk044": ranks(missing=True),
    "v9_066_chunk047": ranks(useful=(11, 12), missing=False),
    "v9_066_chunk049": ranks(useful=(1, 2, 5, 6), uncertain=(9, 11), missing=False),
    "v9_066_chunk055": ranks(useful=(1, 3), uncertain=(2,), missing=False),
    "v9_066_chunk066": ranks(missing=False),
    "v9_066_chunk081": ranks(missing=True),
    "v9_066_chunk089": ranks(missing=True),
    "v9_066_chunk094": ranks(missing=True),
    "v9_066_chunk100": ranks(uncertain=(7, 9, 10), missing=True),
    "v9_066_chunk112": ranks(missing=True),
    "v9_066_chunk130": ranks(useful=(1, 2, 3, 4, 5), uncertain=(6, 7, 8), missing=False),
    "v9_066_chunk141": ranks(missing=True),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def compact(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def case_focus(case: dict[str, Any]) -> str:
    pending = case["pending"]
    heading = " / ".join(
        value for value in (pending.get("chapter"), pending.get("section"), pending.get("article")) if value
    )
    return compact(heading or pending.get("content", ""), 52)


def evidence_note(label: str, case: dict[str, Any], evidence: dict[str, Any]) -> str:
    focus = case_focus(case)
    rule = compact(evidence["content"], 72)
    if label == "useful":
        return f"该候选直接覆盖待审关注点“{focus}”的对象、强制要求或关键阈值，可作为本案有效规则证据。关键内容：{rule}"
    if label == "uncertain":
        return f"该候选与待审关注点“{focus}”有关，但适用对象、前置条件或关键阈值未完全对应，只能作为辅助证据。关键内容：{rule}"
    return f"该候选主要涉及“{rule}”，未直接覆盖待审关注点“{focus}”，不能作为本案有效规则证据。"


def annotate_case(case: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    known_ranks = {item["parent_rank"] for item in case["evidence_annotations"]}
    requested = decision["useful"] | decision["uncertain"]
    unknown = requested - known_ranks
    if unknown:
        raise ValueError(f"{case['case_id']} references missing ranks: {sorted(unknown)}")

    for evidence in case["evidence_annotations"]:
        rank = evidence["parent_rank"]
        if rank in decision["useful"]:
            label, confidence = "useful", "high"
        elif rank in decision["uncertain"]:
            label, confidence = "uncertain", "medium"
        else:
            label, confidence = "useless", "high"
        evidence["label"] = label
        evidence["confidence"] = confidence
        evidence["note"] = evidence_note(label, case, evidence)
        evidence["annotation_source"] = "codex_manual_atomic_parent_v1"

    if decision["missing"] is not None:
        case["missing_correct_evidence"] = decision["missing"]
    case["status"] = "completed"
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    case["annotation_method"] = "Codex manually reviewed the current parent chunk and every final atomic retrieval evidence candidate."
    case["human_review_status"] = "pending_domain_expert_confirmation"
    return case


def build_summary(checkpoint_dir: Path, output: Path) -> dict[str, Any]:
    cases = [load_json(path) for path in sorted(checkpoint_dir.glob("*.json"))]
    labels = Counter(item["label"] for case in cases for item in case["evidence_annotations"])
    finals = Counter(case["final_label"] for case in cases)
    payload = {
        "schema": "atomic_parent_gold_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checkpoint_directory": str(checkpoint_dir),
        "annotation_method": "Codex manual parent-level atomic retrieval evidence annotation; each case saved atomically.",
        "human_review_status": "pending_domain_expert_confirmation",
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "labeled_evidence_count": sum(labels.values()) - labels["unlabeled"],
        "total_evidence_count": sum(case["evidence_count"] for case in cases),
        "final_label_counts": dict(finals),
        "evidence_label_counts": dict(labels),
        "cases": cases,
    }
    atomic_write(output, payload)
    return payload


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def build_html(payload: dict[str, Any]) -> str:
    sections = []
    for index, case in enumerate(payload["cases"], start=1):
        flags = "".join(f"<li>{esc(flag)}</li>" for flag in case.get("review_flags", [])) or "<li>无额外复核标记</li>"
        evidence_html = []
        for item in case["evidence_annotations"]:
            evidence_html.append(
                f"""<article class="evidence {esc(item['label'])}">
                <div class="evidence-head"><b>#{item['parent_rank']} {esc(item['label'])}</b><span>{esc(item['chunk_id'])}</span></div>
                <p>{esc(item['note'])}</p>
                <details><summary>查看规则全文与命中主张</summary><pre>{esc(item['content'])}</pre>
                <pre>{esc(json.dumps(item['covered_claim_ids'], ensure_ascii=False, indent=2))}</pre></details>
                </article>"""
            )
        sections.append(
            f"""<section class="case" data-final="{esc(case['final_label'])}" data-missing="{str(case['missing_correct_evidence']).lower()}">
            <header><span class="index">{index:02d}</span><div><h2>{esc(case['case_id'])}</h2>
            <p>{esc(case['pending'].get('doc_name'))} · {esc(case_focus(case))}</p></div>
            <strong class="verdict {esc(case['final_label'])}">{esc(case['final_label'])}</strong></header>
            <div class="judgment"><b>Codex父块判断</b><p>{esc(case['final_reason'])}</p>
            <span>missing_correct_evidence={str(case['missing_correct_evidence']).lower()}</span>
            <span>not_eval_suitable={str(case['not_eval_suitable']).lower()}</span><ul>{flags}</ul></div>
            <details class="pending" open><summary>待审 chunk 全文</summary><pre>{esc(case['pending']['content'])}</pre></details>
            <div class="evidence-grid">{''.join(evidence_html) or '<p class="empty">本案例没有进入父级候选集的规则证据。</p>'}</div>
            </section>"""
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><title>原子检索黄金标注复核 v1</title>
    <style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f4f2ec;color:#18221d;font-family:"Microsoft YaHei",sans-serif;letter-spacing:0}}
    .top{{position:sticky;top:0;z-index:5;padding:18px 4vw;background:#17251e;color:white;border-bottom:4px solid #c89c42}}
    h1{{margin:0 0 8px;font-size:24px}} .top p{{margin:0 0 12px;color:#d8dfda}} button{{padding:7px 10px;margin:0 5px 5px 0;border:1px solid #809087;background:transparent;color:white;border-radius:3px;cursor:pointer}} button.active{{background:#c89c42;color:#17251e}}
    .stats{{padding:14px 4vw;border-bottom:1px solid #c8c3b8;display:flex;gap:22px;flex-wrap:wrap}} main{{width:min(1600px,94vw);margin:24px auto 80px}}
    .case{{border-top:3px solid #18221d;padding:22px 0 34px}} header{{display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:start}} .index{{font:700 24px Georgia;color:#68736d}} h2{{margin:0;font-size:19px}} header p{{margin:5px 0;color:#68736d;font-size:13px}}
    .verdict{{padding:8px 10px;color:#fff;border-radius:3px}} .compliant{{background:#176b4b}} .non_compliant{{background:#a13d30}} .uncertain{{background:#9a641d}} .not_suitable{{background:#285d79}}
    .judgment{{margin:16px 0;padding:10px 15px;border-left:5px solid #c89c42}} .judgment p{{line-height:1.7}} .judgment span{{display:inline-block;margin:0 8px 5px 0;padding:3px 7px;border:1px solid #c8c3b8;font-size:12px}}
    details{{background:#fffdf8;border:1px solid #c8c3b8}} summary{{padding:9px 11px;cursor:pointer;font-weight:700}} pre{{margin:0;padding:12px;white-space:pre-wrap;word-break:break-word;line-height:1.65;font-family:"Microsoft YaHei",sans-serif;font-size:13px}}
    .evidence-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:16px}} .evidence{{padding:10px;background:#fffdf8;border:1px solid #c8c3b8;border-top-width:4px;min-width:0}} .evidence.useful{{border-top-color:#176b4b}} .evidence.uncertain{{border-top-color:#c08022}} .evidence.useless{{border-top-color:#969e98}} .evidence-head{{display:flex;gap:8px;justify-content:space-between;overflow-wrap:anywhere;font-size:12px}} .evidence p{{font-size:13px;line-height:1.65}} .evidence details pre{{max-height:310px;overflow:auto}} .empty{{color:#68736d}}
    @media(max-width:1050px){{.evidence-grid{{grid-template-columns:1fr}}header{{grid-template-columns:40px 1fr}}.verdict{{grid-column:2;justify-self:start}}}}
    </style></head><body><div class="top"><h1>原子检索黄金标注复核 v1</h1>
    <p>Codex逐案审阅；每个父 chunk 独立保存。此结果等待领域人员最终确认。</p>
    <button class="active" data-filter="all">全部</button><button data-filter="non_compliant">不合规</button>
    <button data-filter="compliant">合规</button><button data-filter="uncertain">不确定</button>
    <button data-filter="not_suitable">不适合作普通样本</button><button data-filter="missing">缺失正确证据</button></div>
    <div class="stats"><b>{payload['completed_case_count']}/{payload['case_count']} 案例已完成</b>
    <span>{payload['labeled_evidence_count']}/{payload['total_evidence_count']} 候选已标注</span>
    <span>父块标签：{esc(payload['final_label_counts'])}</span><span>证据标签：{esc(payload['evidence_label_counts'])}</span></div>
    <main>{''.join(sections)}</main><script>
    document.querySelectorAll('button[data-filter]').forEach(b=>b.addEventListener('click',()=>{{
      document.querySelectorAll('button').forEach(x=>x.classList.remove('active'));b.classList.add('active');
      document.querySelectorAll('.case').forEach(c=>c.style.display=(b.dataset.filter==='all'||c.dataset.final===b.dataset.filter||(b.dataset.filter==='missing'&&c.dataset.missing==='true'))?'block':'none');
    }}));</script></body></html>"""


def validate(payload: dict[str, Any]) -> None:
    if set(DECISIONS) != {case["case_id"] for case in payload["cases"]}:
        raise ValueError("Decision case IDs do not exactly match checkpoint case IDs")
    if payload["case_count"] != 50 or payload["completed_case_count"] != 50:
        raise ValueError("Expected all 50 cases to be completed")
    if payload["total_evidence_count"] != 434 or payload["labeled_evidence_count"] != 434:
        raise ValueError("Expected all 434 evidence candidates to be labeled")
    for case in payload["cases"]:
        if any(item["label"] not in {"useful", "uncertain", "useless"} for item in case["evidence_annotations"]):
            raise ValueError(f"Invalid evidence label in {case['case_id']}")
        if any(not item["note"] for item in case["evidence_annotations"]):
            raise ValueError(f"Missing evidence note in {case['case_id']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    parser.add_argument("--reapply", action="store_true")
    args = parser.parse_args()

    completed = skipped = 0
    for case_id in sorted(DECISIONS):
        path = args.checkpoint_dir / f"{case_id}.json"
        case = load_json(path)
        if case["status"] == "completed" and not args.reapply:
            skipped += 1
            continue
        atomic_write(path, annotate_case(case, DECISIONS[case_id]))
        completed += 1
        print(f"saved {case_id}")

    payload = build_summary(args.checkpoint_dir, args.summary)
    validate(payload)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    with args.html.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(build_html(payload))
    print(f"completed_now={completed} skipped={skipped}")
    print(f"cases={payload['completed_case_count']}/{payload['case_count']}")
    print(f"evidence={payload['labeled_evidence_count']}/{payload['total_evidence_count']}")
    print(f"labels={payload['evidence_label_counts']}")
    print(args.summary)
    print(args.html)


if __name__ == "__main__":
    main()
