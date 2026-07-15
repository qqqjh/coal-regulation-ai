"""Build Codex-reviewed, context-preserving atomic claims for all 50 cases.

The existing heuristic claims are treated as a draft. Codex decisions add
manual claims for cases where the heuristic produced no usable claims, keep
non-retrieval material explicitly excluded, and attach a parent fallback query
without pretending that the fallback is an atomic claim.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "rag_eval" / "data" / "gold_atomic_claims_v9_v1.json"
CHECKPOINTS = ROOT / "rag_eval" / "data" / "codex_atomic_claim_checkpoints_v2"
OUTPUT = ROOT / "rag_eval" / "data" / "gold_atomic_claims_codex_v2.json"
HTML = ROOT / "rag_eval" / "reports" / "gold_atomic_claims_codex_review_v2.html"


NON_RETRIEVAL_CASES = {
    "v9_006_chunk003": "目录页，只描述章节名称和页码，不形成待审事实或要求。",
    "v9_066_chunk141": "事故案例学习材料，属于案例叙述和防范措施，不作为当前规程条款的合规主张。",
}

TASK_OVERRIDES = {
    "v9_006_chunk023": "applicability_check",
    "v9_006_chunk024": "applicability_check",
    "v9_006_chunk026": "applicability_check",
    "v9_006_chunk030": "applicability_check",
    "v9_006_chunk048": "compliance_support_retrieval",
    "v9_066_chunk066": "compliance_support_retrieval",
}


MANUAL_CLAIMS: dict[str, list[dict[str, str]]] = {
    "v9_004_chunk013": [
        {"text": "S1302工作面采用走向长壁、后退式大采高低位放顶煤全部垮落式综合机械化采煤法。", "type": "method_selection"},
        {"text": "S1302工作面切眼斜距350.2 m，煤层平均厚度5.7 m，工作面采高3.2±0.1 m，循环进度0.8 m。", "type": "design_parameter_group"},
        {"text": "S1302工作面割煤回收率为98%，放煤回收率为93%，一采一放为一个循环。", "type": "production_parameter_group"},
    ],
    "v9_006_chunk023": [
        {"text": "S5102高抽巷选用的高强度螺纹钢锚杆长度为2.4 m，大于计算所需的2.32 m。", "type": "design_validation"},
        {"text": "S5102高抽巷选用的锚杆直径为22 mm，大于计算所需的19 mm。", "type": "design_validation"},
    ],
    "v9_006_chunk024": [
        {"text": "余吾煤业高抽巷锚杆支护间距以0.9～1.2 m为宜，S5102高抽巷选用的锚杆间排距为0.8～1.2 m。", "type": "design_validation"},
        {"text": "S5102高抽巷选用的最短锚索长度为5.3 m，大于计算所需的4.21 m。", "type": "design_validation"},
    ],
    "v9_006_chunk026": [
        {"text": "顶锚杆每孔采用2支K2350快速树脂锚固药卷，实际锚固长度为1270 mm。", "type": "support_parameter"},
        {"text": "帮锚杆每孔采用2支Z2350低粘度树脂锚固药卷，实际锚固长度为980 mm。", "type": "support_parameter"},
        {"text": "锚索每孔采用3支K2350型树脂锚固剂，实际锚固长度为1900 mm。", "type": "support_parameter"},
    ],
    "v9_006_chunk030": [
        {"text": "S5102高抽巷回风联巷0 m～56.8 m段采用锚网（索）加双钢筋托梁永久支护。", "type": "support_method"},
        {"text": "顶板锚杆采用直径22 mm、长度2.4 m的HRB500左旋无纵筋螺纹钢锚杆。", "type": "support_parameter"},
        {"text": "顶板锚杆采用2支MSK2350低粘度快速树脂锚固药卷加长锚固，钻孔直径28 mm，锚固长度1270 mm。", "type": "support_parameter"},
        {"text": "顶角锚杆与垂线向上呈10°外偏角，其他锚杆垂直顶板打设。", "type": "installation_requirement"},
        {"text": "顶板锚杆排距800 mm，每排7根，间距830 mm，锚杆扭矩400 N·m，锚固力要求200 kN。", "type": "support_parameter_group"},
        {"text": "锚索采用直径22 mm、长度8300 mm的高强度低松弛预应力钢绞线，钻孔直径28 mm，采用3支MSK2350药卷锚固，锚固长度1900 mm。", "type": "support_parameter_group"},
        {"text": "锚索按3-3-3方式布置，排距800 mm，间距1660 mm，预紧力250 kN，初次张拉至300 kN，锚固力600 kN。", "type": "support_parameter_group"},
    ],
    "v9_006_chunk048": [
        {"text": "恢复通风前必须检查瓦斯浓度；局部通风机及其开关附近10 m范围内瓦斯浓度低于0.4%、停风区域瓦斯浓度低于1.0%、最高二氧化碳浓度低于1.5%，并经通风调度同意后，方可人工开启局部通风机。", "type": "conditional_requirement"},
        {"text": "恢复通风后，只有经瓦检员检查瓦斯浓度不超限，人员方可进入。", "type": "conditional_requirement"},
        {"text": "局部通风机及其开关附近10 m以内瓦斯浓度达到或超过0.4%时，应利用全压供风处理。", "type": "threshold_requirement"},
        {"text": "局部通风机恢复通风前，必须根据停风区域瓦斯浓度确定瓦斯排放等级，并按照排放瓦斯流程分级排放。", "type": "procedure_requirement"},
        {"text": "停风区域瓦斯浓度低于1%、1%至3%、高于3%时，应分别按照对应等级组织和指挥排放瓦斯。", "type": "threshold_procedure_group"},
        {"text": "排放瓦斯时，排出风流与全风压风流汇合处瓦斯浓度必须低于1.5%。", "type": "threshold_requirement"},
        {"text": "巷道恢复通风后，工作面和回风流瓦斯浓度低于1%、二氧化碳浓度低于1.5%，并经瓦检员、安全员和调度室许可后，方可送电恢复生产。", "type": "conditional_requirement"},
        {"text": "有计划处理风筒问题时，应事先办理停电、停风票并通知值班队干，恢复通风按规定执行。", "type": "procedure_requirement"},
        {"text": "每班应由专业瓦检员检查瓦斯浓度并与传感器显示对照，发现显示或传输异常时及时汇报处理。", "type": "inspection_requirement"},
    ],
    "v9_066_chunk066": [
        {"text": "每班必须安排瓦检员对安装工作面进行瓦斯检查。", "type": "inspection_requirement"},
        {"text": "安装面瓦斯检查点包括工作面风流、硐室、工作面回风流及各钻场横贯。", "type": "inspection_scope"},
        {"text": "所有检查点的检查次数和时间必须符合瓦斯检查巡回图表规定，瓦斯检查必须符合“三对口”要求。", "type": "inspection_requirement"},
        {"text": "瓦斯传感器读数超出允许误差时，必须通知通风调度并联系自动化科更换传感器。", "type": "exception_procedure"},
        {"text": "工作面瓦斯预警浓度按《煤矿安全规程》报警浓度下调20%执行。", "type": "threshold_requirement"},
        {"text": "安装作业地点瓦斯浓度达到相应停止作业阈值时，必须停止工作、切断电源并按浓度情况撤出人员；瓦斯浓度低于0.8%后方可恢复作业。", "type": "threshold_procedure_group"},
        {"text": "回风流瓦斯浓度达到0.8%时必须停止工作、切断电源并处理；达到1.0%时还必须撤出人员；低于0.8%后方可恢复作业。", "type": "threshold_procedure_group"},
        {"text": "电动机及开关附近20 m范围内风流中瓦斯达到规定阈值时，必须停止作业、切断电源、撤出人员并处理。", "type": "threshold_requirement"},
        {"text": "跟班队干、班长、电钳工和绞车司机必须佩戴便携式瓦检仪，发现瓦斯异常时及时汇报。", "type": "personnel_requirement"},
        {"text": "工作面进风流瓦斯达到规定阈值时必须停止工作、切断电源并处理，低于0.4%后方可恢复作业。", "type": "threshold_requirement"},
        {"text": "工作面风流中二氧化碳浓度达到1.5%时，必须停止作业、撤出人员并处理。", "type": "threshold_requirement"},
        {"text": "巷道内局部体积大于0.5 m³的空间瓦斯积聚达到2%时，附近20 m内必须切断电源、停止工作、撤出人员并处理。", "type": "threshold_requirement"},
        {"text": "施工过程中发现瓦斯涌出异常时，应立即停止作业并向相关调度和部门汇报。", "type": "exception_procedure"},
        {"text": "回收钻孔时，工作面煤墙的钻孔距工作面不得超过5 m。", "type": "distance_requirement"},
        {"text": "进大型设备时暂时拆卸的抽采管路必须及时恢复。", "type": "restoration_requirement"},
    ],
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def stable_context(pending: dict[str, Any]) -> str:
    return " ".join(
        str(pending.get(key, "")).strip()
        for key in ("chapter", "section", "article")
        if str(pending.get(key, "")).strip()
    )


def enriched_claim(case_id: str, pending: dict[str, Any], claim: dict[str, Any], index: int, source: str) -> dict[str, Any]:
    text = claim.get("text") or claim["claim_text"]
    focused = f"{stable_context(pending)} {text}".strip()
    vague_reference = any(
        marker in text
        for marker in (
            "规定值", "规定长度", "规定检查", "上述规定", "相应措施",
            "相关规定", "正常情况下", "符合要求", "进行处理",
        )
    )
    context_types = {
        "design_validation", "design_parameter_group", "production_parameter_group",
        "threshold_procedure_group", "conditional_requirement",
    }
    return {
        "claim_id": f"{case_id}__codex_claim{index:02d}",
        "source_line": claim.get("source_line"),
        "source_text": claim.get("source_text", text),
        "claim_text": text,
        "claim_type": claim.get("type", "atomic_requirement"),
        "focused_query": focused,
        "retrieval_query": focused,
        "contextual_query": f"{focused}\n父待审块上下文：{pending['content']}",
        "retrieval_priority": claim.get("retrieval_priority", "high"),
        "context_dependency": (
            "requires_parent_context"
            if claim.get("type") in context_types or vague_reference or len(text.replace(" ", "")) < 24
            else "mostly_standalone"
        ),
        "annotation_source": source,
        "claim_review_status": "codex_reviewed",
        "purpose": "retrieval_query_only",
        "is_gold_evaluation_unit": False,
        "target_evidence_ids": [],
    }


def build_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["case_id"]
    pending = case["pending"]
    notes = []
    if case_id in NON_RETRIEVAL_CASES:
        claims = []
        excluded = [{
            "source_text": pending["content"],
            "claim_text": pending["content"],
            "exclusion_reason": "codex_confirmed_non_retrieval_material",
            "note": NON_RETRIEVAL_CASES[case_id],
        }]
        notes.append(NON_RETRIEVAL_CASES[case_id])
        status = "completed_codex_manual_extraction"
        task_type = "not_retrieval_suitable"
    else:
        drafts = MANUAL_CLAIMS.get(case_id)
        if drafts is not None:
            claims = [
                enriched_claim(case_id, pending, claim, index, "codex_manual_claim")
                for index, claim in enumerate(drafts, 1)
            ]
            notes.append("旧规则提取不可用或为零主张，已由Codex根据父块原文重新提取。")
        else:
            claims = [
                enriched_claim(case_id, pending, claim, index, "codex_reviewed_heuristic_draft")
                for index, claim in enumerate(case["claims"], 1)
            ]
            notes.append("Codex复核后保留规则草稿主张，并重建稳定标题上下文查询。")
        excluded = case.get("excluded_units", [])
        status = "completed_codex_manual_extraction"
        task_type = TASK_OVERRIDES.get(case_id, case["evaluation_task_type"])

    return {
        **case,
        "schema": "coal_rag_codex_atomic_claim_checkpoint_v2",
        "annotation_status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "evaluation_task_type": task_type,
        "review_notes": notes,
        "parent_fallback_query": (
            "" if case_id in NON_RETRIEVAL_CASES
            else f"{stable_context(pending)} {pending['content']}".strip()
        ),
        "parent_fallback_purpose": (
            "disabled_for_non_retrieval_material"
            if case_id in NON_RETRIEVAL_CASES
            else "recall_safety_net_not_atomic_claim"
        ),
        "draft_rule_claim_count": len(case.get("claims", [])),
        "draft_excluded_unit_count": len(case.get("excluded_units", [])),
        "claims": claims,
        "excluded_units": excluded,
    }


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def render(payload: dict[str, Any]) -> str:
    cards = []
    for case in payload["cases"]:
        claims = "".join(
            f"""<article><header><b>{esc(item['claim_id'])}</b><span>{esc(item['claim_type'])} · {esc(item['context_dependency'])}</span></header>
            <p>{esc(item['claim_text'])}</p><details><summary>focused query</summary><pre>{esc(item['focused_query'])}</pre></details>
            <details><summary>contextual query</summary><pre>{esc(item['contextual_query'])}</pre></details></article>"""
            for item in case["claims"]
        )
        cards.append(
            f"""<section class="case"><header><div><h2>{esc(case['case_id'])}</h2><p>{esc(case['pending'].get('doc_name'))}</p></div>
            <b>Codex主张 {len(case['claims'])} · 旧规则 {case['draft_rule_claim_count']}</b></header>
            <p class="note">{esc('；'.join(case['review_notes']))}</p>
            <details><summary>父待审块原文</summary><pre>{esc(case['pending']['content'])}</pre></details>
            <details><summary>父块保底 query（不计作原子主张）</summary><pre>{esc(case['parent_fallback_query'])}</pre></details>
            <div>{claims or '<p class=empty>确认不提取原子主张</p>'}</div></section>"""
        )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Codex原子主张人工提取复核</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f3f5f4;color:#17241e;font-family:"Microsoft YaHei",sans-serif}}.top{{position:sticky;top:0;background:#17392c;color:#fff;padding:16px 3vw;z-index:5}}
    main{{width:min(1500px,96vw);margin:18px auto}}.case{{background:#fff;border:1px solid #ccd5cf;padding:13px;margin-bottom:15px}}.case>header,article header{{display:flex;justify-content:space-between;gap:12px}}h1,h2{{margin:0}}.note{{color:#52645a}}
    details{{border:1px solid #d7dfda;margin:7px 0}}summary{{cursor:pointer;font-weight:700;padding:6px}}pre{{white-space:pre-wrap;word-break:break-word;padding:8px;line-height:1.6}}
    article{{border-left:5px solid #237451;background:#f8faf9;padding:9px;margin:9px 0}}article p{{line-height:1.6}}article span{{color:#617168;font-size:13px}}.empty{{color:#7a8780}}</style></head>
    <body><div class="top"><h1>Codex 原子主张人工提取 v2</h1><p>50个父块，主张 {payload['summary']['retrieval_claim_count']}，零主张块 {payload['summary']['zero_claim_case_count']}；每个父块另有不计作原子主张的保底查询。</p></div>
    <main>{''.join(cards)}</main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--checkpoints", type=Path, default=CHECKPOINTS)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--html", type=Path, default=HTML)
    args = parser.parse_args()

    source = load(args.source)
    cases = []
    for case in source["cases"]:
        reviewed = build_case(case)
        atomic_write(args.checkpoints / f"{case['case_id']}.json", reviewed)
        cases.append(reviewed)

    priorities = Counter(claim["retrieval_priority"] for case in cases for claim in case["claims"])
    types = Counter(claim["claim_type"] for case in cases for claim in case["claims"])
    payload = {
        "schema": "coal_rag_gold_atomic_claims_codex_v2",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(args.source.resolve()),
        "annotation_method": "Codex-reviewed context-preserving extraction with per-case atomic checkpoints.",
        "important_notes": [
            "The 50 parent chunks remain the gold evaluation units.",
            "Atomic claims are internal retrieval units.",
            "Every retrieval-suitable parent has a separate parent_fallback_query which is not counted as an atomic claim.",
            "Focused and contextual queries are both retained for later ablation.",
        ],
        "summary": {
            "case_count": len(cases),
            "completed_case_count": sum(case["annotation_status"] == "completed_codex_manual_extraction" for case in cases),
            "retrieval_claim_count": sum(len(case["claims"]) for case in cases),
            "zero_claim_case_count": sum(not case["claims"] for case in cases),
            "manual_reextracted_case_count": len(MANUAL_CLAIMS),
            "confirmed_non_retrieval_case_count": len(NON_RETRIEVAL_CASES),
            "claim_priority_counts": dict(priorities),
            "claim_type_counts": dict(types),
        },
        "cases": cases,
    }
    atomic_write(args.output, payload)
    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text(render(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.html.resolve())


if __name__ == "__main__":
    main()
