"""Build an inspectable HTML report for accepted and excluded retrieval units."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = PROJECT_ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "reports" / "atomic_claim_review_v9_v1.html"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render_units(units: list[dict[str, Any]], accepted: bool) -> str:
    if not units:
        return '<p class="empty">无</p>'
    rows = []
    for unit in units:
        label = unit.get("retrieval_priority") if accepted else unit.get("exclusion_reason")
        query = (
            f'<div class="query"><b>检索 query：</b>{esc(unit.get("retrieval_query"))}</div>'
            if accepted else ""
        )
        rows.append(
            f"""
            <article class="unit {'accepted' if accepted else 'excluded'}">
              <div class="unit-head">
                <span>原文行 {esc(unit.get('source_line'))}</span>
                <span class="tag">{esc(label)}</span>
              </div>
              <div class="claim">{esc(unit.get('claim_text'))}</div>
              {query}
            </article>
            """
        )
    return "".join(rows)


def build_html(payload: dict[str, Any]) -> str:
    cards = []
    for case in payload["cases"]:
        claims = case.get("claims", [])
        excluded = case.get("excluded_units", [])
        searchable = " ".join([
            case["case_id"],
            case["pending"].get("doc_name", ""),
            case["pending"].get("content", ""),
        ])
        cards.append(
            f"""
            <section class="case" data-search="{esc(searchable.lower())}">
              <header>
                <div>
                  <h2>{esc(case['case_id'])}</h2>
                  <p>{esc(case['pending'].get('doc_name'))}</p>
                </div>
                <div class="counts">
                  <span class="ok">检索单元 {len(claims)}</span>
                  <span>排除单元 {len(excluded)}</span>
                </div>
              </header>
              <details>
                <summary>查看父 chunk 原文</summary>
                <pre>{esc(case['pending'].get('content'))}</pre>
              </details>
              <div class="columns">
                <div>
                  <h3>进入检索的内部 query 单元</h3>
                  {render_units(claims, True)}
                </div>
                <div>
                  <h3>被排除的候选单元</h3>
                  {render_units(excluded, False)}
                </div>
              </div>
            </section>
            """
        )
    summary = payload["summary"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>原子检索单元确认报告</title>
<style>
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Microsoft YaHei", sans-serif; background: #f4f6f8; color: #17202a; }}
.top {{ position: sticky; top: 0; z-index: 5; background: #fff; border-bottom: 1px solid #ccd4dc; padding: 18px 24px; }}
.top h1 {{ margin: 0 0 8px; font-size: 24px; }}
.top p {{ margin: 4px 0; color: #52616f; }}
input {{ width: min(680px, 100%); margin-top: 12px; padding: 10px 12px; border: 1px solid #aab6c2; border-radius: 4px; font-size: 15px; }}
main {{ max-width: 1500px; margin: 0 auto; padding: 20px; }}
.case {{ background: #fff; border: 1px solid #ccd4dc; margin-bottom: 18px; }}
.case > header {{ display: flex; justify-content: space-between; gap: 16px; padding: 16px; border-bottom: 1px solid #dce2e8; }}
h2, h3 {{ margin: 0; }}
h2 {{ font-size: 18px; }}
h3 {{ font-size: 16px; padding-bottom: 10px; }}
header p {{ margin: 5px 0 0; color: #52616f; }}
.counts {{ display: flex; gap: 8px; align-items: start; }}
.counts span, .tag {{ border: 1px solid #aab6c2; padding: 3px 7px; font-size: 12px; }}
.counts .ok {{ color: #11633b; border-color: #4f9d73; }}
details {{ padding: 12px 16px; border-bottom: 1px solid #dce2e8; }}
summary {{ cursor: pointer; font-weight: 700; }}
pre {{ white-space: pre-wrap; line-height: 1.65; font-family: inherit; }}
.columns {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; padding: 16px; }}
.unit {{ border-left: 4px solid #6b7b88; padding: 10px 12px; margin-bottom: 10px; background: #f7f9fa; }}
.unit.accepted {{ border-left-color: #238554; }}
.unit.excluded {{ border-left-color: #929da6; color: #59636c; }}
.unit-head {{ display: flex; justify-content: space-between; gap: 8px; font-size: 12px; margin-bottom: 7px; }}
.claim {{ line-height: 1.6; }}
.query {{ margin-top: 8px; padding-top: 8px; border-top: 1px dashed #b9c3cc; color: #34495e; font-size: 13px; line-height: 1.55; }}
.empty {{ color: #7b8791; }}
@media (max-width: 900px) {{ .columns {{ grid-template-columns: 1fr; }} .case > header {{ display: block; }} .counts {{ margin-top: 10px; }} }}
</style>
</head>
<body>
<div class="top">
  <h1>原子检索单元确认报告</h1>
  <p>黄金评估单元始终为 {summary['case_count']} 个父 chunk；内部检索 query 单元 {summary['retrieval_claim_count']} 个；排除候选 {summary['excluded_unit_count']} 个。</p>
  <p>内部 query 不是新增黄金样本。请重点检查错误拆分、遗漏要求和不应进入检索的内容。</p>
  <input id="search" placeholder="搜索 case_id、文档名或父 chunk 内容">
</div>
<main>{''.join(cards)}</main>
<script>
const input = document.getElementById('search');
input.addEventListener('input', () => {{
  const value = input.value.trim().toLowerCase();
  document.querySelectorAll('.case').forEach(card => {{
    card.style.display = !value || card.dataset.search.includes(value) ? '' : 'none';
  }});
}});
</script>
</body>
</html>"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = load_json(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(payload), encoding="utf-8", newline="\n")
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
