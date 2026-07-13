from __future__ import annotations

import json
import shutil
from pathlib import Path

import win32com.client as win32


ROOT = Path(r"D:\work\project\coal-regulation-ai")
OUT = ROOT / "\u5206\u5de5_\u4e09\u4eba\u6574\u5408\u7248.docx"
OUT = ROOT / "\u5206\u5de5_\u4e09\u4eba\u6574\u5408\u7248_20260617.docx"

SRC_MINE = Path(r"D:\work\项目\研电赛\分工_第二三章和第六章1-4.docx")
SRC_LZY = Path(r"D:\work\项目\研电赛\分工_lzy.docx")
SRC_YY = Path(r"D:\work\项目\研电赛\分工_yy.docx")


def para_text(paragraph) -> str:
    return paragraph.Range.Text.replace("\r", "").replace("\x07", "").strip()


def find_para(doc, prefix: str) -> int:
    for i in range(1, doc.Paragraphs.Count + 1):
        text = para_text(doc.Paragraphs(i))
        if text.startswith(prefix):
            return i
    raise RuntimeError(f"heading not found: {prefix}")


def section_range(doc, start_prefix: str, end_prefix: str):
    start_idx = find_para(doc, start_prefix)
    end_idx = find_para(doc, end_prefix)
    start = doc.Paragraphs(start_idx).Range.Start
    end = doc.Paragraphs(end_idx).Range.Start
    return doc.Range(Start=start, End=end)


def replace_section(target_doc, source_doc, start_prefix: str, end_prefix: str) -> None:
    target = section_range(target_doc, start_prefix, end_prefix)
    source = section_range(source_doc, start_prefix, end_prefix)
    target.FormattedText = source.FormattedText


def replace_text(doc, old: str, new: str) -> int:
    count = 0
    find = doc.Content.Find
    find.ClearFormatting()
    find.Replacement.ClearFormatting()
    find.Text = old
    find.Replacement.Text = new
    find.Forward = True
    find.Wrap = 1  # wdFindContinue
    find.Format = False
    find.MatchCase = False
    find.MatchWholeWord = False
    find.MatchWildcards = False
    find.MatchSoundsLike = False
    find.MatchAllWordForms = False
    while find.Execute(Replace=1):  # wdReplaceOne
        count += 1
    return count


def main() -> None:
    if OUT.exists():
        OUT.unlink()
    shutil.copy2(SRC_YY, OUT)

    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0

    docs = {}
    replacements = {}
    try:
        docs["out"] = word.Documents.Open(str(OUT), ReadOnly=False)
        docs["mine"] = word.Documents.Open(str(SRC_MINE), ReadOnly=True)
        docs["lzy"] = word.Documents.Open(str(SRC_LZY), ReadOnly=True)

        # Use yy as the base document. Replace only the sections assigned to
        # the other two authors, preserving yy's front matter, 4.2, 5.1, 5.3,
        # 5.6, 6.5, chapter 7, application value, and references.
        steps = [
            ("mine", "\u7b2c\u4e8c\u7ae0", "\u7b2c\u4e09\u7ae0"),
            ("mine", "\u7b2c\u4e09\u7ae0", "\u7b2c\u56db\u7ae0"),
            ("lzy", "4.1", "4.2"),
            ("lzy", "5.2", "5.3"),
            ("lzy", "5.4", "5.5"),
            ("lzy", "5.5", "5.6"),
            ("mine", "6.1", "6.5"),
        ]
        for source_name, start, end in steps:
            print(f"replace {start} -> {end} from {source_name}", flush=True)
            replace_section(docs["out"], docs[source_name], start, end)
            docs["out"].Save()

        docs["out"].Save()
    finally:
        for doc in list(docs.values()):
            try:
                doc.Close(SaveChanges=False)
            except Exception:
                pass
        word.Quit()

    print(json.dumps({"output": str(OUT), "replacements": replacements}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
