from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn


DOCX = Path("D:/work/project/coal-regulation-ai") / "\u7814\u7535\u8d5b\u6280\u672f\u8bba\u6587_\u538b\u7f29\u683c\u5f0f\u7248.docx"

ABSTRACT_CN = (
    "\u9488\u5bf9\u7164\u77ff\u4f5c\u4e1a\u89c4\u7a0b\u7bc7\u5e45\u957f\u3001\u6761\u6b3e\u5173\u8054\u590d\u6742\u3001\u4eba\u5de5\u5ba1\u67e5\u6548\u7387\u4f4e\u4ee5\u53ca\u73b0\u573a\u98ce\u9669\u4fe1\u606f\u591a\u6e90\u96be\u878d\u5408\u7b49\u95ee\u9898\uff0c\u672c\u4f5c\u54c1\u8bbe\u8ba1\u4e86\u4e00\u5957\u79fb\u52a8\u7aef\u534f\u540c\u591a\u667a\u80fd\u4f53\u7cfb\u7edf\u3002\u7cfb\u7edf\u4ee5 MinerU \u5b8c\u6210 OCR \u4e0e\u7248\u9762\u89e3\u6790\uff0c\u4ee5 BGE-M3 \u8fdb\u884c\u5d4c\u5165\u4e0e\u91cd\u6392\uff0c\u901a\u8fc7 Milvus \u7ba1\u7406\u5411\u91cf\u7d22\u5f15\uff0c\u901a\u8fc7 MongoDB \u4fdd\u5b58\u6587\u6863\u3001\u4efb\u52a1\u3001\u53cd\u9988\u548c\u7ecf\u9a8c\u6570\u636e\u3002\u4e3b\u667a\u80fd\u4f53\u57fa\u4e8e LangChain/LangGraph \u8fdb\u884c\u4efb\u52a1\u7406\u89e3\u3001\u5de5\u5177\u8c03\u7528\u548c\u591a\u667a\u80fd\u4f53\u8c03\u5ea6\uff0c\u5c06 Qwen \u7684\u8bed\u4e49\u7406\u89e3\u4e0e\u786e\u5b9a\u6027\u4ee3\u7801\u6838\u9a8c\u7ed3\u5408\uff0c\u63d0\u9ad8\u5408\u89c4\u5224\u65ad\u53ef\u4fe1\u5ea6\u3002\u7cfb\u7edf\u540c\u65f6\u8bbe\u8ba1 Web \u7aef\u548c\u5fae\u4fe1\u5c0f\u7a0b\u5e8f\u7aef\uff0c\u652f\u6301\u6587\u5b57\u3001\u8bed\u97f3\u4e0e\u56fe\u7247\u4e0a\u62a5\uff0c\u53ef\u7528\u4e8e\u89c4\u7a0b\u5ba1\u67e5\u3001\u73b0\u573a\u8ffd\u95ee\u3001\u98ce\u9669\u7814\u5224\u548c\u7ecf\u9a8c\u56de\u6d41\u3002"
)
ABSTRACT_EN = (
    "This work presents a mobile collaborative multi-agent system for coal mine safety review and field risk assessment. It uses MinerU for OCR, BGE-M3 for embedding and reranking, Milvus for vector search, MongoDB for business data, Redis for cache, and LangChain/LangGraph with Qwen for agent planning and tool calling. The Web client supports document review and feedback, while the WeChat mini program submits text, voice and images for on-site risk judgment."
)
REFS = [
    "[1] \u56fd\u5bb6\u77ff\u5c71\u5b89\u5168\u76d1\u5bdf\u5c40. \u7164\u77ff\u5b89\u5168\u89c4\u7a0b\uff082025\u7248\uff09.",
    "[2] Lewis P, Perez E, Piktus A, et al. Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks. NeurIPS, 2020.",
    "[3] Chen J, et al. BGE M3-Embedding: Multi-Lingual, Multi-Functionality, Multi-Granularity Text Embeddings, 2024.",
    "[4] Yao S, Zhao J, Yu D, et al. ReAct: Synergizing Reasoning and Acting in Language Models. ICLR, 2023.",
    "[5] Schick T, et al. Toolformer: Language Models Can Teach Themselves to Use Tools. NeurIPS, 2023.",
    "[6] Liu N F, et al. Lost in the Middle: How Language Models Use Long Contexts. TACL, 2024.",
    "[7] Guo C, Zeng J, Ma J, et al. Milvus: A Purpose-Built Vector Data Management System. SIGMOD, 2021.",
    "[8] Chodorow K. MongoDB: The Definitive Guide. O'Reilly Media, 2013.",
]


