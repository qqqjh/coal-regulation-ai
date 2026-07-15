"""
Map legacy v5 annotation cases to current v9 pending-document chunks.

The mapper uses stable source evidence instead of assuming chunk numbers remain
valid across versions. It combines page overlap, character n-gram coverage,
topic similarity, and section-title similarity, then writes:

- a machine-readable mapping JSON;
- a self-contained HTML page for human confirmation.
"""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = PROJECT_ROOT / "rag_eval" / "data" / "annotation_cases_v1.json"
DEFAULT_V9 = PROJECT_ROOT / "chunks_visualization" / "pending_doc_chunks_v9_20260528_204330.json"
DEFAULT_OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "v5_to_v9_case_mappings.json"
DEFAULT_OUTPUT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "v5_to_v9_mapping_review.html"
TOP_N = 3


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def compact_text(text: Any) -> str:
    text = normalize_text(text).lower()
    return "".join(re.findall(r"[\u4e00-\u9fffA-Za-z0-9.%≥≤<>]+", text))


def char_ngrams(text: str, n: int = 3) -> Set[str]:
    if not text:
        return set()
    if len(text) <= n:
        return {text}
    return {text[i:i + n] for i in range(len(text) - n + 1)}


def page_numbers(page_range: Any) -> Set[int]:
    nums = [int(x) for x in re.findall(r"\d+", str(page_range or ""))]
    if not nums:
        return set()
    if len(nums) == 1:
        return {nums[0]}
    start, end = nums[0], nums[1]
    if start > end:
        start, end = end, start
    if end - start > 500:
        return {start, end}
    return set(range(start, end + 1))


def page_overlap_score(old_range: Any, new_range: Any) -> float:
    old_pages = page_numbers(old_range)
    new_pages = page_numbers(new_range)
    if not old_pages or not new_pages:
        return 0.0
    overlap = old_pages & new_pages
    if not overlap:
        return 0.0
    return 0.65 * len(overlap) / len(new_pages) + 0.35 * len(overlap) / len(old_pages)


def ngram_scores(source: str, candidate: str) -> Tuple[float, float, float]:
    source_grams = char_ngrams(source)
    candidate_grams = char_ngrams(candidate)
    if not source_grams or not candidate_grams:
        return 0.0, 0.0, 0.0
    common = len(source_grams & candidate_grams)
    source_coverage = common / len(source_grams)
    candidate_coverage = common / len(candidate_grams)
    f1 = (
        2 * source_coverage * candidate_coverage / (source_coverage + candidate_coverage)
        if source_coverage + candidate_coverage
        else 0.0
    )
    return source_coverage, candidate_coverage, f1


def text_similarity(source: Any, candidate: Any) -> Dict[str, float]:
    source_text = compact_text(source)
    candidate_text = compact_text(candidate)
    source_coverage, candidate_coverage, f1 = ngram_scores(source_text, candidate_text)
    containment = 0.0
    if source_text and candidate_text:
        if source_text in candidate_text:
            containment = len(source_text) / len(candidate_text)
        elif candidate_text in source_text:
            containment = len(candidate_text) / len(source_text)
    return {
        "source_coverage": source_coverage,
        "candidate_coverage": candidate_coverage,
        "ngram_f1": f1,
        "containment": containment,
    }


def metadata_text(chunk: Dict[str, Any]) -> str:
    return " ".join(
        normalize_text(chunk.get(field, ""))
        for field in ("part", "chapter", "section", "article", "sub_title", "sub_marker")
    )


def title_similarity(old_pending: Dict[str, Any], candidate: Dict[str, Any]) -> float:
    old_title = " ".join(
        normalize_text(old_pending.get(field, ""))
        for field in ("chapter", "section", "article")
    )
    return text_similarity(old_title, metadata_text(candidate))["ngram_f1"]


def confidence_label(score: float, content_f1: float, page_score: float) -> str:
    if score >= 0.66 and (content_f1 >= 0.48 or page_score >= 0.65):
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


def recommended_confidence(base_confidence: str, top_gap: float) -> str:
    """Downgrade apparently strong matches when adjacent candidates are tied."""
    levels = ["low", "medium", "high"]
    level = levels.index(base_confidence)
    if top_gap < 0.015:
        level = max(0, level - 2)
    elif top_gap < 0.05:
        level = max(0, level - 1)
    return levels[level]


