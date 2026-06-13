"""
优化版章节分块工具 v5
在 v4 基础上的改进：
1. 提取 MinerU 表格内容，附加在所属 chunk 中（解决 RAG 无法看到表格数值的问题）
2. 不再生成纯标题 chunk（chapter/part/section 级别，内容仅有标题行的 chunk 不独立存在）
3. 改进中间点 · 的多情形处理：
   - —· 或 -· → -（范围符号误识别）
   - 数值+单位 · 数值+单位 → 数值+单位-数值+单位（范围）
   - 数字·数字+单位 → 数字-数字+单位（范围，如 10·11m → 10-11m）
   - 字母·字母 保留（单位乘法，如 N·m, m·s-1）
   - 数字·数字（无后续单位）保留（日期格式，如 6·22）
   - 中文/空白后接 · 再接数字 → ~数字（约等于）
4. 超大 chunk 按"一""二"等中文序号或阿拉伯数字子项自动切分
5. 过滤目录/纯标题类 chunk，结构标题只进入元数据，不进入可检索正文
6. 为规则 chunk 增加 semantic_role、retrievable、canonical_rule_id 元数据
"""
import hashlib
import html
import json
import re
import os
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
from html.parser import HTMLParser


# 超大 chunk 的字符数阈值，超过则尝试按子项切分
MAX_CHUNK_CHARS = 3000

# 规则库检索侧的轻量过滤阈值。完整“第X条”即使较短也会保留。
MIN_RETRIEVABLE_CHARS = 80

NORMATIVE_TERMS = (
    '必须', '应当', '不得', '严禁', '禁止', '不准', '不应', '应',
    '可以', '宜', '需', '需要', '符合', '采用', '设置', '配备',
    '制定', '检查', '进行', '采取', '保持', '达到', '不超过',
    '不得低于', '不小于', '不大于', '至少',
)


# ============ 表格 HTML 解析器 ============

class _SimpleTableParser(HTMLParser):
    """将 MinerU 输出的 HTML 表格解析为行列列表"""

    def __init__(self):
        super().__init__()
        self.rows: List[List[str]] = []
        self._current_row: List[str] = []
        self._current_cell: str = ''
        self._in_cell: bool = False

    def handle_starttag(self, tag, attrs):
        if tag == 'tr':
            self._current_row = []
        elif tag in ('td', 'th'):
            self._current_cell = ''
            self._in_cell = True

    def handle_endtag(self, tag):
        if tag in ('td', 'th'):
            self._current_row.append(self._current_cell.strip())
            self._in_cell = False
        elif tag == 'tr':
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data


def html_table_to_text(html: str, caption: str = '') -> str:
    """将 HTML 表格字符串转为可读纯文本"""
    parser = _SimpleTableParser()
    try:
        parser.feed(html)
    except Exception:
        return ''

    if not parser.rows:
        return ''

    lines = []
    if caption.strip():
        lines.append(f'【表格】{caption.strip()}')
    else:
        lines.append('【表格】')

    for row in parser.rows:
        # 过滤空行
        if any(cell.strip() for cell in row):
            lines.append(' | '.join(cell for cell in row))

    return '\n'.join(lines)


# ============ 主分块器 ============

