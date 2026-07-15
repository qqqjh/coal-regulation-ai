"""v10 审查链中的同步主智能体冲突裁决器。

该模块处理两类分歧：初审与复核意见相反，以及数值工具与 LLM 最终意见
相反。它不重新执行整篇审查，也不自行扩展法规知识；裁决所需的待审原文、
两级LLM意见、数值工具结论和本次已召回法规均由调用方一次性提供。

与 ``main_agent_v9.py`` 的异步升级队列不同，本裁决器位于单 chunk 主链内：
前端收到的问题已经是主智能体裁决后的最终结果。证据不足时返回“转人工”，
再由原升级队列和前端人工裁决承接。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List


ChatCallable = Callable[..., str]


MAIN_ADJUDICATION_SYSTEM_PROMPT = """你是煤矿规程审核系统的主智能体裁决员。

你只裁决已经明确列出的分歧，不重新审查未发生分歧的内容。唯一可用证据是
用户消息中提供的待审原文、已召回法规原文、初审问题、复核结论和数值工具结果。

分歧类型与允许决定：
1. verifier_removed：初审报告问题、复核删除。只能“保留初审”“采纳复核”“转人工”。
2. numeric_tool_says_compliant：两个LLM保留问题，但数值工具判合规。只能“保留LLM”“采纳数值工具”“转人工”。
3. numeric_tool_says_noncompliant：LLM最终结果未报告问题，但数值工具判不合规。只能“保留LLM”“采纳数值工具”“转人工”。
“采纳数值工具”在第3类中仅允许按工具提供的原文生成该项数值问题，不得扩展出其他问题。

裁决原则：
1. 待审原文与法规原文存在直接、同场景冲突，保留初审；
2. 初审引用错场景、错对象、知识库不存在的内容，或待审要求实际严于法规，采纳复核；
3. 数值问题优先采用有 pending_quote、rule_quote 且 evidence_grounded=true 的工具结论；
4. 证据缺失、场景无法确认或双方都有合理依据时转人工，禁止猜测；
5. 矿井类型为非突出时，突出矿井/突出煤层专用条款不得作为依据；矿井类型为突出时，通用条款和突出专用条款均可适用；
6. 每个 conflict_id 必须且只能给出一项符合其分歧类型的决定。

只输出严格 JSON，不要 Markdown：
{
  "decisions": [
    {
      "conflict_id": "C1",
      "decision": "保留初审|采纳复核|保留LLM|采纳数值工具|转人工",
      "reason": "引用双方原文说明裁决依据"
    }
  ],
  "summary": "一句话概括本次裁决"
}
"""


def _parse_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_decision(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "保留初审": "保留初审",
        "保留": "保留初审",
        "keep": "保留初审",
        "采纳复核": "采纳复核",
        "采纳": "采纳复核",
        "drop": "采纳复核",
        "删除": "采纳复核",
        "保留llm": "保留LLM",
        "保留模型": "保留LLM",
        "keep_llm": "保留LLM",
        "采纳数值工具": "采纳数值工具",
        "采纳工具": "采纳数值工具",
        "accept_numeric": "采纳数值工具",
        "numeric_tool": "采纳数值工具",
        "转人工": "转人工",
        "人工": "转人工",
        "human": "转人工",
        "uncertain": "转人工",
        "不确定": "转人工",
    }
    return aliases.get(text, "")


class MainAgentConflictAdjudicatorV10:
    """使用审核器现有 LLM 客户端同步裁决复核分歧。"""

    def __init__(self, chat: ChatCallable):
        self._chat = chat

    def adjudicate(
        self,
        *,
        pending_content: str,
        kb_context: str,
        initial_result: Dict[str, Any],
        verified_result: Dict[str, Any],
        conflicts: List[Dict[str, Any]],
        numeric_checks: List[Dict[str, Any]],
        mine_type: str = "non_outburst",
    ) -> Dict[str, Any]:
        payload = {
            "待审原文": str(pending_content or "")[:5000],
            "已召回法规原文": str(kb_context or "")[:7000],
            "初审总体结论": initial_result.get("compliance_status", ""),
            "复核总体结论": verified_result.get("compliance_status", ""),
            "复核说明": verified_result.get("verification_notes", ""),
            "矿井类型": (
                "突出矿井" if mine_type == "outburst" else "非突出矿井"
            ),
            "待裁决分歧": conflicts,
            "数值工具结论": numeric_checks or [],
        }
        raw = self._chat(
            [
                {"role": "system", "content": MAIN_ADJUDICATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请逐项裁决以下分歧：\n" + json.dumps(
                        payload, ensure_ascii=False, indent=2
                    )[:18000],
                },
            ],
            temperature=0,
            max_tokens=1600,
        )
        parsed = _parse_json_object(raw)
        normalized: List[Dict[str, str]] = []
        seen: set[str] = set()
        for item in parsed.get("decisions") or []:
            if not isinstance(item, dict):
                continue
            conflict_id = str(item.get("conflict_id", "")).strip()
            decision = _normalize_decision(item.get("decision"))
            if not conflict_id or not decision or conflict_id in seen:
                continue
            seen.add(conflict_id)
            normalized.append(
                {
                    "conflict_id": conflict_id,
                    "decision": decision,
                    "reason": str(item.get("reason", "")).strip()[:1000],
                }
            )
        return {
            "decisions": normalized,
            "summary": str(parsed.get("summary", "")).strip()[:1000],
            "parse_error": not bool(parsed),
        }
