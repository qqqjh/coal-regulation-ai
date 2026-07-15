"""
优化版章节分块工具 v3
解决的问题：
1. LaTeX格式数值和单位的清理（增强版）
2. 支持"第几编"层级结构
3. 支持数字编号格式（1, 1.1, 1.1.1等）
4. 支持纯"第几条"格式
5. 智能识别文档结构类型
6. 修复日期中的空格问题
7. 修复 \sim 和单位格式问题
8. 修复列表项（a), b), c)等）丢失问题
9. 优化"第几条"的合并逻辑
"""
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime


class ImprovedChunkerV3:
    """改进的文档分块器 v3"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type = None  # 文档结构类型

    def clean_text(self, text: str) -> str:
        """清理文本中的LaTeX格式和多余空格（增强版）"""
        # 1. 处理带花括号的LaTeX命令（顺序：先处理特殊命令，再处理通用）
        # 单位/样式类：\mathrm{mm} → mm, \mathbf{kg} → kg
        text = re.sub(r'\\mathrm\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathbf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathit\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\text\s*\{([^}]+)\}', r'\1', text)
        # \frac{a}{b} → a/b
        text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)

        # 2. 处理无花括号的格式命令（紧跟字母）
        # \mathbfk → k, \mathrms → s, \mathrmm → m 等
        text = re.sub(r'\\mathrm([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathbf([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathit([a-zA-Z]+)', r'\1', text)

        # 3. 处理特殊符号
        text = re.sub(r'\\delta', 'δ', text)
        text = re.sub(r'\\Delta', 'Δ', text)
        text = re.sub(r'\\alpha', 'α', text)
        text = re.sub(r'\\beta', 'β', text)
        text = re.sub(r'\\gamma', 'γ', text)
        text = re.sub(r'\\Gamma', 'Γ', text)
        text = re.sub(r'\\theta', 'θ', text)
        text = re.sub(r'\\phi', 'φ', text)
        text = re.sub(r'\\pi', 'π', text)
        text = re.sub(r'\\mu', 'μ', text)
        text = re.sub(r'\\sigma', 'σ', text)
        text = re.sub(r'\\lambda', 'λ', text)
        text = re.sub(r'\\rho', 'ρ', text)
        text = re.sub(r'\\tau', 'τ', text)
        text = re.sub(r'\\omega', 'ω', text)
        text = re.sub(r'\\Omega', 'Ω', text)
        text = re.sub(r'\\epsilon', 'ε', text)

        # 运算符号
        text = re.sub(r'\\sim', '§SIM§', text)   # 先占位，避免被后续 ~ 替换影响
        text = re.sub(r'\\cdot', '·', text)
        text = re.sub(r'\\times', '×', text)
        text = re.sub(r'\\div', '÷', text)
        text = re.sub(r'\\leq', '≤', text)
        text = re.sub(r'\\geq', '≥', text)
        text = re.sub(r'\\neq', '≠', text)
        text = re.sub(r'\\approx', '≈', text)
        text = re.sub(r'\\pm', '±', text)
        text = re.sub(r'\\%', '%', text)          # LaTeX 转义百分号

        # 度数符号
        text = re.sub(r'\^?\s*\\circ', '°', text)

        # 4. 处理上下标和花括号
        text = re.sub(r'[_^]\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\{([^}]+)\}', r'\1', text)

        # 5. 去除剩余无法识别的 LaTeX 命令（\left, \right, \quad, \tag 等）
        text = re.sub(r'\\(left|right|quad|qquad|tag|dots?|ldots|cdots)\b\s*', '', text)
        # 处理 \_ 下划线
        text = re.sub(r'\\_', '_', text)
        # 去除所有其他剩余的 \command
        text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
        # 去除孤立反斜杠（后接空格或数字或行末）
        text = re.sub(r'\\(?=[\s\d])', '', text)
        text = re.sub(r'\\$', '', text)

        # 6. 把 LaTeX 非断行空格 ~ 替换为普通空格，再还原 \sim 占位符为 ~
        text = text.replace('~', ' ')
        text = text.replace('§SIM§', '~')

        # 7. 清理数字间的空格 (5 0 0 -> 500)，多次执行
        for _ in range(3):
            text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
        # 修复小数点周围的空格 (0 . 5 -> 0.5)
        text = re.sub(r'(\d)\s*\.\s*(\d)', r'\1.\2', text)

        # 8. 修复单位格式
        # 先删除字母间的空格 (m m -> mm, k g -> kg, k W -> kW)
        text = re.sub(r'([a-zA-Z])\s+([a-zA-Z])', r'\1\2', text)
        # 再确保数字和单位之间有空格 (50mm -> 50 mm)
        text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)

        # 8. 日期格式：保留日期中的空格（可选）
        # text = re.sub(r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', r'\1年\2月\3日', text)

        # 9. 清理多余空格
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()

        return text

    def extract_text_blocks(self) -> List[Dict[str, Any]]:
        """提取所有文本块（保留列表项）"""
        blocks = []

        for page_idx, page in enumerate(self.pages):
            para_blocks = page.get('para_blocks', [])

            for block in para_blocks:
                block_type = block.get('type', '')

                # 跳过图片
                if block_type in ['image', 'image_body', 'image_caption']:
                    continue

                # 处理 list 类型：展开其中的 blocks
                if block_type == 'list':
                    sub_blocks = block.get('blocks', [])
                    for sub_block in sub_blocks:
                        lines = sub_block.get('lines', [])
                        text_parts = []
                        for line in lines:
                            for span in line.get('spans', []):
                                content = span.get('content', '')
                                if content:
                                    text_parts.append(content)

                        text = ''.join(text_parts)
                        text = self.clean_text(text)

                        if text:
                            blocks.append({
                                'page': page_idx + 1,
                                'type': 'list_item',  # 标记为列表项
                                'text': text,
                                'bbox': sub_block.get('bbox', [])
                            })
                    continue

                # 提取文本内容
                lines = block.get('lines', [])
                text_parts = []
                for line in lines:
                    for span in line.get('spans', []):
                        content = span.get('content', '')
                        if content:
                            text_parts.append(content)

                text = ''.join(text_parts)
                text = self.clean_text(text)

                if text:
                    blocks.append({
                        'page': page_idx + 1,
                        'type': block_type,
                        'text': text,
                        'bbox': block.get('bbox', [])
                    })

        return blocks

    def detect_structure_type(self, blocks: List[Dict]) -> str:
        """检测文档的结构类型"""
        has_part = False  # 第几编
        has_chapter = False  # 第几章
        has_section = False  # 第几节
        has_article = False  # 第几条
        has_numeric = False  # 数字编号 (1, 1.1, 1.1.1)

        part_pattern = re.compile(r'^第[一二三四五六七八九十百]+编')
        chapter_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+章')
        section_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+节')
        article_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+条')
        numeric_pattern = re.compile(r'^\d+(\.\d+)*\s')  # 1, 1.1, 1.1.1

        for block in blocks[:50]:  # 只检查前50个块
            text = block['text']
            if part_pattern.match(text):
                has_part = True
            if chapter_pattern.match(text):
                has_chapter = True
            if section_pattern.match(text):
                has_section = True
            if article_pattern.match(text):
                has_article = True
            if numeric_pattern.match(text):
                has_numeric = True

        # 根据检测结果确定结构类型
        if has_part and has_chapter:
            return 'part_chapter_section'  # 编-章-节
        elif has_chapter and has_section:
            return 'chapter_section'  # 章-节
        elif has_numeric:
            return 'numeric'  # 数字编号
        elif has_article:
            return 'article_only'  # 仅条款
        else:
            return 'unknown'

    def identify_structure(self, blocks: List[Dict]) -> List[Dict]:
        """识别文档结构"""
        # 检测文档类型
        self.doc_structure_type = self.detect_structure_type(blocks)
        print(f"  检测到文档结构类型: {self.doc_structure_type}")

        # 各种模式
        part_pattern = re.compile(r'^第[一二三四五六七八九十百]+编')
        chapter_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+章')
        section_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+节')
        article_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+条')
        numeric_pattern = re.compile(r'^(\d+(?:\.\d+)*)\s+(.+)')  # 捕获编号和标题
        appendix_pattern = re.compile(r'^([A-Z]\.\d+(?:\.\d+)*)\s+(.+)')  # 附录 A.1, B.1

        structured_blocks = []

        for block in blocks:
            text = block['text']
            structure_type = 'content'
            level = 0  # 层级：0=内容, 1=编, 2=章, 3=节, 4=条, 5=列表项

            # 如果 block 类型已经是 list_item，直接标记为列表项
            if block['type'] == 'list_item':
                structure_type = 'list_item'
                level = 5
            # 根据文档类型识别结构
            elif self.doc_structure_type == 'part_chapter_section':
                if part_pattern.match(text):
                    structure_type = 'part'
                    level = 1
                elif chapter_pattern.match(text):
                    structure_type = 'chapter'
                    level = 2
                elif section_pattern.match(text):
                    structure_type = 'section'
                    level = 3
                elif article_pattern.match(text):
                    structure_type = 'article'
                    level = 4

            elif self.doc_structure_type == 'chapter_section':
                if chapter_pattern.match(text):
                    structure_type = 'chapter'
                    level = 2
                elif section_pattern.match(text):
                    structure_type = 'section'
                    level = 3
                elif article_pattern.match(text):
                    structure_type = 'article'
                    level = 4

            elif self.doc_structure_type == 'numeric':
                # 数字编号格式
                match = numeric_pattern.match(text)
                if match:
                    number = match.group(1)
                    dots = number.count('.')
                    if dots == 0:
                        structure_type = 'chapter'
                        level = 2
                    elif dots == 1:
                        structure_type = 'section'
                        level = 3
                    else:
                        structure_type = 'subsection'
                        level = 4
                # 附录格式
                elif appendix_pattern.match(text):
                    structure_type = 'appendix'
                    level = 3

            elif self.doc_structure_type == 'article_only':
                if article_pattern.match(text):
                    structure_type = 'article'
                    level = 4
                elif block['type'] == 'title':
                    structure_type = 'title'
                    level = 2

            structured_blocks.append({
                **block,
                'structure_type': structure_type,
                'level': level
            })

        return structured_blocks

    def merge_by_structure(self, structured_blocks: List[Dict]) -> List[Dict]:
        """根据文档结构智能合并chunk（优化版）"""
        chunks = []

        # 层级标题栈
        hierarchy = {
            'part': '',
            'chapter': '',
            'section': '',
            'article': ''
        }

        current_content = []
        start_page = 1
        chunk_level = None  # 当前chunk的层级

        for block in structured_blocks:
            structure_type = block['structure_type']
            level = block['level']
            text = block['text']
            page = block['page']

            # 列表项：直接追加到当前内容，不创建新chunk
            if structure_type == 'list_item':
                current_content.append(text)
                continue

            # 遇到新的结构层级
            if level > 0:
                # 保存之前的chunk
                if current_content and chunk_level is not None:
                    chunks.append(self._create_chunk(
                        hierarchy, current_content, start_page, page - 1, chunk_level
                    ))
                    current_content = []

                # 更新层级标题
                if structure_type == 'part':
                    hierarchy['part'] = text
                    hierarchy['chapter'] = ''
                    hierarchy['section'] = ''
                    hierarchy['article'] = ''
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

                # 开始新chunk
                current_content = [text]
                start_page = page
                chunk_level = structure_type

            else:
                # 普通内容
                current_content.append(text)

        # 保存最后一个chunk
        if current_content:
            chunks.append(self._create_chunk(
                hierarchy, current_content, start_page,
                self.pages[-1]['page_idx'] + 1 if self.pages else 1,
                chunk_level
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
        print(f"\n处理文档: {self.filename}")
        print(f"总页数: {len(self.pages)}")

        # 1. 提取文本块
        blocks = self.extract_text_blocks()
        print(f"提取文本块: {len(blocks)} 个")

        # 2. 识别结构
        structured_blocks = self.identify_structure(blocks)

        # 统计各层级数量
        level_counts = {}
        for b in structured_blocks:
            st = b['structure_type']
            level_counts[st] = level_counts.get(st, 0) + 1

        print(f"结构统计: {level_counts}")

        # 3. 按结构合并
        chunks = self.merge_by_structure(structured_blocks)
        print(f"生成chunks: {len(chunks)} 个")

        # 统计chunk大小
        sizes = [c['char_count'] for c in chunks]
        if sizes:
            print(f"  平均大小: {sum(sizes)//len(sizes)} 字符")
            print(f"  最大: {max(sizes)}, 最小: {min(sizes)}")

        return chunks


def generate_visualization(all_results: Dict[str, List[Dict]], output_dir: Path):
    """生成可视化HTML报告"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成HTML报告
    html_parts = [
        "<!DOCTYPE html>",
        "<html><head>",
        "<meta charset='utf-8'>",
        "<title>Chunk分析报告 v3</title>",
        "<style>",
        "body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 20px; background: #f5f5f5; }",
        "h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }",
        "h2 { color: #34495e; margin-top: 30px; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
        ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".doc-section { background: white; padding: 20px; margin: 20px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
        ".chunk { border: 1px solid #ddd; padding: 15px; margin: 10px 0; border-radius: 5px; background: #fafafa; }",
        ".chunk-header { font-weight: bold; color: #2980b9; margin-bottom: 10px; }",
        ".chunk-meta { color: #7f8c8d; font-size: 0.9em; margin: 5px 0; }",
        ".chunk-content { margin-top: 10px; padding: 10px; background: white; border-left: 3px solid #3498db; white-space: pre-wrap; }",
        ".stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; margin: 20px 0; }",
        ".stat-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 8px; text-align: center; }",
        ".stat-value { font-size: 2em; font-weight: bold; }",
        ".stat-label { font-size: 0.9em; opacity: 0.9; margin-top: 5px; }",
        ".level-badge { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 0.85em; margin-right: 5px; }",
        ".level-part { background: #e74c3c; color: white; }",
        ".level-chapter { background: #3498db; color: white; }",
        ".level-section { background: #2ecc71; color: white; }",
        ".level-article { background: #f39c12; color: white; }",
        ".level-subsection { background: #9b59b6; color: white; }",
        "</style>",
        "</head><body>",
        "<h1>📊 煤矿法规知识库 - Chunk分析报告 v3</h1>",
        f"<p style='color: #7f8c8d;'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>"
    ]

    # 总体统计
    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    all_sizes = [c['char_count'] for chunks in all_results.values() for c in chunks]
    avg_size = sum(all_sizes) // len(all_sizes) if all_sizes else 0

    html_parts.append("<div class='summary'>")
    html_parts.append("<h2>📈 总体统计</h2>")
    html_parts.append("<div class='stats'>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_docs}</div><div class='stat-label'>文档总数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>Chunk总数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{avg_size}</div><div class='stat-label'>平均字符数</div></div>")
    html_parts.append(f"<div class='stat-card'><div class='stat-value'>{max(all_sizes) if all_sizes else 0}</div><div class='stat-label'>最大Chunk</div></div>")
    html_parts.append("</div></div>")

    # 每个文档的详细信息
    for doc_name, chunks in all_results.items():
        html_parts.append(f"<div class='doc-section'>")
        html_parts.append(f"<h2>📄 {doc_name}</h2>")
        html_parts.append(f"<p><strong>Chunk数量:</strong> {len(chunks)}</p>")

        # 统计各层级数量
        level_counts = {}
        for chunk in chunks:
            level = chunk['chunk_level']
            level_counts[level] = level_counts.get(level, 0) + 1

        html_parts.append(f"<p><strong>层级分布:</strong> {dict(level_counts)}</p>")

        # 显示每个chunk
        for i, chunk in enumerate(chunks, 1):
            level = chunk['chunk_level']
            level_class = f"level-{level}"

            html_parts.append(f"<div class='chunk'>")
            html_parts.append(f"<div class='chunk-header'>")
            html_parts.append(f"<span class='level-badge {level_class}'>{level}</span>")
            html_parts.append(f"Chunk #{i}")
            html_parts.append(f"</div>")

            # 元数据
            if chunk.get('part'):
                html_parts.append(f"<div class='chunk-meta'>📚 编: {chunk['part']}</div>")
            if chunk.get('chapter'):
                html_parts.append(f"<div class='chunk-meta'>📖 章: {chunk['chapter']}</div>")
            if chunk.get('section'):
                html_parts.append(f"<div class='chunk-meta'>📑 节: {chunk['section']}</div>")
            if chunk.get('article'):
                html_parts.append(f"<div class='chunk-meta'>📝 条: {chunk['article']}</div>")

            html_parts.append(f"<div class='chunk-meta'>📄 页码: {chunk['page_range']} | 字符数: {chunk['char_count']}</div>")

            # 内容预览（前500字符）
            content = chunk['content']
            preview = content[:500] + ('...' if len(content) > 500 else '')
            html_parts.append(f"<div class='chunk-content'>{preview}</div>")
            html_parts.append(f"</div>")

        html_parts.append(f"</div>")

    html_parts.append("</body></html>")

    # 保存HTML
    html_path = output_dir / f"chunks_report_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))

    print(f"[OK] HTML报告已生成: {html_path}")

    # 保存JSON格式
    json_path = output_dir / f"chunks_v3_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"[OK] JSON数据已保存: {json_path}")


def main():
    print("=" * 70)
    print("煤矿法规知识库 - 优化版章节分块工具 v3")
    print("=" * 70)

    json_dir = Path("new_docs/rule_docs_json")
    output_dir = Path("chunks_visualization")

    json_files = list(json_dir.glob("MinerU_*.json"))
    print(f"\n找到 {len(json_files)} 个MinerU JSON文件")

    all_results = {}

    for json_file in json_files:
        chunker = ImprovedChunkerV3(str(json_file))
        chunks = chunker.process()

        doc_name = json_file.stem.replace('MinerU_', '').split('__')[0]
        all_results[doc_name] = chunks

    print("\n" + "=" * 70)
    print("生成可视化报告...")
    generate_visualization(all_results, output_dir)

    print("\n" + "=" * 70)
    print("[OK] 处理完成！")
    print(f"  - 总文档数: {len(all_results)}")
    print(f"  - 总Chunk数: {sum(len(chunks) for chunks in all_results.values())}")
    print(f"  - 报告位置: {output_dir}")
    print("=" * 70)

    return all_results


if __name__ == "__main__":
    all_results = main()