@dataclass
class CandidateScore:
    chunk_index: int
    score: float
    components: Dict[str, float]
    chunk: Dict[str, Any]


def score_candidate(case: Dict[str, Any], chunk: Dict[str, Any], chunk_index: int) -> CandidateScore:
    pending = case["pending"]
    content_scores = text_similarity(pending.get("content", ""), chunk.get("content", ""))
    topic_score = text_similarity(case.get("topic", ""), chunk.get("content", ""))["source_coverage"]
    title_score = title_similarity(pending, chunk)
    pages = page_overlap_score(pending.get("page_range", ""), chunk.get("page_range", ""))

    # A v5 chunk can split into several v9 chunks. Candidate coverage therefore
    # matters more than source coverage: a v9 child fully contained in the old
    # chunk is still a valid mapping candidate.
    content_signal = max(
        content_scores["ngram_f1"],
        0.75 * content_scores["candidate_coverage"] + 0.25 * content_scores["source_coverage"],
        content_scores["containment"],
    )
    score = (
        0.60 * content_signal
        + 0.20 * pages
        + 0.12 * topic_score
        + 0.08 * title_score
    )
    if pages == 0:
        score *= 0.82
    return CandidateScore(
        chunk_index=chunk_index,
        score=score,
        components={
            "content_signal": content_signal,
            "content_source_coverage": content_scores["source_coverage"],
            "content_candidate_coverage": content_scores["candidate_coverage"],
            "content_ngram_f1": content_scores["ngram_f1"],
            "content_containment": content_scores["containment"],
            "page_overlap": pages,
            "topic_coverage": topic_score,
            "title_similarity": title_score,
        },
        chunk=chunk,
    )


def doc_code(name: Any) -> str:
    match = re.match(r"\s*(004|006|066)", str(name or ""))
    return match.group(1) if match else ""


def find_v9_document(v9_data: Dict[str, List[Dict[str, Any]]], old_doc_name: str) -> Tuple[str, List[Dict[str, Any]]]:
    code = doc_code(old_doc_name)
    for name, chunks in v9_data.items():
        if code and doc_code(name) == code:
            return name, chunks
    raise KeyError(f"Cannot find v9 document for {old_doc_name}")


def candidate_payload(candidate: CandidateScore, v9_doc_name: str) -> Dict[str, Any]:
    chunk = candidate.chunk
    return {
        "v9_doc_name": v9_doc_name,
        "v9_chunk_no": candidate.chunk_index + 1,
        "v9_chunk_index": candidate.chunk_index,
        "score": round(candidate.score, 6),
        "confidence": confidence_label(
            candidate.score,
            candidate.components["content_ngram_f1"],
            candidate.components["page_overlap"],
        ),
        "components": {k: round(v, 6) for k, v in candidate.components.items()},
        "page_range": chunk.get("page_range", ""),
        "part": chunk.get("part", ""),
        "chapter": chunk.get("chapter", ""),
        "section": chunk.get("section", ""),
        "article": chunk.get("article", ""),
        "sub_title": chunk.get("sub_title", ""),
        "sub_marker": chunk.get("sub_marker", ""),
        "chunk_level": chunk.get("chunk_level", ""),
        "split_strategy": chunk.get("split_strategy", ""),
        "char_count": chunk.get("char_count", len(chunk.get("content", ""))),
        "content": chunk.get("content", ""),
    }


