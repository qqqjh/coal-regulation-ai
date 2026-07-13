"""Generate a reviewable knowledge-base gap registry from cleaned gold cases."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "kb_gap_registry_v1.json"
GAP_STATUSES = {"likely_not_in_kb", "needs_kb_audit", "known_retrieval_miss"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = load_json(args.input)
    gaps = []
    for case in source["cases"]:
        if case["target_evidence_status"] not in GAP_STATUSES:
            continue
        gaps.append({
            "gap_id": f"gap_{case['case_id']}",
            "case_id": case["case_id"],
            "status": case["target_evidence_status"],
            "scope_note": case.get("scope_note", ""),
            "evaluation_task_type": case["evaluation_task_type"],
            "pending_doc_name": case["pending"].get("doc_name", ""),
            "pending_location": {
                key: case["pending"].get(key, "")
                for key in ("chapter", "section", "article", "page_range")
            },
            "pending_content": case["pending"].get("content", ""),
            "atomic_claims": [
                {
                    "claim_id": claim["claim_id"],
                    "claim_text": claim["claim_text"],
                    "retrieval_query": claim["retrieval_query"],
                }
                for claim in case["claims"]
            ],
            "human_review": {
                "confirmed_gap_type": "",
                "required_rule_topic": "",
                "candidate_source_name": "",
                "candidate_source_version": "",
                "candidate_source_location": "",
                "rule_text_to_add": "",
                "decision": "unreviewed",
                "reviewer": "",
                "note": "",
            },
        })
    payload = {
        "schema": "coal_rag_kb_gap_registry_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(args.input.resolve()),
        "instructions": [
            "known_retrieval_miss means evidence is believed to exist in the current KB; fix retrieval before adding rules.",
            "likely_not_in_kb requires a source-document check before adding rules.",
            "needs_kb_audit is unresolved and must not be counted as a confirmed KB gap.",
            "Only approved, versioned, applicable rule text should be added to the KB.",
        ],
        "summary": {
            "gap_record_count": len(gaps),
            "status_counts": dict(Counter(gap["status"] for gap in gaps)),
        },
        "gaps": gaps,
    }
    write_json(args.output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