class ImprovedChunkerV5:
    """改进的文档分块器 v5"""

    def __init__(self, json_path: str):
        self.json_path = json_path
        self.filename = Path(json_path).stem
        with open(json_path, 'r', encoding='utf-8') as f:
            self.data = json.load(f)
        self.pages = self.data.get('pdf_info', [])
        self.doc_structure_type: Optional[str] = None

    # ============ 文本清洗 ============

    def normalize_dots(self, text: str) -> str:
        """
        处理 MinerU 产生的中间点 · (U+00B7 或普通点) 的各种误识别情形。
        处理顺序很重要，从最具体到最通用。
        """
        # 规则1: —· 或 -· → -（破折号/连接符+中点 → 范围连接符）
        # 如：50 m³/h —· 60 m³/h → 50 m³/h - 60 m³/h
        text = re.sub(r'[—\-]\s*·\s*', '-', text)

        # 规则2: 数值+单位 · 数值+单位 → 范围（如 1.8 m · 3.5 m → 1.8 m-3.5 m）
        # 单位：字母、上标、度符号、百分号
        _unit = r'[a-zA-Z%°℃³²¹/]+'
        _val_unit = r'(\d[\d\.]*\s*' + _unit + r'(?:[-][0-9]+)?)'
        text = re.sub(
            _val_unit + r'\s*·\s*' + _val_unit,
            r'\1-\2',
            text
        )

        # 规则3: 纯数字·数字+单位 → 范围（如 10·11m → 10-11m）
        # 注：若规则2已处理则不会重复匹配
        text = re.sub(
            r'(\d+)\s*·\s*(\d+)\s*([a-zA-Z%°℃³²¹/]+)',
            r'\1-\2\3',
            text
        )

        # 规则4: 字母·字母（单位乘法，如 N·m, m·s-1）→ 保留，不处理
        # 此规则为"不动"，不需要 re.sub

        # 规则5a: 中文字符后紧接 · 再接数字 → ~（约等于）
        # 如：循环进尺控制在·200 → 循环进尺控制在~200（虽不完美，但比·好）
        text = re.sub(r'([\u4e00-\u9fff])\s*·\s*(?=\d)', r'\1~', text)

        # 规则5b: 行首或空格后的 ·（前无字母/数字）接数字 → ~
        # 如：· 200 mm → ~200 mm
        text = re.sub(r'(?<![0-9a-zA-Z\u4e00-\u9fff])·\s*(?=\d)', '~', text)

        # 注：数字·数字（无后续单位，如日期 6·22）被规则3排除，自动保留

        return text

    def clean_text(self, text: str) -> str:
        """清理 LaTeX 格式、多余空格，并修正中间点"""
        # 1. LaTeX 花括号命令
        text = re.sub(r'\\mathrm\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathbf\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\mathit\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\text\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\\frac\s*\{([^}]+)\}\s*\{([^}]+)\}', r'\1/\2', text)

        # 2. 无花括号的格式命令
        text = re.sub(r'\\mathrm([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathbf([a-zA-Z]+)', r'\1', text)
        text = re.sub(r'\\mathit([a-zA-Z]+)', r'\1', text)

        # 3. 特殊符号
        for latex, sym in [
            (r'\\delta', 'δ'), (r'\\Delta', 'Δ'), (r'\\alpha', 'α'),
            (r'\\beta', 'β'), (r'\\gamma', 'γ'), (r'\\Gamma', 'Γ'),
            (r'\\theta', 'θ'), (r'\\phi', 'φ'), (r'\\pi', 'π'),
            (r'\\mu', 'μ'), (r'\\sigma', 'σ'), (r'\\lambda', 'λ'),
            (r'\\rho', 'ρ'), (r'\\tau', 'τ'), (r'\\omega', 'ω'),
            (r'\\Omega', 'Ω'), (r'\\epsilon', 'ε'),
        ]:
            text = re.sub(latex, sym, text)

        # 4. 运算符
        text = re.sub(r'\\sim', '§SIM§', text)
        text = re.sub(r'\\cdot', '·', text)
        text = re.sub(r'\\times', '×', text)
        text = re.sub(r'\\div', '÷', text)
        text = re.sub(r'\\leq', '≤', text)
        text = re.sub(r'\\geq', '≥', text)
        text = re.sub(r'\\neq', '≠', text)
        text = re.sub(r'\\approx', '≈', text)
        text = re.sub(r'\\pm', '±', text)
        text = re.sub(r'\\%', '%', text)
        text = re.sub(r'\^?\s*\\circ', '°', text)

        # 5. 上下标和花括号
        text = re.sub(r'[_^]\s*\{([^}]+)\}', r'\1', text)
        text = re.sub(r'\{([^}]+)\}', r'\1', text)

        # 6. 剩余 LaTeX 命令
        text = re.sub(r'\\(left|right|quad|qquad|tag|dots?|ldots|cdots)\b\s*', '', text)
        text = re.sub(r'\\_', '_', text)
        text = re.sub(r'\\[a-zA-Z]+\s*', '', text)
        text = re.sub(r'\\(?=[\s\d])', '', text)
        text = re.sub(r'\\$', '', text)

        # 7. ~ 与 \sim 处理
        text = text.replace('~', ' ')
        text = text.replace('§SIM§', '~')

        # 8. 数字间多余空格
        for _ in range(3):
            text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
        text = re.sub(r'(\d)\s*\.\s*(\d)', r'\1.\2', text)

        # 9. 单位格式
        text = re.sub(r'([a-zA-Z])\s+([a-zA-Z])', r'\1\2', text)
        text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)

        # 10. 中间点规范化（v4 新增）
        text = self.normalize_dots(text)

        # 11. 清理多余空格
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()

        return text

    # ============ 表格提取 ============

    def _extract_table_from_block(self, table_block: Dict) -> str:
        """从 MinerU 的 table 类型 block 提取文本表格"""
        caption = ''
        html_content = ''

        for sub in table_block.get('blocks', []):
            sub_type = sub.get('type', '')
            if sub_type == 'table_caption':
                # 提取标题文字
                for line in sub.get('lines', []):
                    for span in line.get('spans', []):
                        raw = span.get('content', '')
                        if raw:
                            caption += self.clean_text(raw)
            elif sub_type == 'table_body':
                # 提取 HTML 内容
                for line in sub.get('lines', []):
                    for span in line.get('spans', []):
                        if span.get('type') == 'table' and span.get('html'):
                            html_content = span['html']

        if not html_content:
            return ''

        return html_table_to_text(html_content, caption)

    # ============ 文本块提取 ============

    def extract_text_blocks(self) -> List[Dict[str, Any]]:
        """
        提取所有文本块（含表格）。
        表格被转为纯文本块，插入在其原始位置。
        """
        blocks = []

        for page_idx, page in enumerate(self.pages):
            for block in page.get('para_blocks', []):
                block_type = block.get('type', '')
                page_num = page_idx + 1

                # 跳过图片
                if block_type in ['image', 'image_body', 'image_caption']:
                    continue

                # 处理表格：转为纯文本块
                if block_type == 'table':
                    table_text = self._extract_table_from_block(block)
                    if table_text.strip():
                        blocks.append({
                            'page': page_num,
                            'type': 'table',
                            'text': table_text,
                            'bbox': block.get('bbox', [])
                        })
                    continue

                # 处理列表：展开子块
                if block_type == 'list':
                    for sub_block in block.get('blocks', []):
                        text = self._extract_span_text(sub_block)
                        if text:
                            blocks.append({
                                'page': page_num,
                                'type': 'list_item',
                                'text': text,
                                'bbox': sub_block.get('bbox', [])
                            })
                    continue

                # 普通文本块
                text = self._extract_span_text(block)
                if text:
                    blocks.append({
                        'page': page_num,
                        'type': block_type,
                        'text': text,
                        'bbox': block.get('bbox', [])
                    })

        return blocks

    def _extract_span_text(self, block: Dict) -> str:
        """从 block 的 lines/spans 中提取并清洗文本"""
        parts = []
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                content = span.get('content', '')
                if content:
                    parts.append(content)
        text = ''.join(parts)
        return self.clean_text(text)

    # ============ 文档结构识别 ============

    def detect_structure_type(self, blocks: List[Dict]) -> str:
        """检测文档的结构类型"""
        has_part = False
        has_chapter = False
        has_section = False
        has_article = False
        has_numeric = False

        part_pat = re.compile(r'^第[一二三四五六七八九十百]+编')
        chap_pat = re.compile(r'^第[一二三四五六七八九十百\d]+章')
        sec_pat = re.compile(r'^第[一二三四五六七八九十百\d]+节')
        art_pat = re.compile(r'^第[一二三四五六七八九十百\d]+条')
        num_pat = re.compile(r'^\d+(\.\d+)*\s')

        for block in blocks[:50]:
            t = block['text']
            if part_pat.match(t): has_part = True
            if chap_pat.match(t): has_chapter = True
            if sec_pat.match(t):  has_section = True
            if art_pat.match(t):  has_article = True
            if num_pat.match(t):  has_numeric = True

        if has_part and has_chapter:
            return 'part_chapter_section'
        elif has_chapter and has_section:
            return 'chapter_section'
        elif has_numeric:
            return 'numeric'
        elif has_article:
            return 'article_only'
        else:
            return 'unknown'

    def identify_structure(self, blocks: List[Dict]) -> List[Dict]:
        """为每个文本块标注结构类型和层级"""
        self.doc_structure_type = self.detect_structure_type(blocks)
        print(f'  文档结构类型: {self.doc_structure_type}')

        part_pat    = re.compile(r'^第[一二三四五六七八九十百]+编')
        chapter_pat = re.compile(r'^第[一二三四五六七八九十百\d]+章')
        section_pat = re.compile(r'^第[一二三四五六七八九十百\d]+节')
        article_pat = re.compile(r'^第[一二三四五六七八九十百\d]+条')
        numeric_pat = re.compile(r'^(\d+(?:\.\d+)*)\s+(.+)')
        appendix_pat = re.compile(r'^([A-Z]\.\d+(?:\.\d+)*)\s+(.+)')

        structured = []
        for block in blocks:
            text = block['text']
            structure_type = 'content'
            level = 0

            if block['type'] == 'list_item':
                structure_type = 'list_item'
                level = 5
            elif block['type'] == 'table':
                structure_type = 'table'
                level = 0  # 表格作为内容，不触发新 chunk
            elif self.doc_structure_type == 'part_chapter_section':
                if part_pat.match(text):    structure_type = 'part';    level = 1
                elif chapter_pat.match(text): structure_type = 'chapter'; level = 2
                elif section_pat.match(text): structure_type = 'section'; level = 3
                elif article_pat.match(text): structure_type = 'article'; level = 4
            elif self.doc_structure_type == 'chapter_section':
                if chapter_pat.match(text):   structure_type = 'chapter'; level = 2
                elif section_pat.match(text): structure_type = 'section'; level = 3
                elif article_pat.match(text): structure_type = 'article'; level = 4
            elif self.doc_structure_type == 'numeric':
                m = numeric_pat.match(text)
                if m:
                    dots = m.group(1).count('.')
                    if dots == 0:   structure_type = 'chapter';    level = 2
                    elif dots == 1: structure_type = 'section';    level = 3
                    else:           structure_type = 'subsection'; level = 4
                elif appendix_pat.match(text):
                    structure_type = 'appendix'; level = 3
            elif self.doc_structure_type == 'article_only':
                if article_pat.match(text):
                    structure_type = 'article'; level = 4
                elif block['type'] == 'title':
                    structure_type = 'title';   level = 2

            structured.append({**block, 'structure_type': structure_type, 'level': level})

        return structured

    # ============ Chunk 合并 ============

    def _create_chunk(self, hierarchy: Dict, content: List[str],
                      start_page: int, end_page: int, chunk_level: str) -> Dict:
        return {
            'part':        hierarchy.get('part', ''),
            'chapter':     hierarchy.get('chapter', ''),
            'section':     hierarchy.get('section', ''),
            'article':     hierarchy.get('article', ''),
            'content':     '\n'.join(content),
            'page_range':  f'{start_page}-{end_page}',
            'chunk_level': chunk_level,
            'char_count':  sum(len(c) for c in content),
        }

    _ARTICLE_PATTERN = re.compile(r'^第[一二三四五六七八九十百千万零〇\d]+条')
    _STRUCTURAL_LEVELS = {
        'title', 'part', 'chapter', 'section', 'subsection', 'appendix',
    }

    @staticmethod
    def _meaningful_lines(content: str) -> List[str]:
        return [line.strip() for line in content.splitlines() if line.strip()]

    @staticmethod
    def _compact_text(content: str) -> str:
        return re.sub(r'\s+', '', content or '')

    def _is_article_text(self, content: str) -> bool:
        lines = self._meaningful_lines(content)
        return bool(lines and self._ARTICLE_PATTERN.match(lines[0]))

    def _has_normative_signal(self, content: str) -> bool:
        compact = self._compact_text(content)
        return self._is_article_text(content) or any(term in compact for term in NORMATIVE_TERMS)

    def _looks_like_heading_text(self, content: str, level: str = '') -> bool:
        lines = self._meaningful_lines(content)
        if not lines or '【表格】' in content or self._is_article_text(content):
            return False
        compact = self._compact_text(content)
        structural_pattern = re.compile(
            r'^(?:第[一二三四五六七八九十百千万零〇\d]+[编章节篇卷]'
            r'|附录|附件|[A-Z]\.\d+(?:\.\d+)*|\d+(?:\.\d+)+)'
        )
        if all(structural_pattern.match(line) for line in lines):
            return True
        has_sentence_end = any(re.search(r'[。；！？.!?]$', line) for line in lines)
        return (
            level == 'title'
            and len(lines) == 1
            and len(compact) < MIN_RETRIEVABLE_CHARS
            and not has_sentence_end
        )

    def _looks_like_toc(self, content: str) -> bool:
        lines = self._meaningful_lines(content)
        if not lines:
            return False
        compact = self._compact_text(content)
        if compact in ('目录', '目次'):
            return True
        if len(lines) < 4:
            return False
        toc_lines = sum(
            bool(re.search(r'(?:\.{2,}|…{2,}|\s)\s*\d+\s*$', line))
            for line in lines
        )
        structural_lines = sum(
            bool(re.match(r'^第[一二三四五六七八九十百\d]+[编章节条]', line))
            for line in lines
        )
        return toc_lines >= max(2, len(lines) // 2) or structural_lines >= max(4, len(lines) - 1)

    def _is_pure_heading(self, content_list: List[str], level: str) -> bool:
        """结构标题只作为层级元数据，不生成独立可检索 chunk。"""
        if level not in self._STRUCTURAL_LEVELS:
            return False
        if any(text.startswith('【表格】') for text in content_list):
            return False
        meaningful = [
            text.strip()
            for text in content_list
            if text.strip() and not text.startswith('【表格】')
        ]
        content = '\n'.join(meaningful)
        if self._looks_like_heading_text(content, level):
            return True
        return len(meaningful) == 1 and not self._has_normative_signal(content)

    def _semantic_role(self, chunk: Dict) -> str:
        content = chunk.get('content', '')
        level = chunk.get('chunk_level', '')
        if self._looks_like_toc(content):
            return 'toc'
        if '【表格】' in content:
            return 'table_rule' if self._has_normative_signal(content) else 'table'
        if level.startswith('article') or self._is_article_text(content):
            return 'article_rule'
        if self._has_normative_signal(content):
            return 'rule'
        if len(self._compact_text(content)) < MIN_RETRIEVABLE_CHARS:
            return 'non_rule_short'
        return 'context'

    def _should_keep_chunk(self, chunk: Dict) -> bool:
        content = chunk.get('content', '')
        level = chunk.get('chunk_level', '').replace('_sub', '')
        if self._looks_like_heading_text(content, level):
            return False
        role = self._semantic_role(chunk)
        if role == 'toc':
            return False
        if role in ('article_rule', 'rule', 'table_rule', 'table'):
            return True
        lines = self._meaningful_lines(content)
        if level in self._STRUCTURAL_LEVELS and len(lines) <= 1:
            return False
        return bool(self._compact_text(content))

    def _add_retrieval_metadata(self, chunk: Dict) -> Dict:
        content = chunk.get('content', '')
        role = self._semantic_role(chunk)
        canonical_source = '|'.join([
            self.filename,
            chunk.get('article', ''),
            self._compact_text(content),
        ])
        chunk['char_count'] = len(content)
        chunk['semantic_role'] = role
        chunk['retrievable'] = True
        chunk['canonical_rule_id'] = hashlib.sha1(
            canonical_source.encode('utf-8')
        ).hexdigest()[:16]
        return chunk

    def merge_by_structure(self, structured_blocks: List[Dict]) -> List[Dict]:
        """
        根据文档结构智能合并 chunk。
        v5 改动：
        - 纯标题 chunk（含 title/appendix）不独立保存
        - 表格内容追加到当前 chunk 的内容中
        """
        chunks = []

        hierarchy = {'part': '', 'chapter': '', 'section': '', 'article': ''}
        current_content: List[str] = []
        start_page = 1
        chunk_level = None

        def _save_current(next_page: int):
            nonlocal current_content, chunk_level
            if not current_content or chunk_level is None:
                return
            if self._is_pure_heading(current_content, chunk_level):
                # 纯标题 chunk 不保存，但层级信息已在 hierarchy 中更新
                current_content = []
                return
            end_page = max(start_page, next_page - 1)
            chunks.append(self._create_chunk(
                hierarchy, current_content, start_page, end_page, chunk_level
            ))
            current_content = []

        for block in structured_blocks:
            stype = block['structure_type']
            level = block['level']
            text  = block['text']
            page  = block['page']

            # 列表项直接追加，不触发新 chunk
            if stype == 'list_item':
                current_content.append(text)
                continue

            # 表格：追加到当前 chunk，不触发新 chunk
            if stype == 'table':
                current_content.append(text)
                continue

            # 遇到结构层级标题
            if level > 0:
                _save_current(page)

                # 更新层级标题
                if stype == 'part':
                    hierarchy.update({'part': text, 'chapter': '', 'section': '', 'article': ''})
                elif stype == 'chapter':
                    hierarchy.update({'chapter': text, 'section': '', 'article': ''})
                elif stype in ('section', 'subsection', 'appendix'):
                    hierarchy.update({'section': text, 'article': ''})
                elif stype == 'article':
                    hierarchy['article'] = text
                elif stype == 'title':
                    hierarchy['chapter'] = text

                current_content = [text]
                start_page = page
                chunk_level = stype
            else:
                # 普通内容
                current_content.append(text)

        # 保存最后一个 chunk
        if current_content and chunk_level is not None:
            last_page = self.pages[-1]['page_idx'] + 1 if self.pages else 1
            if not self._is_pure_heading(current_content, chunk_level):
                chunks.append(self._create_chunk(
                    hierarchy, current_content, start_page, last_page, chunk_level
                ))

        return chunks

    # ============ 超大 Chunk 切分 ============

    # 中文序号子项模式（如"一、"、"（一）"、"1."、"1）"）
    _SUB_PATTERNS = [
        # 中文数字带顿号或括号
        re.compile(r'(?=^[一二三四五六七八九十百]{1,3}[、，])', re.MULTILINE),
        re.compile(r'(?=^（[一二三四五六七八九十百]{1,3}）)', re.MULTILINE),
        # 阿拉伯数字带顿号/句号/括号
        re.compile(r'(?=^\d{1,2}[、．.](?!\d))', re.MULTILINE),
        re.compile(r'(?=^（\d{1,2}）)', re.MULTILINE),
        re.compile(r'(?=^\(\d{1,2}\))', re.MULTILINE),
    ]

    _ARTICLE_BOUNDARY_PATTERN = re.compile(
        r'(?=^第[一二三四五六七八九十百千万零〇\d]+条)',
        re.MULTILINE,
    )

    def split_multi_article_chunk(self, chunk: Dict) -> List[Dict]:
        """恢复 OCR/结构识别遗漏造成的多条文合并块。"""
        content = chunk.get('content', '')
        boundaries = list(self._ARTICLE_BOUNDARY_PATTERN.finditer(content))
        if len(boundaries) < 2:
            return [chunk]

        prefix = content[:boundaries[0].start()].strip()
        result = []
        for index, boundary in enumerate(boundaries):
            end = boundaries[index + 1].start() if index + 1 < len(boundaries) else len(content)
            part = content[boundary.start():end].strip()
            if index == 0 and prefix and not self._looks_like_heading_text(prefix):
                part = f'{prefix}\n{part}'
            lines = self._meaningful_lines(part)
            new_chunk = dict(chunk)
            new_chunk['content'] = part
            new_chunk['char_count'] = len(part)
            new_chunk['chunk_level'] = 'article_recovered'
            for line in lines:
                if self._ARTICLE_PATTERN.match(line):
                    new_chunk['article'] = line
                    break
            result.append(new_chunk)
        return result

    def split_large_chunk(self, chunk: Dict) -> List[Dict]:
        """
        对超过 MAX_CHUNK_CHARS 的 chunk 尝试按子项切分。
        依次尝试各种子项模式，找到有效切分点即停止。
        若无法切分则原样返回。
        """
        content = chunk['content']
        if len(content) <= MAX_CHUNK_CHARS:
            return [chunk]

        for pat in self._SUB_PATTERNS:
            parts = pat.split(content)
            # 过滤空片段
            parts = [p.strip() for p in parts if p.strip()]
            if len(parts) >= 2:
                # 按切分点生成新 chunk 列表
                result = []
                for i, part in enumerate(parts):
                    new_chunk = dict(chunk)  # 浅拷贝
                    new_chunk['content'] = part
                    new_chunk['char_count'] = len(part)
                    new_chunk['chunk_level'] = chunk['chunk_level'] + '_sub'
                    if i > 0:
                        new_chunk['section'] = (
                            chunk.get('section', '') or chunk.get('chapter', '')
                        )
                    result.append(new_chunk)
                return result

        # 无法切分，原样返回
        return [chunk]

    def post_process_chunks(self, chunks: List[Dict]) -> List[Dict]:
        """切分超大 chunk，并过滤目录/标题噪声，补充检索元数据。"""
        result = []
        for chunk in chunks:
            for article_chunk in self.split_multi_article_chunk(chunk):
                for split_chunk in self.split_large_chunk(article_chunk):
                    if self._should_keep_chunk(split_chunk):
                        result.append(self._add_retrieval_metadata(split_chunk))
        return result

    # ============ 主流程 ============

    def process(self) -> List[Dict]:
        print(f'\n处理文档: {self.filename}')
        print(f'总页数: {len(self.pages)}')

        blocks = self.extract_text_blocks()
        table_count = sum(1 for b in blocks if b['type'] == 'table')
        print(f'提取文本块: {len(blocks)} 个（含 {table_count} 个表格）')

        structured = self.identify_structure(blocks)

        level_counts: Dict[str, int] = {}
        for b in structured:
            st = b['structure_type']
            level_counts[st] = level_counts.get(st, 0) + 1
        print(f'结构统计: {level_counts}')

        chunks = self.merge_by_structure(structured)
        chunks = self.post_process_chunks(chunks)
        print(f'生成 chunks: {len(chunks)} 个')

        sizes = [c['char_count'] for c in chunks]
        if sizes:
            print(f'  平均: {sum(sizes) // len(sizes)} 字符 | 最大: {max(sizes)} | 最小: {min(sizes)}')
            large = sum(1 for s in sizes if s > MAX_CHUNK_CHARS)
            if large:
                print(f'  仍有 {large} 个 chunk 超过 {MAX_CHUNK_CHARS} 字符（无子项可切）')

        return chunks


# ============ 可视化与输出 ============

def generate_visualization(all_results: Dict[str, List[Dict]], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # ---- HTML 报告 ----
    html_parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<title>Chunk分析报告 v5 - 完整正文</title>",
        "<style>",
        "body{font-family:'Microsoft YaHei',Arial;margin:20px;background:#f5f5f5}",
        "h1{color:#2c3e50;border-bottom:3px solid #3498db;padding-bottom:10px}",
        "h2{color:#34495e;margin-top:30px;background:#ecf0f1;padding:10px;border-radius:5px}",
        ".summary{background:white;padding:20px;border-radius:8px;margin:20px 0;box-shadow:0 2px 4px rgba(0,0,0,.1)}",
        ".doc-section{background:white;padding:20px;margin:20px 0;border-radius:8px;box-shadow:0 2px 4px rgba(0,0,0,.1)}",
        ".chunk{border:1px solid #ddd;padding:15px;margin:10px 0;border-radius:5px;background:#fafafa}",
        ".chunk-header{font-weight:bold;color:#2980b9;margin-bottom:10px}",
        ".chunk-meta{color:#7f8c8d;font-size:.9em;margin:5px 0}",
        ".report-actions{position:sticky;top:0;z-index:10;background:#f5f5f5;padding:10px 0}",
        ".report-actions button,.toggle-content{border:1px solid #bdc3c7;background:white;color:#34495e;padding:5px 10px;border-radius:4px;cursor:pointer}",
        ".toggle-content{margin-left:10px;font-size:.8em}",
        ".chunk-content{margin-top:10px;padding:10px;background:white;border-left:3px solid #3498db;white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}",
        ".chunk-content.collapsed{display:none}",
        ".table-chunk{border-left-color:#e67e22}",
        ".stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:15px;margin:20px 0}",
        ".stat-card{background:linear-gradient(135deg,#667eea,#764ba2);color:white;padding:20px;border-radius:8px;text-align:center}",
        ".stat-value{font-size:2em;font-weight:bold}",
        ".stat-label{font-size:.9em;opacity:.9;margin-top:5px}",
        ".level-badge{display:inline-block;padding:3px 8px;border-radius:3px;font-size:.85em;margin-right:5px}",
        ".level-part{background:#e74c3c;color:white}",
        ".level-chapter{background:#3498db;color:white}",
        ".level-section{background:#2ecc71;color:white}",
        ".level-article{background:#f39c12;color:white}",
        ".level-subsection{background:#9b59b6;color:white}",
        ".level-sub{background:#95a5a6;color:white}",
        "</style></head><body>",
        "<h1>📊 煤矿法规知识库 - Chunk分析报告 v5（完整正文）</h1>",
        f"<p style='color:#7f8c8d'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>",
        "<div class='report-actions'><button onclick='setAll(false)'>全部展开</button> <button onclick='setAll(true)'>全部折叠</button></div>",
    ]

    total_docs = len(all_results)
    total_chunks = sum(len(c) for c in all_results.values())
    total_tables = sum(
        sum(1 for c in chunks if '【表格】' in c['content'])
        for chunks in all_results.values()
    )
    all_sizes = [c['char_count'] for chunks in all_results.values() for c in chunks]
    avg_size = sum(all_sizes) // len(all_sizes) if all_sizes else 0

    html_parts += [
        "<div class='summary'><h2>📈 总体统计</h2><div class='stats'>",
        f"<div class='stat-card'><div class='stat-value'>{total_docs}</div><div class='stat-label'>文档总数</div></div>",
        f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>Chunk总数</div></div>",
        f"<div class='stat-card'><div class='stat-value'>{total_tables}</div><div class='stat-label'>含表格的Chunk</div></div>",
        f"<div class='stat-card'><div class='stat-value'>{avg_size}</div><div class='stat-label'>平均字符数</div></div>",
        f"<div class='stat-card'><div class='stat-value'>{max(all_sizes) if all_sizes else 0}</div><div class='stat-label'>最大Chunk</div></div>",
        "</div></div>",
    ]

    for doc_name, chunks in all_results.items():
        level_counts: Dict[str, int] = {}
        for chunk in chunks:
            lv = chunk['chunk_level']
            level_counts[lv] = level_counts.get(lv, 0) + 1

        html_parts.append(f"<div class='doc-section'><h2>📄 {html.escape(doc_name)}</h2>")
        html_parts.append(f"<p><strong>Chunk数量:</strong> {len(chunks)}</p>")
        html_parts.append(f"<p><strong>层级分布:</strong> {level_counts}</p>")

        for i, chunk in enumerate(chunks, 1):
            level = chunk['chunk_level']
            lv_cls = 'level-' + ('sub' if '_sub' in level else level)
            has_table = '【表格】' in chunk['content']
            content_class = 'table-chunk' if has_table else ''

            html_parts.append(f"<div class='chunk'>")
            html_parts.append(
                f"<div class='chunk-header'>"
                f"<span class='level-badge {lv_cls}'>{html.escape(level)}</span>"
                f"Chunk #{i}"
                + (" 📊含表格" if has_table else "")
                + f"<button class='toggle-content' onclick='toggleChunk(this)'>折叠正文</button></div>"
            )
            for field, label in [('part', '编'), ('chapter', '章'), ('section', '节'), ('article', '条')]:
                if chunk.get(field):
                    html_parts.append(
                        f"<div class='chunk-meta'>{label}: {html.escape(str(chunk[field]))}</div>"
                    )
            html_parts.append(
                f"<div class='chunk-meta'>页码: {html.escape(str(chunk['page_range']))} | 字符数: {chunk['char_count']} | "
                f"角色: {html.escape(str(chunk.get('semantic_role', '')))}</div>"
            )
            html_parts.append(
                f"<div class='chunk-content {content_class}'>{html.escape(chunk['content'])}</div>"
            )
            html_parts.append("</div>")

        html_parts.append("</div>")

    html_parts += [
        "<script>",
        "function toggleChunk(button){const content=button.closest('.chunk').querySelector('.chunk-content');"
        "content.classList.toggle('collapsed');button.textContent=content.classList.contains('collapsed')?'展开正文':'折叠正文';}",
        "function setAll(collapsed){document.querySelectorAll('.chunk-content').forEach(content=>content.classList.toggle('collapsed',collapsed));"
        "document.querySelectorAll('.toggle-content').forEach(button=>button.textContent=collapsed?'展开正文':'折叠正文');}",
        "</script></body></html>",
    ]

    html_path = output_dir / f"chunks_report_v5_{timestamp}.html"
    with open(html_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))
    print(f'[OK] HTML 报告: {html_path}')

    # ---- JSON ----
    json_path = output_dir / f"chunks_v5_{timestamp}.json"
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f'[OK] JSON 数据: {json_path}')

    latest_path = output_dir / 'chunks_v5_latest.json'
    with open(latest_path, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f'[OK] 稳定 JSON: {latest_path}')


