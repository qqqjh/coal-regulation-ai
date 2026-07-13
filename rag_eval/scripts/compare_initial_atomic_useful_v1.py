"""Visualize useful-evidence changes from initial retrieval to atomic-claim retrieval."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
INITIAL = PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
ATOMIC = PROJECT_ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "initial_vs_atomic_useful_comparison_v1.json"
OUTPUT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "initial_vs_atomic_useful_comparison_v1.html"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def compact(text: str, limit: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def useful_map(items: list[dict[str, Any]], rank_key: str) -> dict[str, dict[str, Any]]:
    return {
        item["chunk_id"]: {
            "chunk_id": item["chunk_id"],
            "rank": item[rank_key],
            "doc_name": item.get("doc_name", ""),
            "content": item.get("content", ""),
            "note": item.get("note", ""),
        }
        for item in items
        if item["label"] == "useful"
    }


def category(initial_count: int, atomic_count: int) -> str:
    if initial_count == 0 and atomic_count > 0:
        return "首次获得useful"
    if initial_count > 0 and atomic_count == 0:
        return "完全丢失useful"
    if atomic_count > initial_count:
        return "useful增加"
    if atomic_count < initial_count:
        return "useful减少"
    return "数量不变"


def build() -> dict[str, Any]:
    initial = load(INITIAL)
    atomic = load(ATOMIC)
    atomic_cases = {case["case_id"]: case for case in atomic["cases"]}
    rows = []
    for first in initial["cases"]:
        second = atomic_cases[first["case_id"]]
        first_items = first["candidate_annotations"]
        second_items = second["evidence_annotations"]
        first_useful = useful_map(first_items, "rank")
        second_useful = useful_map(second_items, "parent_rank")
        first_ids = set(first_useful)
        second_ids = set(second_useful)
        first_count = len(first_ids)
        second_count = len(second_ids)
        rows.append(
            {
                "case_id": first["case_id"],
                "pending": first["pending"],
                "final_label_initial": first.get("final_label", ""),
                "final_label_atomic": second.get("final_label", ""),
                "initial_candidate_count": len(first_items),
                "atomic_candidate_count": len(second_items),
                "initial_useful_count": first_count,
                "atomic_useful_count": second_count,
                "useful_delta": second_count - first_count,
                "initial_useful_density": first_count / len(first_items) if first_items else 0,
                "atomic_useful_density": second_count / len(second_items) if second_items else 0,
                "density_delta": (
                    second_count / len(second_items) if second_items else 0
                ) - (first_count / len(first_items) if first_items else 0),
                "initial_mean_useful_rank": mean(item["rank"] for item in first_useful.values())
                if first_useful
                else None,
                "atomic_mean_useful_rank": mean(item["rank"] for item in second_useful.values())
                if second_useful
                else None,
                "category": category(first_count, second_count),
                "shared_useful": [first_useful[item] for item in sorted(first_ids & second_ids)],
                "initial_only_useful": [first_useful[item] for item in sorted(first_ids - second_ids)],
                "atomic_only_useful": [second_useful[item] for item in sorted(second_ids - first_ids)],
            }
        )

    counts = Counter(row["category"] for row in rows)
    return {
        "schema": "initial_vs_atomic_useful_comparison_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "definition_zh": "按相同父待审chunk比较两版人工标为useful的父级法规证据。",
        "summary": {
            "case_count": len(rows),
            "initial_useful_count": sum(row["initial_useful_count"] for row in rows),
            "atomic_useful_count": sum(row["atomic_useful_count"] for row in rows),
            "initial_cases_with_useful": sum(row["initial_useful_count"] > 0 for row in rows),
            "atomic_cases_with_useful": sum(row["atomic_useful_count"] > 0 for row in rows),
            "category_counts": dict(counts),
            "atomic_total_candidate_count": sum(row["atomic_candidate_count"] for row in rows),
            "initial_total_candidate_count": sum(row["initial_candidate_count"] for row in rows),
        },
        "cases": rows,
    }


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def evidence_cards(items: list[dict[str, Any]], title: str, css_class: str) -> str:
    cards = "".join(
        f"""<article class="evidence {css_class}"><b>#{item['rank']} {esc(item['chunk_id'])}</b>
        <p>{esc(compact(item['content'], 260))}</p><details><summary>规则全文</summary>
        <pre>{esc(item['content'])}</pre></details></article>"""
        for item in items
    )
    body = cards or '<p class="empty">无</p>'
    return f"<section class='evidence-group'><h4>{esc(title)}（{len(items)}）</h4>{body}</section>"


def render(payload: dict[str, Any]) -> str:
    rows = sorted(payload["cases"], key=lambda row: (row["useful_delta"], row["density_delta"]))
    cards = []
    for row in rows:
        max_count = max(row["initial_useful_count"], row["atomic_useful_count"], 1)
        first_width = 100 * row["initial_useful_count"] / max_count
        atomic_width = 100 * row["atomic_useful_count"] / max_count
        cards.append(
            f"""<section class="case" data-category="{esc(row['category'])}">
            <header><div><h2>{esc(row['case_id'])}</h2><span class="category">{esc(row['category'])}</span></div>
            <div class="delta">useful变化 <b>{row['useful_delta']:+d}</b></div></header>
            <div class="metrics">
              <div><b>第一版</b><span>{row['initial_useful_count']} useful / {row['initial_candidate_count']} 候选</span>
                <i style="width:{first_width:.1f}%"></i><small>密度 {row['initial_useful_density']:.1%}</small></div>
              <div><b>原子版</b><span>{row['atomic_useful_count']} useful / {row['atomic_candidate_count']} 候选</span>
                <i class="atomic" style="width:{atomic_width:.1f}%"></i><small>密度 {row['atomic_useful_density']:.1%}</small></div>
            </div>
            <details><summary>待审 chunk 原文</summary><pre>{esc(row['pending'].get('content', ''))}</pre></details>
            <div class="evidence-grid">
            {evidence_cards(row['initial_only_useful'], '第一版独有 useful', 'lost')}
            {evidence_cards(row['atomic_only_useful'], '原子版新增 useful', 'gained')}
            {evidence_cards(row['shared_useful'], '两版共同 useful', 'shared')}
            </div></section>"""
        )
    summary = payload["summary"]
    filter_buttons = "".join(
        f"""<button onclick="filter('{esc(name)}')">{esc(name)} {count}</button>"""
        for name, count in summary["category_counts"].items()
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <title>第一版与原子主张版 useful 证据变化</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f3f5f4;color:#1d2923;font-family:"Microsoft YaHei",sans-serif}}
    .top{{position:sticky;top:0;z-index:5;background:#19352a;color:#fff;padding:16px 4vw}}.top h1{{margin:0 0 6px}}
    .stats{{display:flex;gap:18px;flex-wrap:wrap}}button{{padding:6px 10px;margin:9px 5px 0 0;border:1px solid #bbc5bf;background:#fff;cursor:pointer}}
    main{{width:min(1500px,95vw);margin:20px auto}}.case{{background:#fff;border:1px solid #ccd5cf;margin-bottom:18px;padding:15px}}
    .case header{{display:flex;justify-content:space-between;gap:12px}}h2,h4{{margin:0}}.category{{color:#65736b}}.delta b{{font-size:22px}}
    .metrics{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:13px 0}}.metrics div{{display:grid;grid-template-columns:80px 1fr;gap:4px}}
    .metrics i{{grid-column:1/3;height:9px;background:#a55f4c;display:block;min-width:2px}}.metrics i.atomic{{background:#187050}}.metrics small{{grid-column:1/3;color:#65736b}}
    details{{border:1px solid #d8dfda;margin-top:8px}}summary{{cursor:pointer;padding:7px;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;padding:9px;margin:0}}
    .evidence-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:12px}}.evidence-group{{border:1px solid #d8dfda;padding:9px;min-width:0}}
    .evidence{{padding:8px;margin-top:8px;border-left:4px solid #88938c;background:#fafbfa;font-size:13px}}.evidence.lost{{border-color:#b55045}}.evidence.gained{{border-color:#187050}}
    .evidence p{{line-height:1.55}}.empty{{color:#77837c}}@media(max-width:950px){{.metrics,.evidence-grid{{grid-template-columns:1fr}}}}
    </style></head><body><div class="top"><h1>第一版与原子主张版 useful 证据变化</h1>
    <div class="stats"><span>第一版：{summary['initial_useful_count']} useful / {summary['initial_cases_with_useful']} 个chunk</span>
    <span>原子版：{summary['atomic_useful_count']} useful / {summary['atomic_cases_with_useful']} 个chunk</span>
    <span>原子父级候选：{summary['atomic_total_candidate_count']}</span></div>
    <div><button onclick="filter('all')">全部</button>{filter_buttons}</div></div>
    <main>{''.join(cards)}</main><script>function filter(v){{document.querySelectorAll('.case').forEach(x=>x.style.display=v==='all'||x.dataset.category===v?'':'none')}}</script>
    </body></html>"""


def main() -> None:
    payload = build()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_HTML.write_text(render(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(OUTPUT_JSON.resolve())
    print(OUTPUT_HTML.resolve())


if __name__ == "__main__":
    main()
