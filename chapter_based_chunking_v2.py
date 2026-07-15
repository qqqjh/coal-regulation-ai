"""
优化版章节分块工具 v2
解决的问题：
1. LaTeX格式数值和单位的清理
2. 支持"第几编"层级结构
3. 支持数字编号格式（1, 1.1, 1.1.1等）
4. 支持纯"第几条"格式
5. 智能识别文档结构类型
"""
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime


class ImprovedChunker:
    """改进的文档分块器"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type = None  # 文档结构类型

    def clean_text(self, text: str) -> str:
        """清理文本中的LaTeX格式和多余空格"""
        # 1. 处理LaTeX格式的数值
        # \delta _ { 5 } { = } 5 6 ^ { \circ } -> δ5 = 56°
        text = re.sub(r'\\delta', 'δ', text)
        text = re.sub(r'\\alpha', 'α', text)
        text = re.sub(r'\\beta', 'β', text)
        text = re.sub(r'\\gamma', 'γ', text)
        text = re.sub(r'\\theta', 'θ', text)
        text = re.sub(r'\\phi', 'φ', text)

        # 处理上下标和花括号
        text = re.sub(r'[_^]\s*\{\s*([^}]+)\s*\}', r'\1', text)
        text = re.sub(r'\{\s*([^}]+)\s*\}', r'\1', text)

        # 处理度数符号
        text = re.sub(r'\\circ', '°', text)
        text = re.sub(r'\^\\circ', '°', text)

        # 处理单位中的mathrm
        text = re.sub(r'\\mathrm\s*\{\s*([^}]+)\s*\}', r'\1', text)

        # 2. 清理数字间的空格 (5 0 0 -> 500)
        text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)

        # 3. 清理多余空格
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()

        return text

    def extract_text_blocks(self) -> List[Dict[str, Any]]:
        """提取所有文本块"""
        blocks = []

        for page_idx, page in enumerate(self.pages):
            para_blocks = page.get('para_blocks', [])

            for block in para_blocks:
                block_type = block.get('type', '')

                # 跳过图片
                if block_type in ['image', 'image_body', 'image_caption']:
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
        # 统计各种模式出现的次数
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
            level = 0  # 层级：0=内容, 1=编, 2=章, 3=节, 4=条

            # 根据文档类型识别结构
            if self.doc_structure_type == 'part_chapter_section':
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
        """根据文档结构智能合并chunk"""
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
    """生成可视化报告"""
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / f"chunking_report_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    html_content = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>章节分块报告 v2</title>
    <style>
        body { font-family: "Microsoft YaHei", Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        h1 { color: #333; border-bottom: 3px solid #4CAF50; padding-bottom: 10px; }
        h2 { color: #555; margin-top: 30px; }
        .summary { background: #e8f5e9; padding: 15px; border-radius: 5px; margin: 20px 0; }
        .doc-section { margin: 20px 0; border: 1px solid #ddd; border-radius: 5px; }
        .doc-header { background: #4CAF50; color: white; padding: 10px 15px; font-weight: bold; }
        .chunk { margin: 10px; padding: 10px; background: #fafafa; border-left: 4px solid #2196F3; }
        .chunk-meta { color: #666; font-size: 0.9em; margin-bottom: 5px; }
        .chunk-content { margin-top: 10px; padding: 10px; background: white; border-radius: 3px;
                        max-height: 300px; overflow-y: auto; white-space: pre-wrap;
                        font-family: "Courier New", monospace; font-size: 0.85em; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        .stat-card { flex: 1; min-width: 200px; background: #fff3e0; padding: 15px; border-radius: 5px; text-align: center; }
        .stat-number { font-size: 2em; color: #ff9800; font-weight: bold; }
        .stat-label { color: #666; margin-top: 5px; }
        .hierarchy { color: #1976D2; font-weight: bold; }
    </style>
</head>
<body>
    <div class="container">
        <h1>煤矿法规知识库 - 章节分块报告 v2 (优化版)</h1>
        <div class="summary">
            <h2>总体统计</h2>
            <div class="stats">
"""

    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    total_chars = sum(c['char_count'] for chunks in all_results.values() for c in chunks)

    html_content += f"""
                <div class="stat-card">
                    <div class="stat-number">{total_docs}</div>
                    <div class="stat-label">文档数量</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{total_chunks}</div>
                    <div class="stat-label">总Chunk数</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{total_chars:,}</div>
                    <div class="stat-label">总字符数</div>
                </div>
                <div class="stat-card">
                    <div class="stat-number">{total_chars // total_chunks if total_chunks > 0 else 0}</div>
                    <div class="stat-label">平均Chunk大小</div>
                </div>
            </div>
        </div>
"""

    for doc_name, chunks in all_results.items():
        html_content += f"""
        <div class="doc-section">
            <div class="doc-header">{doc_name} ({len(chunks)} chunks)</div>
"""

        for i, chunk in enumerate(chunks):
            part = chunk.get('part', '')
            chapter = chunk.get('chapter', '')
            section = chunk.get('section', '')
            article = chunk.get('article', '')
            page_range = chunk.get('page_range', '')
            content = chunk.get('content', '')
            char_count = chunk.get('char_count', 0)

            hierarchy_parts = []
            if part:
                hierarchy_parts.append(part)
            if chapter:
                hierarchy_parts.append(chapter)
            if section:
                hierarchy_parts.append(section)
            if article:
                hierarchy_parts.append(article)

            hierarchy_str = ' > '.join(hierarchy_parts) if hierarchy_parts else '未分类'

            html_content += f"""
            <div class="chunk">
                <div class="chunk-meta">
                    <strong>Chunk #{i+1}</strong> |
                    <span class="hierarchy">{hierarchy_str}</span> |
                    页码: {page_range} |
                    字符数: {char_count}
                </div>
                <div class="chunk-content">{content[:800]}{'...' if len(content) > 800 else ''}</div>
            </div>
"""

        html_content += """
        </div>
"""

    html_content += """
    </div>
</body>
</html>
"""

    with open(html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    print(f"\n[OK] 可视化报告已生成: {html_path}")

    # JSON格式
    json_path = output_dir / f"chunks_v2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"[OK] JSON数据已保存: {json_path}")


def main():
    print("=" * 70)
    print("煤矿法规知识库 - 优化版章节分块工具 v2")
    print("=" * 70)

    json_dir = Path("new_docs/rule_docs_json")
    output_dir = Path("chunks_visualization")

    json_files = list(json_dir.glob("MinerU_*.json"))
    print(f"\n找到 {len(json_files)} 个MinerU JSON文件")

    all_results = {}

    for json_file in json_files:
        chunker = ImprovedChunker(str(json_file))
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
