"""Build a paired S1302 DOCX benchmark with manually injected review errors.

The script keeps the converted source package intact, changes only text nodes in
``word/document.xml`` for compliance/typo mutations, and clones selected source
paragraphs to create document-level repetition cases.  It emits:

1. an unchanged control DOCX;
2. a blind injected-error DOCX (contains no answer labels);
3. a separate JSON gold manifest.

Run from the project root with the bundled document Python runtime.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import etree


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = (
    PROJECT_ROOT / "data" / "v9_web_work" / "preprocess" / "3aa727075f4e.docx"
)
DEFAULT_OUT_DIR = (
    PROJECT_ROOT / "new_docs" / "test_doc" / "S1302人工错误注入测试集_v1"
)

CONTROL_NAME = "004 S1302工作面作业规程（综采）_原样对照版.docx"
MUTATED_NAME = "004 S1302工作面作业规程（综采）_人工错误注入版_v1.docx"
GOLD_NAME = "004 S1302工作面作业规程（综采）_人工错误金标_v1.json"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def paragraph_text(paragraph: etree._Element) -> str:
    return "".join(node.text or "" for node in paragraph.xpath(".//w:t", namespaces=NS))


def find_unique_paragraph(root: etree._Element, anchor: str) -> etree._Element:
    matches = [
        paragraph
        for paragraph in root.xpath(".//w:p", namespaces=NS)
        if anchor in paragraph_text(paragraph)
    ]
    if len(matches) != 1:
        raise ValueError(f"段落锚点应唯一，实际 {len(matches)} 个: {anchor}")
    return matches[0]


def set_text(node: etree._Element, value: str) -> None:
    node.text = value
    if value.startswith(" ") or value.endswith(" "):
        node.set(XML_SPACE, "preserve")
    else:
        node.attrib.pop(XML_SPACE, None)


def replace_first_in_paragraph(paragraph: etree._Element, old: str, new: str) -> None:
    text_nodes = paragraph.xpath(".//w:t", namespaces=NS)
    full_text = "".join(node.text or "" for node in text_nodes)
    start = full_text.find(old)
    if start < 0:
        raise ValueError(f"目标段落内未找到待替换文本: {old}")
    end = start + len(old)

    spans: list[tuple[int, int]] = []
    cursor = 0
    for node in text_nodes:
        value = node.text or ""
        spans.append((cursor, cursor + len(value)))
        cursor += len(value)

    start_index = next(i for i, (_a, b) in enumerate(spans) if start < b)
    end_index = next(i for i, (a, b) in enumerate(spans) if a < end <= b)
    start_offset = start - spans[start_index][0]
    end_offset = end - spans[end_index][0]

    if start_index == end_index:
        value = text_nodes[start_index].text or ""
        set_text(text_nodes[start_index], value[:start_offset] + new + value[end_offset:])
        return

    first_value = text_nodes[start_index].text or ""
    last_value = text_nodes[end_index].text or ""
    set_text(text_nodes[start_index], first_value[:start_offset] + new)
    for index in range(start_index + 1, end_index):
        set_text(text_nodes[index], "")
    set_text(text_nodes[end_index], last_value[end_offset:])


COMPLIANCE_CASES: list[dict[str, Any]] = [
    {
        "error_id": "C001",
        "type": "compliance",
        "subtype": "methane_stop_threshold_relaxed",
        "baseline_block_index": 548,
        "anchor": "②当工作面（包括回顺距煤墙10m范围",
        "original_text": "瓦斯浓度≥1.2%时",
        "mutated_text": "瓦斯浓度≥1.8%时",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "将工作面停止工作阈值由1.2%放宽至1.8%，属于延迟停工。",
    },
    {
        "error_id": "C002",
        "type": "compliance",
        "subtype": "methane_resume_threshold_relaxed",
        "baseline_block_index": 549,
        "anchor": "③当回风巷回风流中瓦斯浓度",
        "original_text": "瓦斯浓度＜0.8%时",
        "mutated_text": "瓦斯浓度＜1.3%时",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "将恢复作业阈值由低于0.8%放宽至低于1.3%。",
    },
    {
        "error_id": "C003",
        "type": "compliance",
        "subtype": "motor_methane_threshold_relaxed",
        "baseline_block_index": 550,
        "anchor": "④电动机及开关附近20m范围内",
        "original_text": "瓦斯≥1.2%时",
        "mutated_text": "瓦斯≥2.0%时",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "放宽电动机及开关附近的停电撤人阈值。",
    },
    {
        "error_id": "C004",
        "type": "compliance",
        "subtype": "local_methane_accumulation_threshold_relaxed",
        "baseline_block_index": 551,
        "anchor": "⑤工作面顺槽内局部体积大于0.5m³",
        "original_text": "瓦斯积聚≥2％时",
        "mutated_text": "瓦斯积聚≥3％时",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "放宽局部瓦斯积聚的断电、停工和撤人阈值。",
    },
    {
        "error_id": "C005",
        "type": "compliance",
        "subtype": "spray_pressure_below_requirement",
        "baseline_block_index": 593,
        "anchor": "（3）采煤机必须安装内、外喷雾装置",
        "original_text": "内喷雾工作压力不得小于2MPa",
        "mutated_text": "内喷雾工作压力不得小于0.5MPa",
        "expected_verdict": "不合规",
        "severity": "high",
        "basis": "降低采煤机内喷雾最低工作压力；对应知识库《煤矿安全规程》第七百零四条。",
    },
    {
        "error_id": "C006",
        "type": "compliance",
        "subtype": "energized_maintenance_allowed",
        "baseline_block_index": 628,
        "anchor": "（5）机电设备检修时，严禁带电作业",
        "original_text": "严禁带电作业",
        "mutated_text": "允许带电作业",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "允许井下带电检修；对应知识库《煤矿安全规程》第四百七十八条。",
    },
    {
        "error_id": "C007",
        "type": "compliance",
        "subtype": "water_inrush_warning_ignored",
        "baseline_block_index": 877,
        "anchor": "3、工作面在回采过程中，注意工作面来水情况",
        "original_text": "立即停止作业",
        "mutated_text": "可继续作业",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "发现透水征兆仍允许继续作业；对应知识库《煤矿安全规程》第二百九十二条。",
    },
    {
        "error_id": "C008",
        "type": "compliance",
        "subtype": "lighting_methane_threshold_relaxed",
        "baseline_block_index": 896,
        "anchor": "5、工作面瓦斯浓度低于0.8%时",
        "original_text": "低于0.8%时",
        "mutated_text": "低于1.5%时",
        "expected_verdict": "不合规",
        "severity": "high",
        "basis": "放宽开启照明灯的瓦斯浓度条件。",
    },
    {
        "error_id": "C009",
        "type": "compliance",
        "subtype": "power_isolation_removed",
        "baseline_block_index": 1184,
        "anchor": "（13）严禁带电检修和搬迁电气设备",
        "original_text": "必须先切断上级电源",
        "mutated_text": "无需切断上级电源",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "取消打开电气设备外壳前的上级电源隔离要求；对应第四百七十八条。",
    },
    {
        "error_id": "C010",
        "type": "compliance",
        "subtype": "energized_maintenance_permitted",
        "baseline_block_index": 2026,
        "anchor": "⑤工作面不得带电检修、搬迁电气设备",
        "original_text": "不得带电检修",
        "mutated_text": "可以带电检修",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "允许井下带电检修；对应第四百七十八条。",
    },
    {
        "error_id": "C011",
        "type": "compliance",
        "subtype": "ventilation_loss_work_continues",
        "baseline_block_index": 2049,
        "anchor": "1、工作面若发现停风",
        "original_text": "必须立即停止作业",
        "mutated_text": "可继续作业",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "工作面停风后仍允许继续作业。",
    },
    {
        "error_id": "C012",
        "type": "compliance",
        "subtype": "post_ventilation_methane_threshold_relaxed",
        "baseline_block_index": 2050,
        "anchor": "2、恢复送风时间达5分钟后",
        "original_text": "瓦斯浓度不超过1.2%",
        "mutated_text": "瓦斯浓度不超过2.0%",
        "expected_verdict": "不合规",
        "severity": "critical",
        "basis": "放宽恢复送风后人员进入和开机条件。",
    },
]


TYPO_CASES: list[dict[str, Any]] = [
    {
        "error_id": "T001", "type": "typo", "subtype": "homophone",
        "baseline_block_index": 400, "anchor": "S1302工作面通风系统为采用",
        "original_text": "通风系统", "mutated_text": "通疯系统",
        "expected_correction": "通风系统", "severity": "medium",
    },
    {
        "error_id": "T002", "type": "typo", "subtype": "wrong_character",
        "baseline_block_index": 542, "anchor": "①工作面瓦斯检查点包括",
        "original_text": "瓦斯检查点", "mutated_text": "瓦斯检察点",
        "expected_correction": "瓦斯检查点", "severity": "medium",
    },
    {
        "error_id": "T003", "type": "typo", "subtype": "similar_character",
        "baseline_block_index": 545, "anchor": "④跟面瓦检员除进行工作面巡查外",
        "original_text": "监测瓦斯浓度", "mutated_text": "监侧瓦斯浓度",
        "expected_correction": "监测瓦斯浓度", "severity": "medium",
    },
    {
        "error_id": "T004", "type": "typo", "subtype": "homophone",
        "baseline_block_index": 567, "anchor": "（10）机组在机尾割煤时",
        "original_text": "割煤速度", "mutated_text": "割煤速渡",
        "expected_correction": "割煤速度", "severity": "medium",
    },
    {
        "error_id": "T005", "type": "typo", "subtype": "similar_character",
        "baseline_block_index": 569, "anchor": "（12）加强传感器、瓦斯超限断电装置",
        "original_text": "传感器", "mutated_text": "传敢器",
        "expected_correction": "传感器", "severity": "medium",
    },
    {
        "error_id": "T006", "type": "typo", "subtype": "homophone",
        "baseline_block_index": 745, "anchor": "S1302综采工作面供电的4趟10kV电源",
        "original_text": "割煤机", "mutated_text": "割煤鸡",
        "expected_correction": "割煤机", "severity": "medium",
    },
    {
        "error_id": "T007", "type": "typo", "subtype": "similar_character",
        "baseline_block_index": 788, "anchor": "S1302胶带顺槽、回风顺槽各敷设一趟4寸静压水管路",
        "original_text": "静压水管路", "mutated_text": "静压水菅路",
        "expected_correction": "静压水管路", "severity": "medium",
    },
    {
        "error_id": "T008", "type": "typo", "subtype": "similar_character",
        "baseline_block_index": 910, "anchor": "采煤机必须安设机载式甲烷传感器",
        "original_text": "甲烷传感器", "mutated_text": "甲完传感器",
        "expected_correction": "甲烷传感器", "severity": "medium",
    },
    {
        "error_id": "T009", "type": "typo", "subtype": "wrong_character",
        "baseline_block_index": 1133, "anchor": "（2）保证泵站压力达到30MPa以上",
        "original_text": "支架初撑力", "mutated_text": "支架初掌力",
        "expected_correction": "支架初撑力", "severity": "medium",
    },
    {
        "error_id": "T010", "type": "typo", "subtype": "homophone",
        "baseline_block_index": 1451, "anchor": "七、更换乳化液泵站、喷雾泵安全技术措施",
        "original_text": "乳化液泵站", "mutated_text": "乳化夜泵站",
        "expected_correction": "乳化液泵站", "severity": "medium",
    },
]


REPETITION_CASES: list[dict[str, Any]] = [
    {
        "error_id": "R001", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 575, "source_anchor": "（1）初采期间通风科每日对初采面的瓦斯浓度变化曲线",
        "inserted_after_block_index": 1180, "destination_anchor": "（9）采煤机上必须安装机载式瓦斯断电仪",
    },
    {
        "error_id": "R002", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 596, "source_anchor": "（6）各转载点安装喷雾设施，要求喷嘴距落煤点",
        "inserted_after_block_index": 1515, "destination_anchor": "（8）起吊物件时，必须设一人专门观察物件动作趋势",
    },
    {
        "error_id": "R003", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 619, "source_anchor": "（1）8kg干粉灭火器配置：胶带机头3个",
        "inserted_after_block_index": 1728, "destination_anchor": "（5）作业时必须听从跟班队干统一指挥，发现异常情况（如顶板响动、漏矸等）",
    },
    {
        "error_id": "R004", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 627, "source_anchor": "（4）电气设备着火时，应先切断电源",
        "inserted_after_block_index": 1944, "destination_anchor": "⑦打钻工程中，在下风侧10m处悬挂一便携式瓦检仪",
    },
    {
        "error_id": "R005", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 1297, "source_anchor": "（11）拆接链作业过程中，若乳化液泵突然停止供液",
        "inserted_after_block_index": 2112, "destination_anchor": "9、扶架时，用喊话或者晃动矿灯方式联系",
    },
    {
        "error_id": "R006", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 1515, "source_anchor": "（8）起吊物件时，必须设一人专门观察物件动作趋势",
        "inserted_after_block_index": 2250, "destination_anchor": "1、保证工作面支架的良好工作状态，支架初撑力必须达标",
    },
    {
        "error_id": "R007", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 1946, "source_anchor": "⑨打钻过程中，如出现钻孔出水、瓦斯喷孔、夹钻、顶钻等异常现象",
        "inserted_after_block_index": 2428, "destination_anchor": "5、跟班队干、班长必须佩戴便携式瓦斯检测仪",
    },
    {
        "error_id": "R008", "type": "redundancy", "subtype": "exact_cross_section_copy",
        "source_block_index": 2408, "source_anchor": "2、回风顺槽内每隔200m安装一道风水联动喷雾",
        "inserted_after_block_index": 2651, "destination_anchor": "18、更换截齿和滚筒前后5m以内有人作业时",
    },
]


def patch_document_xml(source: Path, destination: Path) -> list[dict[str, Any]]:
    with zipfile.ZipFile(source, "r") as zin:
        document_xml = zin.read("word/document.xml")
        root = etree.fromstring(document_xml)

        applied: list[dict[str, Any]] = []
        for case in [*COMPLIANCE_CASES, *TYPO_CASES]:
            paragraph = find_unique_paragraph(root, case["anchor"])
            before = paragraph_text(paragraph)
            replace_first_in_paragraph(paragraph, case["original_text"], case["mutated_text"])
            after = paragraph_text(paragraph)
            if before == after or case["mutated_text"] not in after:
                raise RuntimeError(f"替换未生效: {case['error_id']}")
            applied.append({**case, "baseline_paragraph": before, "mutated_paragraph": after})

        for case in REPETITION_CASES:
            source_paragraph = find_unique_paragraph(root, case["source_anchor"])
            destination_paragraph = find_unique_paragraph(root, case["destination_anchor"])
            duplicate = copy.deepcopy(source_paragraph)
            parent = destination_paragraph.getparent()
            parent.insert(parent.index(destination_paragraph) + 1, duplicate)
            applied.append(
                {
                    **case,
                    "duplicated_text": paragraph_text(source_paragraph),
                    "expected_verdict": "重复",
                    "severity": "medium",
                }
            )

        patched_xml = etree.tostring(
            root,
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=destination.stem + ".", suffix=".tmp", dir=destination.parent, delete=False
        ) as tmp_handle:
            tmp_path = Path(tmp_handle.name)
        try:
            with zipfile.ZipFile(tmp_path, "w") as zout:
                for info in zin.infolist():
                    payload = patched_xml if info.filename == "word/document.xml" else zin.read(info.filename)
                    zout.writestr(info, payload)
            tmp_path.replace(destination)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    return applied


def validate_package(control: Path, mutated: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    with zipfile.ZipFile(control, "r") as control_zip, zipfile.ZipFile(mutated, "r") as mutated_zip:
        control_names = control_zip.namelist()
        mutated_names = mutated_zip.namelist()
        if control_names != mutated_names:
            raise RuntimeError("DOCX包部件清单发生变化")

        changed_parts = []
        for name in control_names:
            if control_zip.read(name) != mutated_zip.read(name):
                changed_parts.append(name)
        if changed_parts != ["word/document.xml"]:
            raise RuntimeError(f"出现非预期包部件变化: {changed_parts}")

        mutated_text = etree.fromstring(mutated_zip.read("word/document.xml"))
        full_text = "\n".join(paragraph_text(p) for p in mutated_text.xpath(".//w:p", namespaces=NS))

    missing = []
    for case in cases:
        if case["type"] in {"compliance", "typo"} and case["mutated_text"] not in full_text:
            missing.append(case["error_id"])
        if case["type"] == "redundancy" and full_text.count(case["duplicated_text"]) < 2:
            missing.append(case["error_id"])
    if missing:
        raise RuntimeError(f"注入后验证失败: {missing}")

    return {
        "package_parts": len(control_names),
        "changed_parts": changed_parts,
        "validated_case_count": len(cases),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args()

    source = args.source.resolve()
    out_dir = args.out_dir.resolve()
    control = out_dir / CONTROL_NAME
    mutated = out_dir / MUTATED_NAME
    gold = out_dir / GOLD_NAME

    if not source.exists():
        raise FileNotFoundError(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, control)
    cases = patch_document_xml(source, mutated)
    validation = validate_package(control, mutated, cases)

    counts = {
        "compliance": sum(case["type"] == "compliance" for case in cases),
        "typo": sum(case["type"] == "typo" for case in cases),
        "redundancy": sum(case["type"] == "redundancy" for case in cases),
    }
    payload = {
        "schema": "coal_manual_error_injection_gold_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark_id": "s1302_manual_error_injection_v1",
        "source_docx": str(source),
        "source_sha256": sha256(source),
        "control_docx": str(control),
        "control_sha256": sha256(control),
        "mutated_docx": str(mutated),
        "mutated_sha256": sha256(mutated),
        "gold_json": str(gold),
        "blind_test_rule": "审查系统仅接收mutated_docx，不得读取本JSON。",
        "evaluation_rule": (
            "先分别审查control_docx和mutated_docx；以注入版新增输出做差分，"
            "再按error_id进行人工或规则匹配，避免把源文档原有问题计为本测试误报。"
        ),
        "counts": {**counts, "total": len(cases)},
        "validation": validation,
        "cases": cases,
    }
    gold.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in (
        "benchmark_id", "control_docx", "mutated_docx", "gold_json", "counts", "validation"
    )}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
