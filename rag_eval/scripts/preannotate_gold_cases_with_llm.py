"""
Two-pass LLM pre-annotation for v9 gold RAG evaluation cases.

Pass 1 labels the pending chunk and all 15 regulation candidates.
Pass 2 independently reviews the draft and returns the corrected final labels.
Results are saved incrementally so interrupted runs can resume.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = PROJECT_ROOT / "rag_eval" / "data" / "gold_annotation_cases_v9.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_llm_v9.json"
DEFAULT_MODEL = "qwen-plus"

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")


SYSTEM_PROMPT = """你是煤矿作业规程检索评测标注专家。你的任务是为 RAG 黄金评测集做谨慎、可复核的预标注。

你必须区分两个任务：
1. 判断待审 chunk 本身的最终标签。
2. 判断每个法规候选是否能够支撑对该待审 chunk 的审查。

最终标签：
- compliant：待审内容符合适用法规，或属于合理的正向要求。
- non_compliant：待审内容存在明确违反适用法规的问题。
- uncertain：证据不足、适用范围不清、待审 chunk 同时包含多项内容且无法给出单一结论。
- not_suitable：目录、纯表格噪声、OCR 严重错误、事故案例/红线反面案例等不适合作为普通合规结论样本。

法规候选标签：
- useful：候选法规直接适用，并可单独或与其他 useful 法规共同支撑最终判断。
- uncertain：候选有关联，但适用范围、对象、条件或证据完整性不清；不能直接支撑最终结论。
- useless：仅关键词相似、主题邻近、对象不同、适用条件不同，不能支撑该样本判断。

标注原则：
- 不得因为候选排名靠前、相似度高或历史对比说“不合规”就直接判定 useful/non_compliant。
- 必须检查法规适用对象、矿井类型、工序、设备、数值方向、上限下限和例外条件。
- 红线违规清单、事故案例、反面案例描述的是禁止场景，不应当直接当作制度允许该行为。
- OCR 错误、单位错乱应与真实法规违规区分。
- 如果 15 条候选中没有足以支撑判断的法规，missing_correct_evidence=true。
- 每个候选必须返回标签和简短、具体的理由，不允许空理由。
- 输出严格 JSON，不要 Markdown。"""


REVIEW_SYSTEM_PROMPT = """你是煤矿作业规程 RAG 黄金标注复核专家。请独立检查初审标注，纠正误判。

重点检查：
- 是否把关键词相似但对象不同的法规误标 useful；
- 是否遗漏真正直接相关的法规；
- 是否错误理解阈值方向、上限/下限和更严格要求；
- 是否将事故案例、红线反面案例判成普通不合规条款；
- 是否受历史 topic/source_note 诱导；
- missing_correct_evidence 是否正确。

输出与初审完全相同结构的严格 JSON，不要 Markdown。"""


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def strip_fence(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def call_json(client: OpenAI, model: str, system: str, user: str, retries: int = 3) -> Dict[str, Any]:
    error = ""
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0,
            )
            return json.loads(strip_fence(response.choices[0].message.content or ""))
        except Exception as exc:
            error = str(exc)
            if attempt < retries - 1:
                time.sleep(2)
    raise RuntimeError(error)


def case_prompt(case: Dict[str, Any]) -> str:
    candidates = []
    for candidate in case["candidates"]:
        candidates.append({
            "rank": candidate["rank"],
            "chunk_id": candidate["chunk_id"],
            "doc_name": candidate["doc_name"],
            "chapter": candidate.get("chapter", ""),
            "section": candidate.get("section", ""),
            "article": candidate.get("article", ""),
            "content": candidate["content"],
        })
    payload = {
        "case_id": case["case_id"],
        "historical_topic_hint_not_ground_truth": case.get("topic", ""),
        "historical_source_note_not_ground_truth": case.get("source_note", ""),
        "pending": case["pending"],
        "regulation_candidates": candidates,
    }
    return f"""请标注以下样本：

{json.dumps(payload, ensure_ascii=False)}

输出格式：
{{
  "case_id": "{case['case_id']}",
  "final_label": "compliant|non_compliant|uncertain|not_suitable",
  "final_confidence": "high|medium|low",
  "final_reason": "具体判断依据",
  "missing_correct_evidence": true或false,
  "not_eval_suitable": true或false,
  "review_flags": ["需要人工重点复核的风险点"],
  "candidate_annotations": [
    {{
      "chunk_id": "必须与输入完全一致",
      "label": "useful|useless|uncertain",
      "confidence": "high|medium|low",
      "note": "说明适用或不适用的具体原因"
    }}
  ]
}}

candidate_annotations 必须覆盖全部 {len(candidates)} 个候选，且不得新增候选。"""


def review_prompt(case: Dict[str, Any], draft: Dict[str, Any]) -> str:
    return f"""请复核该样本的初审标注。

