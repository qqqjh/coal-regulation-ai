"""
待审文档分块工具 v4
改进：
1. 解决标题单独成块问题 - 将只有标题的chunk与下一个chunk合并
2. 修复数值格式清理问题 - 处理 '10 - 11 m 3 t' → '10-11 m³/t' 等
3. 继承v2的大chunk二次切分功能
4. 修复 · 符号的多种误识别情况：
   - 6·22 日期格式保留
   - ·前无数字·后有数字 → ~（约等于）
   - N · m 单位乘法保留
   - 1.8 m · 3.5 m 范围 → 1.8 m-3.5 m
   - 6×19S-Φ21.5mm 被误识别为 6 × 195 · 21.5 mm → 修复
   - 50 m³/h -60 m³/h 被误识别为 50 m³/h —· 60 m³/h → 修复
"""
import json
import re
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime


# 大 chunk 阈值
LARGE_CHUNK_THRESHOLD = 3000
# 小 chunk 阈值（低于此值认为是纯标题，需要合并）
MIN_CHUNK_THRESHOLD = 50


class PendingDocChunkerV4:
    """待审文档分块器 v4"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type = None

    def clean_text(self, text: str) -> str:
        """清理文本中的LaTeX格式和多余空格（增强版v4）"""
        # ===== 0. 预处理：修复被误识别为 \cdot 的各种情况 =====

        # 0.1 处理单位+\cdot+数字的范围格式: "1.8 m \cdot 3.5 m" → "1.8 m-3.5 m"
        text = re.sub(r'(\\mathsf\s*\{\s*m\s*\})\s*\\cdot\s*(\d)', r'\1-\2', text)
        text = re.sub(r'(\d)\s*(\\mathsf\s*\{\s*m\s*\})\s*\\cdot\s*(\d)', r'\1\2-\3', text)
        text = re.sub(r'(\d)\s*(\\mathrm\s*\{\s*m\s*\})\s*\\cdot\s*(\d)', r'\1\2-\3', text)

        # 0.2 处理 \cdot 后直接跟数字的情况（约等于符号）: "\cdot 200" → "~200"
        text = re.sub(r'\\cdot\s*(\d)', r'~\1', text)

        # 0.3 处理 数字 {\cdot} 数字 的情况（范围符号）: "2 {\cdot} 5.4" → "2~5.4"
        text = re.sub(r'(\d)\s*\{\s*\\cdot\s*\}\s*(\d)', r'\1~\2', text)

        # ===== 1. LaTeX 命令处理 =====
        text = re.sub(r'\\mathrm\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathbf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathit\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathsf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\text\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)
        text = re.sub(r'\\mathrm([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathbf([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathit([a-zA-Z]+)', r'\1', text)

        # ===== 2. 特殊符号替换 =====
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

        # ===== 3. 处理其他LaTeX符号 =====
        text = re.sub(r'\\sim', '~', text)
        text = re.sub(r'\^?\s*\\circ', '°', text)
        text = re.sub(r'[_^]\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\(left|right|quad|qquad|tag|dots?|ldots|cdots)\b\s*', '', text)
        text = re.sub(r'\\_', '_', text)
        text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
        text = re.sub(r'\\(?=[\s\d])', '', text)
        text = re.sub(r'\\$', '', text)

        # ===== 4. 修复数值范围格式 =====
        text = re.sub(r'(\d+)\s*-\s*(\d+)', r'\1-\2', text)
        text = re.sub(r'(\d+)\s*~\s*(\d+)', r'\1~\2', text)

        # ===== 5. 修复单位格式 =====
        text = re.sub(r'\bm\s*3\b', 'm³', text)
        text = re.sub(r'\bm\s*³\b', 'm³', text)
        text = re.sub(r'\bm\s*2\b', 'm²', text)
        text = re.sub(r'\bm\s*²\b', 'm²', text)
        text = re.sub(r'm³\s*/\s*t', 'm³/t', text)
        text = re.sub(r'm²\s*/\s*', 'm²/', text)

        # ===== 6. 清理数字间的空格 =====
        for _ in range(3):
            text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
        text = re.sub(r'(\d)\s*\.\s*(\d)', r'\1.\2', text)

        # ===== 7. 修复单位格式（字母间空格） =====
        text = re.sub(r'([a-zA-Z])\s+([a-zA-Z])', r'\1\2', text)
        text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)

        # ===== 8. 修复单位后的范围符号格式 =====
        text = re.sub(r'(\d\s*m)\s*-\s*(\d)', r'\1-\2', text)
        text = re.sub(r'(\d\s*m)\s*~\s*(\d)', r'\1~\2', text)

        # ===== 9. 修复 · 符号的各种误识别情况（后处理） =====

        # 9.1 修复 "—·" 或 "— ·" → "-"（范围符号被误识别）
        text = re.sub(r'—\s*·\s*', '-', text)

        # 9.2 修复 "· 数字" 开头（前面没有数字）→ "~数字"（约等于）
        # 但要排除日期格式如 "6·22"
        text = re.sub(r'(?<!\d)\s*·\s*(\d)', r'~\1', text)

        # 9.3 修复单位范围: "1.8 m · 3.5 m" → "1.8 m-3.5 m"
        text = re.sub(r'(\d+\.?\d*\s*m)\s*·\s*(\d+\.?\d*\s*m)', r'\1-\2', text)
        text = re.sub(r'(\d+\.?\d*\s*mm)\s*·\s*(\d+\.?\d*\s*mm)', r'\1-\2', text)

        # 9.4 修复流量范围: "50 m³/h · 60 m³/h" → "50 m³/h-60 m³/h"
        text = re.sub(r'(m³\s*/\s*h)\s*·\s*(\d)', r'\1-\2', text)

        # 9.5 修复 "195 · 21.5" 这种被误分割的型号 → "19S-Φ21.5"
        # 这个比较特殊，需要识别 S 被误识别为 5 的情况
        text = re.sub(r'(\d+)5\s*·\s*(\d)', r'\g<1>S-Φ\2', text)

        # 9.6 保留正确的 · 用法：日期格式 6·22、单位乘法 N·m 等不需要处理

        # ===== 10. 清理多余空格 =====
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
        """按中文数字标记切分"""
        pattern = r'\n([一二三四五六七八九十]+)[、.．]'
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

    def split_by_arabic_numbers(self, content: str) -> List[Tuple[str, str]]:
        """按阿拉伯数字标记切分"""
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

        if char_count <= LARGE_CHUNK_THRESHOLD:
            return [chunk]

        result_chunks = []
        cn_parts = self.split_by_chinese_numbers(content)

        for cn_marker, cn_content in cn_parts:
            cn_char_count = len(cn_content)

            if cn_char_count > LARGE_CHUNK_THRESHOLD:
                ar_parts = self.split_by_arabic_numbers(cn_content)
                for ar_marker, ar_content in ar_parts:
                    sub_marker = cn_marker + ar_marker if cn_marker or ar_marker else ''
                    # 把标号加回到内容前面
                    full_content = sub_marker + ar_content if sub_marker else ar_content
                    result_chunks.append({
                        **chunk,
                        'content': full_content,
                        'char_count': len(full_content),
                        'sub_marker': sub_marker,
                        'is_sub_chunk': True
                    })
            else:
                # 把标号加回到内容前面
                full_content = cn_marker + cn_content if cn_marker else cn_content
                result_chunks.append({
                    **chunk,
                    'content': full_content,
                    'char_count': len(full_content),
                    'sub_marker': cn_marker,
                    'is_sub_chunk': bool(cn_marker)
                })

        if len(result_chunks) == 1 and result_chunks[0]['char_count'] == char_count:
            result_chunks[0]['is_sub_chunk'] = False
            result_chunks[0]['sub_marker'] = ''

        # 二次切分后，合并过小的子chunk（如开头的标题部分）
        result_chunks = self._merge_small_sub_chunks(result_chunks)

        return result_chunks

    def _merge_small_sub_chunks(self, chunks: List[Dict]) -> List[Dict]:
        """合并二次切分后过小的子chunk"""
        if len(chunks) <= 1:
            return chunks

        merged = []
        i = 0
        while i < len(chunks):
            current = chunks[i]
            # 如果当前chunk太小且不是最后一个，与下一个合并
            if current['char_count'] < MIN_CHUNK_THRESHOLD and i + 1 < len(chunks):
                next_chunk = chunks[i + 1]
                merged_content = current['content'] + '\n' + next_chunk['content']
                merged_chunk = {
                    **next_chunk,
                    'content': merged_content,
                    'char_count': len(merged_content),
                    'sub_marker': current.get('sub_marker', '') or next_chunk.get('sub_marker', ''),
                    'is_sub_chunk': next_chunk.get('is_sub_chunk', False),
                    'merged_sub_title': True  # 标记合并了子标题
                }
                merged.append(merged_chunk)
                i += 2  # 跳过下一个chunk
            else:
                merged.append(current)
                i += 1

        return merged

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

    def merge_small_chunks(self, chunks: List[Dict]) -> List[Dict]:
        """合并过小的chunk（纯标题）到下一个有内容的chunk"""
        if not chunks:
            return chunks

        merged = []
        pending_chunks = []  # 待合并的小chunk列表（可能连续多个）

        for chunk in chunks:
            if chunk['char_count'] < MIN_CHUNK_THRESHOLD:
                # 这是一个过小的chunk（可能只有标题），暂存等待合并
                pending_chunks.append(chunk)
            else:
                # 这是一个有内容的chunk
                if pending_chunks:
                    # 将所有待合并的小chunk与当前chunk合并
                    all_contents = [p['content'] for p in pending_chunks] + [chunk['content']]
                    merged_content = '\n'.join(all_contents)
                    first_page = pending_chunks[0]['page_range'].split('-')[0]
                    last_page = chunk['page_range'].split('-')[1]
                    merged_chunk = {
                        **chunk,
                        'content': merged_content,
                        'char_count': len(merged_content),
                        'page_range': f"{first_page}-{last_page}",
                        'merged_from_title': True
                    }
                    merged.append(merged_chunk)
                    pending_chunks = []
                else:
                    merged.append(chunk)

        # 如果最后还有待合并的chunk，合并成一个
        if pending_chunks:
            if len(pending_chunks) == 1:
                merged.append(pending_chunks[0])
            else:
                # 多个小chunk合并成一个
                all_contents = [p['content'] for p in pending_chunks]
                merged_content = '\n'.join(all_contents)
                first_page = pending_chunks[0]['page_range'].split('-')[0]
                last_page = pending_chunks[-1]['page_range'].split('-')[1]
                merged_chunk = {
                    **pending_chunks[-1],
                    'content': merged_content,
                    'char_count': len(merged_content),
                    'page_range': f"{first_page}-{last_page}",
                    'merged_from_title': True
                }
                merged.append(merged_chunk)

        return merged

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

        # 合并过小的chunk（纯标题）
        small_chunks = [c for c in chunks if c['char_count'] < MIN_CHUNK_THRESHOLD]
        print(f"过小chunk (<{MIN_CHUNK_THRESHOLD}字符): {len(small_chunks)} 个")
        chunks = self.merge_small_chunks(chunks)
        print(f"合并后: {len(chunks)} 个")

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
        "<title>待审文档 Chunk 分析报告 v4</title>",
        "<style>",
        "body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 20px; background: #f5f5f5; }",
        "h1 { color: #16a085; border-bottom: 3px solid #1abc9c; padding-bottom: 10px; }",
        "h2 { color: #34495e; margin-top: 30px; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
        ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".doc-section { background: white; padding: 20px; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".chunk { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; background: #fafafa; }",
        ".chunk.sub-chunk { border-left: 4px solid #1abc9c; background: #f0faf8; }",
        ".chunk.merged { border-left: 4px solid #3498db; background: #f0f8ff; }",
        ".chunk-header { font-weight: bold; color: #16a085; margin-bottom: 10px; }",
        ".chunk-meta { color: #7f8c8d; font-size: 0.9em; margin: 5px 0; }",
        ".chunk-content { margin-top: 10px; padding: 10px; background: white; border-left: 3px solid #1abc9c; white-space: pre-wrap; font-size: 0.9em; }",
        ".stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin: 20px 0; }",
        ".stat-card { background: linear-gradient(135deg, #1abc9c 0%, #16a085 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }",
        ".stat-value { font-size: 2em; font-weight: bold; }",
        ".stat-label { font-size: 0.9em; opacity: 0.9; margin-top: 5px; }",
        ".level-badge { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 0.85em; margin-right: 5px; }",
        ".level-part { background: #e74c3c; color: white; }",
        ".level-chapter { background: #3498db; color: white; }",
        ".level-section { background: #2ecc71; color: white; }",
        ".level-article { background: #f39c12; color: white; }",
        ".level-subsection { background: #9b59b6; color: white; }",
        ".level-title { background: #1abc9c; color: white; }",
        ".sub-badge { background: #d5f5e3; color: #16a085; padding: 2px 6px; border-radius: 3px; font-size: 0.8em; margin-left: 5px; }",
        ".merged-badge { background: #d6eaf8; color: #2980b9; padding: 2px 6px; border-radius: 3px; font-size: 0.8em; margin-left: 5px; }",
        "</style>",
        "</head><body>",
        "<h1>📋 待审文档 - Chunk 分析报告 v4</h1>",
        f"<p style='color: #7f8c8d;'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 小chunk阈值: {MIN_CHUNK_THRESHOLD} | 大chunk阈值: {LARGE_CHUNK_THRESHOLD}</p>"
    ]

    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    all_sizes = [c['char_count'] for chunks in all_results.values() for c in chunks]
    avg_size = sum(all_sizes) // len(all_sizes) if all_sizes else 0
    sub_chunk_count = sum(1 for chunks in all_results.values() for c in chunks if c.get('is_sub_chunk'))
    merged_count = sum(1 for chunks in all_results.values() for c in chunks if c.get('merged_from_title'))

    html_parts.append("<div class='summary'>")
    html_parts.append("<h2>📈 总体统计</h2>")
    html_parts.append("<div class='stats'>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_docs}</div><div class='stat-label'>待审文档数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>Chunk总数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{merged_count}</div><div class='stat-label'>标题合并块</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{sub_chunk_count}</div><div class='stat-label'>二次切分块</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{avg_size}</div><div class='stat-label'>平均字符数</div></div>")
    html_parts.append("</div></div>")

    # 每个文档的详细信息
    for doc_name, chunks in all_results.items():
        html_parts.append(f"<div class='doc-section'>")
        html_parts.append(f"<h2>📄 {doc_name}</h2>")
        html_parts.append(f"<p style='color: #7f8c8d;'>共 {len(chunks)} 个 chunks</p>")

        for i, chunk in enumerate(chunks, 1):
            is_sub = chunk.get('is_sub_chunk', False)
            is_merged = chunk.get('merged_from_title', False)
            chunk_class = 'chunk'
            if is_sub:
                chunk_class += ' sub-chunk'
            if is_merged:
                chunk_class += ' merged'

            html_parts.append(f"<div class='{chunk_class}'>")

            # chunk header
            level = chunk.get('chunk_level', 'content')
            level_class = f"level-{level}" if level else "level-content"
            header = f"<span class='level-badge {level_class}'>{level}</span>"
            header += f"Chunk {i}"
            if is_sub:
                sub_marker = chunk.get('sub_marker', '')
                header += f"<span class='sub-badge'>二次切分 {sub_marker}</span>"
            if is_merged:
                header += f"<span class='merged-badge'>标题合并</span>"
            html_parts.append(f"<div class='chunk-header'>{header}</div>")

            # chunk meta
            meta_parts = []
            if chunk.get('chapter'):
                meta_parts.append(f"章: {chunk['chapter'][:30]}...")
            if chunk.get('section'):
                meta_parts.append(f"节: {chunk['section'][:30]}...")
            if chunk.get('article'):
                meta_parts.append(f"条: {chunk['article'][:30]}...")
            meta_parts.append(f"页码: {chunk.get('page_range', 'N/A')}")
            meta_parts.append(f"字符数: {chunk.get('char_count', 0)}")
            html_parts.append(f"<div class='chunk-meta'>{' | '.join(meta_parts)}</div>")

            # chunk content (truncated for display)
            content = chunk.get('content', '')
            display_content = content[:500] + '...' if len(content) > 500 else content
            display_content = display_content.replace('<', '&lt;').replace('>', '&gt;')
            html_parts.append(f"<div class='chunk-content'>{display_content}</div>")

            html_parts.append("</div>")

        html_parts.append("</div>")

    html_parts.append("</body></html>")

    # 保存HTML
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    html_path = output_dir / f"pending_doc_chunks_v4_{timestamp}.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))
    print(f"\n可视化报告已保存: {html_path}")

    # 保存JSON
    json_path = output_dir / f"pending_doc_chunks_v4_{timestamp}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"JSON数据已保存: {json_path}")

    return html_path, json_path


def main():
    """主函数"""
    input_dir = Path("new_docs/test_doc_json")
    output_dir = Path("chunks_visualization")

    if not input_dir.exists():
        print(f"错误: 输入目录不存在: {input_dir}")
        return

    json_files = list(input_dir.glob("*.json"))
    if not json_files:
        print(f"错误: 未找到JSON文件: {input_dir}")
        return

    print(f"找到 {len(json_files)} 个待审文档")
    print("=" * 50)

    all_results = {}
    for json_file in json_files:
        try:
            chunker = PendingDocChunkerV4(str(json_file))
            chunks = chunker.process()
            # 使用简短的文档名
            doc_name = json_file.stem.replace('MinerU_', '').strip()
            all_results[doc_name] = chunks
        except Exception as e:
            print(f"处理失败 {json_file.name}: {e}")

    print("\n" + "=" * 50)
    print("生成可视化报告...")
    generate_visualization(all_results, output_dir)
    print("\n处理完成!")


if __name__ == "__main__":
    main()