def map_cases(cases_data: Dict[str, Any], v9_data: Dict[str, List[Dict[str, Any]]], top_n: int) -> Dict[str, Any]:
    mapped_cases = []
    for case in cases_data["cases"]:
        old_pending = case["pending"]
        v9_doc_name, chunks = find_v9_document(v9_data, old_pending["doc_name"])
        scored = [score_candidate(case, chunk, idx) for idx, chunk in enumerate(chunks)]
        scored.sort(key=lambda item: item.score, reverse=True)
        candidates = [candidate_payload(item, v9_doc_name) for item in scored[:top_n]]
        top = candidates[0]
        top_gap = top["score"] - candidates[1]["score"] if len(candidates) > 1 else top["score"]
        confidence = recommended_confidence(top["confidence"], top_gap)
        warnings = []
        if top_gap < 0.05:
            warnings.append("Top1 与 Top2 分差较小，可能需要映射多个相邻 v9 chunk")
        if top["components"]["content_candidate_coverage"] < 0.30:
            warnings.append("推荐 v9 chunk 较大，旧 v5 内容仅覆盖其中一部分")
        mapped_cases.append({
            "case_id": case["case_id"],
            "sample_source": case.get("sample_source", ""),
            "topic": case.get("topic", ""),
            "source_note": case.get("source_note", ""),
            "old_pending": old_pending,
            "recommended_v9_chunk_no": top["v9_chunk_no"],
            "recommended_confidence": confidence,
            "recommended_score": top["score"],
            "top1_top2_gap": round(top_gap, 6),
            "mapping_warnings": warnings,
            "v9_candidates": candidates,
            "human_confirmation": {
                "status": "unreviewed",
                "selected_v9_chunk_nos": [top["v9_chunk_no"]],
                "note": "",
            },
        })

    confidence_counts = Counter(case["recommended_confidence"] for case in mapped_cases)
    return {
        "schema": "coal_rag_v5_to_v9_case_mappings_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_cases": str(DEFAULT_CASES.relative_to(PROJECT_ROOT)),
        "target_pending_chunks": str(DEFAULT_V9.relative_to(PROJECT_ROOT)),
        "mapping_method": {
            "top_n": top_n,
            "signals": [
                "content character trigram coverage",
                "page range overlap",
                "topic coverage",
                "chapter and section title similarity",
            ],
            "note": "A legacy case may map to multiple v9 chunks; human confirmation is required.",
        },
        "summary": {
            "case_count": len(mapped_cases),
            "confidence_counts": dict(confidence_counts),
        },
        "cases": mapped_cases,
    }


