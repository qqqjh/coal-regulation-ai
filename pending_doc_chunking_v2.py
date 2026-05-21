"""
待审文档分块工具 v2
改进：对大 chunk 进行二次切分
- 先按"一、""二、"等中文数字标记切分
- 如果还是太大，再按"1、""2、"等阿拉伯数字段落切分
"""
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime


# 大 chunk 阈值（超过此值进行二次切分）
LARGE_CHUNK_THRESHOLD = 3000


class PendingDocChunkerV2:
    """待审文档分块器 v2 - 支持大 chunk 二次切分"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type = None

    def clean_text(self, text: str) -> str:
        """清理文本中的LaTeX格式和多余空格"""
        text = re.sub(r'\\mathrm\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathbf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathit\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\text\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
        text = re.sub(r'\\mathrm([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathbf([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathit([a-zA-Z]+)', r'\1', text)

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

    def split_by_chinese_numbers(self, content: str) -> List[Tuple[str, str]]:
        """按中文数字标记切分（一、二、三...）
        返回: [(标记, 内容), ...]
        """
        # 匹配 "一、" "二、" 等，或 "一." "二." 或行首的 "一" "二"
        pattern = r'\n([一二三四五六七八九十]+)[、.．]'

        parts = re.split(pattern, content)
        if len(parts) <= 1:
            return [('', content)]

        result = []
        # 第一部分是标记前的内容
        if parts[0].strip():
            result.append(('', parts[0].strip()))

        # 后续是 (标记, 内容) 对
        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                marker = parts[i] + '、'
                text = parts[i + 1].strip()
                if text:
                    result.append((marker, text))

        return result if result else [('', content)]

    def split_by_arabic_numbers(self, content: str) -> List[Tuple[str, str]]:
        """按阿拉伯数字标记切分（1、2、3...）
        返回: [(标记, 内容), ...]
        """
        # 匹配行首的 "1、" "2、" 等，或 "1." "2."
        pattern = r'\n(\d+)[、.．]'

        parts = re.split(pattern, content)
        if len(parts) <= 1:
            return [('', content)]

        result = []
        if parts[0].strip():
            result.append(('', parts[0].strip()))

        for i in range(1, len(parts), 2):
            if i + 1 < len(parts):
                marker = parts[i] + '、'
                text = parts[i + 1].strip()
                if text:
                    result.append((marker, text))

        return result if result else [('', content)]

    def split_large_chunk(self, chunk: Dict) -> List[Dict]:
        """对大 chunk 进行二次切分"""
        content = chunk['content']
        char_count = chunk['char_count']

        # 如果不超过阈值，直接返回
        if char_count <= LARGE_CHUNK_THRESHOLD:
            return [chunk]

        result_chunks = []

        # 第一层：按中文数字切分
        cn_parts = self.split_by_chinese_numbers(content)

        for cn_marker, cn_content in cn_parts:
            cn_char_count = len(cn_content)

            # 如果切分后仍然太大，进行第二层切分
            if cn_char_count > LARGE_CHUNK_THRESHOLD:
                ar_parts = self.split_by_arabic_numbers(cn_content)

                for ar_marker, ar_content in ar_parts:
                    sub_marker = cn_marker + ar_marker if cn_marker or ar_marker else ''
                    result_chunks.append({
                        **chunk,
                        'content': ar_content,
                        'char_count': len(ar_content),
                        'sub_marker': sub_marker,
                        'is_sub_chunk': True
                    })
            else:
                result_chunks.append({
                    **chunk,
                    'content': cn_content,
                    'char_count': cn_char_count,
                    'sub_marker': cn_marker,
                    'is_sub_chunk': bool(cn_marker)
                })

        # 如果切分后只有一个且和原来一样大，说明没有可切分的标记
        if len(result_chunks) == 1 and result_chunks[0]['char_count'] == char_count:
            result_chunks[0]['is_sub_chunk'] = False
            result_chunks[0]['sub_marker'] = ''

        return result_chunks

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
                        hierarchy.copy(), current_content, start_page, page - 1, chunk_level
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
                hierarchy.copy(), current_content, start_page, last_page, chunk_level
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
            'char_count': sum(len(c) for c in content),
            'sub_marker': '',
            'is_sub_chunk': False
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

        # 第一次合并
        chunks = self.merge_by_structure(structured_blocks)
        print(f"初次分块: {len(chunks)} 个")

        # 统计大 chunk
        large_chunks = [c for c in chunks if c['char_count'] > LARGE_CHUNK_THRESHOLD]
        print(f"大chunk (>{LARGE_CHUNK_THRESHOLD}字符): {len(large_chunks)} 个")

        # 对大 chunk 进行二次切分
        final_chunks = []
        split_count = 0
        for chunk in chunks:
            if chunk['char_count'] > LARGE_CHUNK_THRESHOLD:
                sub_chunks = self.split_large_chunk(chunk)
                if len(sub_chunks) > 1:
                    split_count += 1
                final_chunks.extend(sub_chunks)
            else:
                final_chunks.append(chunk)

        print(f"二次切分: {split_count} 个大chunk被拆分")
        print(f"最终chunks: {len(final_chunks)} 个")

        sizes = [c['char_count'] for c in final_chunks]
        if sizes:
            print(f"  平均大小: {sum(sizes)//len(sizes)} 字符")
            print(f"  最大: {max(sizes)}, 最小: {min(sizes)}")

        return final_chunks


def generate_visualization(all_results: Dict[str, List[Dict]], output_dir: Path):
    """生成可视化HTML报告"""
    output_dir.mkdir(parents=True, exist_ok=True)

    html_parts = [
        "<!DOCTYPE html>",
        "<html><head>",
        "<meta charset='utf-8'>",
        "<title>待审文档 Chunk 分析报告 v2</title>",
        "<style>",
        "body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 20px; background: #f5f5f5; }",
        "h1 { color: #8e44ad; border-bottom: 3px solid #9b59b6; padding-bottom: 10px; }",
        "h2 { color: #34495e; margin-top: 30px; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
        ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".doc-section { background: white; padding: 20px; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".chunk { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; background: #fafafa; }",
        ".chunk.sub-chunk { border-left: 4px solid #9b59b6; background: #f9f5fc; }",
        ".chunk-header { font-weight: bold; color: #8e44ad; margin-bottom: 10px; }",
        ".chunk-meta { color: #7f8c8d; font-size: 0.9em; margin: 5px 0; }",
        ".chunk-content { margin-top: 10px; padding: 10px; background: white; border-left: 3px solid #9b59b6; white-space: pre-wrap; font-size: 0.9em; }",
        ".stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin: 20px 0; }",
        ".stat-card { background: linear-gradient(135deg, #9b59b6 0%, #8e44ad 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }",
        ".stat-value { font-size: 2em; font-weight: bold; }",
        ".stat-label { font-size: 0.9em; opacity: 0.9; margin-top: 5px; }",
        ".level-badge { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 0.85em; margin-right: 5px; }",
        ".level-part { background: #e74c3c; color: white; }",
        ".level-chapter { background: #3498db; color: white; }",
        ".level-section { background: #2ecc71; color: white; }",
        ".level-article { background: #f39c12; color: white; }",
        ".level-subsection { background: #9b59b6; color: white; }",
        ".level-title { background: #1abc9c; color: white; }",
        ".sub-badge { background: #e8daef; color: #8e44ad; padding: 2px 6px; border-radius: 3px; font-size: 0.8em; margin-left: 5px; }",
        "</style>",
        "</head><body>",
        "<h1>📋 待审文档 - Chunk 分析报告 v2 (支持大chunk二次切分)</h1>",
        f"<p style='color: #7f8c8d;'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 大chunk阈值: {LARGE_CHUNK_THRESHOLD} 字符</p>"
    ]

    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    all_sizes = [c['char_count'] for chunks in all_results.values() for c in chunks]
    avg_size = sum(all_sizes) // len(all_sizes) if all_sizes else 0
    sub_chunk_count = sum(1 for chunks in all_results.values() for c in chunks if c.get('is_sub_chunk'))

    html_parts.append("<div class='summary'>")
    html_parts.append("<h2>📈 总体统计</h2>")
    html_parts.append("<div class='stats'>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_docs}</div><div class='stat-label'>待审文档数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>Chunk总数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{sub_chunk_count}</div><div class='stat-label'>二次切分块</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{avg_size}</div><div class='stat-label'>平均字符数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{max(all_sizes) if all_sizes else 0}</div><div class='stat-label'>最大Chunk</div></div>")
    html_parts.append("</div></div>")

    for doc_name, chunks in all_results.items():
        html_parts.append(f"<div class='doc-section'>")
        html_parts.append(f"<h2>📄 {doc_name}</h2>")
        html_parts.append(f"<p><strong>Chunk数量:</strong> {len(chunks)}</p>")

        level_counts = {}
        for chunk in chunks:
            level = chunk['chunk_level'] or 'content'
            level_counts[level] = level_counts.get(level, 0) + 1
        doc_sub_count = sum(1 for c in chunks if c.get('is_sub_chunk'))
        html_parts.append(f"<p><strong>层级分布:</strong> {dict(level_counts)} | 二次切分: {doc_sub_count} 个</p>")

        for i, chunk in enumerate(chunks, 1):
            level = chunk['chunk_level'] or 'content'
            level_class = f"level-{level}"
            is_sub = chunk.get('is_sub_chunk', False)
            chunk_class = 'chunk sub-chunk' if is_sub else 'chunk'

            html_parts.append(f"<div class='{chunk_class}'>")
            html_parts.append(f"<div class='chunk-header'>")
            html_parts.append(f"<span class='level-badge {level_class}'>{level}</span>")
            html_parts.append(f"Chunk #{i}")
            if is_sub:
                sub_marker = chunk.get('sub_marker', '')
                html_parts.append(f"<span class='sub-badge'>子块 {sub_marker}</span>")
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
            # 转义 HTML 特殊字符
            preview = preview.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            html_parts.append(f"<div class='chunk-content'>{preview}</div>")
            html_parts.append(f"</div>")

        html_parts.append(f"</div>")

    html_parts.append("</body></html>")

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    html_path = output_dir / f"pending_doc_chunks_v2_{timestamp}.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))
    print(f"[OK] HTML报告已生成: {html_path}")

    json_path = output_dir / f"pending_doc_chunks_v2_{timestamp}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"[OK] JSON数据已保存: {json_path}")

    return html_path, json_path


def main():
    print("=" * 70)
    print("待审文档分块工具 v2 (支持大chunk二次切分)")
    print(f"大chunk阈值: {LARGE_CHUNK_THRESHOLD} 字符")
    print("=" * 70)

    json_dir = Path("new_docs/test_doc_json")
    output_dir = Path("chunks_visualization")

    json_files = list(json_dir.glob("MinerU_*.json"))
    print(f"\n找到 {len(json_files)} 个待审文档")

    all_results = {}
    for json_file in json_files:
        chunker = PendingDocChunkerV2(str(json_file))
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
