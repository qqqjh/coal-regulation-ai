"""
将文档分段并保存到文件
基于 compliance_review.py 的分段逻辑，将 93 个段落保存到文件中便于查看
"""
import sys
import json
import re
from pathlib import Path

# ── 路径配置 ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
BACKEND_DIR = PROJECT_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

DOC_PATH = (
    PROJECT_ROOT
    / "new_docs/test_doc"
    / "006     总工办S5102高抽巷掘进作业规程（掘进.doc"
)

# 输出文件
OUTPUT_JSON = PROJECT_ROOT / "chunks_output.json"
OUTPUT_TXT = PROJECT_ROOT / "chunks_output.txt"
OUTPUT_DIR = PROJECT_ROOT / "chunks"  # 每个段落单独文件的目录


# ══════════════════════════════════════════════════════════
# 1. 读取文档（复用 compliance_review.py 的逻辑）
# ══════════════════════════════════════════════════════════

def load_doc(path: Path) -> str:
    """读取 .doc/.docx 文件，返回纯文本"""
    if not path.exists():
        raise FileNotFoundError(f"找不到文档: {path}")

    ext = path.suffix.lower()

    if ext == ".docx":
        try:
            import docx2txt
            text = docx2txt.process(str(path))
        except ImportError:
            raise ImportError("请安装 docx2txt: pip install docx2txt")

    elif ext == ".doc":
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc_obj = word.Documents.Open(str(path.absolute()))
            text = doc_obj.Range().Text
            doc_obj.Close(False)
            word.Quit()
            pythoncom.CoUninitialize()
        except ImportError:
            raise ImportError("请安装 pywin32: pip install pywin32")
        except Exception as e:
            raise RuntimeError(f"读取 .doc 失败（需要 Windows + MS Word）: {e}")

    else:
        raise ValueError(f"不支持的文件格式: {ext}")

    # 去除多余空行
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text


def split_sections(text: str, max_chars: int = 1500):
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
# 2. 保存段落
# ══════════════════════════════════════════════════════════

def save_as_json(sections, output_path):
    """保存为 JSON 格式"""
    data = {
        "document": DOC_PATH.name,
        "total_sections": len(sections),
        "sections": [
            {
                "index": idx,
                "heading": sec["heading"],
                "content": sec["content"],
                "char_count": len(sec["content"])
            }
            for idx, sec in enumerate(sections, 1)
        ]
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"JSON 文件已保存: {output_path}")


def save_as_txt(sections, output_path):
    """保存为文本格式，带分隔符"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"文档: {DOC_PATH.name}\n")
        f.write(f"总段落数: {len(sections)}\n")
        f.write("=" * 80 + "\n\n")

        for idx, sec in enumerate(sections, 1):
            f.write(f"\n{'='*80}\n")
            f.write(f"段落 {idx}/{len(sections)}\n")
            f.write(f"标题: {sec['heading']}\n")
            f.write(f"字数: {len(sec['content'])}\n")
            f.write(f"{'-'*80}\n")
            f.write(sec['content'])
            f.write(f"\n{'='*80}\n\n")

    print(f"文本文件已保存: {output_path}")


def save_as_separate_files(sections, output_dir):
    """将每个段落保存为单独的文本文件"""
    output_dir = Path(output_dir)

    # 删除旧目录并重建
    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for idx, sec in enumerate(sections, 1):
        # 文件名：chunk_001.txt, chunk_002.txt, ...
        filename = f"chunk_{idx:03d}.txt"
        filepath = output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f"段落 {idx}/{len(sections)}\n")
            f.write(f"标题: {sec['heading']}\n")
            f.write(f"字数: {len(sec['content'])}\n")
            f.write("=" * 60 + "\n\n")
            f.write(sec['content'])

    print(f"已保存 {len(sections)} 个独立文件到: {output_dir}")


# ══════════════════════════════════════════════════════════
# 3. 主流程
# ══════════════════════════════════════════════════════════

def main():
    print("=" * 60)
    print("文档分段保存工具")
    print("=" * 60)

    # 1. 读取文档
    print(f"\n[1/3] 读取文档: {DOC_PATH.name}")
    doc_text = load_doc(DOC_PATH)
    print(f"      文档字数: {len(doc_text)}")

    # 2. 分段
    print("\n[2/3] 按章节切分文档...")
    sections = split_sections(doc_text, max_chars=1500)
    print(f"      切分为 {len(sections)} 个段落块")

    # 3. 保存
    print("\n[3/3] 保存段落到文件...")

    # 保存为 JSON
    save_as_json(sections, OUTPUT_JSON)

    # 保存为文本文件
    save_as_txt(sections, OUTPUT_TXT)

    # 保存为独立文件
    save_as_separate_files(sections, OUTPUT_DIR)

    print("\n" + "=" * 60)
    print("完成！")
    print("=" * 60)
    print(f"\n输出文件:")
    print(f"  1. JSON格式: {OUTPUT_JSON}")
    print(f"  2. 文本格式: {OUTPUT_TXT}")
    print(f"  3. 独立文件: {OUTPUT_DIR}/ (共 {len(sections)} 个文件)")
    print(f"\n段落统计:")
    print(f"  总段落数: {len(sections)}")
    print(f"  平均字数: {sum(len(s['content']) for s in sections) // len(sections)}")
    print(f"  最长段落: {max(len(s['content']) for s in sections)} 字")
    print(f"  最短段落: {min(len(s['content']) for s in sections)} 字")


if __name__ == "__main__":
    main()
