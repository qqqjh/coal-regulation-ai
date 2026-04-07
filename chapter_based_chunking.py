"""
按章节划分的知识库重构工具
1. 从MinerU的JSON中提取文本块
2. 识别章节结构（第X章、第X节）
3. 按章节合并为chunk
4. 生成可视化报告
5. 存储到独立的向量库（不覆盖现有）
"""
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Any
from datetime import datetime
import sqlite3


class ChapterBasedChunker:
    """基于章节的文档分块器"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])

    def extract_text_blocks(self) -> List[Dict[str, Any]]:
        """提取所有文本块（bbox级别）"""
        blocks = []

        for page_idx, page in enumerate(self.pages):
            para_blocks = page.get('para_blocks', [])

            for block in para_blocks:
                block_type = block.get('type', '')

                # 跳过图片、表格（保留表格可选）
                if block_type in ['image', 'image_body', 'image_caption']:
                    continue

                # 提取文本内容
                lines = block.get('lines', [])
                text_parts = []
                for line in lines:
                    for span in line.get('spans', []):
                        content = span.get('content', '')
                        # 清理多余空格
                        content = re.sub(r'\s+', ' ', content).strip()
                        if content:
                            text_parts.append(content)

                text = ''.join(text_parts)

                if text:
                    blocks.append({
                        'page': page_idx + 1,
                        'type': block_type,
                        'text': text,
                        'bbox': block.get('bbox', [])
                    })

        return blocks

    def identify_structure(self, blocks: List[Dict]) -> List[Dict]:
        """识别章节结构"""
        # 章节模式
        chapter_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+章')
        section_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+节')
        article_pattern = re.compile(r'^第[一二三四五六七八九十百\d]+条')

        structured_blocks = []

        for block in blocks:
            text = block['text']
            structure_type = 'content'

            if chapter_pattern.match(text):
                structure_type = 'chapter'
            elif section_pattern.match(text):
                structure_type = 'section'
            elif article_pattern.match(text):
                structure_type = 'article'
            elif block['type'] == 'title':
                structure_type = 'title'

            structured_blocks.append({
                **block,
                'structure_type': structure_type
            })

        return structured_blocks

    def merge_by_chapters(self, structured_blocks: List[Dict]) -> List[Dict]:
        """按章节合并为chunk"""
        chunks = []
        current_chapter = None
        current_section = None
        current_content = []

        chapter_title = ""
        section_title = ""
        start_page = 1

        for block in structured_blocks:
            structure_type = block['structure_type']
            text = block['text']
            page = block['page']

            # 遇到新章节
            if structure_type == 'chapter':
                # 保存之前的chunk
                if current_content:
                    chunks.append({
                        'chapter': chapter_title,
                        'section': section_title,
                        'content': '\n'.join(current_content),
                        'page_range': f"{start_page}-{page-1}",
                        'chunk_type': 'section' if section_title else 'chapter'
                    })

                # 开始新章
                current_chapter = text
                chapter_title = text
                section_title = ""
                current_content = [text]
                start_page = page

            # 遇到新节
            elif structure_type == 'section':
                # 保存之前的节
                if current_content and section_title:
                    chunks.append({
                        'chapter': chapter_title,
                        'section': section_title,
                        'content': '\n'.join(current_content),
                        'page_range': f"{start_page}-{page-1}",
                        'chunk_type': 'section'
                    })

                # 开始新节
                section_title = text
                current_content = [text]
                start_page = page

            # 普通内容
            else:
                current_content.append(text)

        # 保存最后一个chunk
        if current_content:
            chunks.append({
                'chapter': chapter_title,
                'section': section_title,
                'content': '\n'.join(current_content),
                'page_range': f"{start_page}-{self.pages[-1]['page_idx']+1 if self.pages else 1}",
                'chunk_type': 'section' if section_title else 'chapter'
            })

        return chunks

    def process(self) -> List[Dict]:
        """完整处理流程"""
        print(f"\n处理文档: {self.filename}")
        print(f"总页数: {len(self.pages)}")

        # 1. 提取文本块
        blocks = self.extract_text_blocks()
        print(f"提取文本块: {len(blocks)} 个")

        # 2. 识别结构
        structured_blocks = self.identify_structure(blocks)
        chapter_count = sum(1 for b in structured_blocks if b['structure_type'] == 'chapter')
        section_count = sum(1 for b in structured_blocks if b['structure_type'] == 'section')
        print(f"识别章节: {chapter_count} 章, {section_count} 节")

        # 3. 按章节合并
        chunks = self.merge_by_chapters(structured_blocks)
        print(f"生成chunks: {len(chunks)} 个")

        return chunks


def generate_visualization(all_results: Dict[str, List[Dict]], output_dir: Path):
    """生成可视化报告"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成HTML报告
    html_path = output_dir / f"chunking_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    html_content = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>章节分块报告</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        h1 { color: #333; border-bottom: 3px solid #4CAF50; padding-bottom: 10px; }
        h2 { color: #555; margin-top: 30px; }
        .summary { background: #e8f5e9; padding: 15px; border-radius: 5px; margin: 20px 0; }
        .doc-section { margin: 20px 0; border: 1px solid #ddd; border-radius: 5px; }
        .doc-header { background: #4CAF50; color: white; padding: 10px 15px; font-weight: bold; }
        .chunk { margin: 10px; padding: 10px; background: #fafafa; border-left: 4px solid #2196F3; }
        .chunk-meta { color: #666; font-size: 0.9em; margin-bottom: 5px; }
        .chunk-content { margin-top: 10px; padding: 10px; background: white; border-radius: 3px; max-height: 200px; overflow-y: auto; }
        .stats { display: flex; gap: 20px; flex-wrap: wrap; }
        .stat-card { flex: 1; min-width: 200px; background: #fff3e0; padding: 15px; border-radius: 5px; text-align: center; }
        .stat-number { font-size: 2em; color: #ff9800; font-weight: bold; }
        .stat-label { color: #666; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📚 煤矿法规知识库 - 章节分块报告</h1>
        <div class="summary">
            <h2>📊 总体统计</h2>
            <div class="stats">
"""

    total_docs = len(all_results)
    total_chunks = sum(len(chunks) for chunks in all_results.values())
    total_chars = sum(len(chunk['content']) for chunks in all_results.values() for chunk in chunks)

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

    # 每个文档的详细信息
    for doc_name, chunks in all_results.items():
        html_content += f"""
        <div class="doc-section">
            <div class="doc-header">{doc_name} ({len(chunks)} chunks)</div>
"""

        for i, chunk in enumerate(chunks):
            chapter = chunk.get('chapter', '未分类')
            section = chunk.get('section', '')
            page_range = chunk.get('page_range', '')
            content = chunk.get('content', '')

            html_content += f"""
            <div class="chunk">
                <div class="chunk-meta">
                    <strong>Chunk #{i+1}</strong> |
                    章节: {chapter} {section} |
                    页码: {page_range} |
                    字符数: {len(content)}
                </div>
                <div class="chunk-content">{content[:500]}{'...' if len(content) > 500 else ''}</div>
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

    # 同时生成JSON格式
    json_path = output_dir / f"chunks_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"[OK] JSON数据已保存: {json_path}")


def main():
    print("=" * 70)
    print("煤矿法规知识库 - 按章节分块工具")
    print("=" * 70)

    # 输入输出路径
    json_dir = Path("new_docs/rule_docs_json")
    output_dir = Path("chunks_visualization")

    # 获取所有JSON文件
    json_files = list(json_dir.glob("MinerU_*.json"))
    print(f"\n找到 {len(json_files)} 个MinerU JSON文件")

    all_results = {}

    # 处理每个文档
    for json_file in json_files:
        chunker = ChapterBasedChunker(str(json_file))
        chunks = chunker.process()

        # 提取文档名称
        doc_name = json_file.stem.replace('MinerU_', '').split('__')[0]
        all_results[doc_name] = chunks

    # 生成可视化报告
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
