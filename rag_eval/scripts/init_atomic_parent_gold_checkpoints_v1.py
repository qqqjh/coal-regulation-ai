# -*- coding: utf-8 -*-
"""Initialize resumable parent-chunk gold annotation checkpoints.

Each parent case is stored in its own JSON file. Existing checkpoint files are
never overwritten unless --overwrite is explicitly passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARENT = PROJECT_ROOT / "rag_eval" / "data" / "parent_chunk_evidence_v9_v1.json"
DEFAULT_PRIOR = PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
DEFAULT_DIR = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_checkpoints_v1"
DEFAULT_SUMMARY = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_content(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def initialize_case(case: dict[str, Any], prior: dict[str, Any] | None) -> dict[str, Any]:
    prior_by_content = {
        normalized_content(item["content"]): item
        for item in (prior or {}).get("candidate_annotations", [])
    }
    annotations = []
    inherited = 0
    for evidence in case["final_evidence"]:
        matched = prior_by_content.get(normalized_content(evidence["content"]))
        if matched:
            inherited += 1
        annotations.append({
            "parent_rank": evidence["parent_rank"],
            "chunk_id": evidence["chunk_id"],
            "doc_name": evidence["doc_name"],
            "content": evidence["content"],
            "covered_claim_ids": evidence["covered_claim_ids"],
            "claim_hits": evidence["claim_hits"],
            "label": matched["label"] if matched else "unlabeled",
            "confidence": matched.get("confidence", "medium") if matched else "unlabeled",
            "note": (
                f"继承上一轮同一父chunk下完全相同法规全文的标注：{matched['note']}"
                if matched else ""
            ),
            "annotation_source": "prior_same_case_exact_content" if matched else "unlabeled",
        })

    return {
        "schema": "atomic_parent_gold_checkpoint_v1",
        "case_id": case["case_id"],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "in_progress",
        "evaluation_task_type": case["evaluation_task_type"],
        "target_evidence_status": case["target_evidence_status"],
        "pending": case["pending"],
        "retrieval_claims": case["retrieval_claims"],
        "final_label": (prior or {}).get("final_label", "unlabeled"),
        "final_confidence": (prior or {}).get("final_confidence", "unlabeled"),
        "final_reason": (prior or {}).get("final_reason", ""),
        "missing_correct_evidence": (prior or {}).get("missing_correct_evidence", False),
        "not_eval_suitable": (prior or {}).get("not_eval_suitable", False),
        "review_flags": (prior or {}).get("review_flags", []),
        "evidence_count": len(annotations),
        "inherited_evidence_count": inherited,
        "evidence_annotations": annotations,
    }


def build_summary(checkpoint_dir: Path, output: Path) -> dict[str, Any]:
    cases = [
        load_json(path)
        for path in sorted(checkpoint_dir.glob("*.json"))
        if path.name != output.name
    ]
    payload = {
        "schema": "atomic_parent_gold_annotations_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checkpoint_directory": str(checkpoint_dir),
        "case_count": len(cases),
        "completed_case_count": sum(case["status"] == "completed" for case in cases),
        "labeled_evidence_count": sum(
            item["label"] != "unlabeled"
            for case in cases
            for item in case["evidence_annotations"]
        ),
        "total_evidence_count": sum(case["evidence_count"] for case in cases),
        "cases": cases,
    }
    atomic_write(output, payload)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, default=DEFAULT_PARENT)
    parser.add_argument("--prior", type=Path, default=DEFAULT_PRIOR)
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    parent = load_json(args.parent)
    prior_cases = {case["case_id"]: case for case in load_json(args.prior)["cases"]}
    created = skipped = 0
    for case in parent["cases"]:
        path = args.checkpoint_dir / f"{case['case_id']}.json"
        if path.exists() and not args.overwrite:
            skipped += 1
            continue
        atomic_write(path, initialize_case(case, prior_cases.get(case["case_id"])))
        created += 1

    summary = build_summary(args.checkpoint_dir, args.summary)
    print(f"created={created} skipped={skipped}")
    print(
        f"cases={summary['case_count']} completed={summary['completed_case_count']} "
        f"evidence={summary['labeled_evidence_count']}/{summary['total_evidence_count']}"
    )
    print(args.checkpoint_dir)
    print(args.summary)


if __name__ == "__main__":
    main()