def json_for_script(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def build_html(mapping: Dict[str, Any], output_path: Path) -> None:
    cases_json = json_for_script(mapping["cases"])
    summary = mapping["summary"]
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>v5 → v9 Chunk 映射确认</title>
<style>
:root {{
  --ink:#18201e; --muted:#65706c; --paper:#f4f5f1; --panel:#ffffff;
  --line:#d8ddd8; --green:#176b52; --amber:#a96316; --red:#a33c32; --blue:#285f8f;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:var(--ink); background:var(--paper); font-family:"Microsoft YaHei","Noto Sans CJK SC",sans-serif; }}
button,select,textarea,input {{ font:inherit; }}
.topbar {{ position:sticky; top:0; z-index:20; background:#192521; color:white; border-bottom:4px solid #d1a84b; }}
.topbar-inner {{ max-width:1700px; margin:auto; padding:14px 22px; display:flex; gap:20px; align-items:center; }}
h1 {{ font-size:20px; margin:0; letter-spacing:0; white-space:nowrap; }}
.stats {{ display:flex; gap:8px; flex-wrap:wrap; flex:1; }}
.stat {{ padding:5px 8px; border:1px solid #52605a; font-size:12px; }}
.toolbar {{ display:flex; gap:8px; align-items:center; }}
.toolbar button,.toolbar select {{ border:1px solid #78867f; background:#263630; color:white; padding:7px 10px; cursor:pointer; }}
.layout {{ max-width:1700px; margin:0 auto; display:grid; grid-template-columns:320px minmax(0,1fr); min-height:calc(100vh - 68px); }}
.sidebar {{ border-right:1px solid var(--line); background:#eef0eb; padding:14px; position:sticky; top:68px; height:calc(100vh - 68px); overflow:auto; }}
.filters {{ display:grid; grid-template-columns:1fr 1fr; gap:7px; margin-bottom:12px; }}
.filters input,.filters select {{ width:100%; padding:8px; border:1px solid var(--line); background:white; }}
.case-nav {{ width:100%; text-align:left; padding:10px; margin-bottom:6px; border:1px solid var(--line); background:white; cursor:pointer; }}
.case-nav.active {{ border-color:var(--green); box-shadow:inset 4px 0 var(--green); }}
.case-nav small {{ display:block; margin-top:5px; color:var(--muted); }}
.badge {{ display:inline-block; padding:2px 6px; border:1px solid currentColor; font-size:11px; margin-right:5px; }}
.high {{ color:var(--green); }} .medium {{ color:var(--amber); }} .low {{ color:var(--red); }}
.main {{ padding:20px; min-width:0; }}
.case-header {{ display:grid; grid-template-columns:1fr auto; gap:16px; border-bottom:2px solid var(--ink); padding-bottom:13px; margin-bottom:16px; }}
.case-title {{ margin:0 0 8px; font-size:22px; }}
.case-meta {{ color:var(--muted); font-size:13px; }}
.review-box {{ min-width:310px; display:grid; grid-template-columns:1fr 1fr; gap:8px; }}
.review-box select,.review-box textarea {{ border:1px solid var(--line); padding:8px; background:white; }}
.review-box textarea {{ grid-column:1 / -1; min-height:58px; resize:vertical; }}
.old {{ background:#fff; border:1px solid var(--line); padding:16px; margin-bottom:18px; }}
.old h2,.candidates h2 {{ font-size:15px; margin:0 0 10px; }}
.text {{ white-space:pre-wrap; line-height:1.68; font-size:13px; max-height:310px; overflow:auto; padding:12px; background:#fafbf8; border:1px solid #e2e6df; }}
.candidates-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
.candidate {{ border:1px solid var(--line); background:var(--panel); min-width:0; }}
.candidate.selected {{ border:2px solid var(--green); }}
.candidate-head {{ padding:11px; border-bottom:1px solid var(--line); background:#f7f8f4; }}
.candidate-title {{ display:flex; justify-content:space-between; gap:8px; font-weight:700; }}
.candidate-body {{ padding:11px; }}
.metrics {{ display:grid; grid-template-columns:repeat(2,1fr); gap:5px; margin:10px 0; font-size:11px; color:var(--muted); }}
.metric {{ border-left:3px solid #bdc7c0; padding-left:6px; }}
.select-row {{ display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:10px; }}
.select-row button {{ border:1px solid var(--green); background:white; color:var(--green); padding:7px 10px; cursor:pointer; }}
.candidate.selected .select-row button {{ background:var(--green); color:white; }}
.empty {{ padding:30px; color:var(--muted); }}
@media(max-width:1200px) {{ .candidates-grid {{ grid-template-columns:1fr; }} .layout {{ grid-template-columns:260px minmax(0,1fr); }} }}
@media(max-width:760px) {{ .layout {{ display:block; }} .sidebar {{ position:static; height:auto; border-right:0; }} .case-nav {{ display:none; }} .case-header {{ grid-template-columns:1fr; }} .review-box {{ min-width:0; }} }}
</style>
</head>
<body>
<header class="topbar"><div class="topbar-inner">
  <h1>v5 → v9 Chunk 映射确认</h1>
  <div class="stats" id="stats"></div>
  <div class="toolbar">
    <button onclick="prevCase()" title="上一条">←</button>
    <button onclick="nextCase()" title="下一条">→</button>
    <button onclick="exportReview()">导出确认结果</button>
  </div>
</div></header>
<div class="layout">
  <aside class="sidebar">
    <div class="filters">
      <input id="search" placeholder="搜索主题/文档" oninput="renderNav()">
      <select id="statusFilter" onchange="renderNav()">
        <option value="">全部状态</option><option value="unreviewed">未确认</option>
        <option value="confirmed">已确认</option><option value="multi">多块映射</option>
        <option value="rejected">均不匹配</option>
      </select>
      <select id="confidenceFilter" onchange="renderNav()">
        <option value="">全部置信度</option><option value="high">高</option>
        <option value="medium">中</option><option value="low">低</option>
      </select>
      <select id="sourceFilter" onchange="renderNav()">
        <option value="">全部来源</option><option value="comparison_v5">三版本对比</option>
        <option value="stratified_random">分层随机</option>
      </select>
    </div>
    <div id="nav"></div>
  </aside>
  <main class="main" id="main"></main>
</div>
<script>
const CASES={cases_json};
const STORAGE_KEY="coal_rag_v5_to_v9_mapping_review_v1";
let state=JSON.parse(localStorage.getItem(STORAGE_KEY)||"{{}}");
let current=0;
function esc(s){{return String(s??"").replace(/[&<>"']/g,c=>({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));}}
function caseState(c){{return state[c.case_id]||(state[c.case_id]=JSON.parse(JSON.stringify(c.human_confirmation)));}}
function persist(){{localStorage.setItem(STORAGE_KEY,JSON.stringify(state)); renderStats(); renderNav();}}
function renderStats(){{
 const statuses=CASES.map(c=>caseState(c).status);
 const count=x=>statuses.filter(s=>s===x).length;
 document.getElementById("stats").innerHTML=`<span class="stat">总计 {summary['case_count']}</span><span class="stat">已确认 ${{count("confirmed")}}</span><span class="stat">多块 ${{count("multi")}}</span><span class="stat">均不匹配 ${{count("rejected")}}</span><span class="stat">待处理 ${{count("unreviewed")}}</span>`;
}}
function filteredCases(){{
 const q=document.getElementById("search").value.trim().toLowerCase();
 const sf=document.getElementById("statusFilter").value, cf=document.getElementById("confidenceFilter").value, src=document.getElementById("sourceFilter").value;
 return CASES.map((c,i)=>[c,i]).filter(([c])=>(!q||JSON.stringify([c.case_id,c.topic,c.old_pending.doc_name,c.old_pending.chapter,c.old_pending.section]).toLowerCase().includes(q))&&(!sf||caseState(c).status===sf)&&(!cf||c.recommended_confidence===cf)&&(!src||c.sample_source===src));
}}
function renderNav(){{
 const rows=filteredCases();
 document.getElementById("nav").innerHTML=rows.length?rows.map(([c,i])=>`<button class="case-nav ${{i===current?"active":""}}" onclick="openCase(${{i}})"><span class="badge ${{c.recommended_confidence}}">${{c.recommended_confidence}}</span>${{esc(c.case_id)}}<small>${{esc(c.topic||"无主题")}}</small><small>v5 #${{c.old_pending.chunk_no}} → v9 #${{c.recommended_v9_chunk_no}} · ${{caseState(c).status}}</small></button>`).join(""):`<div class="empty">没有符合筛选条件的样本</div>`;
}}
function openCase(i){{current=i; renderMain(); renderNav();}}
function prevCase(){{if(current>0)openCase(current-1);}}
function nextCase(){{if(current<CASES.length-1)openCase(current+1);}}
function toggleCandidate(no){{
 const c=CASES[current], s=caseState(c), set=new Set(s.selected_v9_chunk_nos||[]);
 set.has(no)?set.delete(no):set.add(no); s.selected_v9_chunk_nos=[...set].sort((a,b)=>a-b);
 if(s.selected_v9_chunk_nos.length>1)s.status="multi";
 else if(s.selected_v9_chunk_nos.length===1&&s.status==="multi")s.status="confirmed";
 persist(); renderMain();
}}
function setStatus(v){{caseState(CASES[current]).status=v; persist(); renderMain();}}
function setNote(v){{caseState(CASES[current]).note=v; persist();}}
function metrics(c){{return Object.entries(c.components).map(([k,v])=>`<div class="metric">${{esc(k)}}<br><strong>${{Number(v).toFixed(3)}}</strong></div>`).join("");}}
function renderMain(){{
 const c=CASES[current], s=caseState(c), p=c.old_pending;
 document.getElementById("main").innerHTML=`
 <section class="case-header"><div><h2 class="case-title">${{esc(c.topic||c.case_id)}}</h2><div class="case-meta">${{esc(c.case_id)}} · ${{esc(c.sample_source)}} · ${{esc(c.source_note)}}</div></div>
 <div class="review-box"><select onchange="setStatus(this.value)"><option value="unreviewed" ${{s.status==="unreviewed"?"selected":""}}>未确认</option><option value="confirmed" ${{s.status==="confirmed"?"selected":""}}>确认所选映射</option><option value="multi" ${{s.status==="multi"?"selected":""}}>应映射多个块</option><option value="rejected" ${{s.status==="rejected"?"selected":""}}>候选均不匹配</option></select><span class="badge ${{c.recommended_confidence}}">推荐置信度：${{c.recommended_confidence}} · ${{c.recommended_score}} · 分差 ${{c.top1_top2_gap}}</span><textarea placeholder="人工备注" oninput="setNote(this.value)">${{esc(s.note||"")}}</textarea></div></section>
 ${{c.mapping_warnings.length?`<section class="old"><h2>映射提醒</h2>${{c.mapping_warnings.map(x=>`<span class="badge medium">${{esc(x)}}</span>`).join("")}}</section>`:""}}
 <section class="old"><h2>旧 v5 样本 · Chunk #${{p.chunk_no}} · 页码 ${{esc(p.page_range)}} · ${{esc(p.chapter)}} / ${{esc(p.section)}}</h2><div class="text">${{esc(p.content)}}</div></section>
 <section class="candidates"><h2>v9 映射候选（可选择一个或多个）</h2><div class="candidates-grid">${{c.v9_candidates.map((x,idx)=>`<article class="candidate ${{(s.selected_v9_chunk_nos||[]).includes(x.v9_chunk_no)?"selected":""}}"><div class="candidate-head"><div class="candidate-title"><span>#${{idx+1}} · v9 Chunk #${{x.v9_chunk_no}}</span><span class="badge ${{x.confidence}}">${{x.confidence}} · ${{x.score}}</span></div><div class="case-meta">页码 ${{esc(x.page_range)}} · ${{esc(x.chunk_level)}} · ${{esc(x.split_strategy)}}</div><div class="case-meta">${{esc(x.chapter)}} / ${{esc(x.section)}} / ${{esc(x.sub_title)}}</div></div><div class="candidate-body"><div class="metrics">${{metrics(x)}}</div><div class="text">${{esc(x.content)}}</div><div class="select-row"><span>${{x.char_count}} 字</span><button onclick="toggleCandidate(${{x.v9_chunk_no}})">${{(s.selected_v9_chunk_nos||[]).includes(x.v9_chunk_no)?"已选择":"选择此块"}}</button></div></div></article>`).join("")}}</div></section>`;
}}
function exportReview(){{
 const out={{schema:"coal_rag_v5_to_v9_mapping_confirmations_v1",exported_at:new Date().toISOString(),confirmations:CASES.map(c=>({{case_id:c.case_id,recommended_v9_chunk_no:c.recommended_v9_chunk_no,...caseState(c)}}))}};
 const blob=new Blob([JSON.stringify(out,null,2)],{{type:"application/json;charset=utf-8"}}), a=document.createElement("a"); a.href=URL.createObjectURL(blob); a.download="v5_to_v9_mapping_confirmations.json"; a.click(); URL.revokeObjectURL(a.href);
}}
renderStats(); renderNav(); renderMain();
</script>
</body></html>"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(page, encoding="utf-8")


def write_summary(mapping: Dict[str, Any]) -> None:
    print(f"Mapped cases: {mapping['summary']['case_count']}")
    print(f"Confidence: {mapping['summary']['confidence_counts']}")
    lows = [case for case in mapping["cases"] if case["recommended_confidence"] == "low"]
    if lows:
        print("Low-confidence cases:")
        for case in lows:
            print(
                f"  {case['case_id']}: v5#{case['old_pending']['chunk_no']} "
                f"-> v9#{case['recommended_v9_chunk_no']} ({case['recommended_score']:.3f})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Map legacy v5 annotation cases to v9 chunks.")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--v9", type=Path, default=DEFAULT_V9)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_JSON)
    parser.add_argument("--output-html", type=Path, default=DEFAULT_OUTPUT_HTML)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    args = parser.parse_args()

    cases_data = load_json(args.cases)
    v9_data = load_json(args.v9)
    mapping = map_cases(cases_data, v9_data, args.top_n)
    mapping["source_cases"] = str(args.cases)
    mapping["target_pending_chunks"] = str(args.v9)

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with args.output_json.open("w", encoding="utf-8") as f:
        json.dump(mapping, f, ensure_ascii=False, indent=2)
    build_html(mapping, args.output_html)

    write_summary(mapping)
    print(f"Mapping JSON: {args.output_json}")
    print(f"Review HTML: {args.output_html}")


if __name__ == "__main__":
    main()
