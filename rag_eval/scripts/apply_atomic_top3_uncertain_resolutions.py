"""Apply checkpointed final resolutions to uncertain atomic Top3 evidence."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from init_atomic_bgem3_gold_v1 import render


ROOT = Path(__file__).resolve().parents[2]
CHECKPOINTS = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top3_gold_checkpoints"
RESOLUTIONS = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top3_uncertain_resolutions.json"
SUMMARY = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top3_gold_annotations.json"
HTML = ROOT / "rag_eval" / "reports" / "atomic_codex_v2_focused_top3_gold_review.html"


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


def resolution_note(label: str, reason: str) -> str:
    prefix = "Codex最终复核：可作为有效证据。" if label == "useful" else "Codex最终复核：不能作为有效证据。"
    return f"{prefix}{reason}"


def apply_resolutions(checkpoints: Path, resolutions_path: Path) -> tuple[int, int]:
    payload = load(resolutions_path)
    saved = 0
    resolved = 0
    for case_id, decisions in payload["cases"].items():
        path = checkpoints / f"{case_id}.json"
        case = load(path)
        by_rank = {item["parent_rank"]: item for item in case["evidence_annotations"]}
        changed = False
        for rank_text, decision in decisions.items():
            rank = int(rank_text)
            item = by_rank[rank]
            label = decision["label"]
            if label not in {"useful", "useless"}:
                raise ValueError(f"{case_id} rank {rank}: invalid label {label}")
            if item["label"] not in {"uncertain", label}:
                raise ValueError(f"{case_id} rank {rank}: expected uncertain, found {item['label']}")
            item["label"] = label
            item["confidence"] = "high"
            item["note"] = resolution_note(label, decision["reason"])
            item["annotation_source"] = "codex_manual_atomic_v2_top3_uncertain_resolution"
            changed = True
            resolved += 1
        if changed:
            case["updated_at"] = datetime.now().isoformat(timespec="seconds")
            case["human_review_status"] = "pending_domain_expert_confirmation"
            atomic_write(path, case)
            saved += 1
    return saved, resolved


def rebuild(checkpoints: Path, summary_path: Path, html_path: Path) -> dict[str, Any]:
    cases = [load(path) for path in sorted(checkpoints.glob("*.json"))]
    labels = Counter(item["label"] for case in cases for item in case["evidence_annotations"])
    sources = Counter(item["annotation_source"] for case in cases for item in case["evidence_annotations"])
    payload = {
        "schema": "atomic_bgem3_gold_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "annotation_method": "Checkpointed Codex review with final uncertain evidence resolutions.",
        "human_review_status": "pending_domain_expert_confirmation",
        "checkpoint_directory": str(checkpoints.resolve()),
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "total_evidence_count": sum(case["evidence_count"] for case in cases),
        "labeled_evidence_count": sum(case["labeled_evidence_count"] for case in cases),
        "evidence_label_counts": dict(labels),
        "annotation_source_counts": dict(sources),
        "migration_conflict_count": sum(
            bool(item.get("migration_conflict"))
            for case in cases
            for item in case["evidence_annotations"]
        ),
        "cases": cases,
    }
    atomic_write(summary_path, payload)
    html_path.write_text(render(payload), encoding="utf-8")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, default=CHECKPOINTS)
    parser.add_argument("--resolutions", type=Path, default=RESOLUTIONS)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--html", type=Path, default=HTML)
    args = parser.parse_args()

    saved, resolved = apply_resolutions(args.checkpoints, args.resolutions)
    payload = rebuild(args.checkpoints, args.summary, args.html)
    print(f"saved_cases={saved} resolved_entries={resolved}")
    print(f"labels={payload['evidence_label_counts']}")


if __name__ == "__main__":
    main()
