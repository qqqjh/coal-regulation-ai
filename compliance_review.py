"""
煤矿掘进作业规程合规性审查
===============================
目标文档: 006 总工办S5102高抽巷掘进作业规程（掘进.doc
向量数据库: backend/data/chroma_db  (kb_id=2)
审查模型:  Qwen3-max (DashScope)
"""

import os
import sys
import json
import asyncio
import re
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any, Tuple

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
BACKEND_DIR  = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

DOC_PATH = (
    PROJECT_ROOT
    / "new_docs/test_doc"
    / "006     总工办S5102高抽巷掘进作业规程（掘进.doc"
)

REPORT_PATH = PROJECT_ROOT / "compliance_report.json"

# RAG 知识库 ID（重建时全部入库到 kb_id=2）
KB_ID = 2

# ── 依赖 ──────────────────────────────────────────────────
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain_core.prompts import ChatPromptTemplate
from app.core.config import settings


# ══════════════════════════════════════════════════════════
# 1. 读取文档
# ══════════════════════════════════════════════════════════

def load_doc(path: Path) -> str:
    """读取 .doc/.docx 文件，返回纯文本"""
    if not path.exists():
        raise FileNotFoundError(f"找不到文档: {path}")

    ext = path.suffix.lower()

    if ext == ".docx":
        # .docx 用 docx2txt
        try:
            import docx2txt
            text = docx2txt.process(str(path))
        except ImportError:
            raise ImportError("请安装 docx2txt: pip install docx2txt")

    elif ext == ".doc":
        # .doc 用 pywin32 (Windows only)
        try:
            import win32com.client
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            doc = word.Documents.Open(str(path.absolute()))
            text = doc.Content.Text
            doc.Close(False)
            word.Quit()
        except ImportError:
            raise ImportError("请安装 pywin32: pip install pywin32")
        except Exception as e:
            raise RuntimeError(f"读取 .doc 失败（需要 Windows + MS Word）: {e}")

    else:
        raise ValueError(f"不支持的文件格式: {ext}")

    # 去除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def split_sections(text: str, max_chars: int = 1500) -> List[Dict[str, str]]:
    """
    将文档按章节标题切分为若干段落块。
    每块不超过 max_chars 字符，避免单次 prompt 过长。
    """
    # 常见章节标题模式：第X章、X.X、一、二、三……
    heading_pattern = re.compile(
        r'(?m)^(?:第[一二三四五六七八九十百\d]+[章节条]|'
        r'\d+[\.\s]+\S|'
        r'[一二三四五六七八九十]+[、\.\s])'
    )

    boundaries = [m.start() for m in heading_pattern.finditer(text)]
    if not boundaries:
        boundaries = []

    boundaries = [0] + boundaries + [len(text)]
    raw_sections = [text[boundaries[i]:boundaries[i+1]].strip()
                    for i in range(len(boundaries) - 1)
                    if text[boundaries[i]:boundaries[i+1]].strip()]

    # 超长段落再切分
    sections = []
    for sec in raw_sections:
        if len(sec) <= max_chars:
            heading = sec.split('\n')[0][:40]
            sections.append({"heading": heading, "content": sec})
        else:
            for start in range(0, len(sec), max_chars):
                chunk = sec[start:start + max_chars]
                heading = sec.split('\n')[0][:40]
                sections.append({
                    "heading": f"{heading}（续{start//max_chars + 1}）",
                    "content": chunk
                })

    return sections


# ══════════════════════════════════════════════════════════
# 2. RAG 检索
# ══════════════════════════════════════════════════════════

def build_retriever():
    """构建与原向量库相同配置的检索器"""
    embeddings = OpenAIEmbeddings(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
    )
    vector_store = Chroma(
        persist_directory=str(settings.CHROMA_DB_DIR),
        embedding_function=embeddings,
        collection_name=f"kb_{KB_ID}",
    )
    return vector_store


