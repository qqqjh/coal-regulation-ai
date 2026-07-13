"""Initialize per-case checkpoints for Codex-reviewed atomic claim extraction."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
CHECKPOINTS = ROOT / "rag_eval" / "data" / "codex_atomic_claim_checkpoints_v2"
SUMMARY = ROOT / "rag_eval" / "data" / "gold_atomic_claims_codex_v2.json"


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


def main() -> None:
    source = load(SOURCE)
    created = skipped = 0
    for case in source["cases"]:
        path = CHECKPOINTS / f"{case['case_id']}.json"
        if path.exists():
            skipped += 1
            continue
        checkpoint = {
            **case,
            "schema": "coal_rag_codex_atomic_claim_checkpoint_v2",
            "annotation_status": "pending_codex_manual_extraction",
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "review_notes": [],
            "draft_rule_claim_count": len(case["claims"]),
            "draft_excluded_unit_count": len(case["excluded_units"]),
            "claims": [],
            "excluded_units": [],
        }
        atomic_write(path, checkpoint)
        created += 1

    cases = [load(path) for path in sorted(CHECKPOINTS.glob("*.json"))]
    payload = {
        "schema": "coal_rag_gold_atomic_claims_codex_v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(SOURCE.resolve()),
        "annotation_method": "Codex manual context-preserving atomic claim extraction; one case saved atomically.",
        "case_count": len(cases),
        "completed_case_count": sum(
            case["annotation_status"] == "completed_codex_manual_extraction" for case in cases
        ),
        "retrieval_claim_count": sum(len(case["claims"]) for case in cases),
        "cases": cases,
    }
    atomic_write(SUMMARY, payload)
    print(f"created={created} skipped={skipped}")
    print(SUMMARY.resolve())


if __name__ == "__main__":
    main()
