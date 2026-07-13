"""Build a three-version diagnostic report for initial, atomic, and BGE atomic retrieval."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
INITIAL = ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
ATOMIC = ROOT / "rag_eval" / "data" / "atomic_parent_gold_annotations_v1.json"
BGE_PARENT = ROOT / "rag_eval" / "data" / "parent_bgem3_annotations_grouped_v1.json"
BGE_ATOMIC = ROOT / "rag_eval" / "data" / "parent_chunk_evidence_local_bgem3_grouped_v1.json"
GROUPS = ROOT / "rag_eval" / "data" / "rule_evidence_groups_v1.json"
OUTPUT = ROOT / "rag_eval" / "data" / "atomic_bgem3_diagnostic_v1.json"
HTML = ROOT / "rag_eval" / "reports" / "atomic_bgem3_diagnostic_v1.html"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def compact(text: str, limit: int = 260) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "..."


def build() -> dict[str, Any]:
    initial = {case["case_id"]: case for case in load(INITIAL)["cases"]}
    atomic = {case["case_id"]: case for case in load(ATOMIC)["cases"]}
    parent = {case["case_id"]: case for case in load(BGE_PARENT)["cases"]}
    new = {case["case_id"]: case for case in load(BGE_ATOMIC)["cases"]}
    chunk_to_group = load(GROUPS)["chunk_to_group"]

    def key(item: dict[str, Any]) -> str:
        return item.get("evidence_group_id") or chunk_to_group.get(item["chunk_id"]) or item["chunk_id"]

    def labeled(items: list[dict[str, Any]], label: str) -> dict[str, dict[str, Any]]:
        return {key(item): item for item in items if item.get("label") == label}

    def all_labeled(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            key(item): item
            for item in items
            if item.get("label") and item.get("label") != "unlabeled"
        }

    rows = []
    for case_id, first in initial.items():
        first_items = first["candidate_annotations"]
        atomic_items = atomic[case_id]["evidence_annotations"]
        parent_items = parent[case_id]["candidate_annotations"]
        new_items = new[case_id]["final_evidence"]
        first_useful = labeled(first_items, "useful")
        atomic_useful = labeled(atomic_items, "useful")
        parent_useful = labeled(parent_items, "useful")
        known_useful = first_useful | atomic_useful | parent_useful
        known_labeled = all_labeled(first_items) | all_labeled(atomic_items) | all_labeled(parent_items)
        new_by_key = {key(item): item for item in new_items}
        recovered_keys = set(known_useful) & set(new_by_key)
        missed_keys = set(known_useful) - set(new_by_key)
        first_lost_by_old = set(first_useful) - set(atomic_useful)
        recovered_first_lost = first_lost_by_old & set(new_by_key)
        rows.append(
            {
                "case_id": case_id,
                "pending": first["pending"],
                "initial_candidate_count": len(first_items),
                "old_atomic_candidate_count": len(atomic_items),
                "bge_atomic_candidate_count": len(new_items),
                "initial_useful_count": len(first_useful),
                "old_atomic_useful_count": len(atomic_useful),
                "known_useful_count": len(known_useful),
                "bge_atomic_known_useful_recovered_count": len(recovered_keys),
                "bge_atomic_unjudged_candidate_count": sum(
                    key(item) not in known_labeled for item in new_items
                ),
                "first_lost_by_old_count": len(first_lost_by_old),
                "recovered_first_lost_count": len(recovered_first_lost),
                "recovered_known_useful": [
                    {
                        "chunk_id": new_by_key[item]["chunk_id"],
                        "content": new_by_key[item]["content"],
                        "evidence_key": item,
                        "parent_rank": new_by_key[item]["parent_rank"],
                    }
                    for item in sorted(recovered_keys)
                ],
                "missed_known_useful": [
                    {
                        "chunk_id": known_useful[item]["chunk_id"],
                        "content": known_useful[item]["content"],
                        "evidence_key": item,
                    }
                    for item in sorted(missed_keys)
                ],
                "recovered_first_lost": [
                    {
                        "chunk_id": new_by_key[item]["chunk_id"],
                        "content": new_by_key[item]["content"],
                        "evidence_key": item,
                        "parent_rank": new_by_key[item]["parent_rank"],
                    }
                    for item in sorted(recovered_first_lost)
                ],
            }
        )

    old_loss_cases = [row for row in rows if row["initial_useful_count"] > 0 and row["old_atomic_useful_count"] == 0]
    return {
        "schema": "atomic_bgem3_diagnostic_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "definition_zh": (
            "BGE原子版尚未完成新候选人工标注，因此其useful数量采用在同一待审块下命中三套既有黄金标注useful证据的数量，"
            "仅表示已知useful召回下界，不代表最终useful总数。证据组成员按同一证据计算。"
        ),
        "summary": {
            "case_count": len(rows),
            "initial_useful_count": sum(row["initial_useful_count"] for row in rows),
            "old_atomic_useful_count": sum(row["old_atomic_useful_count"] for row in rows),
            "known_useful_union_count": sum(row["known_useful_count"] for row in rows),
            "bge_atomic_known_useful_recovered_count": sum(
                row["bge_atomic_known_useful_recovered_count"] for row in rows
            ),
            "bge_atomic_known_useful_recovery_rate": (
                sum(row["bge_atomic_known_useful_recovered_count"] for row in rows)
                / sum(row["known_useful_count"] for row in rows)
            ),
            "bge_atomic_total_candidate_count": sum(row["bge_atomic_candidate_count"] for row in rows),
            "bge_atomic_unjudged_candidate_count": sum(
                row["bge_atomic_unjudged_candidate_count"] for row in rows
            ),
            "old_atomic_complete_loss_case_count": len(old_loss_cases),
            "old_loss_cases_with_any_bge_atomic_recovery": sum(
                row["recovered_first_lost_count"] > 0 for row in old_loss_cases
            ),
            "old_loss_first_useful_count": sum(row["first_lost_by_old_count"] for row in old_loss_cases),
            "old_loss_first_useful_recovered_count": sum(
                row["recovered_first_lost_count"] for row in old_loss_cases
            ),
            "cases_with_bge_atomic_known_useful": sum(
                row["bge_atomic_known_useful_recovered_count"] > 0 for row in rows
            ),
            "candidate_totals": {
                "initial": sum(row["initial_candidate_count"] for row in rows),
                "old_atomic": sum(row["old_atomic_candidate_count"] for row in rows),
                "bge_atomic": sum(row["bge_atomic_candidate_count"] for row in rows),
            },
        },
        "cases": rows,
    }


def evidence(items: list[dict[str, Any]], title: str, css: str) -> str:
    cards = "".join(
        f"""<article class="{css}"><b>{esc(item['chunk_id'])}</b>
        <p>{esc(compact(item['content']))}</p><details><summary>全文</summary>
        <pre>{esc(item['content'])}</pre></details></article>"""
        for item in items
    )
    return f"<section><h4>{esc(title)} ({len(items)})</h4>{cards or '<p class=empty>无</p>'}</section>"


def render(payload: dict[str, Any]) -> str:
    rows = sorted(
        payload["cases"],
        key=lambda row: (
            row["recovered_first_lost_count"] == 0,
            -row["first_lost_by_old_count"],
            -row["bge_atomic_known_useful_recovered_count"],
        ),
    )
    cards = []
    for row in rows:
        category = (
            "恢复旧原子版漏召回"
            if row["recovered_first_lost_count"]
            else "仍有已知useful漏召回"
            if row["missed_known_useful"]
            else "已知useful全部召回"
        )
        cards.append(
            f"""<section class="case" data-category="{esc(category)}"><header><div><h2>{esc(row['case_id'])}</h2>
            <span>{esc(category)}</span></div><b>BGE原子命中已知 useful {row['bge_atomic_known_useful_recovered_count']}/{row['known_useful_count']}</b></header>
            <div class="metrics"><span>第一版：{row['initial_useful_count']} useful / {row['initial_candidate_count']} 候选</span>
            <span>旧原子版：{row['old_atomic_useful_count']} useful / {row['old_atomic_candidate_count']} 候选</span>
            <span>BGE原子版：已知 useful 下界 {row['bge_atomic_known_useful_recovered_count']} / {row['bge_atomic_candidate_count']} 候选，未判断 {row['bge_atomic_unjudged_candidate_count']}</span></div>
            <details><summary>待审 chunk 原文</summary><pre>{esc(row['pending'].get('content', ''))}</pre></details>
            <div class="grid">{evidence(row['recovered_first_lost'], 'BGE原子版恢复的第一版 useful', 'recovered')}
            {evidence(row['recovered_known_useful'], 'BGE原子版命中的全部已知 useful', 'hit')}
            {evidence(row['missed_known_useful'], 'BGE原子版仍漏掉的已知 useful', 'miss')}</div></section>"""
        )
    summary = payload["summary"]
    categories = Counter(
        "恢复旧原子版漏召回"
        if row["recovered_first_lost_count"]
        else "仍有已知useful漏召回"
        if row["missed_known_useful"]
        else "已知useful全部召回"
        for row in rows
    )
    buttons = "".join(
        f"<button onclick=\"filter('{esc(name)}')\">{esc(name)} {count}</button>"
        for name, count in categories.items()
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>BGE原子主张检索诊断</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f2f5f3;color:#18251f;font-family:"Microsoft YaHei",sans-serif}}
    .top{{position:sticky;top:0;z-index:5;background:#17392c;color:#fff;padding:15px 3vw}}h1,h2,h4{{margin:0}}.top p{{margin:5px 0}}button{{margin:6px 5px 0 0;padding:6px 9px}}
    main{{width:min(1550px,96vw);margin:18px auto}}.case{{background:#fff;border:1px solid #ccd5cf;padding:13px;margin-bottom:16px}}.case>header{{display:flex;justify-content:space-between;gap:12px}}
    .metrics{{display:flex;gap:18px;flex-wrap:wrap;background:#edf2ef;padding:9px;margin:10px 0}}details{{border:1px solid #d6ddd9;margin-top:7px}}summary{{cursor:pointer;font-weight:700;padding:6px}}pre{{white-space:pre-wrap;word-break:break-word;padding:8px}}
    .grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px}}.grid section{{border:1px solid #d5ddd8;padding:8px;min-width:0}}article{{padding:8px;margin-top:8px;background:#f8faf9;border-left:5px solid #7d8b83}}article.recovered{{border-color:#146f4b}}article.hit{{border-color:#39826a}}article.miss{{border-color:#b24f42}}article p{{line-height:1.5}}.empty{{color:#77847c}}
    @media(max-width:1000px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><div class="top"><h1>BGE-M3 + 原子主张检索诊断</h1>
    <p>已知 useful 召回下界：{summary['bge_atomic_known_useful_recovered_count']}/{summary['known_useful_union_count']} ({summary['bge_atomic_known_useful_recovery_rate']:.1%})；
    旧原子完全丢失的 {summary['old_atomic_complete_loss_case_count']} 个块中，BGE原子恢复 {summary['old_loss_cases_with_any_bge_atomic_recovery']} 个。</p>
    <button onclick="filter('all')">全部</button>{buttons}</div><main>{''.join(cards)}</main>
    <script>function filter(v){{document.querySelectorAll('.case').forEach(x=>x.style.display=v==='all'||x.dataset.category===v?'':'none')}}</script></body></html>"""


def main() -> None:
    payload = build()
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    HTML.write_text(render(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(OUTPUT.resolve())
    print(HTML.resolve())


if __name__ == "__main__":
    main()
