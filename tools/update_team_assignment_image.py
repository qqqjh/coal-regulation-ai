from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


SRC = Path(r"C:\Users\Administrator\AppData\Local\Temp\codex-clipboard-0ec21393-e3d9-4056-940f-1e3ce8530c33.png")
OUT = Path(r"D:\work\project\coal-regulation-ai\output\team_assignment_project_updated.png")


def draw_lines(draw, xy, lines, font, line_gap=27):
    x, y = xy
    for i, text in enumerate(lines):
        draw.text((x, y + i * line_gap), text, fill=(23, 23, 23), font=font)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image = Image.open(SRC).convert("RGBA")
    draw = ImageDraw.Draw(image)

    font_path = r"C:\Windows\Fonts\STKAITI.TTF"
    font = ImageFont.truetype(font_path, 22)

    # Cover only the original right-side responsibility lists, preserving the blue braces.
    bg = (250, 249, 247, 255)
    draw.rectangle((462, 44, 786, 205), fill=bg)
    draw.rectangle((462, 236, 786, 398), fill=bg)

    member_one = [
        "主智能体策略规划与任务调度",
        "专业智能体协同编排",
        "合规/纠错/判重智能体设计",
        "数值核验与工具调用机制",
        "争议升级与人工反馈闭环",
        "...",
    ]
    member_two = [
        "规则文档结构化解析",
        "父子块切分与元数据标注",
        "BGE-M3 混合向量检索",
        "法规证据重排序与去重",
        "MongoDB+Milvus 知识存储",
        "...",
    ]

    draw_lines(draw, (467, 49), member_one, font)
    draw_lines(draw, (467, 244), member_two, font)
    image.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()
