"""
Plan-and-Execute 文档编辑器
完全依靠模型多轮 function calling 完成文档修改。
模型自主规划搜索关键词、确认段落、选择替换策略，最多 MAX_ROUNDS 轮。
"""

import json
from typing import Optional, Dict, List
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from word_modifier import _replace_in_para, _replace_para_content, _para_text, _normalize

API_KEY   = ""
API_BASE  = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL     = "qwen-plus"
MAX_ROUNDS = 10

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_paragraphs",
            "description": (
                "在文档中搜索包含关键词的段落，返回段落索引和内容（最多15条）。"
                "建议用原文中4-10个连续汉字作为关键词，避免使用标点符号。"
                "若无结果，请换用更短的片段重试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_paragraph",
            "description": "获取指定索引处段落的完整文本，用于确认是否是目标段落。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "段落索引（来自 search_paragraphs 的结果）"},
                },
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text_in_paragraph",
            "description": (
                "在指定段落中将 old_text 精确替换为 new_text，保留段落内格式（推荐优先使用）。"
                "old_text 必须与段落中的文字完全一致（包括标点和空格）。"
                "若替换失败，可先用 get_paragraph 获取段落原文再重试，或改用 replace_whole_paragraph。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index":    {"type": "integer", "description": "段落索引"},
                    "old_text": {"type": "string",  "description": "要替换的原文片段，必须与段落内容完全一致"},
                    "new_text": {"type": "string",  "description": "替换为的新文本"},
                },
                "required": ["index", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_whole_paragraph",
            "description": (
                "将指定段落的全部内容替换为新文本（保留段落级格式如缩进/编号，丢失行内加粗等样式）。"
                "当 replace_text_in_paragraph 多次失败时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index":    {"type": "integer", "description": "段落索引"},
                    "new_text": {"type": "string",  "description": "新的段落完整内容"},
                },
                "required": ["index", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_annotation_before",
            "description": (
                "在指定段落前插入一段橙色斜体批注文字，不修改原文内容。"
                "专用于重复内容标注场景。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index":           {"type": "integer", "description": "在哪个段落前插入批注"},
                    "annotation_text": {"type": "string",  "description": "批注文字内容"},
                },
                "required": ["index", "annotation_text"],
            },
        },
    },
]


def _build_system_prompt() -> str:
    return (
        "你是煤矿作业规程专家兼文档编辑助手。你的任务是通过调用工具对Word文档进行精确修改。\n\n"
        "你必须遵循【先计划后执行】的两阶段工作方式：\n\n"
        "═══ 阶段一：制定计划（不调用任何工具）═══\n"
        "收到修改任务后，先输出一段简短的计划，说明：\n"
        "  1. 准备用哪些关键词搜索目标段落（列出2-3个备选词，以防第一个搜不到）\n"
        "  2. 预期的修改策略（精确替换 / 整段替换 / 插入批注）\n"
        "  3. 若搜索失败的备选方案\n"
        "计划输出完毕后不要调用任何工具，等待系统指示执行。\n\n"
        "═══ 阶段二：执行计划（调用工具）═══\n"
        "按计划依次调用工具完成修改：\n"
        "  1. search_paragraphs → 定位段落\n"
        "  2. get_paragraph（可选）→ 确认段落内容\n"
        "  3. replace_text_in_paragraph（优先）或 replace_whole_paragraph → 完成修改\n"
        "  4. 重复内容只用 insert_annotation_before，不修改原文\n\n"
        "执行提示：\n"
        "- replace_text_in_paragraph 的 old_text 必须与段落原文完全一致\n"
        "- 替换失败时先 get_paragraph 获取准确原文再重试，或改用 replace_whole_paragraph\n"
        "- 搜索无结果时按计划中的备选关键词重试\n"
        "- 修改成功后立即停止，不输出其他内容"
    )