def set_cell(cell, text: str) -> None:
    cell.text = text
    for p in cell.paragraphs:
        p.paragraph_format.line_spacing = Pt(20)
        for r in p.runs:
            r.font.size = Pt(9)
            r.font.name = "Times New Roman"
            r._element.rPr.rFonts.set(qn("w:eastAsia"), "\u5b8b\u4f53")


def replace_table(table, rows: list[list[str]]) -> None:
    while len(table.rows) < len(rows):
        table.add_row()
    while len(table.rows) > len(rows):
        tr = table.rows[-1]._tr
        tr.getparent().remove(tr)
    for row_idx, row in enumerate(rows):
        for col_idx, value in enumerate(row):
            set_cell(table.cell(row_idx, col_idx), value)
        for col_idx in range(len(row), len(table.rows[row_idx].cells)):
            set_cell(table.cell(row_idx, col_idx), "")


def count_chars(doc: Document):
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    text = "\n".join(parts)
    return len(re.findall(r"[\u4e00-\u9fff]", text)), len(re.sub(r"\s+", "", text))


def main() -> None:
    doc = Document(str(DOCX))
    for i, p in enumerate(doc.paragraphs):
        t = p.text.strip()
        if t == "\u6458  \u8981" and i + 1 < len(doc.paragraphs):
            doc.paragraphs[i + 1].text = ABSTRACT_CN
        if t == "Abstract" and i + 1 < len(doc.paragraphs):
            doc.paragraphs[i + 1].text = ABSTRACT_EN
        if t.startswith("\u53c2\u8003\u6587\u732e"):
            j = i + 1
            for k, ref in enumerate(REFS):
                if j + k < len(doc.paragraphs):
                    doc.paragraphs[j + k].text = ref
            for k in range(j + len(REFS), len(doc.paragraphs)):
                if doc.paragraphs[k].text.strip().startswith("["):
                    doc.paragraphs[k].text = ""

    if len(doc.tables) >= 9:
        replace_table(doc.tables[0], [
            ["\u9879  \u76ee", "\u5185  \u5bb9"],
            ["\u4f5c\u54c1\u540d\u79f0", "\u9762\u5411\u7164\u77ff\u5b89\u5168\u89c4\u7a0b\u5ba1\u67e5\u4e0e\u73b0\u573a\u98ce\u9669\u7814\u5224\u7684\u79fb\u52a8\u7aef\u534f\u540c\u591a\u667a\u80fd\u4f53\u7cfb\u7edf"],
            ["\u8d5b\u9898\u65b9\u5411", "\u5927\u8bed\u8a00\u6a21\u578b\u3001\u591a\u6a21\u6001\u6a21\u578b\u53ca\u667a\u80fd\u4f53\u7cfb\u7edf"],
            ["\u5d4c\u5165\u5f0f\u5e73\u53f0", "\u5fae\u4fe1\u5c0f\u7a0b\u5e8f+\u667a\u80fd\u624b\u673a"],
        ])
        replace_table(doc.tables[1], [
            ["\u65b9\u6848", "\u4e3b\u8981\u8bbe\u7f6e", "\u6307\u6807"],
            ["\u7a20\u5bc6\u68c0\u7d22", "BGE-M3 \u5411\u91cf", "Hit@K/MRR"],
            ["\u6df7\u5408\u68c0\u7d22", "Dense+Sparse", "Recall/NDCG"],
            ["\u7236\u5b50\u68c0\u7d22", "\u7236\u5757+\u7247\u6bb5", "\u8986\u76d6\u7387/\u8017\u65f6"],
        ])
        replace_table(doc.tables[2], [
            ["\u65b9\u6848", "Hit@15", "MRR", "NDCG@10"],
            ["\u7a20\u5bc6\u68c0\u7d22", "0.7727", "0.4015", "0.2999"],
            ["\u6df7\u5408+\u91cd\u6392", "0.7727", "0.3439", "0.2912"],
            ["\u7236\u5b50\u68c0\u7d22", "0.7955", "0.4203", "0.3217"],
        ])
        replace_table(doc.tables[3], [
            ["\u667a\u80fd\u4f53", "\u529f\u80fd", "\u6307\u6807", "\u72b6\u6001"],
            ["\u68c0\u7d22", "\u53ec\u56de\u8bc1\u636e", "Hit@K/MRR", "\u5df2\u6d4b"],
            ["\u5408\u89c4", "\u5224\u65ad\u95ee\u9898", "P/R/F1", "\u5f85\u5b8c\u5584"],
            ["\u7ea0\u9519", "\u8bc6\u522b\u8868\u8ff0", "\u51c6\u786e\u7387", "\u5df2\u63a5\u5165"],
            ["\u6570\u503c", "\u5de5\u5177\u6838\u9a8c", "\u6b63\u786e\u7387", "\u5df2\u63a5\u5165"],
        ])
        replace_table(doc.tables[4], [
            ["\u5bf9\u8c61", "\u6307\u6807", "\u7ed3\u679c", "\u8bf4\u660e"],
            ["\u68c0\u7d22", "Hit@5/MRR", "0.6136/0.4203", "44\u4f8b"],
            ["\u5408\u89c4", "\u5339\u914d\u51c6\u786e\u7387", "56.82%", "\u5f85\u6269\u5145"],
            ["\u7ea0\u9519", "RAGAS", "\u5f85\u8865\u5145", "\u9884\u671f"],
            ["\u6570\u503c", "\u89e6\u53d1\u8986\u76d6", "56\u5757", "\u5de5\u5177\u6838\u9a8c"],
        ])
        replace_table(doc.tables[5], [
            ["\u6307\u6807", "\u8ba1\u7b97\u65b9\u5f0f", "\u76ee\u7684"],
            ["\u54cd\u5e94\u65f6\u95f4", "\u8f93\u5165\u5230\u7b56\u7565", "\u6548\u7387"],
            ["\u4efb\u52a1\u8bc6\u522b", "\u6b63\u786e\u5206\u7c7b\u7387", "\u7406\u89e3"],
            ["\u5de5\u5177\u9009\u62e9", "\u5de5\u5177\u5339\u914d\u7387", "\u8c03\u7528"],
            ["\u5347\u7ea7\u51b3\u7b56", "\u4eba\u5de5\u590d\u6838\u547d\u4e2d", "\u98ce\u9669"],
        ])
        replace_table(doc.tables[6], [
            ["\u4efb\u52a1", "\u5e73\u5747/s", "\u8bc6\u522b", "\u5de5\u5177", "\u5347\u7ea7"],
            ["\u5165\u5e93", "3.30", "100%", "80%", "100%"],
            ["\u5ba1\u67e5", "3.81", "100%", "11%", "100%"],
            ["\u590d\u6838", "3.70", "100%", "87%", "67%"],
            ["\u95ee\u7b54", "3.88", "100%", "61%", "100%"],
        ])
        replace_table(doc.tables[7], [
            ["\u6a21\u5f0f", "\u65b9\u5f0f", "\u6307\u6807"],
            ["\u4e32\u884c", "\u94fe\u8def\u4f9d\u6b21\u6267\u884c", "\u603b\u8017\u65f6"],
            ["\u94fe\u5e76\u884c", "\u4e13\u4e1a\u667a\u80fd\u4f53\u5e76\u884c", "\u9996\u6761\u8fd4\u56de"],
            ["\u5757\u5e76\u53d1", "\u591a\u5757+\u591a\u94fe", "\u541e\u5410/\u5f02\u5e38"],
        ])
        replace_table(doc.tables[8], [
            ["\u6a21\u5f0f", "\u5757\u6570", "\u603b\u8017\u65f6/s", "\u52a0\u901f\u6bd4", "\u5f02\u5e38"],
            ["\u4e32\u884c", "142", "940.6", "1.00x", "0%"],
            ["\u5e76\u884c", "142", "415.3", "2.26x", "0%"],
        ])

    doc.save(str(DOCX))
    cn, nonspace = count_chars(doc)
    print(f"cn_chars={cn}")
    print(f"nonspace_chars={nonspace}")
    print(f"paragraphs={len(doc.paragraphs)} tables={len(doc.tables)} shapes={len(doc.inline_shapes)}")


if __name__ == "__main__":
    main()
