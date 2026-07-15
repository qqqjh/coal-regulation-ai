"""6.2 错别字 / 重复性智能体 RAGAS 数据集构建（无金标，groundedness 口径）。

从最新 v9 审查结果中抽取两个智能体的真实输出，构造可直接喂给
run_ragas_groundedness_v1.py 的 JSONL（user_input / retrieved_contexts / response）。

口径说明（为何这两个智能体能用 RAGAS 而无需人工金标）：
- 错别字：retrieved_contexts = 被检查的原文块；response = 智能体给出的纠错。
  Faithfulness 检验“纠错所依据的原词是否真的出现在原文”（抓幻觉/凭空报错），
  ResponseRelevancy 检验回答是否切合“查错别字”这一请求。
- 重复性：retrieved_contexts = 被判定相似的两段文本；response = 智能体给出的重复判定。
  同理用 Faithfulness/ResponseRelevancy 度量判定的有据性与相关性。
注意：纠错/判定属于推断性结论，Faithfulness 偏保守，应作为“有据性/无幻觉”代理而非准确率。

运行：python rag_eval/scripts/build_typo_redundancy_ragas_dataset_v1.py
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TYPO_OUT = PROJECT_ROOT / "rag_eval" / "data" / "typo_agent_ragas_dataset_v1.jsonl"
REDUNDANCY_OUT = PROJECT_ROOT / "rag_eval" / "data" / "redundancy_agent_ragas_dataset_v1.jsonl"
CTX_MAX = 1500


def latest_review_result() -> Path:
    files = sorted(
        glob.glob(str(PROJECT_ROOT / "review_results" / "review_result_v9_20*.json")),
        key=os.path.getmtime,
    )
    if not files:
        raise SystemExit("未找到 review_results/review_result_v9_20*.json")
    return Path(files[-1])


def truncate(text: str, limit: int = CTX_MAX) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + " …"


def build_typo_rows(data: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc, chunks in data.items():
        for r in chunks:
            tr = r.get("typo_result") or {}
            issues = tr.get("issues") or []
            if not (tr.get("has_issues") and issues):
                continue
            content = r.get("chunk_content", "")
            findings = []
            for it in issues:
                orig = it.get("original") or it.get("wrong_char") or ""
                sugg = it.get("suggestion") or it.get("correct_char") or ""
                reason = it.get("reason") or ""
                findings.append(f"将“{orig}”改为“{sugg}”：{reason}")
            response = "；".join(findings)
            rows.append({
                "case_id": f"typo::{doc}::chunk{r.get('chunk_index')}",
                "user_input": "请检查以下煤矿作业规程文本中的错别字和不规范用词，"
                              "指出错字以及应改正的字。\n\n文本：\n" + truncate(content),
                "retrieved_contexts": [truncate(content)],
                "response": response,
                "metadata": {"doc_name": doc, "chunk_index": r.get("chunk_index"),
                             "issue_count": len(issues), "agent": "typo"},
            })
            if 0 < limit <= len(rows):
                return rows
    return rows


def build_redundancy_rows(data: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for doc, chunks in data.items():
        for r in chunks:
            rr = r.get("repetition_result") or {}
            dups = rr.get("duplicates") or []
            if not (rr.get("has_duplicates") and dups):
                continue
            contexts: list[str] = []
            seen: set[str] = set()
            findings = []
            for d in dups:
                a = (d.get("chunk_a") or {}).get("content", "")
                b = (d.get("chunk_b") or {}).get("content", "")
                for c in (a, b):
                    key = c[:80]
                    if c and key not in seen:
                        seen.add(key)
                        contexts.append(truncate(c))
                rtype = d.get("redundancy_type", "重复")
                sim = d.get("similarity", "")
                desc = d.get("description") or d.get("note") or ""
                findings.append(f"{rtype}（相似度{sim}）：{desc}")
            if not contexts:
                continue
            rows.append({
                "case_id": f"redundancy::{doc}::chunk{r.get('chunk_index')}",
                "user_input": "请判断以下来自同一作业规程的文本片段之间是否存在"
                              "内容重复或高度相似，并说明重复之处。",
                "retrieved_contexts": contexts,
                "response": "；".join(findings),
                "metadata": {"doc_name": doc, "chunk_index": r.get("chunk_index"),
                             "duplicate_count": len(dups), "agent": "redundancy"},
            })
            if 0 < limit <= len(rows):
                return rows
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", type=Path, default=None, help="审查结果 JSON（默认最新）")
    parser.add_argument("--limit", type=int, default=0, help="每个数据集最多样本数，0=全部")
    parser.add_argument("--typo-out", type=Path, default=TYPO_OUT)
    parser.add_argument("--redundancy-out", type=Path, default=REDUNDANCY_OUT)
    args = parser.parse_args()

    review_path = args.review or latest_review_result()
    data = json.loads(Path(review_path).read_text(encoding="utf-8"))

    typo_rows = build_typo_rows(data, args.limit)
    redundancy_rows = build_redundancy_rows(data, args.limit)
    write_jsonl(args.typo_out, typo_rows)
    write_jsonl(args.redundancy_out, redundancy_rows)

    print(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "review_result": str(Path(review_path).resolve()),
        "typo_rows": len(typo_rows),
        "redundancy_rows": len(redundancy_rows),
        "typo_out": str(args.typo_out.resolve()),
        "redundancy_out": str(args.redundancy_out.resolve()),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
