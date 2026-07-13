"""Build a cleaned, claim-level retrieval evaluation dataset.

This script never overwrites the existing 50-case annotation files. It:
- separates retrieval task types and knowledge-base coverage status;
- extracts reviewable atomic claims while keeping the v9 parent chunk fixed;
- preserves confirmed useful evidence at parent-case level;
- excludes unsuitable, mapping-drift, and target-less cases from strict recall.

Atomic claim extraction is heuristic and every generated claim is explicitly
marked as requiring human confirmation before it becomes claim-level gold.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
DEFAULT_OVERRIDES = PROJECT_ROOT / "rag_eval" / "config" / "gold_scope_overrides_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"

LIST_MARKER = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十百]+[、.]|"
    r"\d+(?:\.\d+)*[、.)．]|"
    r"[（(][一二三四五六七八九十百\d]+[）)]|"
    r"[A-Za-z][.)、]"
    r")\s*"
)
SENTENCE_SPLIT = re.compile(r"(?<=[。；;！？!?])\s*")
NORMATIVE_TERMS = (
    "必须", "应当", "严禁", "禁止", "不得", "不准", "需要", "要求",
    "不低于", "不小于", "不大于", "不超过", "至少", "方可",
)
STRONG_NORMATIVE_TERMS = (
    "必须", "应当", "严禁", "禁止", "不得", "不准", "不低于", "不小于",
    "不大于", "不超过", "至少", "方可",
)
THRESHOLD_RE = re.compile(
    r"(?:不低于|不小于|不大于|不超过|至少|达到|超过|小于|大于|间距|浓度|力矩|锚固力)"
    r".{0,24}\d"
)
TOC_RE = re.compile(r"^第[一二三四五六七八九十百\d]+[章节].{0,35}(?:\d+|[.。·]{2,}\d*)$")
DOMAIN_TERMS = (
    "瓦斯", "锚杆", "锚索", "支护", "风量", "浓度", "间距", "高度", "长度",
    "速度", "力矩", "预紧力", "锚固力", "断电", "复电", "送电", "停电",
    "作业", "支架", "设备", "顶板", "钻孔", "管路", "喷雾", "风筒",
    "传感器", "采煤机", "胶带", "起吊", "防尘", "通风", "抽采", "防爆",
)
FORMULA_RE = re.compile(r"^(?:Q\s*[采放循]|[A-Za-z]\w{0,5})\s*=")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def compact(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def no_space(text: str) -> str:
    return re.sub(r"\s+", "", text)


def is_context_only(line: str) -> bool:
    normalized = no_space(line)
    marker_stripped = no_space(LIST_MARKER.sub("", line))
    if line.startswith("【表格】"):
        return True
    if TOC_RE.match(normalized):
        return True
    if re.match(r"^第[一二三四五六七八九十百\d]+[章节条款]", normalized):
        return len(normalized) <= 34 and not any(term in normalized for term in NORMATIVE_TERMS)
    if (
        len(marker_stripped) <= 22
        and marker_stripped
        and not re.search(r"\d", marker_stripped)
        and not any(term in marker_stripped for term in NORMATIVE_TERMS)
        and not marker_stripped.endswith(("。", "；", ";", "！", "？", ":", "："))
    ):
        return True
    return False


def classify_candidate(text: str) -> Dict[str, Any]:
    normalized = no_space(text)
    has_strong_norm = any(term in normalized for term in STRONG_NORMATIVE_TERMS)
    has_norm = has_strong_norm or any(term in normalized for term in NORMATIVE_TERMS)
    has_domain = any(term in normalized for term in DOMAIN_TERMS)
    has_threshold = bool(THRESHOLD_RE.search(normalized))

    if "|" in text and not has_strong_norm:
        return {"include_as_claim": False, "exclusion_reason": "table_or_inventory_row"}
    if FORMULA_RE.match(normalized) or (
        "=" in normalized
        and not has_norm
        and re.fullmatch(r"[\dA-Za-z.%‰±=+\-×÷/()（）·]+", normalized)
    ):
        return {"include_as_claim": False, "exclusion_reason": "formula_or_unit_conversion"}
    if len(normalized) < 16 and not has_strong_norm:
        return {"include_as_claim": False, "exclusion_reason": "short_context_or_fragment"}
    if normalized.endswith(("：", ":")) and not has_strong_norm:
        return {"include_as_claim": False, "exclusion_reason": "label_or_heading"}
    if has_strong_norm:
        return {
            "include_as_claim": True,
            "retrieval_priority": "high",
            "priority_reason": "contains explicit normative language",
        }
    if has_threshold and has_domain:
        return {
            "include_as_claim": True,
            "retrieval_priority": "high",
            "priority_reason": "contains a domain-specific numeric threshold",
        }
    if has_norm and has_domain and len(normalized) >= 24:
        return {
            "include_as_claim": True,
            "retrieval_priority": "medium",
            "priority_reason": "contains a domain-specific requirement",
        }
    if has_domain and re.search(r"\d", normalized) and len(normalized) >= 28:
        return {
            "include_as_claim": True,
            "retrieval_priority": "medium",
            "priority_reason": "contains a checkable domain parameter",
        }
    return {"include_as_claim": False, "exclusion_reason": "descriptive_or_non_checkable_context"}


def split_long_unit(text: str, max_chars: int) -> Iterable[str]:
    if len(text) <= max_chars:
        yield text
        return
    parts = [part.strip() for part in SENTENCE_SPLIT.split(text) if part.strip()]
    if len(parts) <= 1:
        yield text
        return
    current = ""
    for part in parts:
        if current and len(current) + len(part) > max_chars:
            yield current
            current = part
        else:
            current += part
    if current:
        yield current


def build_semantic_units(raw_lines: Sequence[str]) -> List[Dict[str, Any]]:
    units: List[Dict[str, Any]] = []
    buffer = ""
    buffer_start = 0

    def flush() -> None:
        nonlocal buffer, buffer_start
        if buffer:
            units.append({"source_line": buffer_start, "text": buffer})
        buffer = ""
        buffer_start = 0

    for line_no, line in enumerate(raw_lines, start=1):
        standalone_table = line.startswith("【表格】") or "|" in line
        standalone_context = is_context_only(line)
        starts_new_item = bool(LIST_MARKER.match(line))
        previous_complete = bool(re.search(r"[。；;！？!?]$", buffer))
        if standalone_table or standalone_context:
            flush()
            units.append({"source_line": line_no, "text": line})
        elif not buffer:
            buffer = line
            buffer_start = line_no
        elif starts_new_item or previous_complete:
            flush()
            buffer = line
            buffer_start = line_no
        else:
            buffer += line
    flush()
    return units


def extract_atomic_claims(pending: Dict[str, Any], max_chars: int = 320) -> Dict[str, List[Dict[str, Any]]]:
    content = str(pending.get("content", "")).replace("\r\n", "\n").replace("\r", "\n")
    raw_lines = [compact(line) for line in content.split("\n") if compact(line)]
    context = [compact(pending.get(key, "")) for key in ("chapter", "section", "article")]
    context = [item for item in context if item]

    claims: List[Dict[str, Any]] = []
    excluded_units: List[Dict[str, Any]] = []
    current_heading = ""
    semantic_units = build_semantic_units(raw_lines)
    for semantic_unit in semantic_units:
        line_no = semantic_unit["source_line"]
        line = semantic_unit["text"]
        if is_context_only(line):
            current_heading = line
            continue
        for part in split_long_unit(line, max_chars=max_chars):
            claim_text = LIST_MARKER.sub("", part).strip() or part
            retrieval_parts = list(context)
            if current_heading and current_heading not in retrieval_parts:
                retrieval_parts.append(current_heading)
            retrieval_parts.append(claim_text)
            classification = classify_candidate(claim_text)
            unit = {
                "source_line": line_no,
                "source_text": part,
                "claim_text": claim_text,
                "retrieval_query": " ".join(retrieval_parts),
                "claim_review_status": "needs_human_confirmation",
                "purpose": "retrieval_query_only",
                "is_gold_evaluation_unit": False,
                "target_evidence_ids": [],
                **classification,
            }
            if classification["include_as_claim"]:
                claims.append(unit)
            else:
                excluded_units.append(unit)

    if not claims and compact(content):
        classification = classify_candidate(compact(content))
        fallback = {
            "source_line": 1,
            "source_text": compact(content),
            "claim_text": compact(content),
            "retrieval_query": " ".join(context + [compact(content)]),
            "claim_review_status": "needs_human_confirmation",
            "purpose": "retrieval_query_only",
            "is_gold_evaluation_unit": False,
            "target_evidence_ids": [],
            **classification,
        }
        if classification["include_as_claim"]:
            claims.append(fallback)
        elif not excluded_units:
            excluded_units.append(fallback)

    seen = set()
    deduplicated = []
    for unit in claims:
        key = re.sub(r"\s+", "", unit["claim_text"])
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(unit)
    return {"claims": deduplicated, "excluded_units": excluded_units}


def classify_task(case: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    case_id = case["case_id"]
    if case_id in overrides.get("not_retrieval_suitable", {}):
        return {
            "evaluation_task_type": "not_retrieval_suitable",
            "target_evidence_status": "not_retrieval_task",
            "scope_note": overrides["not_retrieval_suitable"][case_id],
        }
    if case_id in overrides.get("mapping_drift", {}):
        return {
            "evaluation_task_type": "not_retrieval_suitable",
            "target_evidence_status": "mapping_drift",
            "scope_note": overrides["mapping_drift"][case_id],
        }
    if case_id in overrides.get("likely_not_in_kb", {}):
        return {
            "evaluation_task_type": "applicability_check"
            if case.get("final_label") == "uncertain"
            else "compliance_support_retrieval",
            "target_evidence_status": "likely_not_in_kb",
            "scope_note": overrides["likely_not_in_kb"][case_id],
        }
    if case_id in overrides.get("applicability_unclear", {}):
        return {
            "evaluation_task_type": "applicability_check",
            "target_evidence_status": "applicability_unclear",
            "scope_note": overrides["applicability_unclear"][case_id],
        }

    useful = [item for item in case.get("candidate_annotations", []) if item.get("label") == "useful"]
    label = case.get("final_label")
    if label == "not_suitable" or case.get("not_eval_suitable"):
        task_type = "not_retrieval_suitable"
    elif label == "non_compliant":
        task_type = "violation_evidence_retrieval"
    elif label == "compliant":
        task_type = "compliance_support_retrieval"
    else:
        task_type = "applicability_check"

    if useful:
        evidence_status = "found_in_kb"
    elif case_id in overrides.get("known_retrieval_miss", {}):
        evidence_status = "known_retrieval_miss"
    elif task_type == "not_retrieval_suitable":
        evidence_status = "not_retrieval_task"
    else:
        evidence_status = "needs_kb_audit"

    return {
        "evaluation_task_type": task_type,
        "target_evidence_status": evidence_status,
        "scope_note": overrides.get("known_retrieval_miss", {}).get(case_id, ""),
    }


def build_case(case: Dict[str, Any], overrides: Dict[str, Any], max_claim_chars: int) -> Dict[str, Any]:
    scope = classify_task(case, overrides)
    useful = [item for item in case.get("candidate_annotations", []) if item.get("label") == "useful"]
    target_ids = list(dict.fromkeys(item["chunk_id"] for item in useful))
    extracted = (
        {"claims": [], "excluded_units": []}
        if scope["evaluation_task_type"] == "not_retrieval_suitable"
        else extract_atomic_claims(case["pending"], max_chars=max_claim_chars)
    )
    claims = extracted["claims"]
    for index, claim in enumerate(claims, start=1):
        claim["claim_id"] = f"{case['case_id']}__claim{index:02d}"
        claim["parent_target_evidence_ids"] = target_ids

    include_in_strict_recall = (
        scope["evaluation_task_type"] in {
            "violation_evidence_retrieval",
            "compliance_support_retrieval",
        }
        and scope["target_evidence_status"] == "found_in_kb"
        and bool(target_ids)
    )
    return {
        "case_id": case["case_id"],
        "annotation_status": "codex_preannotation_needs_human_confirmation",
        **scope,
        "include_in_strict_recall": include_in_strict_recall,
        "final_label": case.get("final_label"),
        "final_confidence": case.get("final_confidence"),
        "final_reason": case.get("final_reason", ""),
        "parent_target_evidence_ids": target_ids,
        "parent_target_evidence": [
            {
                "chunk_id": item["chunk_id"],
                "doc_name": item.get("doc_name", ""),
                "content": item.get("content", ""),
                "annotation_note": item.get("note", ""),
            }
            for item in useful
        ],
        "pending": case["pending"],
        "claims": claims,
        "excluded_units": extracted["excluded_units"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-claim-chars", type=int, default=320)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = load_json(args.input)
    overrides = load_json(args.overrides)
    cases = [build_case(case, overrides, args.max_claim_chars) for case in source["cases"]]
    payload = {
        "schema": "coal_rag_gold_atomic_claims_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(args.input.resolve()),
        "scope_overrides": str(args.overrides.resolve()),
        "important_notes": [
            "v9 parent pending chunks remain unchanged.",
            "The 50 parent cases remain the gold evaluation units.",
            "Extracted claims are internal retrieval-query units, not new gold samples.",
            "Atomic claims are heuristic and require human confirmation.",
            "Strict recall includes only retrieval-suitable cases with existing confirmed target evidence.",
            "Claim-level target_evidence_ids remain empty until evidence is assigned and confirmed per claim.",
        ],
        "summary": {
            "case_count": len(cases),
            "retrieval_claim_count": sum(len(case["claims"]) for case in cases),
            "excluded_unit_count": sum(len(case["excluded_units"]) for case in cases),
            "zero_retrieval_claim_case_count": sum(not case["claims"] for case in cases),
            "strict_recall_case_count": sum(case["include_in_strict_recall"] for case in cases),
            "task_type_counts": dict(Counter(case["evaluation_task_type"] for case in cases)),
            "evidence_status_counts": dict(Counter(case["target_evidence_status"] for case in cases)),
            "claim_priority_counts": dict(Counter(
                claim["retrieval_priority"]
                for case in cases
                for claim in case["claims"]
            )),
        },
        "cases": cases,
    }
    write_json(args.output, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
