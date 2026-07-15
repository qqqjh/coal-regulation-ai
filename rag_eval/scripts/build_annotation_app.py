"""
Build a small human-in-the-loop RAG annotation app.

The generated HTML is self-contained. Open it in a browser, label candidate
regulation chunks as useful / useless / uncertain, then export the annotations.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import re
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import jieba  # type: ignore
except ModuleNotFoundError:
    jieba = None

try:
    from rank_bm25 import BM25Okapi  # type: ignore
except ModuleNotFoundError:
    BM25Okapi = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PENDING_JSON = PROJECT_ROOT / "chunks_visualization" / "pending_doc_chunks_v5_20260408_182348.json"
DEFAULT_KB_JSON = PROJECT_ROOT / "chunks_visualization" / "chunks_v4_20260413_183532.json"
DEFAULT_COMPARISON_MD = PROJECT_ROOT / "刘致远" / "三版本不合规对比_20260421(1).md"
DEFAULT_CASES_JSON = PROJECT_ROOT / "rag_eval" / "data" / "annotation_cases_v1.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "annotation_app.html"


DOC_CODE_TO_NAME = {
    "004": "004",
    "006": "006",
    "066": "066",
}


@dataclass
class PendingSeed:
    doc_code: str
    chunk_no: int
    page_range: str
    topic: str
    source_note: str
    priority: int


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def tokenize(text: str) -> List[str]:
    text = str(text or "")
    if jieba is not None:
        return [t.strip() for t in jieba.cut(text) if t.strip()]
    tokens = re.findall(r"[A-Za-z0-9.]+|[\u4e00-\u9fff]", text)
    compact = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    bigrams = [compact[i:i + 2] for i in range(max(0, len(compact) - 1))]
    return tokens + bigrams


class SimpleBM25:
    def __init__(self, corpus: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self.corpus = corpus
        self.k1 = k1
        self.b = b
        self.doc_len = [len(doc) for doc in corpus]
        self.avgdl = sum(self.doc_len) / max(1, len(self.doc_len))
        self.term_freqs: List[Dict[str, int]] = []
        doc_freq: Dict[str, int] = defaultdict(int)
        for doc in corpus:
            tf: Dict[str, int] = defaultdict(int)
            for token in doc:
                tf[token] += 1
            self.term_freqs.append(dict(tf))
            for token in tf:
                doc_freq[token] += 1
        n_docs = max(1, len(corpus))
        self.idf = {
            token: math.log(1 + (n_docs - freq + 0.5) / (freq + 0.5))
            for token, freq in doc_freq.items()
        }

    def get_scores(self, query_tokens: List[str]) -> List[float]:
        scores = []
        query_terms = set(query_tokens)
        for idx, tf in enumerate(self.term_freqs):
            score = 0.0
            dl = self.doc_len[idx] or 1
            for term in query_terms:
                freq = tf.get(term, 0)
                if not freq:
                    continue
                denom = freq + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += self.idf.get(term, 0.0) * freq * (self.k1 + 1) / denom
            scores.append(score)
        return scores


def doc_code_from_heading(line: str) -> Optional[str]:
    m = re.search(r"文件[一二三]：\s*(004|006|066)", line)
    if m:
        return m.group(1)
    return None


def clean_md_cell(cell: str) -> str:
    cell = re.sub(r"<br\s*/?>", " ", cell, flags=re.I)
    cell = re.sub(r"[*`>]", "", cell)
    return normalize_text(cell)


def score_seed_priority(line: str) -> int:
    priority = 50
    if "三版本" in line or "一致" in line or "均检出" in line:
        priority += 30
    if "误判" in line or "存疑" in line or "已删除" in line or "建议复查" in line:
        priority += 20
    if "漏检" in line or "未检出" in line:
        priority += 10
    if "正确" in line or "真实违规" in line:
        priority += 10
    return priority


def parse_v5_seeds(comparison_md: Path, max_cases: int) -> List[PendingSeed]:
    seeds_by_key: Dict[Tuple[str, int], PendingSeed] = {}
    current_doc_code: Optional[str] = None

    for raw_line in comparison_md.read_text(encoding="utf-8").splitlines():
        heading_code = doc_code_from_heading(raw_line)
        if heading_code:
            current_doc_code = heading_code
            continue
        if not current_doc_code or "|" not in raw_line or "chunk" not in raw_line:
            continue

        line = raw_line.lstrip("> ").strip()
        if not line.startswith("|") or "---" in line:
            continue

        cells = [clean_md_cell(c) for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        page_range, topic, _lzy, v5_cell, v6_cell = cells[:5]
        if "chunk" not in v5_cell:
            continue

        for chunk_match in re.finditer(r"chunk\s*(\d+)", v5_cell, flags=re.I):
            chunk_no = int(chunk_match.group(1))
            key = (current_doc_code, chunk_no)
            source_note = f"comparison_md: V5={v5_cell}; V6={v6_cell}"
            priority = score_seed_priority(raw_line)
            if key in seeds_by_key:
                existing = seeds_by_key[key]
                existing.topic = f"{existing.topic}；{topic}"
                existing.source_note = f"{existing.source_note} | {source_note}"
                existing.priority = max(existing.priority, priority)
            else:
                seeds_by_key[key] = PendingSeed(
                    doc_code=current_doc_code,
                    chunk_no=chunk_no,
                    page_range=page_range,
                    topic=topic,
                    source_note=source_note,
                    priority=priority,
                )

    seeds = sorted(
        seeds_by_key.values(),
        key=lambda s: (-s.priority, s.doc_code, s.chunk_no),
    )
    return seeds[:max_cases]


def find_pending_doc_name(pending_data: Dict[str, List[Dict[str, Any]]], doc_code: str) -> str:
    for name in pending_data:
        if name.strip().startswith(doc_code):
            return name
    raise KeyError(f"Cannot find pending doc for code {doc_code}")


def flatten_kb(kb_data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    kb_chunks: List[Dict[str, Any]] = []
    for doc_name, chunks in kb_data.items():
        for idx, chunk in enumerate(chunks):
            if not normalize_text(chunk.get("content")):
                continue
            item = dict(chunk)
            item["doc_name"] = doc_name
            item["kb_chunk_index"] = idx
            item["chunk_id"] = f"{doc_name}::chunk{idx + 1}"
            kb_chunks.append(item)
    return kb_chunks


class BM25Retriever:
    def __init__(self, kb_chunks: List[Dict[str, Any]]):
        self.kb_chunks = kb_chunks
        self.tokenized = [
            tokenize(c.get("retrieval_text") or c["content"])
            for c in kb_chunks
        ]
        self.bm25 = BM25Okapi(self.tokenized) if BM25Okapi is not None else SimpleBM25(self.tokenized)

    def search(self, query: str, top_k: int) -> List[Tuple[Dict[str, Any], float, str]]:
        tokens = tokenize(query)
        scores = self.bm25.get_scores(tokens)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: top_k * 3]
        results: List[Tuple[Dict[str, Any], float, str]] = []
        for idx in top_indices:
            score = float(scores[idx])
            if score <= 0:
                continue
            results.append((self.kb_chunks[idx], score, "bm25"))
            if len(results) >= top_k:
                break
        return results


def build_keyword_hint(query: str, content: str, limit: int = 10) -> List[str]:
    stop_words = {
        "的", "了", "和", "及", "与", "或", "在", "为", "应", "必须", "不得", "进行", "工作",
        "作业", "煤矿", "规程", "要求", "安全", "采用", "以上", "以下", "时", "中",
    }
    query_tokens = [t.strip() for t in tokenize(query) if len(t.strip()) > 1 and t.strip() not in stop_words]
    content_set = set(tokenize(content))
    seen = set()
    hints: List[str] = []
    for token in query_tokens:
        if token in content_set and token not in seen:
            seen.add(token)
            hints.append(token)
        if len(hints) >= limit:
            break
    return hints


def summarize_candidate_reason(source: str, score: float, keywords: List[str]) -> str:
    parts = [f"候选来源: {source}", f"得分: {score:.4f}"]
    if keywords:
        parts.append("共同关键词: " + "、".join(keywords))
    else:
        parts.append("共同关键词较少，建议重点看条款适用范围")
    return "；".join(parts)


def pending_quote(content: str, max_len: int = 120) -> str:
    text = normalize_text(content)
    return text[:max_len]


def build_case(
    case_id: str,
    pending_doc_name: str,
    pending_chunk: Dict[str, Any],
    chunk_no: int,
    sample_source: str,
    topic: str,
    source_note: str,
    retriever: BM25Retriever,
    candidates_per_case: int,
) -> Dict[str, Any]:
    query = pending_chunk.get("content", "")
    raw_candidates = retriever.search(query, top_k=candidates_per_case)
    candidates = []
    for rank, (kb_chunk, score, source) in enumerate(raw_candidates, start=1):
        keywords = build_keyword_hint(query, kb_chunk.get("content", ""))
        candidates.append({
            "rank": rank,
            "chunk_id": kb_chunk["chunk_id"],
            "doc_name": kb_chunk.get("doc_name", ""),
            "kb_chunk_index": kb_chunk.get("kb_chunk_index"),
            "chapter": kb_chunk.get("chapter", ""),
            "section": kb_chunk.get("section", ""),
            "article": kb_chunk.get("article", ""),
            "page_range": kb_chunk.get("page_range", ""),
            "chunk_level": kb_chunk.get("chunk_level", ""),
            "score": round(score, 6),
            "source": source,
            "reason": summarize_candidate_reason(source, score, keywords),
            "content": kb_chunk.get("content", ""),
            "anchor_quote": pending_quote(kb_chunk.get("content", ""), 100),
            "label": "unlabeled",
            "note": "",
        })

    return {
        "case_id": case_id,
        "sample_source": sample_source,
        "topic": topic,
        "source_note": source_note,
        "pending": {
            "doc_name": pending_doc_name,
            "chunk_no": chunk_no,
            "chunk_index": chunk_no - 1,
            "chapter": pending_chunk.get("chapter", ""),
            "section": pending_chunk.get("section", ""),
            "article": pending_chunk.get("article", ""),
            "page_range": pending_chunk.get("page_range", ""),
            "chunk_level": pending_chunk.get("chunk_level", ""),
            "quote": pending_quote(query),
            "content": query,
        },
        "final_label": "unlabeled",
        "missing_correct_evidence": False,
        "not_eval_suitable": False,
        "human_note": "",
        "candidates": candidates,
    }


def build_seed_cases(
    pending_data: Dict[str, List[Dict[str, Any]]],
    seeds: List[PendingSeed],
    retriever: BM25Retriever,
    candidates_per_case: int,
) -> List[Dict[str, Any]]:
    cases = []
    for seed in seeds:
        pending_doc_name = find_pending_doc_name(pending_data, seed.doc_code)
        chunks = pending_data[pending_doc_name]
        if seed.chunk_no < 1 or seed.chunk_no > len(chunks):
            continue
        case_id = f"cmp_{seed.doc_code}_chunk{seed.chunk_no:03d}"
        cases.append(build_case(
            case_id=case_id,
            pending_doc_name=pending_doc_name,
            pending_chunk=chunks[seed.chunk_no - 1],
            chunk_no=seed.chunk_no,
            sample_source="comparison_v5",
            topic=seed.topic,
            source_note=seed.source_note,
            retriever=retriever,
            candidates_per_case=candidates_per_case,
        ))
    return cases


def sample_random_cases(
    pending_data: Dict[str, List[Dict[str, Any]]],
    existing_keys: Iterable[Tuple[str, int]],
    count: int,
    rng: random.Random,
) -> List[Tuple[str, int]]:
    selected = set(existing_keys)
    by_doc: Dict[str, List[int]] = {}
    for doc_name, chunks in pending_data.items():
        candidates = [
            i + 1 for i, c in enumerate(chunks)
            if len(normalize_text(c.get("content", ""))) >= 80
            and (doc_name, i + 1) not in selected
        ]
        by_doc[doc_name] = candidates

    doc_names = list(by_doc.keys())
    quotas = {doc: count // len(doc_names) for doc in doc_names}
    for doc in doc_names[: count % len(doc_names)]:
        quotas[doc] += 1

    picks: List[Tuple[str, int]] = []
    for doc_name in doc_names:
        candidates = by_doc[doc_name]
        rng.shuffle(candidates)
        for chunk_no in candidates[: quotas[doc_name]]:
            picks.append((doc_name, chunk_no))
            selected.add((doc_name, chunk_no))

    while len(picks) < count:
        pool = [
            (doc, idx)
            for doc, candidates in by_doc.items()
            for idx in candidates
            if (doc, idx) not in selected
        ]
        if not pool:
            break
        doc_name, chunk_no = rng.choice(pool)
        picks.append((doc_name, chunk_no))
        selected.add((doc_name, chunk_no))
    return picks[:count]


def build_random_cases(
    pending_data: Dict[str, List[Dict[str, Any]]],
    existing_cases: List[Dict[str, Any]],
    count: int,
    retriever: BM25Retriever,
    candidates_per_case: int,
    seed: int,
) -> List[Dict[str, Any]]:
    existing_keys = {
        (case["pending"]["doc_name"], int(case["pending"]["chunk_no"]))
        for case in existing_cases
    }
    picks = sample_random_cases(pending_data, existing_keys, count, random.Random(seed))
    cases = []
    for order, (doc_name, chunk_no) in enumerate(picks, start=1):
        chunks = pending_data[doc_name]
        case_id = f"rand_{order:03d}_{doc_name.strip()[:3]}_chunk{chunk_no:03d}"
        cases.append(build_case(
            case_id=case_id,
            pending_doc_name=doc_name,
            pending_chunk=chunks[chunk_no - 1],
            chunk_no=chunk_no,
            sample_source="stratified_random",
            topic="三待审文档分层随机补样",
            source_note="random seed sample",
            retriever=retriever,
            candidates_per_case=candidates_per_case,
        ))
    return cases


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RAG 法规证据标注台</title>
  <style>
    :root {{
      --ink: #1f2933;
      --muted: #687385;
      --line: #d7dde7;
      --paper: #f7f4ed;
      --panel: #fffdf8;
      --steel: #2f4858;
      --red: #b83d3d;
      --green: #2f7d5b;
      --amber: #9a6b18;
      --blue: #2f5f98;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        linear-gradient(90deg, rgba(47,72,88,.06) 1px, transparent 1px),
        linear-gradient(rgba(47,72,88,.05) 1px, transparent 1px),
        var(--paper);
      background-size: 28px 28px;
      font-family: "Microsoft YaHei", "Noto Sans CJK SC", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      background: rgba(247, 244, 237, .96);
      border-bottom: 1px solid var(--line);
      padding: 12px 18px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
    }}
    h1 {{
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      color: var(--steel);
    }}
    .toolbar {{ display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }}
    button, select, textarea, input {{
      font: inherit;
    }}
    button {{
      border: 1px solid var(--steel);
      background: var(--panel);
      color: var(--steel);
      padding: 7px 10px;
      border-radius: 6px;
      cursor: pointer;
    }}
    button:hover {{ background: #eef3f5; }}
    button.primary {{ background: var(--steel); color: white; }}
    main {{
      display: grid;
      grid-template-columns: minmax(360px, 42vw) 1fr;
      gap: 14px;
      padding: 14px;
      min-height: calc(100vh - 58px);
    }}
    .pane {{
      background: rgba(255, 253, 248, .94);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-width: 0;
      overflow: hidden;
    }}
    .left {{ display: grid; grid-template-rows: auto 1fr auto; max-height: calc(100vh - 86px); }}
    .case-nav {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 8px;
      padding: 12px;
      border-bottom: 1px solid var(--line);
      align-items: center;
    }}
    .case-select {{ width: 100%; padding: 7px 8px; border: 1px solid var(--line); border-radius: 6px; background: white; }}
    .pending {{
      padding: 14px;
      overflow: auto;
    }}
    .meta {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 10px;
      color: var(--muted);
      font-size: 12px;
    }}
    .tag {{
      border: 1px solid var(--line);
      background: #f6f8fa;
      padding: 3px 7px;
      border-radius: 999px;
    }}
    .content-box {{
      white-space: pre-wrap;
      line-height: 1.72;
      font-size: 14px;
      border-left: 4px solid var(--steel);
      background: #fff;
      padding: 12px;
      border-radius: 6px;
    }}
    .case-footer {{
      border-top: 1px solid var(--line);
      padding: 12px;
      background: #faf8f2;
      display: grid;
      gap: 9px;
    }}
    .label-row {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    .label-row select {{ padding: 7px 8px; border: 1px solid var(--line); border-radius: 6px; background: white; }}
    textarea {{
      width: 100%;
      min-height: 70px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      resize: vertical;
      background: white;
    }}
    .right {{
      max-height: calc(100vh - 86px);
      overflow: auto;
    }}
    .candidate {{
      border-bottom: 1px solid var(--line);
      padding: 12px 14px;
      background: rgba(255,255,255,.72);
    }}
    .candidate:last-child {{ border-bottom: 0; }}
    .candidate-head {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: start;
    }}
    .candidate-title {{
      font-weight: 700;
      color: var(--steel);
      line-height: 1.35;
    }}
    .reason {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
    }}
    .vote {{
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }}
    .vote button {{
      padding: 5px 8px;
      border-color: var(--line);
      color: var(--ink);
      background: #fff;
    }}
    .vote button.active[data-label="useful"] {{ background: var(--green); color: white; border-color: var(--green); }}
    .vote button.active[data-label="useless"] {{ background: var(--red); color: white; border-color: var(--red); }}
    .vote button.active[data-label="uncertain"] {{ background: var(--amber); color: white; border-color: var(--amber); }}
    details {{
      margin-top: 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
    }}
    summary {{
      cursor: pointer;
      padding: 8px 10px;
      color: var(--blue);
      font-weight: 600;
    }}
    .kb-content {{
      white-space: pre-wrap;
      padding: 0 10px 10px 10px;
      line-height: 1.68;
      font-size: 13px;
      max-height: 260px;
      overflow: auto;
    }}
    .candidate-note {{
      margin-top: 8px;
      min-height: 38px;
    }}
    .progress {{
      font-size: 13px;
      color: var(--muted);
      margin-left: 10px;
    }}
    @media (max-width: 980px) {{
      main {{ grid-template-columns: 1fr; }}
      .left, .right {{ max-height: none; }}
      header {{ grid-template-columns: 1fr; }}
      .toolbar {{ justify-content: flex-start; }}
    }}
  </style>
</head>
<body>
  <header>
    <div>
      <h1>RAG 法规证据标注台 <span id="progress" class="progress"></span></h1>
    </div>
    <div class="toolbar">
      <button id="prevBtn">上一条</button>
      <button id="nextBtn">下一条</button>
      <button id="exportBtn" class="primary">导出标注 JSON</button>
      <button id="importBtn">导入检查点</button>
      <input id="importFile" type="file" accept=".json,application/json" hidden>
      <button id="clearBtn">清空本地标注</button>
      <span id="saveStatus" class="progress"></span>
    </div>
  </header>
  <main>
    <section class="pane left">
      <div class="case-nav">
        <button id="prevBtn2">←</button>
        <select id="caseSelect" class="case-select"></select>
        <button id="nextBtn2">→</button>
      </div>
      <div id="pendingPane" class="pending"></div>
      <div class="case-footer">
        <div class="label-row">
          <strong>最终标签</strong>
          <select id="finalLabel">
            <option value="unlabeled">未标注</option>
            <option value="compliant">合规</option>
            <option value="noncompliant">不合规</option>
            <option value="uncertain">不确定</option>
            <option value="non_regulatory">非法规问题/OCR问题</option>
          </select>
          <label><input type="checkbox" id="missingEvidence"> 候选中没有正确法规</label>
          <label><input type="checkbox" id="notSuitable"> 不适合作为评测样本</label>
        </div>
        <textarea id="humanNote" placeholder="人工备注：为什么这样标，是否需要补充法规 quote。"></textarea>
      </div>
    </section>
    <section id="candidatePane" class="pane right"></section>
  </main>
  <script>
    const CASES = __CASES_JSON__;
    const STORAGE_KEY = "__STORAGE_KEY__";
    const LAST_SAVED_KEY = STORAGE_KEY + "_last_saved";
    let state = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{{}}");
    let current = 0;

    function escapeHtml(value) {{
      return String(value ?? "").replace(/[&<>"']/g, ch => ({{
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      }}[ch]));
    }}

    function caseState(caseId) {{
      if (!state[caseId]) {{
        state[caseId] = {{
          final_label: "unlabeled",
          missing_correct_evidence: false,
          not_eval_suitable: false,
          human_note: "",
          candidate_labels: {{}}
        }};
      }}
      return state[caseId];
    }}

    function saveState() {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      localStorage.setItem(LAST_SAVED_KEY, new Date().toISOString());
      updateProgress();
    }}

    function updateProgress() {{
      const done = CASES.filter(c => caseState(c.case_id).final_label !== "unlabeled").length;
      document.getElementById("progress").textContent = `${{done}} / ${{CASES.length}} 已标最终标签`;
      const savedAt = localStorage.getItem(LAST_SAVED_KEY);
      document.getElementById("saveStatus").textContent = savedAt
        ? `已自动保存：${{new Date(savedAt).toLocaleString()}}`
        : "尚未产生本地标注";
    }}

    function renderCaseOptions() {{
      const select = document.getElementById("caseSelect");
      select.innerHTML = CASES.map((c, idx) => {{
        const p = c.pending;
        const label = `${{idx + 1}}. ${{c.case_id}} | ${{p.doc_name.slice(0, 3)}} chunk${{p.chunk_no}} | ${{c.topic || c.sample_source}}`;
        return `<option value="${{idx}}">${{escapeHtml(label)}}</option>`;
      }}).join("");
      select.value = current;
    }}

    function render() {{
      const c = CASES[current];
      const cs = caseState(c.case_id);
      document.getElementById("caseSelect").value = current;
      document.getElementById("finalLabel").value = cs.final_label || "unlabeled";
      document.getElementById("missingEvidence").checked = !!cs.missing_correct_evidence;
      document.getElementById("notSuitable").checked = !!cs.not_eval_suitable;
      document.getElementById("humanNote").value = cs.human_note || "";

      const p = c.pending;
      document.getElementById("pendingPane").innerHTML = `
        <div class="meta">
          <span class="tag">${{escapeHtml(c.sample_source)}}</span>
          <span class="tag">${{escapeHtml(p.doc_name)}}</span>
          <span class="tag">chunk ${{p.chunk_no}}</span>
          <span class="tag">页码 ${{escapeHtml(p.page_range || "-")}}</span>
          <span class="tag">${{escapeHtml(p.chunk_level || "-")}}</span>
        </div>
        <div class="meta">
          <span class="tag">${{escapeHtml(p.chapter || "-")}}</span>
          <span class="tag">${{escapeHtml(p.section || "-")}}</span>
        </div>
        <p><strong>样本主题：</strong>${{escapeHtml(c.topic || "")}}</p>
        <p><strong>来源说明：</strong>${{escapeHtml(c.source_note || "")}}</p>
        <div class="content-box">${{escapeHtml(p.content)}}</div>
      `;

      document.getElementById("candidatePane").innerHTML = c.candidates.map(kb => {{
        const label = (cs.candidate_labels[kb.chunk_id] || {{label: "unlabeled", note: ""}});
        return `
          <article class="candidate">
            <div class="candidate-head">
              <div>
                <div class="candidate-title">#${{kb.rank}} ${{escapeHtml(kb.doc_name)}} · chunk ${{Number(kb.kb_chunk_index) + 1}}</div>
                <div class="meta">
                  <span class="tag">${{escapeHtml(kb.chapter || "-")}}</span>
                  <span class="tag">${{escapeHtml(kb.section || "-")}}</span>
                  <span class="tag">页码 ${{escapeHtml(kb.page_range || "-")}}</span>
                </div>
                <div class="reason">${{escapeHtml(kb.reason)}}</div>
              </div>
              <div class="vote" data-chunk-id="${{escapeHtml(kb.chunk_id)}}">
                <button data-label="useful" class="${{label.label === "useful" ? "active" : ""}}">有用</button>
                <button data-label="useless" class="${{label.label === "useless" ? "active" : ""}}">无用</button>
                <button data-label="uncertain" class="${{label.label === "uncertain" ? "active" : ""}}">不确定</button>
              </div>
            </div>
            <details>
              <summary>查看法规 chunk 全文</summary>
              <div class="kb-content">${{escapeHtml(kb.content)}}</div>
            </details>
            <textarea class="candidate-note" data-note-id="${{escapeHtml(kb.chunk_id)}}" placeholder="候选法规备注，可选。">${{escapeHtml(label.note || "")}}</textarea>
          </article>
        `;
      }}).join("");
      bindCandidateEvents();
      updateProgress();
    }}

    function bindCandidateEvents() {{
      document.querySelectorAll(".vote button").forEach(btn => {{
        btn.addEventListener("click", () => {{
          const chunkId = btn.parentElement.dataset.chunkId;
          const label = btn.dataset.label;
          const cs = caseState(CASES[current].case_id);
          const old = cs.candidate_labels[chunkId] || {{label: "unlabeled", note: ""}};
          cs.candidate_labels[chunkId] = {{...old, label}};
          saveState();
          render();
        }});
      }});
      document.querySelectorAll("[data-note-id]").forEach(area => {{
        area.addEventListener("input", () => {{
          const chunkId = area.dataset.noteId;
          const cs = caseState(CASES[current].case_id);
          const old = cs.candidate_labels[chunkId] || {{label: "unlabeled", note: ""}};
          cs.candidate_labels[chunkId] = {{...old, note: area.value}};
          saveState();
        }});
      }});
    }}

    function go(delta) {{
      current = Math.max(0, Math.min(CASES.length - 1, current + delta));
      render();
    }}

    function exportAnnotations() {{
      const payload = {{
        exported_at: new Date().toISOString(),
        cases: CASES.map(c => {{
          const cs = caseState(c.case_id);
          return {{
            case_id: c.case_id,
            sample_source: c.sample_source,
            topic: c.topic,
            pending: c.pending,
            final_label: cs.final_label,
            missing_correct_evidence: !!cs.missing_correct_evidence,
            not_eval_suitable: !!cs.not_eval_suitable,
            human_note: cs.human_note || "",
            candidate_annotations: c.candidates.map(kb => ({{
              chunk_id: kb.chunk_id,
              doc_name: kb.doc_name,
              kb_chunk_index: kb.kb_chunk_index,
              rank: kb.rank,
              score: kb.score,
              label: (cs.candidate_labels[kb.chunk_id] || {{label: "unlabeled"}}).label,
              note: (cs.candidate_labels[kb.chunk_id] || {{note: ""}}).note || "",
              anchor_quote: kb.anchor_quote
            }}))
          }};
        }})
      }};
      const blob = new Blob([JSON.stringify(payload, null, 2)], {{type: "application/json"}});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `rag_annotations_${{new Date().toISOString().slice(0, 10)}}.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    }}

    async function importAnnotations(file) {{
      const payload = JSON.parse(await file.text());
      if (!Array.isArray(payload.cases)) {{
        throw new Error("检查点 JSON 必须包含 cases 数组。");
      }}

      const validCaseIds = new Set(CASES.map(c => c.case_id));
      let imported = 0;
      for (const item of payload.cases) {{
        if (!item || !validCaseIds.has(item.case_id)) continue;
        const candidateLabels = {{}};
        for (const annotation of item.candidate_annotations || []) {{
          if (!annotation || !annotation.chunk_id) continue;
          candidateLabels[annotation.chunk_id] = {{
            label: annotation.label || "unlabeled",
            note: annotation.note || ""
          }};
        }}
        state[item.case_id] = {{
          final_label: item.final_label || "unlabeled",
          missing_correct_evidence: !!item.missing_correct_evidence,
          not_eval_suitable: !!item.not_eval_suitable,
          human_note: item.human_note || item.final_reason || "",
          candidate_labels: candidateLabels
        }};
        imported += 1;
      }}
      saveState();
      render();
      alert(`已导入 ${{imported}} 个匹配 case，并保存到当前浏览器。`);
    }}

    document.getElementById("caseSelect").addEventListener("change", e => {{
      current = Number(e.target.value);
      render();
    }});
    for (const id of ["prevBtn", "prevBtn2"]) document.getElementById(id).addEventListener("click", () => go(-1));
    for (const id of ["nextBtn", "nextBtn2"]) document.getElementById(id).addEventListener("click", () => go(1));
    document.getElementById("exportBtn").addEventListener("click", exportAnnotations);
    document.getElementById("importBtn").addEventListener("click", () => {{
      document.getElementById("importFile").click();
    }});
    document.getElementById("importFile").addEventListener("change", async e => {{
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      try {{
        await importAnnotations(file);
      }} catch (error) {{
        alert(`导入失败：${{error.message || error}}`);
      }} finally {{
        e.target.value = "";
      }}
    }});
    document.getElementById("clearBtn").addEventListener("click", () => {{
      if (confirm("确定清空当前浏览器中的本地标注吗？")) {{
        state = {{}};
        saveState();
        render();
      }}
    }});
    document.getElementById("finalLabel").addEventListener("change", e => {{
      caseState(CASES[current].case_id).final_label = e.target.value;
      saveState();
    }});
    document.getElementById("missingEvidence").addEventListener("change", e => {{
      caseState(CASES[current].case_id).missing_correct_evidence = e.target.checked;
      saveState();
    }});
    document.getElementById("notSuitable").addEventListener("change", e => {{
      caseState(CASES[current].case_id).not_eval_suitable = e.target.checked;
      saveState();
    }});
    document.getElementById("humanNote").addEventListener("input", e => {{
      caseState(CASES[current].case_id).human_note = e.target.value;
      saveState();
    }});
    window.addEventListener("beforeunload", saveState);

    renderCaseOptions();
    render();
  </script>
</body>
</html>
"""


