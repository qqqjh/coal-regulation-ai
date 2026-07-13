"""Apply checkpointed Codex labels to remaining atomic candidates."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from init_atomic_bgem3_gold_v1 import render


ROOT = Path(__file__).resolve().parents[2]
CHECKPOINTS = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top10_gold_checkpoints"
DECISIONS = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top10_manual_decisions.json"
SUMMARY = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top10_gold_annotations.json"
HTML = ROOT / "rag_eval" / "reports" / "atomic_codex_v2_focused_top10_gold_review.html"


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


def compact(text: str, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def note(label: str, case: dict[str, Any], evidence: dict[str, Any]) -> str:
    focus = compact(case["pending"].get("content", ""), 70)
    rule = compact(evidence.get("content", ""), 105)
    if label == "useful":
        return f"Codex复核：该规则直接覆盖待审块的关键对象、行为、参数或强制要求，可作为有效证据。待审关注点：{focus}；规则要点：{rule}"
    if label == "uncertain":
        return f"Codex复核：该规则与待审块存在实质关联，但适用对象、条件、范围或阈值不能完全确认，仅作为辅助证据。待审关注点：{focus}；规则要点：{rule}"
    return f"Codex复核：该规则未直接覆盖本待审块的核心审查事项，不能作为有效证据。规则要点：{rule}"


def apply_case(case: dict[str, Any], decision: dict[str, Any], variant: str) -> dict[str, Any]:
    useful = set(decision.get("useful", []))
    uncertain = set(decision.get("uncertain", []))
    if useful & uncertain:
        raise ValueError(f"{case['case_id']} has overlapping useful/uncertain ranks")
    ranks = {item["parent_rank"] for item in case["evidence_annotations"]}
    if (useful | uncertain) - ranks:
        raise ValueError(f"{case['case_id']} references missing ranks: {sorted((useful | uncertain) - ranks)}")

    for item in case["evidence_annotations"]:
        if item["label"] != "unlabeled":
            continue
        rank = item["parent_rank"]
        label = "useful" if rank in useful else "uncertain" if rank in uncertain else "useless"
        item["label"] = label
        item["confidence"] = "high" if label != "uncertain" else "medium"
        item["note"] = note(label, case, item)
        item["annotation_source"] = f"codex_manual_atomic_v2_{variant}_remaining"
    case["labeled_evidence_count"] = sum(
        item["label"] != "unlabeled" for item in case["evidence_annotations"]
    )
    case["status"] = "completed" if case["labeled_evidence_count"] == case["evidence_count"] else "in_progress"
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    case["annotation_method"] = (
        f"Codex manually reviewed remaining {variant} atomic candidates; inherited labels preserved."
    )
    return case


def build_summary(checkpoints: Path, variant: str) -> dict[str, Any]:
    cases = [load(path) for path in sorted(checkpoints.glob("*.json"))]
    labels = Counter(item["label"] for case in cases for item in case["evidence_annotations"])
    sources = Counter(item["annotation_source"] for case in cases for item in case["evidence_annotations"])
    return {
        "schema": "atomic_bgem3_gold_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "annotation_method": (
            f"Existing gold migration plus checkpointed Codex manual review of remaining {variant} candidates."
        ),
        "human_review_status": "pending_domain_expert_confirmation",
        "checkpoint_directory": str(checkpoints.resolve()),
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "total_evidence_count": sum(case["evidence_count"] for case in cases),
        "labeled_evidence_count": sum(labels.values()) - labels["unlabeled"],
        "evidence_label_counts": dict(labels),
        "annotation_source_counts": dict(sources),
        "migration_conflict_count": sum(
            item.get("migration_conflict", False)
            for case in cases
            for item in case["evidence_annotations"]
        ),
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, default=CHECKPOINTS)
    parser.add_argument("--decisions", type=Path, default=DECISIONS)
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--html", type=Path, default=HTML)
    parser.add_argument("--variant", default="top10")
    parser.add_argument("--reapply", action="store_true")
    args = parser.parse_args()

    decisions = load(args.decisions)
    saved = skipped = 0
    for case_id, decision in decisions.get("cases", {}).items():
        path = args.checkpoints / f"{case_id}.json"
        case = load(path)
        if case["status"] == "completed" and not args.reapply:
            skipped += 1
            continue
        atomic_write(path, apply_case(case, decision, args.variant))
        saved += 1
        print(f"saved {case_id}", flush=True)

    payload = build_summary(args.checkpoints, args.variant)
    atomic_write(args.summary, payload)
    args.html.write_text(render(payload), encoding="utf-8")
    print(f"saved={saved} skipped={skipped}")
    print(f"cases={payload['completed_case_count']}/{payload['case_count']}")
    print(f"evidence={payload['labeled_evidence_count']}/{payload['total_evidence_count']}")
    print(f"labels={payload['evidence_label_counts']}")


if __name__ == "__main__":
    main()