def retrieve_regulations(vector_store: Chroma, query: str, k: int = 5) -> str:
    """检索最相关的规程条款，返回格式化字符串"""
    docs = vector_store.similarity_search(query, k=k)
    if not docs:
        return "（未检索到相关规程条款）"

    parts = []
    for i, doc in enumerate(docs, 1):
        fname = doc.metadata.get("filename", "规程")
        page  = doc.metadata.get("page", "?")
        parts.append(f"[条款{i}] {fname} 第{page}页\n{doc.page_content.strip()}")

    return "\n\n".join(parts)


# ══════════════════════════════════════════════════════════
# 3. LLM 合规审查（Qwen3-max）
# ══════════════════════════════════════════════════════════

def build_llm() -> ChatOpenAI:
    """构建 Qwen3-max 模型实例（DashScope OpenAI 兼容接口）"""
    api_key = "sk-ed464c0a923b47e8b61c5b82af7acfa8"

    return ChatOpenAI(
        model="qwen-max",
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        temperature=0.1,
    )


SECTION_REVIEW_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一位资深的煤矿安全合规专家，专门审查掘进作业规程。

**审查依据（RAG检索到的规程条款）：**
{regulations}

**审查任务：**
1.优先参考RAG检索到的规程条款。
2.结合通用工程常识和煤矿安全法规逻辑，判断文档中的数值是否实质上满足安全要求。
3.如果文档中的参数（如3m）高于检索条款中的最低标准（如2m），则判定为合规。只有当文档参数低于最低标准，或缺失关键条款要求时，才判定为违规。
4.严禁生搬硬套检索结果，必须进行实质性的逻辑对比。

**输出要求：**
严格输出 JSON 数组，每个问题一个对象，字段如下：
- "issue"：问题简述（≤30字）
- "original"：文档中存在问题的原文片段（≤80字）
- "regulation_ref"：所依据的规程条款摘要（≤60字）
- "suggestion"：修改建议（≤80字）
- "severity"：严重程度，只能是 "严重" / "一般" / "建议"

若该片段无任何合规问题，则输出空数组 []。
不要在 JSON 之外输出任何文字。"""),
    ("human", "【文档片段标题】{heading}\n\n【文档片段内容】\n{content}"),
])

SUMMARY_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是煤矿安全合规专家，请根据各段落的审查结果生成综合审查报告摘要。

要求：
1. 总结文档的整体合规情况
2. 列出最关键的问题（按严重程度排序）
3. 提出总体改进建议
4. 语言简洁、专业
5. 字数 300-500 字"""),
    ("human", """文档名称：{doc_name}
总问题数：{total}（严重: {severe}，一般: {normal}，建议: {advice}）

各段落审查结果汇总：
{issue_summary}

请生成综合审查报告摘要。"""),
])


async def review_section(
    llm: ChatOpenAI,
    vector_store: Chroma,
    section: Dict[str, str],
    idx: int,
    total: int,
) -> List[Dict]:
    """审查单个章节，返回问题列表"""
    # 用章节前200字检索相关规程
    query = section["content"][:200]
    regulations = retrieve_regulations(vector_store, query, k=5)

    chain = SECTION_REVIEW_PROMPT | llm
    try:
        result = await chain.ainvoke({
            "regulations": regulations,
            "heading":     section["heading"],
            "content":     section["content"],
        })
        raw = result.content.strip()
        # 去除可能的 markdown code fence
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        issues = json.loads(raw)
        if not isinstance(issues, list):
            issues = []
    except Exception as e:
        print(f"  [WARN] 第{idx}段解析失败: {e}")
        issues = []

    label = "OK" if not issues else f"{len(issues)}个问题"
    print(f"  [{idx}/{total}] {section['heading'][:30]:<32} → {label}")
    return issues


async def generate_summary(
    llm: ChatOpenAI,
    doc_name: str,
    all_issues: List[Dict],
) -> str:
    """生成综合审查摘要"""
    severe = sum(1 for i in all_issues if i.get("severity") == "严重")
    normal = sum(1 for i in all_issues if i.get("severity") == "一般")
    advice = sum(1 for i in all_issues if i.get("severity") == "建议")

    # 每类取前3条作为摘要输入
    top_issues = (
        [i for i in all_issues if i.get("severity") == "严重"][:3]
        + [i for i in all_issues if i.get("severity") == "一般"][:3]
        + [i for i in all_issues if i.get("severity") == "建议"][:2]
    )
    issue_summary = "\n".join(
        f"- [{i.get('severity','')}] {i.get('issue','')}：{i.get('suggestion','')}"
        for i in top_issues
    )

    chain = SUMMARY_PROMPT | llm
    result = await chain.ainvoke({
        "doc_name":     doc_name,
        "total":        len(all_issues),
        "severe":       severe,
        "normal":       normal,
        "advice":       advice,
        "issue_summary": issue_summary or "（无问题）",
    })
    return result.content.strip()


