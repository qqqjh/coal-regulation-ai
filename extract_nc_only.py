"""
从三个审查报告 HTML 中提取不合规(NC)chunks，生成仅含NC结果的精简HTML。
"""

import re
import os

FILES = [
    ("刘致远/review_report_v5_20260417_203603.html", "LZY_v5_NC_only.html", "LZY（刘致远 v5）"),
    ("review_results/review_report_v5_20260417_204536.html", "V5_NC_only.html", "本系统 v5"),
    ("review_results/review_report_v6_final_20260421_with_kb.html", "V6_NC_only.html", "本系统 v6"),
]
OUTPUT_DIR = "review_results/nc_only_20260427"


def extract_nc_html(src_path: str, label: str) -> str:
    with open(src_path, encoding="utf-8") as f:
        content = f.read()

    # ── 1. 提取 <head>…</head> 及 <body> 开头直到第一个 doc-header ──────────
    body_open = content.find("<body")
    first_doc_header = content.find("<div class='doc-header'>")
    html_head = content[:first_doc_header]          # 含 <head> 和 body 开头统计卡

    # ── 2. 切分为 [段落]：每段以 doc-header 或 chunk-card 开头 ───────────────
    # 先把 doc-header 和 chunk-card 都当作切分点
    splitter = re.compile(
        r"(?=<div class='doc-header'>|<div class='chunk-card'>)"
    )
    segments = splitter.split(content[first_doc_header:])

    # ── 3. 只保留 doc-header 和含 badge-noncompliant 的 chunk-card ────────────
    kept = []
    for seg in segments:
        if seg.startswith("<div class='doc-header'>"):
            kept.append(seg)
        elif seg.startswith("<div class='chunk-card'>") and "badge-noncompliant" in seg:
            kept.append(seg)

    # ── 4. 去掉末尾空 doc-header（后面没有 NC chunk 的文档段）─────────────────
    cleaned = []
    for i, seg in enumerate(kept):
        if seg.startswith("<div class='doc-header'>"):
            # 判断后面是否紧跟着至少一个 chunk-card
            has_chunk = any(
                s.startswith("<div class='chunk-card'>")
                for s in kept[i+1:]
                if not s.startswith("<div class='doc-header'>")
                    or kept.index(s) == i+1  # 直接相邻的下一段
            )
            # 简单判断：下一个非空段是否是 chunk-card
            next_non_empty = next(
                (s for s in kept[i+1:] if s.strip()), None
            )
            if next_non_empty and next_non_empty.startswith("<div class='chunk-card'>"):
                cleaned.append(seg)
        else:
            cleaned.append(seg)

    nc_count = sum(1 for s in cleaned if s.startswith("<div class='chunk-card'>"))

    # ── 5. 提取 </body></html> 之前的脚本尾部 ────────────────────────────────
    script_tail_start = content.rfind("<script>")
    script_tail = content[script_tail_start:]       # 含 JS + </body></html>

    # ── 6. 替换 <head> 标题，注入 NC-only 说明横幅 ───────────────────────────
    html_head = re.sub(
        r"<title>[^<]*</title>",
        f"<title>【仅NC】{label}</title>",
        html_head,
    )

    banner = (
        f"<div style='background:#c0392b;color:white;padding:12px 20px;"
        f"font-size:1em;font-weight:bold;margin-bottom:16px;border-radius:6px;'>"
        f"⚠️ 仅显示不合规（NC）结果 — {label} &nbsp;|&nbsp; 共 {nc_count} 项"
        f"</div>\n"
    )

    # 在第一个 doc-header 之前插入横幅（html_head 末尾是 body 开头部分）
    body_content = banner + "".join(cleaned)

    return html_head + body_content + "\n" + script_tail


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for src, out_name, label in FILES:
        print(f"处理: {src} ...", end=" ", flush=True)
        html = extract_nc_html(src, label)
        out_path = os.path.join(OUTPUT_DIR, out_name)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        nc_count = html.count("badge-noncompliant") - html.count(".badge-noncompliant")
        print(f"完成 → {out_path}  (NC badge数≈{nc_count})")


if __name__ == "__main__":
    main()
