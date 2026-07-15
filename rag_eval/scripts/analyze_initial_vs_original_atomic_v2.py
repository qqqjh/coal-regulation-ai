"""Build an inspectable comparison of the initial parent query and original atomic-claim query."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INITIAL_PATH = ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
CLAIMS_PATH = ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
ATOMIC_PATH = ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
OUTPUT_JSON = ROOT / "rag_eval" / "data" / "initial_vs_original_atomic_analysis_v2.json"
OUTPUT_HTML = ROOT / "rag_eval" / "reports" / "initial_vs_original_atomic_analysis_v2.html"

LABEL_ORDER = ("useful", "uncertain", "useless", "unlabeled")
LABEL_ZH = {
    "useful": "有效",
    "uncertain": "待确认",
    "useless": "无效",
    "unlabeled": "未标注",
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def compact(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def count_labels(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(str(item.get("label", "unlabeled")) for item in items)
    return {label: counts.get(label, 0) for label in LABEL_ORDER}


def candidate_id(item: dict[str, Any]) -> str:
    return str(item.get("chunk_id", ""))


def candidate_rank(item: dict[str, Any], atomic: bool) -> int:
    return int(item.get("parent_rank" if atomic else "rank", 0))


def classify(claim_count: int, initial_useful: int, atomic_useful: int, overlap: int) -> str:
    if claim_count == 0:
        return "零原子主张"
    if initial_useful > 0 and atomic_useful == 0:
        return "有效证据完全丢失"
    if atomic_useful > initial_useful:
        return "原子版有效证据增加"
    if atomic_useful < initial_useful:
        return "原子版有效证据减少"
    if initial_useful == 0 and atomic_useful == 0:
        return "两版均无有效证据"
    if overlap == 0:
        return "有效数相同但候选完全变化"
    return "有效证据数量不变"


def ranks_for_label(items: list[dict[str, Any]], atomic: bool, label: str) -> list[int]:
    return [
        candidate_rank(item, atomic)
        for item in items
        if item.get("label") == label
    ]


def build() -> dict[str, Any]:
    initial = load(INITIAL_PATH)
    claims_payload = load(CLAIMS_PATH)
    atomic = load(ATOMIC_PATH)
    claim_cases = {case["case_id"]: case for case in claims_payload["cases"]}
    atomic_cases = {case["case_id"]: case for case in atomic["cases"]}
    rows: list[dict[str, Any]] = []

    for initial_case in initial["cases"]:
        case_id = initial_case["case_id"]
        claim_case = claim_cases[case_id]
        atomic_case = atomic_cases[case_id]
        initial_items = initial_case.get("candidate_annotations", [])
        atomic_items = atomic_case.get("evidence_annotations", [])
        claims = claim_case.get("claims", [])
        excluded_units = claim_case.get("excluded_units", [])
        initial_ids = {candidate_id(item) for item in initial_items}
        atomic_ids = {candidate_id(item) for item in atomic_items}
        shared_ids = initial_ids & atomic_ids
        initial_useful_ids = {
            candidate_id(item) for item in initial_items if item.get("label") == "useful"
        }
        atomic_useful_ids = {
            candidate_id(item) for item in atomic_items if item.get("label") == "useful"
        }
        union_ids = initial_ids | atomic_ids
        initial_useful_ranks = ranks_for_label(initial_items, False, "useful")
        atomic_useful_ranks = ranks_for_label(atomic_items, True, "useful")
        rows.append(
            {
                "case_id": case_id,
                "topic": initial_case.get("topic", ""),
                "pending": initial_case["pending"],
                "evaluation_task_type": claim_case.get("evaluation_task_type", ""),
                "target_evidence_status": claim_case.get("target_evidence_status", ""),
                "claim_count": len(claims),
                "excluded_unit_count": len(excluded_units),
                "claims": claims,
                "excluded_units": excluded_units,
                "initial_candidates": initial_items,
                "atomic_candidates": atomic_items,
                "initial_candidate_count": len(initial_items),
                "atomic_candidate_count": len(atomic_items),
                "initial_label_counts": count_labels(initial_items),
                "atomic_label_counts": count_labels(atomic_items),
                "shared_candidate_count": len(shared_ids),
                "candidate_union_count": len(union_ids),
                "candidate_jaccard": len(shared_ids) / len(union_ids) if union_ids else 0,
                "shared_candidate_ids": sorted(shared_ids),
                "initial_only_ids": sorted(initial_ids - atomic_ids),
                "atomic_only_ids": sorted(atomic_ids - initial_ids),
                "initial_useful_ids": sorted(initial_useful_ids),
                "atomic_useful_ids": sorted(atomic_useful_ids),
                "shared_useful_ids": sorted(initial_useful_ids & atomic_useful_ids),
                "initial_only_useful_ids": sorted(initial_useful_ids - atomic_useful_ids),
                "atomic_only_useful_ids": sorted(atomic_useful_ids - initial_useful_ids),
                "initial_useful_mean_rank": mean(initial_useful_ranks)
                if initial_useful_ranks
                else None,
                "atomic_useful_mean_rank": mean(atomic_useful_ranks)
                if atomic_useful_ranks
                else None,
                "initial_unique_docs": len({item.get("doc_name", "") for item in initial_items}),
                "atomic_unique_docs": len({item.get("doc_name", "") for item in atomic_items}),
                "missing_correct_evidence_initial": initial_case.get(
                    "missing_correct_evidence", False
                ),
                "missing_correct_evidence_atomic": atomic_case.get(
                    "missing_correct_evidence", False
                ),
                "category": classify(
                    len(claims),
                    len(initial_useful_ids),
                    len(atomic_useful_ids),
                    len(shared_ids),
                ),
            }
        )

    category_counts = Counter(row["category"] for row in rows)
    initial_useful_ranks = [
        rank
        for row in rows
        for rank in ranks_for_label(row["initial_candidates"], False, "useful")
    ]
    atomic_useful_ranks = [
        rank
        for row in rows
        for rank in ranks_for_label(row["atomic_candidates"], True, "useful")
    ]

    def topk_count(ranks: list[int], k: int) -> int:
        return sum(rank <= k for rank in ranks)

    summary = {
        "case_count": len(rows),
        "atomic_claim_count": sum(row["claim_count"] for row in rows),
        "zero_claim_case_count": sum(row["claim_count"] == 0 for row in rows),
        "initial_candidate_count": sum(row["initial_candidate_count"] for row in rows),
        "atomic_candidate_count": sum(row["atomic_candidate_count"] for row in rows),
        "initial_useful_count": sum(row["initial_label_counts"]["useful"] for row in rows),
        "atomic_useful_count": sum(row["atomic_label_counts"]["useful"] for row in rows),
        "initial_cases_with_useful": sum(
            row["initial_label_counts"]["useful"] > 0 for row in rows
        ),
        "atomic_cases_with_useful": sum(
            row["atomic_label_counts"]["useful"] > 0 for row in rows
        ),
        "shared_candidate_count": sum(row["shared_candidate_count"] for row in rows),
        "mean_candidate_jaccard": mean(row["candidate_jaccard"] for row in rows),
        "mean_initial_candidates": mean(row["initial_candidate_count"] for row in rows),
        "mean_atomic_candidates": mean(row["atomic_candidate_count"] for row in rows),
        "mean_initial_unique_docs": mean(row["initial_unique_docs"] for row in rows),
        "mean_atomic_unique_docs": mean(row["atomic_unique_docs"] for row in rows),
        "candidate_reduction_rate": 1
        - sum(row["atomic_candidate_count"] for row in rows)
        / sum(row["initial_candidate_count"] for row in rows),
        "initial_useful_density": sum(
            row["initial_label_counts"]["useful"] for row in rows
        )
        / sum(row["initial_candidate_count"] for row in rows),
        "atomic_useful_density": sum(
            row["atomic_label_counts"]["useful"] for row in rows
        )
        / sum(row["atomic_candidate_count"] for row in rows),
        "zero_claim_initial_useful_count": sum(
            row["initial_label_counts"]["useful"] for row in rows if row["claim_count"] == 0
        ),
        "zero_claim_initial_cases_with_useful": sum(
            row["initial_label_counts"]["useful"] > 0
            for row in rows
            if row["claim_count"] == 0
        ),
        "initial_useful_top5": topk_count(initial_useful_ranks, 5),
        "initial_useful_top10": topk_count(initial_useful_ranks, 10),
        "initial_useful_top15": topk_count(initial_useful_ranks, 15),
        "atomic_useful_top5": topk_count(atomic_useful_ranks, 5),
        "atomic_useful_top10": topk_count(atomic_useful_ranks, 10),
        "atomic_useful_top15": topk_count(atomic_useful_ranks, 15),
        "category_counts": dict(category_counts),
    }
    return {
        "schema": "initial_vs_original_atomic_analysis_v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "inputs": {
            "initial": str(INITIAL_PATH.relative_to(ROOT)),
            "atomic_claims": str(CLAIMS_PATH.relative_to(ROOT)),
            "atomic_parent_annotations": str(ATOMIC_PATH.relative_to(ROOT)),
        },
        "summary": summary,
        "cases": rows,
    }


def label_chips(counts: dict[str, int]) -> str:
    return "".join(
        f'<span class="chip {label}">{LABEL_ZH[label]} {counts[label]}</span>'
        for label in LABEL_ORDER
        if counts[label]
    )


def candidate_card(
    item: dict[str, Any],
    *,
    atomic: bool,
    shared_ids: set[str],
    other_useful_ids: set[str],
) -> str:
    chunk_id = candidate_id(item)
    rank = candidate_rank(item, atomic)
    label = str(item.get("label", "unlabeled"))
    relation = "共同候选" if chunk_id in shared_ids else "本版独有"
    cross = ""
    if item.get("label") == "useful":
        cross = " · 两版共同有效" if chunk_id in other_useful_ids else " · 本版独有有效"
    claims = item.get("claim_hits", []) if atomic else []
    claim_html = ""
    if claims:
        rows = "".join(
            f"""<li><b>{esc(hit.get('claim_id', ''))}</b> · 主张排名 {esc(hit.get('claim_rank', ''))}
            · rerank {float(hit.get('rerank_score', 0)):.4f}<br>{esc(compact(hit.get('claim_text', ''), 260))}</li>"""
            for hit in claims
        )
        claim_html = f"<details><summary>命中的原子主张（{len(claims)}）</summary><ul>{rows}</ul></details>"
    source_line = ""
    if not atomic:
        source_line = (
            f"<small>来源：{esc(item.get('source', ''))} · score={esc(item.get('score', ''))}</small>"
        )
    return f"""<article class="candidate {label}" data-label="{label}">
      <header><b>#{rank} {esc(item.get('doc_name', ''))} :: {esc(chunk_id)}</b>
      <span>{relation}{cross}</span></header>
      {source_line}
      <p class="note">{esc(item.get('note', ''))}</p>
      <p>{esc(compact(item.get('content', ''), 360))}</p>
      {claim_html}
      <details><summary>规则全文</summary><pre>{esc(item.get('content', ''))}</pre></details>
    </article>"""


def case_card(row: dict[str, Any]) -> str:
    shared_ids = set(row["shared_candidate_ids"])
    initial_useful = set(row["initial_useful_ids"])
    atomic_useful = set(row["atomic_useful_ids"])
    claims = "".join(
        f"""<li><b>{esc(claim.get('claim_id', ''))}</b>
        <span class="priority">{esc(claim.get('retrieval_priority', ''))}</span>
        <p>{esc(claim.get('claim_text', ''))}</p>
        <details><summary>检索 query</summary><pre>{esc(claim.get('retrieval_query', ''))}</pre></details></li>"""
        for claim in row["claims"]
    )
    claims = claims or '<p class="empty">该待审 chunk 没有进入检索的原子主张。</p>'
    initial_cards = "".join(
        candidate_card(
            item,
            atomic=False,
            shared_ids=shared_ids,
            other_useful_ids=atomic_useful,
        )
        for item in row["initial_candidates"]
    )
    atomic_cards = "".join(
        candidate_card(
            item,
            atomic=True,
            shared_ids=shared_ids,
            other_useful_ids=initial_useful,
        )
        for item in row["atomic_candidates"]
    )
    atomic_cards = atomic_cards or '<p class="empty">原子主张版无父级候选。</p>'
    jaccard = row["candidate_jaccard"]
    return f"""<section class="case" data-category="{esc(row['category'])}"
      data-search="{esc(row['case_id'] + ' ' + row['pending'].get('doc_name', '') + ' ' + row['pending'].get('content', ''))}">
      <header class="case-head"><div><h2>{esc(row['case_id'])}</h2>
      <span class="category">{esc(row['category'])}</span></div>
      <div class="case-summary">主张 <b>{row['claim_count']}</b> · 候选重合 <b>{row['shared_candidate_count']}</b>
      · Jaccard <b>{jaccard:.1%}</b></div></header>
      <div class="meta">{esc(row['pending'].get('doc_name', ''))} ·
      {esc(row['pending'].get('chapter', ''))} / {esc(row['pending'].get('section', ''))}</div>
      <div class="metrics">
        <div><h3>原始整块检索</h3><b>{row['initial_candidate_count']} 候选</b>
        {label_chips(row['initial_label_counts'])}<small>规则文档 {row['initial_unique_docs']} 个</small></div>
        <div><h3>原始原子主张检索</h3><b>{row['atomic_candidate_count']} 候选</b>
        {label_chips(row['atomic_label_counts'])}<small>规则文档 {row['atomic_unique_docs']} 个</small></div>
      </div>
      <details class="pending"><summary>待审 chunk 全文</summary><pre>{esc(row['pending'].get('content', ''))}</pre></details>
      <details class="claims"><summary>原始原子主张（{row['claim_count']}）与排除单元（{row['excluded_unit_count']}）</summary>
      <ol>{claims}</ol></details>
      <div class="columns">
        <section><h3>原始整块检索候选</h3>{initial_cards}</section>
        <section><h3>原始原子主张检索候选</h3>{atomic_cards}</section>
      </div>
    </section>"""


def render(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    cases = sorted(
        payload["cases"],
        key=lambda row: (
            row["claim_count"] != 0,
            row["atomic_label_counts"]["useful"] - row["initial_label_counts"]["useful"],
            row["candidate_jaccard"],
        ),
    )
    category_buttons = "".join(
        f'<button onclick="setCategory(this, \'{esc(category)}\')">{esc(category)} {count}</button>'
        for category, count in sorted(summary["category_counts"].items())
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <title>原始整块检索与原始原子主张检索分析</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f4f6f5;color:#17241e;font-family:"Microsoft YaHei",Arial,sans-serif}}
    .top{{position:sticky;top:0;z-index:10;background:#16382b;color:#fff;padding:15px 3vw;border-bottom:4px solid #d69a2d}}
    h1{{margin:0 0 8px;font-size:25px}}.top p{{margin:4px 0;color:#d7e5de}}.toolbar{{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}}
    button,input{{border:1px solid #b8c5be;background:#fff;padding:7px 10px;font:inherit}}button{{cursor:pointer}}button.active{{background:#d69a2d;border-color:#d69a2d}}
    input{{min-width:280px}}main{{width:min(1800px,96vw);margin:18px auto}}.overview{{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:8px;margin-bottom:15px}}
    .stat{{background:#fff;border:1px solid #ccd6d0;padding:11px}}.stat b{{display:block;font-size:23px;color:#176447}}.stat span{{color:#64736b;font-size:13px}}
    .analysis{{background:#fff;border-left:5px solid #d69a2d;padding:12px;margin-bottom:16px;line-height:1.7}}.analysis ul{{margin:5px 0}}
    .case{{background:#fff;border:1px solid #cbd5cf;margin-bottom:20px;padding:14px}}.case-head{{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #e1e7e3;padding-bottom:9px}}
    h2,h3{{margin:0}}h2{{font-size:21px}}h3{{font-size:16px}}.category{{display:inline-block;margin-top:5px;color:#805916}}.case-summary{{color:#52635a}}
    .meta{{color:#68786f;margin:8px 0}}.metrics{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}}.metrics>div{{border:1px solid #d8e0db;padding:9px}}
    .metrics h3{{display:inline;margin-right:12px}}.metrics small{{display:block;margin-top:6px;color:#68786f}}.chip{{display:inline-block;padding:3px 6px;margin-left:6px;font-size:12px;border:1px solid}}
    .chip.useful{{color:#12633f;background:#e4f4eb}}.chip.uncertain{{color:#805916;background:#fff5d9}}.chip.useless{{color:#8a3c35;background:#f8e8e6}}.chip.unlabeled{{color:#5b6670;background:#edf0f2}}
    details{{border:1px solid #d8e0db;margin-top:8px}}summary{{cursor:pointer;padding:7px;font-weight:700}}pre{{white-space:pre-wrap;word-break:break-word;margin:0;padding:10px;line-height:1.6}}
    .claims ol{{margin:0;padding:8px 8px 8px 32px}}.claims li{{padding:6px;border-bottom:1px solid #edf0ee}}.claims p{{margin:5px 0}}.priority{{color:#805916;margin-left:6px}}
    .columns{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:13px}}.columns>section{{min-width:0}}.columns>section>h3{{position:sticky;top:136px;background:#e9efeb;padding:8px;z-index:2}}
    .candidate{{border:1px solid #d8e0db;border-left:5px solid #9da8a2;padding:9px;margin-top:8px;min-width:0}}.candidate.useful{{border-left-color:#168252;background:#f7fcf9}}
    .candidate.uncertain{{border-left-color:#d69a2d;background:#fffdf6}}.candidate.useless{{border-left-color:#b45c52}}.candidate header{{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}}
    .candidate header span,.candidate small{{color:#68786f;font-size:12px}}.candidate p{{line-height:1.55;margin:6px 0}}.candidate .note{{color:#495a51}}.candidate ul{{margin:0;padding:6px 8px 8px 28px}}
    .empty{{color:#79877f;padding:10px}}@media(max-width:1100px){{.overview{{grid-template-columns:repeat(2,1fr)}}.columns,.metrics{{grid-template-columns:1fr}}.columns>section>h3{{position:static}}}}
    </style></head><body>
    <div class="top"><h1>原始整块检索 vs 原始原子主张检索</h1>
    <p>比较第一版父 chunk 直接检索与旧原子主张拆分检索。报告只使用两版既有人工标注，不修改黄金集。</p>
    <div class="toolbar"><button class="active" onclick="setCategory(this,'all')">全部 50</button>{category_buttons}
    <button onclick="setCandidateLabel(this,'all')">显示全部候选</button>
    <button onclick="setCandidateLabel(this,'useful')">仅看有效候选</button>
    <button onclick="setCandidateLabel(this,'uncertain')">仅看待确认</button>
    <input id="search" placeholder="搜索 case、文档或待审内容" oninput="applyFilters()"></div></div>
    <main><section class="overview">
      <div class="stat"><b>{summary['atomic_claim_count']}</b><span>原始原子主张</span></div>
      <div class="stat"><b>{summary['zero_claim_case_count']}</b><span>零主张待审块</span></div>
      <div class="stat"><b>{summary['initial_candidate_count']} / {summary['atomic_candidate_count']}</b><span>整块版 / 原子版候选</span></div>
      <div class="stat"><b>{summary['initial_useful_count']} / {summary['atomic_useful_count']}</b><span>整块版 / 原子版有效证据</span></div>
      <div class="stat"><b>{summary['initial_cases_with_useful']} / {summary['atomic_cases_with_useful']}</b><span>存在有效证据的待审块</span></div>
      <div class="stat"><b>{summary['mean_candidate_jaccard']:.1%}</b><span>平均候选 Jaccard</span></div>
    </section>
    <section class="analysis"><b>整体观察：</b><ul>
      <li>原子版平均每块 {summary['mean_atomic_candidates']:.2f} 个候选，整块版固定平均 {summary['mean_initial_candidates']:.2f} 个候选。</li>
      <li>原子版候选总量减少 {summary['candidate_reduction_rate']:.1%}；有效证据密度由整块版 {summary['initial_useful_density']:.1%} 提高到原子版 {summary['atomic_useful_density']:.1%}。</li>
      <li>原子版有效证据共 {summary['atomic_useful_count']} 条，覆盖 {summary['atomic_cases_with_useful']} 个待审块；整块版有效证据共 {summary['initial_useful_count']} 条，覆盖 {summary['initial_cases_with_useful']} 个待审块。</li>
      <li>{summary['zero_claim_case_count']} 个零主张待审块中，整块版原本有 {summary['zero_claim_initial_cases_with_useful']} 个块、{summary['zero_claim_initial_useful_count']} 条有效证据；纯原子主张检索无法保留这些结果。</li>
      <li>有效证据进入 Top5：整块版 {summary['initial_useful_top5']} 条，原子版 {summary['atomic_useful_top5']} 条；进入 Top10：整块版 {summary['initial_useful_top10']} 条，原子版 {summary['atomic_useful_top10']} 条。</li>
      <li>平均候选重合率仅 {summary['mean_candidate_jaccard']:.1%}，说明原子主张不是简单重排，而是在明显改变候选池。</li>
    </ul></section>
    <div id="cases">{''.join(case_card(row) for row in cases)}</div></main>
    <script>
    let currentCategory='all', currentLabel='all';
    function setCategory(btn,value){{currentCategory=value;document.querySelectorAll('.toolbar button').forEach(x=>{{if(x.textContent.includes('显示')||x.textContent.includes('仅看'))return;x.classList.remove('active')}});btn.classList.add('active');applyFilters()}}
    function setCandidateLabel(btn,value){{currentLabel=value;document.querySelectorAll('.candidate').forEach(x=>x.style.display=value==='all'||x.dataset.label===value?'':'none')}}
    function applyFilters(){{const q=document.getElementById('search').value.toLowerCase();document.querySelectorAll('.case').forEach(x=>{{const okCat=currentCategory==='all'||x.dataset.category===currentCategory;const okSearch=!q||x.dataset.search.toLowerCase().includes(q);x.style.display=okCat&&okSearch?'':'none'}});setCandidateLabel(document.body,currentLabel)}}
    </script></body></html>"""


def main() -> None:
    payload = build()
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUTPUT_HTML.write_text(render(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(OUTPUT_JSON.resolve())
    print(OUTPUT_HTML.resolve())


if __name__ == "__main__":
    main()
