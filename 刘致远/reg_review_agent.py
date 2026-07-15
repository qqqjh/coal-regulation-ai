"""
单智能体规程审查工具
- 加载待审查文档 chunks（test_doc_chunks_visualization/）
- 加载规程参照 chunks（chunks_visualization/）
- 基于 jieba 关键词检索相关规程条款
- 调用 Qwen API 逐块进行合规审查
- 生成 HTML 审查报告
"""

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from openai import OpenAI
import jieba
import jieba.analyse

# ── API 配置 ──────────────────────────────────────────
API_KEY  = os.getenv("DASHSCOPE_API_KEY")
API_BASE = os.getenv("DASHSCOPE_BASE_URL")
MODEL    = "qwen-plus"

# ── 路径配置 ──────────────────────────────────────────
REG_CHUNKS_DIR      = Path("chunks_visualization")
TEST_CHUNKS_DIR     = Path("test_doc_chunks_visualization")
OUTPUT_DIR          = Path("reg_review_output")

TOP_K_REGS = 5        # 每个 chunk 检索的规程条款数
MAX_CHUNKS  = None    # None = 处理全部；设置整数可限制数量（调试用）


# ═══════════════════════════════════════════════════════
# 1. 加载 chunks
# ═══════════════════════════════════════════════════════

def load_latest_chunks(directory: Path) -> dict:
    """加载目录中最新的 chunks JSON 文件"""
    files = sorted(directory.glob("chunks_*.json"))
    if not files:
        raise FileNotFoundError(f"未找到 chunks JSON 文件：{directory}")
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    all_chunks = []
    for doc_name, chunks in data.items():
        for i, chunk in enumerate(chunks):
            chunk = dict(chunk)
            chunk["doc_name"] = doc_name
            chunk["chunk_id"] = f"{doc_name}__{chunk.get('chapter','')}_{chunk.get('section','')}_{i}"
            all_chunks.append(chunk)
    print(f"  加载 {len(all_chunks)} 个 chunks  ←  {files[-1].name}")
    return data, all_chunks


# ═══════════════════════════════════════════════════════
# 2. 关键词检索
# ═══════════════════════════════════════════════════════

def extract_keywords(text: str, topK: int = 15) -> list[str]:
    return jieba.analyse.extract_tags(text, topK=topK)


def build_reg_index(reg_chunks: list) -> list:
    """为每个规程 chunk 预提取关键词"""
    for chunk in reg_chunks:
        chunk["_keywords"] = set(extract_keywords(chunk["content"], topK=20))
    return reg_chunks


def retrieve_top_k(test_chunk: dict, reg_chunks: list, top_k: int = TOP_K_REGS) -> list:
    """基于关键词重叠度检索最相关的规程 chunks"""
    test_kws = set(extract_keywords(test_chunk["content"], topK=15))
    if not test_kws:
        return reg_chunks[:top_k]

    scored = []
    for rc in reg_chunks:
        overlap = len(test_kws & rc.get("_keywords", set()))
        if overlap > 0:
            scored.append((overlap, rc))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [rc for _, rc in scored[:top_k]]


# ═══════════════════════════════════════════════════════
# 3. LLM 审查
# ═══════════════════════════════════════════════════════

REVIEW_PROMPT = """你是煤矿作业规程审查专家，请审查以下【作业规程片段】是否符合【安全规程条款】的要求。

【作业规程片段】
文档：{doc_name}
章节：{chapter} / {section}
内容：
{content}

【相关安全规程条款（供参考）】
{reg_refs}

审查重点：
1. 参数数值（风速、风量、支护间距、瓦斯浓度阈值等）是否符合规程下限/上限要求
2. 是否缺失规程要求必须具备的安全措施或操作步骤
3. 表述是否与规程条款产生矛盾
4. 操作程序是否完整合规

输出格式（严格 JSON 数组，不含任何 Markdown 代码块标记）：
若发现问题，返回：
[
  {{
    "severity": "严重|警告|建议",
    "issue_type": "参数违规|缺失条款|表述不当|程序缺失",
    "description": "问题的具体描述（中文，100字以内）",
    "regulation_ref": "所依据的规程名称及条款编号",
    "original_text": "作业规程中有问题的原文片段（30字以内）",
    "suggested_fix": "修正建议（中文，80字以内）"
  }}
]
若无问题，返回：[]
"""