def _build_user_message(task: dict) -> str:
    fix_type = task.get("fix_type", "compliance")

    if fix_type == "typo":
        return (
            f"请完成以下错别字修正：\n"
            f"【错字】「{task.get('wrong_char', '')}」→「{task.get('corrected', '')}」\n"
            f"【上下文参考】…{task.get('original_context', '')}…\n\n"
            f"请搜索并定位含有该错字的段落，完成替换。"
        )

    if fix_type == "redundancy":
        annotation = task.get("rewritten_text") or task.get("suggested_fix", "")
        return (
            f"请在以下重复段落前插入批注（不修改原文）：\n"
            f"【目标段落片段】{task.get('original_text', '')[:80]}\n"
            f"【批注内容】{annotation}\n\n"
            f"请搜索定位该段落，然后在段落前插入批注。"
        )

    # compliance
    rewritten = task.get("rewritten_text", "").strip()
    original  = task.get("original_text", "")
    if rewritten:
        return (
            f"请将文档中以下原文片段替换为改写版本：\n"
            f"【原文片段】{original}\n"
            f"【改写版本】{rewritten}\n\n"
            f"请搜索定位含有原文片段的段落，将原文替换为改写版本。"
            f"若原文片段较长，可用其中4-10字作为搜索关键词。"
        )
    else:
        return (
            f"请根据修改建议修改文档：\n"
            f"【有问题的原文】{original}\n"
            f"【修改建议】{task.get('suggested_fix', '')}\n\n"
            f"请搜索定位原文所在段落，按修改建议完成最小化改动。"
        )


