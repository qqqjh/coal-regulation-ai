"""Build rule evidence groups from manually reviewed similar-rule pairs."""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ANNOTATIONS = (
    PROJECT_ROOT
    / "rag_eval"
    / "data"
    / "similar_rule_pairs_bgem3_v6_threshold090_codex_annotated.json"
)
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def build_groups(source: dict[str, Any]) -> dict[str, Any]:
    union_find = UnionFind()
    accepted_pairs = []
    for pair in source["pairs"]:
        annotation = pair["codex_annotation"]
        if not annotation["same_evidence_group"]:
            continue
        left_id = pair["left"]["chunk_id"]
        right_id = pair["right"]["chunk_id"]
        union_find.union(left_id, right_id)
        accepted_pairs.append(
            {
                "pair_id": pair["pair_id"],
                "left_chunk_id": left_id,
                "right_chunk_id": right_id,
                "relation": annotation["duplicate_relation"],
                "containment_direction": annotation["containment_direction"],
                "reason_zh": annotation["reason_zh"],
            }
        )

    components: dict[str, list[str]] = defaultdict(list)
    for chunk_id in union_find.parent:
        components[union_find.find(chunk_id)].append(chunk_id)

    ordered_components = sorted(
        (sorted(members) for members in components.values()),
        key=lambda members: members[0],
    )
    chunk_to_group: dict[str, str] = {}
    groups = []
    for index, members in enumerate(ordered_components, start=1):
        group_id = f"EG_{index:04d}"
        group_pairs = [
            pair
            for pair in accepted_pairs
            if pair["left_chunk_id"] in members and pair["right_chunk_id"] in members
        ]
        relation_types = sorted({pair["relation"] for pair in group_pairs})
        groups.append(
            {
                "evidence_group_id": group_id,
                "member_count": len(members),
                "members": members,
                "relation_types": relation_types,
                "source_pairs": group_pairs,
            }
        )
        for chunk_id in members:
            chunk_to_group[chunk_id] = group_id

    return {
        "schema": "coal_rule_evidence_groups_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(DEFAULT_ANNOTATIONS.resolve()),
        "definition_zh": "同组规则在单次检索结果中仅保留reranker排序最高的一条；包含关系也纳入组，但不删除原始规则。",
        "group_count": len(groups),
        "grouped_chunk_count": len(chunk_to_group),
        "accepted_pair_count": len(accepted_pairs),
        "groups": groups,
        "chunk_to_group": chunk_to_group,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", type=Path, default=DEFAULT_ANNOTATIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    source = load_json(args.annotations)
    payload = build_groups(source)
    payload["source"] = str(args.annotations.resolve())
    atomic_write(args.output, payload)
    print(
        f"groups={payload['group_count']} grouped_chunks={payload['grouped_chunk_count']} "
        f"accepted_pairs={payload['accepted_pair_count']}"
    )
    print(args.output.resolve())


if __name__ == "__main__":
    main()