def main():
    print('=' * 70)
    print('煤矿法规知识库 - 优化版章节分块工具 v5')
    print('=' * 70)

    json_dir = Path('new_docs/rule_docs_json')
    output_dir = Path('chunks_visualization')

    json_files = list(json_dir.glob('MinerU_*.json'))
    print(f'\n找到 {len(json_files)} 个 MinerU JSON 文件')

    all_results: Dict[str, List[Dict]] = {}

    for json_file in json_files:
        chunker = ImprovedChunkerV5(str(json_file))
        chunks = chunker.process()
        doc_name = json_file.stem.replace('MinerU_', '').split('__')[0]
        all_results[doc_name] = chunks

    print('\n' + '=' * 70)
    print('生成可视化报告...')
    generate_visualization(all_results, output_dir)

    total_tables = sum(
        sum(1 for c in chunks if '【表格】' in c['content'])
        for chunks in all_results.values()
    )
    print('\n' + '=' * 70)
    print('[OK] 处理完成！')
    print(f'  文档数: {len(all_results)}')
    print(f'  总 Chunk 数: {sum(len(c) for c in all_results.values())}')
    print(f'  含表格的 Chunk: {total_tables}')
    print('=' * 70)

    return all_results


if __name__ == '__main__':
    all_results = main()