def apply_fix_with_llm(client, doc: Document, task: dict) -> Optional[Dict]:
    """
    通过多轮 function calling 完成文档修改。

    task 字段（按 fix_type 选填）：
        fix_type:         "compliance" | "typo" | "redundancy"
        original_text:    有问题的原文片段
        rewritten_text:   LLM 已生成的改写版本（compliance 优先使用）
        suggested_fix:    审查建议
        wrong_char:       错别字原字（typo）
        corrected:        修正字（typo）
        original_context: 错别字上下文（typo）

    成功返回 {"before": str, "after": str}，失败返回 None。
    """
    paras = doc.paragraphs

    # ── 工具实现 ────────────────────────────────────────────────

    def _search_paragraphs(keyword: str) -> List[dict]:
        norm_kw = _normalize(keyword)
        results = []
        for i, p in enumerate(paras):
            t = _para_text(p)
            if keyword in t or (norm_kw and norm_kw in _normalize(t)):
                results.append({"index": i, "text": t[:300]})
            if len(results) >= 15:
                break
        if not results:
            return [{"message": f'未找到包含"{keyword}"的段落，请尝试更短的关键词或去掉标点后重试'}]
        return results

    def _get_paragraph(index: int) -> dict:
        if 0 <= index < len(paras):
            return {"index": index, "text": _para_text(paras[index])}
        return {"error": f"索引 {index} 超出范围（共 {len(paras)} 段）"}

    def _replace_text(index: int, old_text: str, new_text: str) -> dict:
        if not (0 <= index < len(paras)):
            return {"success": False, "message": f"索引 {index} 超出范围"}
        ok = _replace_in_para(paras[index], old_text, new_text)
        if ok:
            return {"success": True, "message": "替换成功"}
        # 给出诊断信息帮助模型重试
        actual = _para_text(paras[index])[:200]
        return {
            "success": False,
            "message": f"未在该段落中找到 old_text，段落实际内容为：{actual}",
        }

    def _replace_whole(index: int, new_text: str) -> dict:
        if not (0 <= index < len(paras)):
            return {"success": False, "message": f"索引 {index} 超出范围"}
        ok = _replace_para_content(paras[index], new_text)
        return {"success": ok, "message": "段落已整体替换" if ok else "替换失败（段落无 run）"}

    def _insert_annotation(index: int, annotation_text: str) -> dict:
        if not (0 <= index < len(paras)):
            return {"success": False, "message": f"索引 {index} 超出范围"}
        new_p  = OxmlElement("w:p")
        new_r  = OxmlElement("w:r")
        rPr    = OxmlElement("w:rPr")
        col    = OxmlElement("w:color")
        col.set(qn("w:val"), "E67E22")
        ital   = OxmlElement("w:i")
        rPr.extend([col, ital])
        new_r.append(rPr)
        t_elem = OxmlElement("w:t")
        t_elem.text = annotation_text
        t_elem.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        new_r.append(t_elem)
        new_p.append(new_r)
        paras[index]._element.addprevious(new_p)
        return {"success": True, "message": "批注已插入"}

    # ── 对话初始化 ──────────────────────────────────────────────

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user",   "content": _build_user_message(task)},
    ]

    last_result: Optional[Dict] = None

    # ── 阶段一：Plan（禁止工具调用，强制输出计划文字）──────────

    plan_resp = client.chat.completions.create(
        model=MODEL,
        messages=messages,
        tools=_TOOLS,
        tool_choice="none",   # 不允许调工具，只允许输出文字
        temperature=0.2,
    )
    plan_text = (plan_resp.choices[0].message.content or "").strip()
    print(f"  [LLM计划]\n{plan_text}\n")

    # 把计划加入对话，后续执行阶段模型可参考
    messages.append({"role": "assistant", "content": plan_text})
    messages.append({"role": "user", "content": "计划已收到，请按计划执行修改。"})

    # ── 阶段二：Execute（多轮工具调用）────────────────────────

    for round_idx in range(MAX_ROUNDS):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
            temperature=0,
        )
        msg = resp.choices[0].message

        # 模型不再调用工具 → 任务完成或无法继续
        if not msg.tool_calls:
            print(f"  [LLM编辑] 第{round_idx+1}轮：模型停止工具调用，结束")
            break

        # 追加助手消息
        messages.append({
            "role":       "assistant",
            "content":    msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # 逐个执行工具调用
        for tc in msg.tool_calls:
            name = tc.function.name
            args = json.loads(tc.function.arguments)
            print(f"  [LLM编辑] 第{round_idx+1}轮 → {name}({args})")

            if name == "search_paragraphs":
                result = _search_paragraphs(args["keyword"])

            elif name == "get_paragraph":
                result = _get_paragraph(args["index"])

            elif name == "replace_text_in_paragraph":
                result = _replace_text(args["index"], args["old_text"], args["new_text"])
                if result["success"]:
                    last_result = {
                        "before": args["old_text"][:80] + ("…" if len(args["old_text"]) > 80 else ""),
                        "after":  args["new_text"][:80] + ("…" if len(args["new_text"]) > 80 else ""),
                    }

            elif name == "replace_whole_paragraph":
                result = _replace_whole(args["index"], args["new_text"])
                if result["success"]:
                    # 记录整段替换前的原文（已被覆盖，取任务中的原文字段）
                    before_text = task.get("original_text", "") or task.get("wrong_char", "")
                    last_result = {
                        "before": before_text[:80] + ("…" if len(before_text) > 80 else ""),
                        "after":  args["new_text"][:80] + ("…" if len(args["new_text"]) > 80 else ""),
                    }

            elif name == "insert_annotation_before":
                result = _insert_annotation(args["index"], args["annotation_text"])
                if result["success"]:
                    last_result = {"before": "", "after": args["annotation_text"][:80]}

            else:
                result = {"error": f"未知工具：{name}"}

            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      json.dumps(result, ensure_ascii=False),
            })

        # 写操作成功后立即返回，避免模型二次修改
        if last_result is not None:
            print(f"  [LLM编辑] 修改完成：{last_result}")
            return last_result

    if last_result is None:
        print("  [LLM编辑] 全部轮次结束，未能完成修改")
    return last_result