def format_reg_refs(reg_chunks: list) -> str:
    parts = []
    for i, rc in enumerate(reg_chunks, 1):
        doc  = rc.get("doc_name", "")
        ch   = rc.get("chapter", "")
        sec  = rc.get("section", "")
        text = rc.get("content", "")[:300]
        parts.append(f"[{i}] 来源：{doc} / {ch} {sec}\n{text}")
    return "\n\n".join(parts)


def review_chunk(client: OpenAI, test_chunk: dict, reg_chunks: list) -> list:
    """调用 LLM 审查单个 chunk，返回问题列表"""
    prompt = REVIEW_PROMPT.format(
        doc_name  = test_chunk.get("doc_name", ""),
        chapter   = test_chunk.get("chapter", ""),
        section   = test_chunk.get("section", ""),
        content   = test_chunk.get("content", "")[:800],
        reg_refs  = format_reg_refs(reg_chunks),
    )

    try:
        resp = client.chat.completions.create(
            model    = MODEL,
            messages = [{"role": "user", "content": prompt}],
            temperature = 0.1,
            max_tokens  = 1024,
        )
        raw = resp.choices[0].message.content.strip()
        # 清理可能的 markdown 代码块
        raw = re.sub(r"^```[a-z]*\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        issues = json.loads(raw)
        if not isinstance(issues, list):
            issues = []
        return issues
    except json.JSONDecodeError:
        return []
    except Exception as e:
        print(f"    [警告] LLM 调用失败: {e}")
        return []


# ═══════════════════════════════════════════════════════
# 4. 报告生成
# ═══════════════════════════════════════════════════════

SEVERITY_COLOR = {"严重": "#f44336", "警告": "#ff9800", "建议": "#2196F3"}
SEVERITY_RANK  = {"严重": 0, "警告": 1, "建议": 2}


def generate_html_report(review_results: list, output_path: Path):
    """生成 HTML 审查报告"""
    total_issues = sum(len(r["issues"]) for r in review_results)
    severe = sum(1 for r in review_results for i in r["issues"] if i.get("severity") == "严重")
    warning = sum(1 for r in review_results for i in r["issues"] if i.get("severity") == "警告")
    suggest = sum(1 for r in review_results for i in r["issues"] if i.get("severity") == "建议")

    chunks_html = ""
    issue_idx = 0
    for r in review_results:
        chunk = r["chunk"]
        issues = r["issues"]
        if not issues:
            continue
        ch  = chunk.get("chapter", "")
        sec = chunk.get("section", "")
        pg  = chunk.get("page_range", "")
        content_preview = chunk.get("content", "")[:200].replace("<", "&lt;").replace(">", "&gt;")

        issues_html = ""
        for issue in sorted(issues, key=lambda x: SEVERITY_RANK.get(x.get("severity", "建议"), 2)):
            issue_idx += 1
            sev   = issue.get("severity", "建议")
            color = SEVERITY_COLOR.get(sev, "#999")
            itype = issue.get("issue_type", "")
            desc  = issue.get("description", "").replace("<", "&lt;")
            ref   = issue.get("regulation_ref", "").replace("<", "&lt;")
            orig  = issue.get("original_text", "").replace("<", "&lt;")
            fix   = issue.get("suggested_fix", "").replace("<", "&lt;")
            issues_html += f"""
            <div class="issue" style="border-left:4px solid {color}">
              <div class="issue-header">
                <span class="badge" style="background:{color}">{sev}</span>
                <span class="itype">{itype}</span>
                <span class="issue-no">#{issue_idx}</span>
              </div>
              <div class="issue-row"><b>问题描述：</b>{desc}</div>
              <div class="issue-row"><b>规程依据：</b>{ref}</div>
              {"<div class='issue-row'><b>原文片段：</b><code>" + orig + "</code></div>" if orig else ""}
              <div class="issue-row"><b>修正建议：</b>{fix}</div>
            </div>"""

        chunks_html += f"""
        <div class="chunk-block">
          <div class="chunk-meta">
            <b>{ch}</b>{' / ' + sec if sec else ''}
            <span class="pg">页码 {pg}</span>
            <span class="issue-count" style="color:{SEVERITY_COLOR.get(issues[0].get('severity','建议'),'#999') if issues else '#999'}">
              {len(issues)} 个问题
            </span>
          </div>
          <div class="chunk-text">{content_preview}{'...' if len(chunk.get('content',''))>200 else ''}</div>
          {issues_html}
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>规程审查报告</title>
<style>
  body {{ font-family: "Microsoft YaHei", Arial, sans-serif; margin:0; background:#f0f2f5; }}
  .header {{ background:#1a237e; color:white; padding:24px 40px; }}
  .header h1 {{ margin:0 0 4px; font-size:1.6em; }}
  .header p {{ margin:0; opacity:.8; font-size:.9em; }}
  .container {{ max-width:1100px; margin:24px auto; padding:0 20px; }}
  .stat-row {{ display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }}
  .stat-card {{ flex:1; min-width:160px; background:white; border-radius:8px; padding:20px;
                text-align:center; box-shadow:0 1px 4px rgba(0,0,0,.1); }}
  .stat-num {{ font-size:2.2em; font-weight:bold; }}
  .stat-label {{ color:#666; margin-top:4px; font-size:.9em; }}
  .doc-section {{ background:white; border-radius:8px; margin-bottom:24px;
                  box-shadow:0 1px 4px rgba(0,0,0,.1); overflow:hidden; }}
  .doc-title {{ background:#283593; color:white; padding:12px 20px; font-size:1.05em; font-weight:bold; }}
  .chunk-block {{ border-bottom:1px solid #eee; padding:16px 20px; }}
  .chunk-block:last-child {{ border-bottom:none; }}
  .chunk-meta {{ font-size:.95em; color:#444; margin-bottom:8px; }}
  .pg {{ color:#888; font-size:.85em; margin-left:12px; }}
  .issue-count {{ font-size:.85em; margin-left:12px; font-weight:bold; }}
  .chunk-text {{ background:#f9f9f9; border-radius:4px; padding:8px 12px; font-size:.88em;
                 color:#555; margin-bottom:10px; white-space:pre-wrap; line-height:1.6; }}
  .issue {{ margin:8px 0; padding:10px 14px; background:#fafafa; border-radius:4px; }}
  .issue-header {{ margin-bottom:6px; display:flex; align-items:center; gap:8px; }}
  .badge {{ color:white; border-radius:3px; padding:2px 8px; font-size:.82em; font-weight:bold; }}
  .itype {{ color:#555; font-size:.88em; }}
  .issue-no {{ margin-left:auto; color:#aaa; font-size:.8em; }}
  .issue-row {{ font-size:.88em; color:#444; margin:3px 0; line-height:1.5; }}
  code {{ background:#fff3e0; padding:1px 5px; border-radius:3px; font-size:.88em; }}
  .no-issues {{ padding:24px; text-align:center; color:#888; }}
</style>
</head>
<body>
<div class="header">
  <h1>煤矿作业规程合规审查报告</h1>
  <p>生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} &nbsp;|&nbsp; 模型：{MODEL}</p>
</div>
<div class="container">
  <div class="stat-row">
    <div class="stat-card">
      <div class="stat-num" style="color:#333">{total_issues}</div>
      <div class="stat-label">发现问题总数</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#f44336">{severe}</div>
      <div class="stat-label">严重</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#ff9800">{warning}</div>
      <div class="stat-label">警告</div>
    </div>
    <div class="stat-card">
      <div class="stat-num" style="color:#2196F3">{suggest}</div>
      <div class="stat-label">建议</div>
    </div>
  </div>
  {'<div class="doc-section"><div class="doc-title">审查结果</div>' + chunks_html + '</div>'
   if chunks_html else '<div class="no-issues">✓ 未发现合规问题</div>'}
</div>
</body>
</html>"""

    output_path.write_text(html, encoding="utf-8")
    print(f"  [OK] HTML 报告：{output_path}")


def save_json_report(review_results: list, output_path: Path):
    output_path.write_text(
        json.dumps(review_results, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"  [OK] JSON 报告：{output_path}")


# ═══════════════════════════════════════════════════════
# 5. 主流程
# ═══════════════════════════════════════════════════════

def main(target_doc: str = None):
    print("=" * 60)
    print("规程审查智能体")
    print("=" * 60)

    OUTPUT_DIR.mkdir(exist_ok=True)

    # 加载规程 chunks
    print("\n[1] 加载安全规程知识库...")
    _, reg_chunks = load_latest_chunks(REG_CHUNKS_DIR)
    reg_chunks = build_reg_index(reg_chunks)
    print(f"  规程 chunks 已建立关键词索引")

    # 加载待审查文档 chunks
    print("\n[2] 加载待审查文档...")
    test_data, test_chunks = load_latest_chunks(TEST_CHUNKS_DIR)

    # 可选：只处理指定文档
    if target_doc:
        test_chunks = [c for c in test_chunks if target_doc in c.get("doc_name", "")]
        print(f"  筛选后待审查 chunks：{len(test_chunks)} 个（匹配：{target_doc}）")

    if MAX_CHUNKS:
        test_chunks = test_chunks[:MAX_CHUNKS]
        print(f"  [调试模式] 仅处理前 {MAX_CHUNKS} 个 chunks")

    # 初始化 Qwen 客户端
    if not API_KEY:
        raise RuntimeError("Missing environment variable: DASHSCOPE_API_KEY")
    if not API_BASE:
        raise RuntimeError("Missing environment variable: DASHSCOPE_BASE_URL")
    client = OpenAI(api_key=API_KEY, base_url=API_BASE)

    # 逐块审查
    print(f"\n[3] 开始审查（共 {len(test_chunks)} 个 chunks）...")
    review_results = []
    issue_count = 0

    for i, test_chunk in enumerate(test_chunks, 1):
        ch  = test_chunk.get("chapter", "")[:20]
        sec = test_chunk.get("section", "")[:20]
        print(f"  [{i}/{len(test_chunks)}] {ch} {sec} ...", end="", flush=True)

        # 跳过内容过短的块（< 50 字，无实质内容）
        if len(test_chunk.get("content", "")) < 50:
            print(" 跳过（内容过短）")
            continue

        # 检索相关规程
        relevant_regs = retrieve_top_k(test_chunk, reg_chunks, top_k=TOP_K_REGS)

        # LLM 审查
        issues = review_chunk(client, test_chunk, relevant_regs)
        issue_count += len(issues)

        review_results.append({
            "chunk": {k: v for k, v in test_chunk.items() if not k.startswith("_")},
            "relevant_regs": [{"doc_name": r.get("doc_name"), "chapter": r.get("chapter"),
                                "section": r.get("section"), "content": r.get("content", "")[:200]}
                               for r in relevant_regs],
            "issues": issues,
        })

        print(f" {len(issues)} 个问题")
        time.sleep(0.3)   # 避免触发限流

    # 生成报告
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n[4] 生成报告（共发现 {issue_count} 个问题）...")
    generate_html_report(review_results, OUTPUT_DIR / f"review_report_{ts}.html")
    save_json_report(review_results, OUTPUT_DIR / f"review_report_{ts}.json")

    print("\n" + "=" * 60)
    print(f"[完成] 审查结束，结果保存至 {OUTPUT_DIR}/")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    # 用法：python reg_review_agent.py [文档关键词]
    # 示例：python reg_review_agent.py S1302
    target = sys.argv[1] if len(sys.argv) > 1 else None
    main(target_doc=target)
