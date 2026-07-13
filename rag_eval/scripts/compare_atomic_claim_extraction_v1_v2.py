"""Compare heuristic v1 and Codex-reviewed v2 atomic claim extraction."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
NEW = ROOT / "rag_eval" / "data" / "gold_atomic_claims_codex_v2.json"
OUTPUT = ROOT / "rag_eval" / "data" / "atomic_claim_extraction_v1_v2_comparison.json"
HTML = ROOT / "rag_eval" / "reports" / "atomic_claim_extraction_v1_v2_comparison.html"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def main() -> None:
    old = {case["case_id"]: case for case in load(OLD)["cases"]}
    new = {case["case_id"]: case for case in load(NEW)["cases"]}
    rows = []
    for case_id, before in old.items():
        after = new[case_id]
        rows.append(
            {
                "case_id": case_id,
                "pending": after["pending"],
                "old_task_type": before["evaluation_task_type"],
                "new_task_type": after["evaluation_task_type"],
                "old_claim_count": len(before["claims"]),
                "new_claim_count": len(after["claims"]),
                "delta": len(after["claims"]) - len(before["claims"]),
                "review_notes": after["review_notes"],
                "new_claims": after["claims"],
            }
        )
    payload = {
        "schema": "atomic_claim_extraction_v1_v2_comparison",
        "summary": {
            "case_count": len(rows),
            "old_claim_count": sum(row["old_claim_count"] for row in rows),
            "new_claim_count": sum(row["new_claim_count"] for row in rows),
            "old_zero_claim_cases": sum(row["old_claim_count"] == 0 for row in rows),
            "new_zero_claim_cases": sum(row["new_claim_count"] == 0 for row in rows),
            "changed_task_type_cases": sum(row["old_task_type"] != row["new_task_type"] for row in rows),
            "cases_with_claim_count_change": sum(row["delta"] != 0 for row in rows),
        },
        "cases": rows,
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cards = []
    for row in sorted(rows, key=lambda item: (item["delta"] == 0, -abs(item["delta"]))):
        claims = "".join(
            f"<li><b>{esc(item['claim_type'])}</b> {esc(item['claim_text'])}</li>"
            for item in row["new_claims"]
        )
        cards.append(
            f"""<section><header><div><h2>{esc(row['case_id'])}</h2><p>{esc(row['old_task_type'])} → {esc(row['new_task_type'])}</p></div>
            <b>{row['old_claim_count']} → {row['new_claim_count']} ({row['delta']:+d})</b></header>
            <p>{esc('；'.join(row['review_notes']))}</p><details><summary>父块原文</summary><pre>{esc(row['pending']['content'])}</pre></details>
            <details><summary>v2 主张</summary><ol>{claims}</ol></details></section>"""
        )
    summary = payload["summary"]
    HTML.write_text(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>原子主张v1-v2对比</title><style>
        body{{margin:0;background:#f3f5f4;color:#17241e;font-family:"Microsoft YaHei",sans-serif}}.top{{background:#17392c;color:#fff;padding:16px 3vw}}main{{width:min(1400px,95vw);margin:18px auto}}
        section{{background:#fff;border:1px solid #ccd5cf;padding:12px;margin-bottom:12px}}section header{{display:flex;justify-content:space-between}}h1,h2{{margin:0}}p{{color:#596960}}summary{{cursor:pointer;font-weight:700}}pre{{white-space:pre-wrap;line-height:1.6}}li{{margin:7px 0;line-height:1.55}}</style></head>
        <body><div class="top"><h1>规则提取 v1 与 Codex 提取 v2 对比</h1><p>主张 {summary['old_claim_count']} → {summary['new_claim_count']}；零主张块 {summary['old_zero_claim_cases']} → {summary['new_zero_claim_cases']}；任务类型修正 {summary['changed_task_type_cases']} 个。</p></div>
        <main>{''.join(cards)}</main></body></html>""",
        encoding="utf-8",
    )
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(OUTPUT.resolve())
    print(HTML.resolve())


if __name__ == "__main__":
    main()
