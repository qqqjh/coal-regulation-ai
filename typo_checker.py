"""
煤矿作业规程错别字审查系统
- 单智能体，无需RAG
- 逐chunk调用LLM检查错别字、别字、漏字、多字、词语混淆
- 生成HTML报告
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime

from openai import OpenAI

# ============ 配置 ============
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen-plus"

PENDING_CHUNKS_DIR = Path("chunks_visualization")
OUTPUT_DIR = Path("review_results")
DOC_FILTER = []  # 空列表 = 检查全部文档


class TypoChecker:
    def __init__(self):
        if not API_KEY:
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
        self.llm_client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

    def check_chunk(self, content: str) -> Dict:
        """对单个chunk进行错别字检查，返回发现的问题列表"""
        prompt = f"""请对下方煤矿作业规程文本进行错别字审查。

【待审文本】
{content}

请以JSON格式输出，若无问题则issues为空列表：
{{
    "has_issues": true/false,
    "issues": [
        {{
            "original": "原文中有问题的词/字（含前后各2个字的上下文）",
            "error_type": "错别字/术语错误",
            "correction": "建议修改为",
            "explanation": "说明原因（15字以内）"
        }}
    ]
}}"""

        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": """你是一名专业的煤矿安全文档审校专家，负责检查作业规程中的错别字。

检查范围（只报告以下两类，其余一律不报）：
1. 错别字：汉字字形写错，如"既"与"即"、"做"与"作"、"在"与"再"混用
2. 术语错误：煤矿专业术语的汉字字形写错，如"综采"误写为"综彩"

不检查的内容（以下情况一律不报）：
- 标点符号、格式、排版问题
- 数字、单位数值（单位符号错误由规程审查负责，此处不报）
- 纯符号数学公式（如仅由字母、数字、运算符、括号组成的表达式，不含汉字）
- 单位格式问题：上标缺失（"m 2"）、单位字母拆分（"m i n"）、斜杠截断（"m 3 /"）等
- 语法不通顺但无错字的句子
- 专有名词、地名、人名的用字习惯差异
- 一切简称、缩写、文档内自定义简写（不论是否"行业通用"，简称不是错别字）
- 《》书名号或引号内的文本（文件名、规程名常用简称）
- 漏字/补字建议（如"展皮带"→"展放皮带"，不报）
- 同义词/近义词替换（两个词本身都是正确汉语词汇，仅措辞风格不同，不报；如"死者"与"遇难者"）
- 以下 OCR 扫描噪声，一律不报：
  * 型号/规格中多余的符号或空格（如"MD155-. 30×4"中的"-."）
  * 编号中字母与数字形近的OCR误读（如"S↔5"、"O↔0"）
  * 文本末尾孤立的单个字母（如"200 mm c"末尾的"c"）
  * 重复的间隔符（如"··"）