def write_html(cases: List[Dict[str, Any]], output_html: Path) -> None:
    output_html.parent.mkdir(parents=True, exist_ok=True)
    cases_json = json.dumps(cases, ensure_ascii=False)
    template = HTML_TEMPLATE.replace("{{", "{").replace("}}", "}")
    html_text = template.replace("__CASES_JSON__", cases_json)
    html_text = html_text.replace("__STORAGE_KEY__", "coal_rag_eval_annotations_v1")
    output_html.write_text(html_text, encoding="utf-8")


def write_cases_json(cases: List[Dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({
            "schema": "coal_rag_annotation_cases_v1",
            "case_count": len(cases),
            "cases": cases,
        }, f, ensure_ascii=False, indent=2)


def build_cases(args: argparse.Namespace) -> List[Dict[str, Any]]:
    pending_data = load_json(args.pending_json)
    kb_data = load_json(args.kb_json)
    kb_chunks = flatten_kb(kb_data)
    retriever = BM25Retriever(kb_chunks)

    seeds = parse_v5_seeds(args.comparison_md, args.comparison_cases)
    seed_cases = build_seed_cases(
        pending_data=pending_data,
        seeds=seeds,
        retriever=retriever,
        candidates_per_case=args.candidates_per_case,
    )
    random_needed = max(0, args.total_cases - len(seed_cases))
    random_cases = build_random_cases(
        pending_data=pending_data,
        existing_cases=seed_cases,
        count=random_needed,
        retriever=retriever,
        candidates_per_case=args.candidates_per_case,
        seed=args.random_seed,
    )
    return (seed_cases + random_cases)[: args.total_cases]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build RAG evidence annotation cases and a static HTML app.")
    parser.add_argument("--pending-json", type=Path, default=DEFAULT_PENDING_JSON)
    parser.add_argument("--kb-json", type=Path, default=DEFAULT_KB_JSON)
    parser.add_argument("--comparison-md", type=Path, default=DEFAULT_COMPARISON_MD)
    parser.add_argument("--total-cases", type=int, default=50)
    parser.add_argument("--comparison-cases", type=int, default=30)
    parser.add_argument("--candidates-per-case", type=int, default=15)
    parser.add_argument("--random-seed", type=int, default=20260421)
    parser.add_argument("--cases-json", type=Path, default=DEFAULT_CASES_JSON)
    parser.add_argument("--output-html", type=Path, default=DEFAULT_HTML)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cases = build_cases(args)
    write_cases_json(cases, args.cases_json)
    write_html(cases, args.output_html)
    print(f"Wrote {len(cases)} cases: {args.cases_json}")
    print(f"Wrote annotation app: {args.output_html}")


if __name__ == "__main__":
    main()