# ══════════════════════════════════════════════════════════
# 4. 主流程
# ══════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("煤矿掘进作业规程合规性审查")
    print("=" * 60)

    # ── 读取文档 ─────────────────────────────────────
    print(f"\n[1/4] 读取文档: {DOC_PATH.name}")
    doc_text = load_doc(DOC_PATH)
    print(f"      文档字数: {len(doc_text)}")

    # ── 分段 ─────────────────────────────────────────
    print("\n[2/4] 按章节切分文档...")
    sections = split_sections(doc_text, max_chars=1500)
    print(f"      切分为 {len(sections)} 个段落块")

    # ── 初始化模型和向量库 ───────────────────────────
    print("\n[3/4] 初始化模型和 RAG 向量库...")
    llm          = build_llm()
    vector_store = build_retriever()
    print(f"      模型: qwen3-max  |  向量库: kb_{KB_ID}")

    # ── 逐段审查 ─────────────────────────────────────
    print(f"\n[4/4] 逐段合规审查（共 {len(sections)} 段）...")
    all_issues: List[Dict] = []
    section_results = []

    for idx, sec in enumerate(sections, 1):
        try:
            issues = await review_section(llm, vector_store, sec, idx, len(sections))
            # 给每个问题加上来源章节
            for iss in issues:
                iss["section"] = sec["heading"]
            all_issues.extend(issues)
            section_results.append({
                "section_index": idx,
                "heading":       sec["heading"],
                "issues":        issues,
            })
        except Exception as e:
            print(f"  [ERROR] 第{idx}段审查失败: {e}")
            section_results.append({
                "section_index": idx,
                "heading":       sec["heading"],
                "issues":        [],
                "error":         str(e),
            })

    # ── 生成汇总摘要 ─────────────────────────────────
    print("\n生成综合审查摘要...")
    summary = await generate_summary(llm, DOC_PATH.name, all_issues)

    # ── 统计 ──────────────────────────────────────────
    severe = [i for i in all_issues if i.get("severity") == "严重"]
    normal = [i for i in all_issues if i.get("severity") == "一般"]
    advice = [i for i in all_issues if i.get("severity") == "建议"]

    # ── 生成报告 ─────────────────────────────────────
    report = {
        "meta": {
            "document":    DOC_PATH.name,
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            "model":       "qwen3-max",
            "kb_id":       KB_ID,
            "total_sections": len(sections),
        },
        "statistics": {
            "total_issues":  len(all_issues),
            "severe":        len(severe),
            "normal":        len(normal),
            "advice":        len(advice),
        },
        "summary": summary,
        "severe_issues": severe,
        "all_issues":    all_issues,
        "section_results": section_results,
    }

    # ── 保存报告 ─────────────────────────────────────
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # ── 控制台输出 ───────────────────────────────────
    print("\n" + "=" * 60)
    print("审查完成")
    print("=" * 60)
    print(f"  总问题数 : {len(all_issues)}")
    print(f"  严重     : {len(severe)}")
    print(f"  一般     : {len(normal)}")
    print(f"  建议     : {len(advice)}")
    print(f"\n报告已保存: {REPORT_PATH}")

    if severe:
        print("\n【严重问题列表】")
        for i, iss in enumerate(severe, 1):
            print(f"  {i}. [{iss.get('section','')}] {iss.get('issue','')}")
            print(f"     原文: {iss.get('original','')[:50]}")
            print(f"     建议: {iss.get('suggestion','')}")

    print("\n【审查摘要】")
    print(summary)


if __name__ == "__main__":
    asyncio.run(main())
