"""法规 chunk 的矿井适用性标注规则。

该模块同时供 Web 规则上传链和 v10 审查引擎使用，保证入库时的
标签与检索时的过滤逻辑一致。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple


APPLICABILITY_AUTO = "auto"
APPLICABILITY_GENERAL = "general"
APPLICABILITY_OUTBURST_ONLY = "outburst_only"
VALID_APPLICABILITY_MODES = {
    APPLICABILITY_AUTO,
    APPLICABILITY_GENERAL,
    APPLICABILITY_OUTBURST_ONLY,
}

OUTBURST_DOC_PATTERNS = ("防治煤与瓦斯突出",)
OUTBURST_STRUCT_KEYWORDS = (
    "突出矿井",
    "煤与瓦斯突出",
    "突出煤层",
    "突出危险",
    "防突",
    "石门揭煤",
)
STRUCTURE_FIELDS = (
    "part",
    "chapter",
    "section",
    "context_prefix",
    "parent_context",
)


def normalize_applicability_mode(value: Any, *, allow_auto: bool = True) -> str:
    mode = str(value or APPLICABILITY_AUTO).strip().lower()
    allowed = VALID_APPLICABILITY_MODES if allow_auto else {
        APPLICABILITY_GENERAL,
        APPLICABILITY_OUTBURST_ONLY,
    }
    if mode not in allowed:
        raise ValueError(f"不支持的规则适用性: {value}")
    return mode


def _field_text(chunk: Dict[str, Any], field: str) -> str:
    value = chunk.get(field, "")
    if isinstance(value, (list, tuple)):
        return " / ".join(str(item) for item in value if item is not None)
    return str(value or "")


def classify_rule_applicability(
    chunk: Dict[str, Any],
    doc_name: str,
    mode: str = APPLICABILITY_AUTO,
) -> Tuple[str, Dict[str, Any]]:
    """返回 ``(applicability, basis)``。

    ``mode`` 为 general/outburst_only 时表示上传人对整份文档做了
    明确覆盖；auto 时只使用文档名与结构标题，不扫描正文零星提及。
    """
    normalized = normalize_applicability_mode(mode)
    if normalized != APPLICABILITY_AUTO:
        return normalized, {
            "mode": "manual",
            "reason": "document_override",
            "matches": [],
        }

    document_name = str(doc_name or "")
    document_matches = [
        pattern for pattern in OUTBURST_DOC_PATTERNS if pattern in document_name
    ]
    if document_matches:
        return APPLICABILITY_OUTBURST_ONLY, {
            "mode": "auto",
            "reason": "document_name",
            "matches": [f"doc_name:{item}" for item in document_matches],
        }

    matches = []
    for field in STRUCTURE_FIELDS:
        text = _field_text(chunk, field)
        for keyword in OUTBURST_STRUCT_KEYWORDS:
            if keyword in text:
                matches.append(f"{field}:{keyword}")
    if matches:
        return APPLICABILITY_OUTBURST_ONLY, {
            "mode": "auto",
            "reason": "structure_heading",
            "matches": list(dict.fromkeys(matches)),
        }

    return APPLICABILITY_GENERAL, {
        "mode": "auto",
        "reason": "no_outburst_scope_in_structure",
        "matches": [],
    }


def with_rule_applicability(
    chunk: Dict[str, Any],
    doc_name: str,
    mode: str = APPLICABILITY_AUTO,
) -> Dict[str, Any]:
    """复制并补全适用性字段，不修改调用方原始数据。"""
    result = dict(chunk or {})
    applicability, basis = classify_rule_applicability(result, doc_name, mode)
    result["applicability"] = applicability
    result["applicability_mode"] = basis["mode"]
    result["applicability_reason"] = basis["reason"]
    result["applicability_matches"] = basis["matches"]
    return result


def ensure_rule_applicability(
    chunk: Dict[str, Any],
    doc_name: str,
) -> Dict[str, Any]:
    """兼容旧数据：已有有效标签则保留，否则按 auto 规则补全。"""
    current = str((chunk or {}).get("applicability", "")).strip().lower()
    if current in {APPLICABILITY_GENERAL, APPLICABILITY_OUTBURST_ONLY}:
        result = dict(chunk or {})
        result.setdefault("applicability_mode", "stored")
        result.setdefault("applicability_reason", "stored_metadata")
        result.setdefault("applicability_matches", [])
        return result
    return with_rule_applicability(chunk, doc_name, APPLICABILITY_AUTO)