【原始样本】
{case_prompt(case)}

【初审标注】
{json.dumps(draft, ensure_ascii=False)}

请输出纠正后的最终 JSON。"""


def normalize_result(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    valid_final = {"compliant", "non_compliant", "uncertain", "not_suitable"}
    valid_candidate = {"useful", "useless", "uncertain"}
    candidate_map = {
        item.get("chunk_id"): item
        for item in result.get("candidate_annotations", [])
        if isinstance(item, dict)
    }
    annotations = []
    for candidate in case["candidates"]:
        item = candidate_map.get(candidate["chunk_id"], {})
        label = item.get("label", "uncertain")
        if label not in valid_candidate:
            label = "uncertain"
        annotations.append({
            "chunk_id": candidate["chunk_id"],
            "doc_name": candidate["doc_name"],
            "kb_chunk_index": candidate["kb_chunk_index"],
            "rank": candidate["rank"],
            "score": candidate["score"],
            "label": label,
            "confidence": item.get("confidence", "low"),
            "note": item.get("note", "模型未给出完整理由，需人工复核"),
            "anchor_quote": candidate.get("anchor_quote", ""),
        })
    final_label = result.get("final_label", "uncertain")
    if final_label not in valid_final:
        final_label = "uncertain"
    return {
        "case_id": case["case_id"],
        "sample_source": case["sample_source"],
        "topic": case.get("topic", ""),
        "pending": case["pending"],
        "final_label": final_label,
        "final_confidence": result.get("final_confidence", "low"),
        "final_reason": result.get("final_reason", ""),
        "missing_correct_evidence": bool(result.get("missing_correct_evidence", False)),
        "not_eval_suitable": bool(result.get("not_eval_suitable", final_label == "not_suitable")),
        "review_flags": result.get("review_flags", []),
        "human_note": "LLM 双遍预标注，需人工最终确认。",
        "candidate_annotations": annotations,
    }


def save_incremental(
    path: Path,
    source: str,
    completed: Dict[str, Any],
    drafts: Dict[str, Any],
    failures: Dict[str, Any],
) -> None:
    payload = {
        "schema": "coal_rag_llm_preannotations_v9_v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_cases": source,
        "case_count": len(completed),
        "draft_count": len(drafts),
        "failure_count": len(failures),
        "cases": list(completed.values()),
        "drafts": drafts,
        "failures": failures,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp_path.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Two-pass LLM pre-annotation for v9 gold cases.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--single-pass", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY is required")

    source = load_json(args.cases)
    cases: List[Dict[str, Any]] = source["cases"]
    if args.limit > 0:
        cases = cases[:args.limit]

    completed: Dict[str, Any] = {}
    drafts: Dict[str, Any] = {}
    failures: Dict[str, Any] = {}
    if args.output.exists():
        existing = load_json(args.output)
        completed = {case["case_id"]: case for case in existing.get("cases", [])}
        drafts = existing.get("drafts", {})
        failures = existing.get("failures", {})
        print(
            f"Loaded {len(completed)} completed cases, "
            f"{len(drafts)} saved drafts, {len(failures)} recorded failures"
        )

    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    for index, case in enumerate(cases, start=1):
        case_id = case["case_id"]
        if case_id in completed:
            print(f"[{index}/{len(cases)}] skip {case_id}")
            continue

        try:
            if case_id in drafts:
                print(f"[{index}/{len(cases)}] resume review {case_id}")
                draft = drafts[case_id]
            else:
                print(f"[{index}/{len(cases)}] annotate {case_id}")
                draft = call_json(client, args.model, SYSTEM_PROMPT, case_prompt(case))
                drafts[case_id] = draft
                failures.pop(case_id, None)
                save_incremental(
                    args.output, str(args.cases), completed, drafts, failures
                )
                print(f"[{index}/{len(cases)}] draft checkpoint saved {case_id}")

            if args.single_pass:
                final = draft
            else:
                final = call_json(
                    client, args.model, REVIEW_SYSTEM_PROMPT, review_prompt(case, draft)
                )

            completed[case_id] = normalize_result(case, final)
            drafts.pop(case_id, None)
            failures.pop(case_id, None)
            save_incremental(args.output, str(args.cases), completed, drafts, failures)
            print(f"[{index}/{len(cases)}] completed checkpoint saved {case_id}")
        except Exception as exc:
            failures[case_id] = {
                "stage": "review" if case_id in drafts else "draft",
                "error": str(exc),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            save_incremental(args.output, str(args.cases), completed, drafts, failures)
            print(f"[{index}/{len(cases)}] failure checkpoint saved {case_id}: {exc}")
            raise

    print(f"Preannotations: {args.output}")


if __name__ == "__main__":
    main()
