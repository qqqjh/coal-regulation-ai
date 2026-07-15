"""Manage atomic per-case checkpoints for local BGE-M3 parent annotations."""

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
DEFAULT_CANDIDATES = PROJECT_ROOT / "rag_eval" / "data" / "parent_candidates_local_bgem3_v6.json"
DEFAULT_CHECKPOINT_DIR = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotation_checkpoints_v1"
DEFAULT_SUMMARY = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "parent_bgem3_annotations_review_v1.html"


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


def initialize(candidates_path: Path, checkpoint_dir: Path) -> None:
    payload = load_json(candidates_path)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    created = skipped = 0
    for case in payload["cases"]:
        path = checkpoint_dir / f"{case['case_id']}.json"
        if path.exists():
            skipped += 1
            continue
        checkpoint = {
            "schema": "coal_rag_parent_bgem3_annotation_checkpoint_v1",
            "case_id": case["case_id"],
            "status": "pending",
            "annotation_source": "codex_manual_local_bgem3_v6",
            "human_review_status": "pending_domain_expert_confirmation",
            "pending": case["pending"],
            "source_top_k": case["source_top_k"],
            "final_label": "unlabeled",
            "final_reason": "",
            "missing_correct_evidence": False,
            "not_eval_suitable": False,
            "review_flags": [],
            "candidate_annotations": [
                {
                    **candidate,
                    "label": "unlabeled",
                    "confidence": "",
                    "note": "",
                }
                for candidate in case["candidates"]
            ],
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        atomic_write(path, checkpoint)
        created += 1
    print(f"created={created} skipped={skipped} directory={checkpoint_dir}")


def build_summary(checkpoint_dir: Path, output: Path) -> dict[str, Any]:
    cases = [load_json(path) for path in sorted(checkpoint_dir.glob("*.json"))]
    candidate_labels = Counter(
        candidate["label"]
        for case in cases
        for candidate in case["candidate_annotations"]
    )
    final_labels = Counter(case["final_label"] for case in cases)
    payload = {
        "schema": "coal_rag_parent_bgem3_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checkpoint_directory": str(checkpoint_dir.resolve()),
        "human_review_status": "pending_domain_expert_confirmation",
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "total_candidate_count": sum(len(case["candidate_annotations"]) for case in cases),
        "labeled_candidate_count": sum(
            candidate["label"] != "unlabeled"
            for case in cases
            for candidate in case["candidate_annotations"]
        ),
        "final_label_counts": dict(final_labels),
        "candidate_label_counts": dict(candidate_labels),
        "cases": cases,
    }
    atomic_write(output, payload)
    return payload


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def build_html(payload: dict[str, Any]) -> str:
    sections = []
    for index, case in enumerate(payload["cases"], start=1):
        candidates = []
        for candidate in case["candidate_annotations"]:
            details = " · ".join(
                f"{source} Top{detail['rank']}"
                for source, detail in candidate.get("retrieval_details", {}).items()
            )
            candidates.append(
                f"""<article class="candidate {esc(candidate['label'])}">
                <h3>#{candidate['rank']} {esc(candidate['label'])} · {esc(candidate['chunk_id'])}</h3>
                <p class="meta">{esc(details)} · rerank={candidate.get('rerank_score', 0):.6f}</p>
                <p>{esc(candidate.get('note'))}</p>
                <details><summary>规则全文</summary><pre>{esc(candidate['content'])}</pre></details>
                </article>"""
            )
        sections.append(
            f"""<section class="case" data-status="{esc(case['status'])}" data-final="{esc(case['final_label'])}">
            <header><span>{index:02d}</span><div><h2>{esc(case['case_id'])}</h2>
            <p>{esc(case['pending'].get('doc_name'))}</p></div><b>{esc(case['final_label'])}</b></header>
            <div class="judgment"><p>{esc(case['final_reason'])}</p>
            <span>missing_correct_evidence={str(case['missing_correct_evidence']).lower()}</span>
            <span>not_eval_suitable={str(case['not_eval_suitable']).lower()}</span></div>
            <details open><summary>待审 chunk</summary><pre>{esc(case['pending']['content'])}</pre></details>
            <div class="grid">{''.join(candidates)}</div></section>"""
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>BGE-M3 标注复核</title>
    <style>*{{box-sizing:border-box}}body{{margin:0;background:#f3f4f2;color:#1c2822;font-family:"Microsoft YaHei",sans-serif}}
    .top{{position:sticky;top:0;z-index:5;background:#1d2d25;color:white;padding:18px 4vw}}main{{width:min(1600px,94vw);margin:24px auto}}
    .case{{background:white;border-top:4px solid #1d2d25;margin-bottom:24px;padding:18px}}header{{display:grid;grid-template-columns:45px 1fr auto;gap:10px}}
    h2,h3{{margin:0}}header p,.meta{{color:#68766f;font-size:12px}}.judgment{{border-left:4px solid #b68b38;padding:8px 12px;margin:12px 0}}
    .judgment span{{display:inline-block;border:1px solid #ccd2ce;padding:3px 6px;margin-right:6px;font-size:12px}}details{{border:1px solid #d8ddda;margin-top:8px}}
    summary{{cursor:pointer;padding:8px;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;margin:0;padding:10px;line-height:1.6}}
    .grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:14px}}.candidate{{border:1px solid #d8ddda;border-top:4px solid #969e99;padding:10px;min-width:0}}
    .candidate.useful{{border-top-color:#187050}}.candidate.uncertain{{border-top-color:#bd7c21}}.candidate.useless{{border-top-color:#969e99}}.candidate.unlabeled{{border-top-color:#b33a32}}
    .candidate h3{{font-size:13px;overflow-wrap:anywhere}}.candidate p{{font-size:13px;line-height:1.55}}@media(max-width:1000px){{.grid{{grid-template-columns:1fr}}}}</style></head>
    <body><div class="top"><h1>BGE-M3 黄金标注复核</h1><p>{payload['completed_case_count']}/{payload['case_count']} 案例完成 · {payload['labeled_candidate_count']}/{payload['total_candidate_count']} 候选已标注</p></div>
    <main>{''.join(sections)}</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "build"))
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.candidates, args.checkpoint_dir)
    payload = build_summary(args.checkpoint_dir, args.summary)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(build_html(payload), encoding="utf-8")
    print(
        f"cases={payload['completed_case_count']}/{payload['case_count']} "
        f"candidates={payload['labeled_candidate_count']}/{payload['total_candidate_count']}"
    )
    print(args.summary)
    print(args.html)


if __name__ == "__main__":
    main()