审查原则：
- 只报告汉字字形写错的情况，不做措辞建议、补字建议或风格改写
- 煤矿专业术语存疑时仍可上报，但在 explanation 中注明"不确定是否为专业术语，建议人工核实"
- 每个错别字单独报告，不合并
- 严格按JSON格式输出"""},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            result_text = response.choices[0].message.content
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if json_match:
                return json.loads(json_match.group())
            return {"has_issues": False, "issues": []}
        except Exception as e:
            print(f"    检查失败: {e}")
            return {"has_issues": False, "issues": [], "error": str(e)}

    def check_document(self, pending_json_path: str = None,
                       doc_filter: List[str] = None) -> Dict:
        if pending_json_path is None:
            for version in ['v5', 'v4', 'v3', 'v2']:
                files = sorted(PENDING_CHUNKS_DIR.glob(f"pending_doc_chunks_{version}_*.json"), reverse=True)
                if files:
                    pending_json_path = files[0]
                    break
            if pending_json_path is None:
                raise FileNotFoundError("未找到待审文档chunks文件")

        print(f"\n加载待审文档: {pending_json_path}")
        with open(pending_json_path, 'r', encoding='utf-8') as f:
            pending_data = json.load(f)

        if doc_filter is None:
            doc_filter = DOC_FILTER
        if doc_filter:
            pending_data = {
                k: v for k, v in pending_data.items()
                if any(f in k for f in doc_filter)
            }

        results = {}
        total = sum(len(chunks) for chunks in pending_data.values())
        processed = 0

        for doc_name, chunks in pending_data.items():
            print(f"\n文档: {doc_name} ({len(chunks)} chunks)")
            doc_results = []
            for i, chunk in enumerate(chunks):
                processed += 1
                print(f"  [{processed}/{total}] chunk {i+1}/{len(chunks)}...", end=" ", flush=True)
                result = self.check_chunk(chunk['content'])
                issue_count = len(result.get('issues', []))
                print(f"{'发现 ' + str(issue_count) + ' 处问题' if issue_count else '无问题'}")
                doc_results.append({
                    'chunk_index': i,
                    'chunk': chunk,
                    'check_result': result
                })
                time.sleep(0.3)
            results[doc_name] = doc_results

        return results

    # ============ 生成HTML报告 ============

    def generate_report(self, results: Dict) -> str:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = OUTPUT_DIR / f"typo_report_{timestamp}.html"

        # 统计
        total_chunks = 0
        total_issues = 0
        chunks_with_issues = 0
        for doc_results in results.values():
            for r in doc_results:
                total_chunks += 1
                issues = r['check_result'].get('issues', [])
                if issues:
                    chunks_with_issues += 1
                    total_issues += len(issues)

        error_type_colors = {
            '错别字': '#e74c3c',
            '术语错误': '#e67e22',
            '用词错误': '#9b59b6',
        }

        html_parts = [f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>错别字审查报告 {timestamp}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #2c3e50; font-size: 14px; }}
  .header {{ background: #2c3e50; color: white; padding: 20px 30px; }}
  .header h1 {{ font-size: 20px; }}
  .header p {{ margin-top: 6px; color: #bdc3c7; font-size: 13px; }}
  .stats {{ display: flex; gap: 16px; padding: 20px 30px; flex-wrap: wrap; }}
  .stat-card {{ background: white; border-radius: 8px; padding: 14px 20px; min-width: 120px; text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .stat-value {{ font-size: 28px; font-weight: bold; }}
  .stat-label {{ font-size: 12px; color: #7f8c8d; margin-top: 4px; }}
  .stat-card.blue .stat-value {{ color: #2980b9; }}
  .stat-card.red .stat-value {{ color: #e74c3c; }}
  .stat-card.orange .stat-value {{ color: #e67e22; }}
  .stat-card.green .stat-value {{ color: #27ae60; }}
  .doc-section {{ padding: 0 30px 30px; }}
  .doc-title {{ font-size: 16px; font-weight: bold; padding: 16px 0 10px; border-bottom: 2px solid #3498db; margin-bottom: 16px; color: #2c3e50; }}
  .chunk-card {{ background: white; border-radius: 8px; margin-bottom: 12px; box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }}
  .chunk-card.no-issue {{ border-left: 4px solid #27ae60; }}
  .chunk-card.has-issue {{ border-left: 4px solid #e74c3c; }}
  .chunk-header {{ display: flex; align-items: center; gap: 10px; padding: 12px 16px; background: #fafafa; border-bottom: 1px solid #eee; flex-wrap: wrap; }}
  .chunk-id {{ font-weight: bold; font-size: 15px; }}
  .badge {{ padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; color: white; }}
  .badge.green {{ background: #27ae60; }}
  .badge.red {{ background: #e74c3c; }}
  .chunk-meta {{ font-size: 12px; color: #7f8c8d; padding: 8px 16px; }}
  .content-box {{ padding: 10px 16px; max-height: 120px; overflow-y: auto; font-size: 13px; color: #555; line-height: 1.7; background: #f9f9f9; border-bottom: 1px solid #eee; white-space: pre-wrap; }}
  .issues-box {{ padding: 12px 16px; }}
  .issue-item {{ background: #fff5f5; border: 1px solid #fdd; border-radius: 6px; padding: 10px 14px; margin-bottom: 8px; }}
  .issue-item:last-child {{ margin-bottom: 0; }}
  .issue-type-tag {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: bold; color: white; margin-right: 8px; }}
  .issue-row {{ margin-top: 6px; font-size: 13px; line-height: 1.8; }}
  .issue-original {{ color: #c0392b; font-weight: bold; }}
  .issue-arrow {{ color: #7f8c8d; margin: 0 6px; }}
  .issue-correction {{ color: #27ae60; font-weight: bold; }}
  .issue-explanation {{ color: #7f8c8d; font-size: 12px; margin-top: 2px; }}
</style>
</head>
<body>
<div class="header">
  <h1>煤矿作业规程 — 错别字审查报告</h1>
  <p>生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")} | 模型：{QWEN_MODEL}</p>
</div>
<div class="stats">
  <div class="stat-card blue"><div class="stat-value">{total_chunks}</div><div class="stat-label">审查chunks数</div></div>
  <div class="stat-card {'red' if chunks_with_issues else 'green'}"><div class="stat-value">{chunks_with_issues}</div><div class="stat-label">含问题chunks</div></div>
  <div class="stat-card orange"><div class="stat-value">{total_issues}</div><div class="stat-label">发现问题总数</div></div>
  <div class="stat-card green"><div class="stat-value">{total_chunks - chunks_with_issues}</div><div class="stat-label">无问题chunks</div></div>
</div>
"""]

        for doc_name, doc_results in results.items():
            doc_issues = sum(len(r['check_result'].get('issues', [])) for r in doc_results)
            html_parts.append(f'<div class="doc-section">')
            html_parts.append(f'<div class="doc-title">{doc_name}（发现 {doc_issues} 处问题）</div>')

            for r in doc_results:
                chunk = r['chunk']
                check = r['check_result']
                issues = check.get('issues', [])
                has_issue = bool(issues)
                chunk_idx = r['chunk_index'] + 1

                card_class = 'has-issue' if has_issue else 'no-issue'
                badge_class = 'red' if has_issue else 'green'
                badge_text = f'发现 {len(issues)} 处' if has_issue else '无问题'

                meta_parts = []
                if chunk.get('chapter'):
                    meta_parts.append(f"章: {chunk['chapter']}")
                if chunk.get('section'):
                    meta_parts.append(f"节: {chunk['section']}")
                if chunk.get('page_range'):
                    meta_parts.append(f"页码: {chunk['page_range']}")
                meta_str = ' | '.join(meta_parts)

                content_escaped = (chunk.get('content', '')
                                   .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

                html_parts.append(f"""<div class="chunk-card {card_class}">
  <div class="chunk-header">
    <span class="chunk-id">Chunk #{chunk_idx}</span>
    <span class="badge {badge_class}">{badge_text}</span>
  </div>
  <div class="chunk-meta">{meta_str}</div>
  <div class="content-box">{content_escaped}</div>""")

                if has_issue:
                    html_parts.append('<div class="issues-box">')
                    for issue in issues:
                        error_type = issue.get('error_type', '错误')
                        tag_color = error_type_colors.get(error_type, '#7f8c8d')
                        original = (issue.get('original', '') or '').replace('<', '&lt;').replace('>', '&gt;')
                        correction = (issue.get('correction', '') or '').replace('<', '&lt;').replace('>', '&gt;')
                        explanation = (issue.get('explanation', '') or '').replace('<', '&lt;').replace('>', '&gt;')
                        html_parts.append(f"""<div class="issue-item">
    <span class="issue-type-tag" style="background:{tag_color}">{error_type}</span>
    <div class="issue-row">
      <span class="issue-original">「{original}」</span>
      <span class="issue-arrow">→</span>
      <span class="issue-correction">「{correction}」</span>
    </div>
    <div class="issue-explanation">{explanation}</div>
  </div>""")
                    html_parts.append('</div>')

                html_parts.append('</div>')  # chunk-card

            html_parts.append('</div>')  # doc-section

        html_parts.append('</body></html>')

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(html_parts))

        print(f"\n报告已保存: {output_path}")
        return str(output_path)


def main():
    print("=" * 60)
    print("煤矿作业规程错别字审查系统")
    print(f"文档过滤: {DOC_FILTER if DOC_FILTER else '全部'}")
    print("=" * 60)

    checker = TypoChecker()
    results = checker.check_document()

    total = sum(len(v) for v in results.values())
    issues = sum(
        len(r['check_result'].get('issues', []))
        for doc_results in results.values()
        for r in doc_results
    )
    print(f"\n审查完成：{total} 个chunks，发现 {issues} 处问题")

    report_path = checker.generate_report(results)
    print(f"报告: {report_path}")


if __name__ == "__main__":
    main()
