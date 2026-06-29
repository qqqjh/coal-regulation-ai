"""
Word 文档最小改动工具
支持：合规问题修改 / 错别字修复 / 重复内容批注插入
所有操作通过 ChangeRecord 支持字节快照级撤回。
"""

import io
import re
import difflib
from typing import Optional, Dict, List, Tuple
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement


# ── 内部工具 ─────────────────────────────────────────────────────────

def _para_text(para) -> str:
    return "".join(r.text for r in para.runs)


def _normalize(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _find_para(doc, needle: str, threshold: float = 0.82) -> Optional[int]:
    """四步查找：精确 → 规范化 → 滑动短子串 → fuzzy（兜底）。

    重点改进：步骤3 用滑动窗口在 needle 中取 12-25 字的短片段做规范化精确匹配。
    PDF 提取文本与 DOCX 存在空格差异，但 12+ 字的汉字片段几乎不可能重复，
    且规范化后能消除单位数字间的空格（'100 mm' vs '100mm'）。
    步骤4 的 fuzzy 仅用于 needle 与段落长度相近时，避免"短串 vs 长段"的低比分陷阱。
    """
    norm_needle = _normalize(needle)
    paras = doc.paragraphs

    # 步骤1：精确包含
    for i, p in enumerate(paras):
        if needle in _para_text(p):
            return i

    # 步骤2：规范化后包含（去除所有空白）
    if norm_needle:
        for i, p in enumerate(paras):
            if norm_needle in _normalize(_para_text(p)):
                return i

    # 步骤3：滑动短子串匹配（核心改进）
    # 在 needle 中每隔 5 字取一个长度为 12~25 的窗口，只要任一窗口能在段落中找到就命中
    MIN_SUB = 12
    for win in (25, 20, 15, MIN_SUB):
        step = max(1, (len(needle) - win) // 6)
        for start in range(0, max(1, len(needle) - win + 1), step):
            sub_norm = _normalize(needle[start: start + win])
            if len(sub_norm) < MIN_SUB:
                continue
            for i, p in enumerate(paras):
                if sub_norm in _normalize(_para_text(p)):
                    return i

    # 步骤4：fuzzy（仅当 needle 与段落长度相近时才有意义）
    best_r, best_i = 0.0, None
    for i, p in enumerate(paras):
        pt = _normalize(_para_text(p))
        if not pt:
            continue
        # 防止"短串 vs 超长段落"给出虚低比值
        r = difflib.SequenceMatcher(None, norm_needle, pt).ratio()
        if r > best_r:
            best_r, best_i = r, i
    return best_i if best_r >= threshold else None


def _replace_in_para(para, old: str, new: str) -> bool:
    """跨 run 原位替换 old→new，保留各 run 格式。精确失败时尝试规范化匹配。"""
    full = _para_text(para)

    # 尝试在规范化文本中定位，找到原始文本中的等价位置
    if old not in full:
        norm_old  = _normalize(old)
        norm_full = _normalize(full)
        if not norm_old or norm_old not in norm_full:
            return False
        # 用 difflib 找到 full 中与 old 对齐程度最高的子串
        best_old = _find_actual_span(full, old)
        if best_old is None or best_old not in full:
            return False
        old = best_old

    start = full.index(old)
    end   = start + len(old)
    pos, new_texts = 0, []
    for r in para.runs:
        rs, re_ = pos, pos + len(r.text)
        pos = re_
        if re_ <= start or rs >= end:
            new_texts.append(r.text)
        elif rs <= start and re_ >= end:
            new_texts.append(r.text[: start - rs] + new + r.text[end - rs :])
        elif rs <= start:
            new_texts.append(r.text[: start - rs] + new)
        elif re_ >= end:
            new_texts.append(r.text[end - rs :])
        else:
            new_texts.append("")
    for r, t in zip(para.runs, new_texts):
        r.text = t
    return True


def _find_actual_span(full_text: str, target: str) -> Optional[str]:
    """
    在 full_text 中滑动窗口搜索与 target（规范化后）最接近的子串。
    返回在 full_text 中的原始子串，或 None。
    """
    norm_t = _normalize(target)
    if not norm_t:
        return None
    win = len(target)                       # 窗口大小与 target 等长
    best_r, best_span = 0.0, None
    for start in range(max(0, len(full_text) - win * 2)):
        for end in range(start + max(len(norm_t) - 5, 5),
                         min(start + win * 2, len(full_text)) + 1):
            span = full_text[start:end]
            r = difflib.SequenceMatcher(None, norm_t, _normalize(span)).ratio()
            if r > best_r:
                best_r, best_span = r, span
    return best_span if best_r >= 0.82 else None


def _replace_para_content(para, new_text: str) -> bool:
    """
    整段替换：把新文本写入第一个 run，清空其余 run。
    保留段落级格式（缩进/编号），丢失 run 内行内格式（加粗等）。
    仅当 _replace_in_para 失败时作为最后手段调用。
    """
    if not para.runs:
        return False
    para.runs[0].text = new_text
    for r in para.runs[1:]:
        r.text = ""
    return True


# ── 三类修改函数 ──────────────────────────────────────────────────────

def apply_para_rewrite(doc, original_text: str, rewritten_text: str) -> Optional[Dict]:
    """
    用 LLM 改写后的文本替换段落中对应的原文片段。
    优先：在段落内精确/规范化替换 original_text → rewritten_text（最小改动）。
    保底：若无法精确定位，才做整段替换（保留段落格式，丢失行内样式）。
    """
    para_idx = _find_para(doc, original_text, threshold=0.70)
    if para_idx is None:
        return None
    para = doc.paragraphs[para_idx]

    # 优先：只替换 original_text 那一部分，不影响段落其余内容
    if _replace_in_para(para, original_text, rewritten_text):
        return {
            "before": original_text[:80] + ("…" if len(original_text) > 80 else ""),
            "after":  rewritten_text[:80] + ("…" if len(rewritten_text) > 80 else ""),
        }

    # 保底：整段替换
    full_before = _para_text(para)
    if _replace_para_content(para, rewritten_text):
        return {
            "before": full_before[:80] + ("…" if len(full_before) > 80 else ""),
            "after":  rewritten_text[:80] + ("…" if len(rewritten_text) > 80 else ""),
        }
    return None


def apply_compliance_fix(doc, original_text: str, suggested_fix: str) -> Optional[Dict]:
    """
    合规问题：解析 suggested_fix 中的 '将X改为Y' 模式执行最小替换。
    解析失败时以 original_text→suggested_fix 整体替换保底。
    两者均失败时做整段替换（保留段落格式，丢失行内样式）。
    返回 {"before", "after"} 或 None（定位失败）。
    """
    m = re.search(
        r"将\s*[\"'「『【](.+?)[\"'」』】]\s*改为\s*[\"'「『【](.+?)[\"'」』】]",
        suggested_fix,
    )
    old_val = m.group(1) if m else original_text
    new_val = m.group(2) if m else suggested_fix

    # 阶段1：用解析出的 old_val 定位段落
    para_idx = _find_para(doc, old_val, threshold=0.75)
    if para_idx is None and old_val != original_text:
        para_idx = _find_para(doc, original_text, threshold=0.75)
        if para_idx is not None:
            old_val, new_val = original_text, suggested_fix

    if para_idx is None:
        return None

    para = doc.paragraphs[para_idx]

    # 阶段2：精确/规范化替换（_replace_in_para 内部含规范化回退）
    if _replace_in_para(para, old_val, new_val):
        return {"before": old_val, "after": new_val}

    # 阶段3：整段替换保底（找到了段落但无法精确定位字符）
    full_before = _para_text(para)
    if _replace_para_content(para, new_val):
        return {"before": full_before[:60] + ("…" if len(full_before) > 60 else ""),
                "after":  new_val[:60]   + ("…" if len(new_val) > 60 else "")}

    return None


def apply_typo_fix(doc, original_context: str, wrong_char: str, corrected: str) -> Optional[Dict]:
    """
    错别字：在含 original_context 的段落中原位替换第一个 wrong_char→corrected。
    """
    para_idx = _find_para(doc, original_context or wrong_char)
    if para_idx is None:
        return None
    if not _replace_in_para(doc.paragraphs[para_idx], wrong_char, corrected):
        return None
    return {"before": wrong_char, "after": corrected}


def apply_redundancy_fix(
    doc,
    chunk_b_content: str,
    chunk_b_chapter: str = "",
    chunk_b_section: str = "",
    similarity: float = 0.0,
) -> Optional[Dict]:
    """
    重复内容：在重复段落前插入橙色斜体批注段。
    不删除正文，只做标注；撤回时删除插入段。
    """
    search = chunk_b_content[:40].strip()
    para_idx = _find_para(doc, search, threshold=0.70)
    if para_idx is None:
        return None

    loc = f"{chunk_b_chapter} {chunk_b_section}".strip()
    note = (
        f'【重复内容批注】本段与"{loc}"高度相似（相似度 {similarity:.0%}），'
        '建议人工核查后决定是否删除。'
    )

    # 构造批注段 XML（橙色 + 斜体）
    new_p = OxmlElement("w:p")
    new_r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    col = OxmlElement("w:color")
    col.set(qn("w:val"), "E67E22")
    ital = OxmlElement("w:i")
    rPr.extend([col, ital])
    new_r.append(rPr)
    t_elem = OxmlElement("w:t")
    t_elem.text = note
    t_elem.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    new_r.append(t_elem)
    new_p.append(new_r)

    doc.paragraphs[para_idx]._element.addprevious(new_p)
    return {"before": "", "after": note, "_inserted_xml_id": id(new_p)}


# ── 序列化 / 反序列化 ─────────────────────────────────────────────────

def doc_to_bytes(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def bytes_to_doc(data: bytes) -> Document:
    return Document(io.BytesIO(data))


# ── DOCX → PDF（Word COM，Windows only）─────────────────────────────

def _word_com_convert(src_docx: Path, page_from: int = 0, page_to: int = 0) -> bytes:
    """
    Word COM 转换核心：把 src_docx 转为 PDF 字节。
    先复制到临时文件再打开，确保不锁住 src_docx。
    page_from/page_to 均为 0 时导出全文，否则只导出指定页范围。
    """
    import win32com.client
    import pythoncom
    import tempfile
    import shutil

    tmp_docx  = Path(tempfile.mktemp(suffix=".docx"))
    pdf_path  = Path(tempfile.mktemp(suffix=".pdf"))
    word, doc = None, None

    shutil.copy2(src_docx, tmp_docx)
    pythoncom.CoInitialize()
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(str(tmp_docx.resolve()))
        if page_from > 0 and page_to > 0:
            doc.ExportAsFixedFormat(
                str(pdf_path.resolve()),
                17,        # wdExportFormatPDF
                False,     # OpenAfterExport
                0,         # OptimizeFor = wdExportOptimizeForPrint
                3,         # Range = wdExportFromTo
                page_from,
                page_to,
            )
        else:
            doc.SaveAs2(str(pdf_path.resolve()), FileFormat=17)
    finally:
        # 每一步都独立捕获，确保清理不中断也不向上抛异常
        if doc is not None:
            try: doc.Close(False)
            except Exception: pass
        if word is not None:
            try: word.Quit()
            except Exception: pass
        try: pythoncom.CoUninitialize()
        except Exception: pass
        try: tmp_docx.unlink()
        except Exception: pass

    # 若 PDF 未生成（转换失败），read_bytes 会自然抛 FileNotFoundError
    data = pdf_path.read_bytes()
    try: pdf_path.unlink()
    except Exception: pass
    return data


def docx_page_to_pdf_bytes(docx_path: Path, page_num: int,
                           cache_path: Optional[Path] = None) -> bytes:
    """
    导出 DOCX 中第 page_num 页为 PDF（1-based）。
    策略：先将整份 DOCX 转为完整 PDF（缓存到 cache_path），
    再用 pypdf 提取目标页——比 ExportAsFixedFormat 更可靠。
    cache_path 由调用方提供，命中时直接读缓存，跳过 Word COM 转换。
    """
    import io
    from pypdf import PdfReader, PdfWriter

    # 读缓存或重新转换
    if cache_path and cache_path.exists():
        full_pdf = cache_path.read_bytes()
    else:
        full_pdf = _word_com_convert(docx_path)
        if cache_path:
            try: cache_path.write_bytes(full_pdf)
            except Exception: pass

    # 提取单页
    reader = PdfReader(io.BytesIO(full_pdf))
    idx    = max(0, page_num - 1)
    if idx >= len(reader.pages):
        idx = len(reader.pages) - 1
    writer = PdfWriter()
    writer.add_page(reader.pages[idx])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def docx_to_pdf_bytes(docx_path: Path) -> bytes:
    """将 DOCX 全文转为 PDF 字节流。"""
    return _word_com_convert(docx_path)
