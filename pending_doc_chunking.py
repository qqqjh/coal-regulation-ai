"""
待审文档分块工具
用于对 new_docs/test_doc_json 中的待审文档进行 chunk 切分
复用 chapter_based_chunking_v3.py 的核心逻辑
"""
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime


class PendingDocChunker:
    """待审文档分块器 - 复用 v3 的核心逻辑"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type = None

    def clean_text(self, text: str) -> str:
        """清理文本中的LaTeX格式和多余空格"""
        # LaTeX 命令处理
        text = re.sub(r'\\mathrm\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathbf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathit\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\text\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
        text = re.sub(r'\\mathrm([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathbf([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathit([a-zA-Z]+)', r'\1', text)

        # 特殊符号
        symbols = {
            r'\\delta': 'δ', r'\\Delta': 'Δ', r'\\alpha': 'α', r'\\beta': 'β',
            r'\\gamma': 'γ', r'\\theta': 'θ', r'\\phi': 'φ', r'\\pi': 'π',
            r'\\mu': 'μ', r'\\sigma': 'σ', r'\\lambda': 'λ', r'\\rho': 'ρ',
            r'\\omega': 'ω', r'\\Omega': 'Ω', r'\\epsilon': 'ε',
            r'\\cdot': '·', r'\\times': '×', r'\\div': '÷',
            r'\\leq': '≤', r'\\geq': '≥', r'\\neq': '≠',
            r'\\approx': '≈', r'\\pm': '±', r'\\%': '%'
        }
        for pattern, replacement in symbols.items():
            text = re.sub(pattern, replacement, text)

        text = re.sub(r'\\sim', '~', text)
        text = re.sub(r'\^?\s*\\circ', '°', text)
        text = re.sub(r'[_^]\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\(left|right|quad|qquad|tag|dots?|ldots|cdots)\b\s*', '', text)
        text = re.sub(r'\\_', '_', text)
        text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
        text = re.sub(r'\\(?=[\s\d])', '', text)
        text = re.sub(r'\\$', '', text)
        text = text.replace('~', ' ')

        # 数字和单位格式
        for _ in range(3):
            text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
        text = re.sub(r'(\d)\s*\.\s*(\d)', r'\1.\2', text)
        text = re.sub(r'([a-zA-Z])\s+([a-zA-Z])', r'\1\2', text)
        text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def extract_text_blocks(self) -> List[Dict[str, Any]]:
        """提取所有文本块"""
        blocks = []
        for page_idx, page in enumerate(self.pages):
            para_blocks = page.get('para_blocks', [])
            for block in para_blocks:
                block_type = block.get('type', '')
                if block_type in ['image', 'image_body', 'image_caption']:
                    continue

                if block_type == 'list':
                    for sub_block in block.get('blocks', []):
                        text_parts = []
                        for line in sub_block.get('lines', []):
                            for span in line.get('spans', []):
                                if content := span.get('content', ''):
                                    text_parts.append(content)
                        text = self.clean_text(''.join(text_parts))
                        if text:
                            blocks.append({
                                'page': page_idx + 1,
                                'type': 'list_item',
                                'text': text,
                                'bbox': sub_block.get('bbox', [])
                            })
                    continue

                text_parts = []
                for line in block.get('lines', []):
                    for span in line.get('spans', []):
                        if content := span.get('content', ''):
                            text_parts.append(content)
                text = self.clean_text(''.join(text_parts))
                if text:
                    blocks.append({
                        'page': page_idx + 1,
                        'type': block_type,
                        'text': text,
                        'bbox': block.get('bbox', [])
                    })
        return blocks

    def detect_structure_type(self, blocks: List[Dict]) -> str:
        """检测文档结构类型"""
        patterns = {
            'part': re.compile(r'^第\s*[一二三四五六七八九十百]+\s*编'),
            'chapter': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*章'),
            'section': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*节'),
            'article': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*条'),
            'numeric': re.compile(r'^\d+(\.\d+)*\s')
        }
        found = {k: False for k in patterns}

        for block in blocks[:50]:
            text = block['text']
            for key, pattern in patterns.items():
                if pattern.match(text):
                    found[key] = True

        if found['part'] and found['chapter']:
            return 'part_chapter_section'
        elif found['chapter'] and found['section']:
            return 'chapter_section'
        elif found['numeric']:
            return 'numeric'
        elif found['article']:
            return 'article_only'
        return 'unknown'

    def identify_structure(self, blocks: List[Dict]) -> List[Dict]:
        """识别文档结构"""
        self.doc_structure_type = self.detect_structure_type(blocks)
        print(f"  检测到文档结构类型: {self.doc_structure_type}")

        patterns = {
            'part': re.compile(r'^第\s*[一二三四五六七八九十百]+\s*编'),
            'chapter': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*章'),
            'section': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*节'),
            'article': re.compile(r'^第\s*[一二三四五六七八九十百\d]+\s*条'),
            'numeric': re.compile(r'^(\d+(?:\.\d+)*)\s+(.+)'),
            'appendix': re.compile(r'^([A-Z]\.\d+(?:\.\d+)*)\s+(.+)')
        }

        structured_blocks = []
        for block in blocks:
            text = block['text']
            structure_type = 'content'
            level = 0

            if block['type'] == 'list_item':
                structure_type = 'list_item'
                level = 5
            elif self.doc_structure_type == 'part_chapter_section':
                if patterns['part'].match(text):
                    structure_type, level = 'part', 1
                elif patterns['chapter'].match(text):
                    structure_type, level = 'chapter', 2
                elif patterns['section'].match(text):
                    structure_type, level = 'section', 3
                elif patterns['article'].match(text):
                    structure_type, level = 'article', 4
            elif self.doc_structure_type == 'chapter_section':
                if patterns['chapter'].match(text):
                    structure_type, level = 'chapter', 2
                elif patterns['section'].match(text):
                    structure_type, level = 'section', 3
                elif patterns['article'].match(text):
                    structure_type, level = 'article', 4
            elif self.doc_structure_type == 'numeric':
                if match := patterns['numeric'].match(text):
                    dots = match.group(1).count('.')
                    if dots == 0:
                        structure_type, level = 'chapter', 2
                    elif dots == 1:
                        structure_type, level = 'section', 3
                    else:
                        structure_type, level = 'subsection', 4
                elif patterns['appendix'].match(text):
                    structure_type, level = 'appendix', 3
            elif self.doc_structure_type == 'article_only':
                if patterns['article'].match(text):
                    structure_type, level = 'article', 4
                elif block['type'] == 'title':
                    structure_type, level = 'title', 2

            structured_blocks.append({
                **block,
                'structure_type': structure_type,
                'level': level
            })
        return structured_blocks

    def merge_by_structure(self, structured_blocks: List[Dict]) -> List[Dict]:
        """根据文档结构智能合并chunk"""
        chunks = []
        hierarchy = {'part': '', 'chapter': '', 'section': '', 'article': ''}
        current_content = []
        start_page = 1
        chunk_level = None

        for block in structured_blocks:
            structure_type = block['structure_type']
            level = block['level']
            text = block['text']
            page = block['page']

            if structure_type == 'list_item':
                current_content.append(text)
                continue

            if level > 0:
                if current_content and chunk_level is not None:
                    chunks.append(self._create_chunk(
                        hierarchy, current_content, start_page, page - 1, chunk_level
                    ))
                    current_content = []

                if structure_type == 'part':
                    hierarchy = {'part': text, 'chapter': '', 'section': '', 'article': ''}
                elif structure_type == 'chapter':
                    hierarchy['chapter'] = text
                    hierarchy['section'] = ''
                    hierarchy['article'] = ''
                elif structure_type in ['section', 'subsection', 'appendix']:
                    hierarchy['section'] = text
                    hierarchy['article'] = ''
                elif structure_type == 'article':
                    hierarchy['article'] = text
                elif structure_type == 'title':
                    hierarchy['chapter'] = text

                current_content = [text]
                start_page = page
                chunk_level = structure_type
            else:
                current_content.append(text)

        if current_content:
            last_page = self.pages[-1]['page_idx'] + 1 if self.pages else 1
            chunks.append(self._create_chunk(
                hierarchy, current_content, start_page, last_page, chunk_level
            ))
        return chunks

    def _create_chunk(self, hierarchy: Dict, content: List[str],
                     start_page: int, end_page: int, chunk_level: str) -> Dict:
        """创建chunk对象"""
        return {
            'part': hierarchy.get('part', ''),
            'chapter': hierarchy.get('chapter', ''),
            'section': hierarchy.get('section', ''),
            'article': hierarchy.get('article', ''),
            'content': '\n'.join(content),
            'page_range': f"{start_page}-{end_page}",
            'chunk_level': chunk_level,
            'char_count': sum(len(c) for c in content)
        }

    def process(self) -> List[Dict]:
        """完整处理流程"""
        print(f"\n处理待审文档: {self.filename}")
        print(f"总页数: {len(self.pages)}")

        blocks = self.extract_text_blocks()
        print(f"提取文本块: {len(blocks)} 个")

        structured_blocks = self.identify_structure(blocks)
        level_counts = {}
        for b in structured_blocks:
            st = b['structure_type']
            level_counts[st] = level_counts.get(st, 0) + 1
        print(f"结构统计: {level_counts}")

        chunks = self.merge_by_structure(structured_blocks)
        print(f"生成chunks: {len(chunks)} 个")

        sizes = [c['char_count'] for c in chunks]
        if sizes:
            print(f"  平均大小: {sum(sizes)//len(sizes)} 字符")
            print(f"  最大: {max(sizes)}, 最小: {min(sizes)}")
        return chunks


def generate_visualization(all_results: Dict[str, List[Dict]], output_dir: Path):
    """生成可视化HTML报告"""
    output_dir.mkdir(parents=True, exist_ok=True)

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head>",
        "<meta charset='utf-8'>",
        "<title>待审文档 Chunk 分析报告</title>",
        "<style>",
        "body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 20px; background: #f5f5f5; }",
        "h1 { color: #c0392b; border-bottom: 3px solid #e74c3c; padding-bottom: 10px; }",
        "h2 { color: #34495e; margin-top: 30px; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
        ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".doc-section { background: white; padding: 20px; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".chunk { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; background: #fafafa; }",
        ".chunk-header { font-weight: bold; color: #c0392b; margin-bottom: 10px; }",
        ".chunk-meta { color: #7f8c8d; font-size: 0.9em; margin: 5px 0; }",
        ".chunk-content { margin-top: 10px; padding: 10px; background: white; border-left: 3px solid #e74c3c; white-space: pre-wrap; }",
        ".stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }",
        ".stat-card { background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }",
        ".stat-value { font-size: 2em; font-weight: bold; }",
        ".stat-label { font-size: 0.9em; opacity: 0.9; margin-top: 5px; }",
        ".level-badge { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 0.85em; margin-right: 5px; }",
        ".level-part { background: #e74c3c; color: white; }",
        ".level-chapter { background: #3498db; color: white; }",
        ".level-section { background: #2ecc71; color: white; }",
        ".level-article { background: #f39c12; color: white; }",
        ".level-subsection { background: #9b59b6; color: white; }",
        ".level-title { background: #1abc9c; color: white; }",
        "</style>",
        "</head><body>",
        "<h1>📋 待审文档 - Chunk 分析报告</h1>",
        f"<p style='color: #7f8c8d;'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
    ]

    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    all_sizes = [c['char_count'] for chunks in all_results.values() for c in chunks]
    avg_size = sum(all_sizes) // len(all_sizes) if all_sizes else 0

    html_parts.append("<div class='summary'>")
    html_parts.append("<h2>📈 总体统计</h2>")
    html_parts.append("<div class='stats'>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_docs}</div><div class='stat-label'>待审文档数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>Chunk总数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{avg_size}</div><div class='stat-label'>平均字符数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{max(all_sizes) if all_sizes else 0}</div><div class='stat-label'>最大Chunk</div></div>")
    html_parts.append("</div></div>")

    for doc_name, chunks in all_results.items():
        html_parts.append(f"<div class='doc-section'>")
        html_parts.append(f"<h2>📄 {doc_name}</h2>")
        html_parts.append(f"<p><strong>Chunk数量:</strong> {len(chunks)}</p>")

        level_counts = {}
        for chunk in chunks:
            level = chunk['chunk_level']
            level_counts[level] = level_counts.get(level, 0) + 1
        html_parts.append(f"<p><strong>层级分布:</strong> {dict(level_counts)}</p>")

        for i, chunk in enumerate(chunks, 1):
            level = chunk['chunk_level'] or 'content'
            level_class = f"level-{level}"

            html_parts.append(f"<div class='chunk'>")
            html_parts.append(f"<div class='chunk-header'>")
            html_parts.append(f"<span class='level-badge {level_class}'>{level}</span>")
            html_parts.append(f"Chunk #{i}")
            html_parts.append(f"</div>")

            if chunk.get('part'):
                html_parts.append(f"<div class='chunk-meta'>📚 编: {chunk['part']}</div>")
            if chunk.get('chapter'):
                html_parts.append(f"<div class='chunk-meta'>📖 章: {chunk['chapter']}</div>")
            if chunk.get('section'):
                html_parts.append(f"<div class='chunk-meta'>📑 节: {chunk['section']}</div>")
            if chunk.get('article'):
                html_parts.append(f"<div class='chunk-meta'>📝 条: {chunk['article']}</div>")

            html_parts.append(f"<div class='chunk-meta'>📄 页码: {chunk['page_range']} | 字符数: {chunk['char_count']}</div>")

            content = chunk['content']
            preview = content[:500] + ('...' if len(content) > 500 else '')
            html_parts.append(f"<div class='chunk-content'>{preview}</div>")
            html_parts.append(f"</div>")

        html_parts.append(f"</div>")

    html_parts.append("</body></html>")

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    html_path = output_dir / f"pending_doc_chunks_{timestamp}.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))
    print(f"[OK] HTML报告已生成: {html_path}")

    json_path = output_dir / f"pending_doc_chunks_{timestamp}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"[OK] JSON数据已保存: {json_path}")

    return html_path, json_path


def main():
    print("=" * 70)
    print("待审文档分块工具")
    print("=" * 70)

    json_dir = Path("new_docs/test_doc_json")
    output_dir = Path("chunks_visualization")

    json_files = list(json_dir.glob("MinerU_*.json"))
    print(f"\n找到 {len(json_files)} 个待审文档")

    all_results = {}
    for json_file in json_files:
        chunker = PendingDocChunker(str(json_file))
        chunks = chunker.process()
        doc_name = json_file.stem.replace('MinerU_', '').split('__')[0]
        all_results[doc_name] = chunks

    print("\n" + "=" * 70)
    print("生成可视化报告...")
    generate_visualization(all_results, output_dir)

    print("\n" + "=" * 70)
    print("[OK] 处理完成！")
    print(f"  - 待审文档数: {len(all_results)}")
    print(f"  - 总Chunk数: {sum(len(chunks) for chunks in all_results.values())}")
    print(f"  - 报告位置: {output_dir}")
    print("=" * 70)

    return all_results


if __name__ == "__main__":
    all_results = main()
