"""Print compact pending/candidate material for manual atomic annotation."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
CHECKPOINTS = ROOT / "rag_eval" / "data" / "atomic_codex_v2_focused_top10_gold_checkpoints"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, default=CHECKPOINTS)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=4)
    parser.add_argument("--only-incomplete", action="store_true")
    args = parser.parse_args()

    paths = sorted(args.checkpoints.glob("*.json"))
    if args.only_incomplete:
        paths = [path for path in paths if load(path)["status"] != "completed"]
    for path in paths[args.start: args.start + args.count]:
        case = load(path)
        print(f"\n\n===== {case['case_id']} | {case['status']} | {case['labeled_evidence_count']}/{case['evidence_count']} =====")
        print("PENDING:", compact(case["pending"]["content"], 650))
        print("CLAIMS:")
        for claim in case.get("retrieval_claims", []):
            print(f"  - {claim['claim_id']}: {compact(claim['claim_text'], 180)}")
        print("CANDIDATES:")
        for item in case["evidence_annotations"]:
            print(
                f"  #{item['parent_rank']:02d} [{item['label']}] {item['chunk_id']} "
                f"| claims={','.join(item['covered_claim_ids'])} "
                f"| {compact(item['content'], 330)}"
            )


if __name__ == "__main__":
    main()
