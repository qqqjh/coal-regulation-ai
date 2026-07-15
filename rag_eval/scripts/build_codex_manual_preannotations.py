# -*- coding: utf-8 -*-
"""Build Codex manual pre-annotations and a self-contained review HTML."""

from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = PROJECT_ROOT / "rag_eval" / "data" / "gold_annotation_cases_v9.json"
DEFAULT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "gold_preannotations_codex_v9.json"
DEFAULT_HTML = PROJECT_ROOT / "rag_eval" / "reports" / "gold_preannotations_codex_review_v9.html"


def decision(
    label: str,
    confidence: str,
    reason: str,
    *,
    missing: bool = False,
    unsuitable: bool = False,
    flags: list[str] | None = None,
    useful: Dict[int, str] | None = None,
    uncertain: Dict[int, str] | None = None,
) -> Dict[str, Any]:
    return {
        "final_label": label,
        "final_confidence": confidence,
        "final_reason": reason,
        "missing_correct_evidence": missing,
        "not_eval_suitable": unsuitable,
        "review_flags": flags or [],
        "useful": useful or {},
        "uncertain": uncertain or {},
    }


# These decisions were made by reading the current v9 pending chunk and its
# 15 candidates. Historical topics are treated only as clues, not ground truth.
DECISIONS: Dict[str, Dict[str, Any]] = {
    "v9_004_chunk013": decision("uncertain", "medium", "采煤方法和参数描述未呈现明确违规，但候选法规不足以验证采高、回收率及工艺适用条件。", missing=True, flags=["需结合工作面设计和煤层条件复核"], uncertain={2: "规定采煤工作面正规开采和特定矿井不得前进式开采，与后退式采煤方法有条件关联。", 3: "规定综采设计和采高控制，可用于核查但未给出本工作面的支架有效高度。"}),
    "v9_004_chunk038": decision("compliant", "medium", "送电前检查20 m范围瓦斯并采用0.8%和0.4%的更严格阈值，未发现放宽法定要求。", useful={2: "直接规定恢复工作和开启电气设备前必须检查瓦斯，是送电程序的直接依据。"}, uncertain={1: "涉及瓦斯异常分析制度，但不能直接验证本段送电阈值。", 12: "规定瓦斯检查和报告制度，可支撑管理要求但不直接给出送电阈值。"}),
    "v9_004_chunk060": decision("compliant", "medium", "通讯、视频监控和闭锁系统配置属于正向安全要求，当前候选未检出与其冲突的强制条款。", missing=True, flags=["候选未覆盖通讯系统具体配置标准"]),
    "v9_004_chunk061": decision("non_compliant", "high", "待审块将压风自救装置设置范围写为距工作面25~50 m，适用条款要求25~40 m。", useful={1: "直接规定突出煤层相关地点压风自救装置距采掘工作面25~40 m。", 2: "直接规定突出煤层、冲击地压煤层压风自救装置设置在距工作面25~40 m。"}, uncertain={3: "规定冲击地压危险工作面必须设置压风自救系统，但未给出25~40 m距离。", 4: "规定突出煤层工作面设置压风自救装置，但未给出具体距离。"}),
    "v9_004_chunk076": decision("non_compliant", "high", "待审块把永久支护锚索作为起吊点，技术规范明确永久支护锚杆等不得用于起吊或悬挂重物。", useful={6: "直接禁止将永久支护锚杆、组合构件等用于起吊设备或悬挂重物。"}, uncertain={1: "规定一般吊装安全要求，但未规定永久支护锚索能否作为吊点。"}),
    "v9_004_chunk087": decision("compliant", "medium", "支架部件检修包含停电闭锁、卸压、监护和防坠落措施，未发现明确放宽要求。", missing=True, uncertain={1: "涉及检修安全的一般要求，可辅助核查但对象并非完全一致。", 2: "规定采煤工作面支架牢固和防倒，可辅助评价支架检修风险。", 3: "涉及设备检修和危险能量控制，但未完整覆盖液压支架更换流程。"}),
    "v9_004_chunk120": decision("non_compliant", "medium", "停风后允许机尾上隅角瓦斯浓度不超过1.2%即进入并开机，未检出支持该放宽阈值的条款。", missing=True, flags=["15条候选未给出上隅角恢复作业的精确阈值"], uncertain={3: "规定恢复工作前必须确认无危险及检查瓦斯，但未给出上隅角1.0%阈值。"}),
    "v9_004_chunk131": decision("compliant", "high", "喷雾、转载点降尘、清洗浮煤和隔爆设施管理均与综合防尘要求一致。", useful={1: "直接规定采煤、转载点喷雾和综合防尘要求。", 3: "直接规定清除浮煤、冲洗煤尘和隔爆设施检查管理。"}, uncertain={4: "仅适用于露天煤矿防尘，与井下综采工作面对象不一致。"}),
    "v9_004_chunk142": decision("uncertain", "medium", "待审块设置了瓦斯管路防静电和隔离措施，但允许特殊情况下管路与电缆距离较近，候选不足以确认该替代措施是否获准。", flags=["需核查矿级专项措施及审批"], useful={4: "直接规定瓦斯抽采管路不得接触非本质安全型带电物体。", 6: "直接规定局部接地极形式、尺寸和埋设要求。", 7: "直接规定设备外壳与接地母线等连接线的最小截面。", 11: "规定爆炸危险场所管道接地和防静电要求。"}, uncertain={1: "规定抽采泵房和临时泵站，和顺槽管路防静电仅主题相关。", 8: "规定总接地网，不能单独验证瓦斯管路独立接地方案。"}),
    "v9_004_chunk153": decision("compliant", "high", "要求登高超过1.5 m即系安全带，比2 m触发要求更严格，不构成放宽。", missing=True, flags=["候选未召回高处作业安全带高度条款"]),
    "v9_004_chunk159": decision("uncertain", "medium", "强制使用单体柱是否违规取决于该工作面是否属于禁止使用单体支护的冲击地压区域，当前块和候选未证明适用条件。", missing=True, flags=["需确认冲击地压属性及超前支护设计"], uncertain={1: "规定采煤工作面超前压力影响范围加强支护，与超前支护有关但不禁止单体柱。", 3: "规定单体液压支柱最低初撑力，可核查参数但不能判断其是否允许使用。", 15: "规定支柱类型和检修要求，不能证明冲击地压区域适用性。"}),
    "v9_004_chunk161": decision("compliant", "medium", "水管使用专用管卡和钢丝绳吊挂、排列和防埋措施均为正向管理要求，候选未显示冲突。", missing=True),
    "v9_006_chunk003": decision("not_suitable", "high", "该块主要是目录页和章节页码，不是可独立作出合规结论的实体要求。", unsuitable=True, missing=True, flags=["目录类块应从普通合规评测集中剔除"]),
    "v9_006_chunk008": decision("uncertain", "medium", "该块是地质情况与测量安排，未出现明确违规，但是否满足地测和防突要求需结合矿井属性及完整设计。", useful={2: "直接要求巷道掘进前分析地质构造、陷落柱和瓦斯等情况并对异常开展超前探测。"}, uncertain={1: "涉及煤层地质和瓦斯资料，可辅助核查但不直接规定本段测量频率。", 4: "涉及突出矿井地质资料要求，适用性取决于矿井属性。", 5: "涉及揭煤地质探测，和本段高抽巷层位测量仅条件相关。", 9: "涉及突出煤层地质测量，需先确认适用范围。", 14: "规定突出矿井地质测量工作，可能相关但不能直接判断本段合规性。"}),
    "v9_006_chunk023": decision("compliant", "medium", "当前v9块中的锚杆长度和直径计算量纲完整，计算结果与选型关系合理，未出现历史样本所称1800 m问题。", missing=True, flags=["历史v5问题未映射到该v9块"], uncertain={1: "规定锚杆参数匹配，可辅助验证选型。", 2: "列出支护参数设计内容，但不给出本块计算结果的合格阈值。"}),
    "v9_006_chunk024": decision("compliant", "medium", "当前v9块使用0.9~1.2 m锚杆间排距和5.3 m锚索长度，未出现1800 m单位错误。", missing=True, flags=["历史v5问题未映射到该v9块"], uncertain={1: "规定锚杆参数匹配，可用于整体设计复核。", 2: "规定支护设计应包含间排距和锚索参数，但无直接数值判据。"}),
    "v9_006_chunk026": decision("not_suitable", "medium", "锚固长度公式存在大量OCR符号破坏，虽然结果数值可读，但不适合作为稳定的普通合规评测样本。", unsuitable=True, missing=True, flags=["公式OCR污染"]),
    "v9_006_chunk030": decision("not_suitable", "high", "该块包含“锚固力要求200 ~ KN”等残缺量纲及明显异常尺寸，文本质量不足以可靠判断技术合规。", unsuitable=True, missing=True, flags=["单位和尺寸OCR污染"], uncertain={1: "规定锚杆、钻孔和锚固剂直径匹配，可辅助发现部分参数问题。", 2: "规定设计必须包含预紧力和设计锚固力，但不能修复残缺数值。"}),
    "v9_006_chunk040": decision("compliant", "medium", "当前v9块要求预紧力达到设计值400 N·m即合格，并未采用低于设计值的合格标准；历史问题未在本块复现。", missing=True, flags=["候选未召回预紧力矩90%合格条款"], uncertain={11: "规定预紧力矩和安装要求，但没有给出90%验收阈值。"}),
    "v9_006_chunk046": decision("compliant", "medium", "局部通风机采用更严格的六专、双风机双电源、风电和甲烷电闭锁，整体符合候选强制要求。", useful={3: "直接规定局部通风机三专、备用风机、自动切换、风筒和双闭锁要求。", 13: "要求在作业规程中明确掘进通风方式及局部通风机、风筒使用。", 15: "规定掘进巷道采用全风压或压入式局部通风机通风。"}, uncertain={9: "规定恢复通风条件，与本块停风管理相关但本块未完整展开恢复程序。"}),
    "v9_006_chunk048": decision("not_suitable", "high", "关键二氧化碳阈值被写成“<1.5 ~ %”，属于影响结论的OCR/文本损坏。", unsuitable=True, flags=["修复文本后可转为普通合规样本"], useful={1: "直接给出恢复通风前甲烷、二氧化碳和开关附近浓度阈值。"}, uncertain={2: "规定恢复工作和瓦斯检查的一般要求，但没有完整分级排放阈值。", 3: "给出旧井巷作业的1.0%和1.5%阈值，对象不同。"}),
    "v9_006_chunk050": decision("non_compliant", "high", "计划停风措施虽规定撤人、警戒和恢复前检查，但未明确切断停风区全部非本质安全型电气设备电源。", useful={1: "直接规定局部通风机恢复前的浓度和排放条件。", 2: "直接要求停风时撤人、切断非本安电源、设栅栏和警示标志。", 3: "规定停风后的断电撤人及恢复通风一般要求。"}, uncertain={8: "规定局部通风机双闭锁，相关但不能替代停风措施中的明确断电要求。"}),
    "v9_006_chunk053": decision("non_compliant", "high", "瓦斯治理分级自设W、P等阈值和区域类别，部分阈值与突出细则临界值不一致，且K1未按干湿煤样区分。", flags=["单块包含多个违规点，人工复核时可拆分"], useful={4: "直接给出干、湿煤样不同K1临界值。", 5: "直接给出区域预测P<0.74 MPa、W<8 m3/t等临界条件。", 7: "直接给出突出煤层鉴定P≥0.74 MPa等临界指标。", 10: "给出煤巷掘进复合指标临界值。", 14: "给出钻屑指标法K1、S等临界值。", 15: "规定区域防突效果检验使用P、W临界值。"}),
    "v9_006_chunk074": decision("uncertain", "medium", "钻杆直径63~75 mm与钻孔不大于94 mm并不必然矛盾；同时块内另有使用原支护锚杆起吊问题，单一标签难以完整表达。", flags=["块内包含不同审查主题"], useful={2: "直接要求探放水使用专用钻机，可核查设备选型。"}, uncertain={1: "规定防突钻孔施工安全，但未规定钻杆与孔径差。", 4: "给出揭煤防突钻孔75~120 mm范围，仅在相应揭煤场景适用。"}),
    "v9_006_chunk075": decision("compliant", "medium", "钻杆63~75 mm配不大于94 mm钻孔没有明显技术冲突，并明确要求钻头孔径匹配和防卡钻措施。", useful={2: "直接要求探放水使用专用钻机，与待审作业对象一致。"}, uncertain={1: "防突钻孔安全要求仅在对应防突场景适用。", 3: "揭煤防突钻孔直径范围与本超前探场景未必相同。"}),
    "v9_006_chunk076": decision("compliant", "medium", "该块是掉钻防治和异常处置要求，未显示钻杆与孔径存在违规关系。", useful={2: "直接要求探放水采用专用钻机和专业队伍。"}, uncertain={1: "防突钻孔安全要求需先确认本钻孔性质。", 3: "揭煤钻孔直径范围与本块超前探场景适用性不明。"}),
    "v9_006_chunk088": decision("compliant", "high", "人员定位系统覆盖入井检测、定位识别卡、动态查询和联网管理，符合人员位置监测要求。", useful={1: "直接规定人员位置监测读卡分站、入井唯一性检测和实时监测要求。", 2: "规定人员位置监测系统相关要求，与系统配置直接相关。", 3: "规定人员位置监测系统相关功能和管理要求。", 14: "规定人员位置监测系统设备和使用要求。"}),
    "v9_006_chunk091": decision("uncertain", "medium", "1000 m避难硐室要求对一般场景可能成立，但突出煤层掘进长度超过500 m时适用更严格的500 m要求；当前块未证明突出属性。", flags=["需确认是否为突出煤层掘进巷道"], useful={1: "直接规定突出煤层超过500 m时在工作面500 m范围内设临时避险设施。", 2: "直接规定突出煤层超过500 m时的500 m避险设施要求。"}, uncertain={3: "规定自救器防护时间，可支撑前提但不能决定避难硐室距离。", 6: "规定突出煤层采掘安全防护措施，适用条件相关。"}),
    "v9_006_chunk095": decision("uncertain", "medium", "待审块明确采用风镐开口，但只有在工作面被预测或认定为突出危险区时才被禁止；当前块未证明该条件。", missing=True, flags=["需确认工作面是否预测或认定为突出危险区"], uncertain={6: "直接规定突出危险区采掘工作面严禁使用风镐，但当前块未证明适用前提。"}),
    "v9_006_chunk096": decision("compliant", "high", "当前块仅规定掘进机操作、停电闭锁、喷雾和检修安全，并未采用风镐；历史问题未映射到本块。", useful={1: "直接规定掘进机启动、喷雾、停机断电等安全要求。"}, uncertain={12: "包含突出危险区禁用风镐条款，但当前块未出现风镐。"}),
    "v9_006_chunk097": decision("compliant", "high", "当前块规定掘进机进退、甩接电缆和禁区管理，未出现风镐作业，历史问题未映射到本块。", useful={2: "直接规定掘进机启动、调机和停机断电等要求。"}, uncertain={10: "包含突出危险区禁用风镐条款，但当前块没有风镐内容。"}),
    "v9_006_chunk104": decision("non_compliant", "high", "待审块普遍允许瓦斯抽采管路与电缆交叉并用隔离材料处理，未限定岔门且未明确总工程师批准的专项措施。", useful={1: "直接规定瓦斯抽采管路与电缆严禁同侧，岔门确需同侧或交叉时必须有总工程师批准的专项措施。"}, uncertain={7: "规定接地网要求，与交叉敷设结论仅间接相关。"}),
    "v9_006_chunk113": decision("non_compliant", "high", "待审块允许5 t以下设备选择原永久支护锚杆（索）起吊，与永久支护不得用于起吊的规范冲突。", useful={6: "直接禁止永久支护锚杆、组合构件等用于起吊设备或悬挂重物。"}, uncertain={1: "规定一般吊装要求，但未解决永久支护能否作为吊点。", 7: "规定单轨吊运输安全，与普通倒链起吊对象不完全一致。"}),
    "v9_006_chunk118": decision("compliant", "medium", "待审块要求起吊锚杆打设牢固，语义更接近专用起吊锚杆，并未明确使用永久支护锚杆。", missing=True, flags=["需人工确认“起吊锚杆”是否专用"], uncertain={6: "禁止使用永久支护锚杆起吊；若本块起吊锚杆并非专用则适用。", 15: "一般吊装安全要求可辅助核查。"}),
    "v9_006_chunk135": decision("uncertain", "medium", "检修过程中受控点动用于移动和张紧胶带，是否允许需结合专门的带式输送机检修规程；候选仅给出一般停电闭锁要求。", missing=True, flags=["需核查带式输送机钉卡检修专门标准"], uncertain={2: "后配套设备检修应停电闭锁，但对象并非该带式输送机钉卡流程。", 3: "规定检修前切断动力源和挂牌，但不能直接判断受控点动例外。", 4: "规定掘进机停机断电，与本块带式输送机对象不同。", 11: "采煤机检修断电要求仅可类比。"}),
    "v9_006_chunk159": decision("compliant", "medium", "当前块规定锚杆孔深度仅允许0~+30 mm、锚杆间排距±100 mm，与候选规范一致；锚索孔距±150 mm需另行核查。", flags=["锚索孔距标准候选未覆盖"], useful={2: "直接规定预紧力矩不低于设计值90%的验收要求，可核查本块质量标准。", 6: "直接规定锚杆孔深0~30 mm、角度和间排距误差要求。"}),
    "v9_006_chunk161": decision("compliant", "medium", "煤质管理要求主要防止垃圾、积水和杂物进入煤流，属于正向管理要求。", missing=True),
    "v9_066_chunk035": decision("non_compliant", "high", "待审块允许5 t以上设备选用合格锚杆（索）经拉拔后起吊，仍违反永久支护不得用于起吊的要求。", useful={6: "直接禁止永久支护锚杆、组合构件等用于起吊设备或悬挂重物。"}, uncertain={5: "一般吊装要求不能替代永久支护吊点禁令。"}),
    "v9_066_chunk044": decision("compliant", "high", "设备外壳使用50 mm²镀锌钢绞线连接主接地母线，达到耐腐蚀铁线不小于50 mm²的要求。", useful={6: "直接规定设备外壳与接地母线等连接可采用不小于50 mm²耐腐蚀铁线。", 14: "直接要求可能带危险电压的设备金属外壳保护接地。"}, uncertain={2: "规定总接地网和主接地极，不能单独验证连接线截面。", 7: "规定局部接地极设置地点，与连接线截面不是同一问题。"}),
    "v9_066_chunk047": decision("uncertain", "medium", "固定段要求使用电缆钩且与瓦斯管路分侧，但设备电缆使用废旧皮带条吊挂是否属于允许的移动电缆处理，候选不足以确定。", flags=["需区分固定敷设电缆与移动设备电缆"], useful={1: "直接规定井下电缆吊挂、与管路距离及瓦斯抽采管路分侧要求。"}, uncertain={4: "规定特定巷道不应敷设电力电缆，与本块吊挂方式不是同一问题。"}),
    "v9_066_chunk049": decision("non_compliant", "high", "待审块一方面称钢管接地极全部埋设，另一方面允许外露100~150 mm，且35 mm²接地连接线低于候选规定的50 mm²耐腐蚀铁线要求。", useful={2: "直接规定钢管局部接地极应全部垂直埋入底板或满足双管埋深要求。", 4: "直接规定设备外壳等连接应采用不小于50 mm²耐腐蚀铁线。"}, uncertain={1: "规定总接地网和主接地极，不能验证局部钢管外露长度。", 6: "规定接地电阻要求，与埋设和截面问题不同。"}),
    "v9_066_chunk055": decision("non_compliant", "medium", "待审块安装后仅等待3分钟即逐根拉拔，候选规范要求拉拔试验在安装后1~24小时进行；搅拌时长还应遵守药卷说明。", useful={4: "直接规定树脂锚杆搅拌和等待时间应遵守锚固剂安装说明。", 7: "直接规定锚杆拉拔试验应在安装后1~24小时进行。"}, uncertain={6: "附录测试锚杆的搅拌时间仅适用于特定试验方法，不能直接作为正常施工上限。"}),
    "v9_066_chunk066": decision("not_suitable", "high", "多个关键瓦斯阈值被OCR为约21.2%、21.5%和20.4%，已严重改变安全含义。", unsuitable=True, flags=["应修复OCR后再进入普通合规评测"], useful={6: "直接规定采掘工作面甲烷浓度达到1.0%时的停止作业、断电撤人要求。", 14: "直接规定停风恢复时甲烷和二氧化碳浓度阈值。"}, uncertain={1: "规定瓦斯超限停电撤人和恢复工作一般要求。", 13: "规定煤层瓦斯参数和管理，与现场作业阈值仅间接相关。", 15: "涉及有瓦斯喷出煤层治理，与本段现场浓度阈值对象不同。"}),
    "v9_066_chunk081": decision("non_compliant", "high", "待审块允许5 t以上设备选择合格锚杆（索）经拉拔后起吊，仍与永久支护不得用于起吊的规范冲突。", useful={6: "直接禁止永久支护锚杆、组合构件等用于起吊设备或悬挂重物。"}, uncertain={1: "一般吊装要求未解决永久支护吊点禁令。", 7: "单轨吊运输要求与普通起吊仅部分相关。"}),
    "v9_066_chunk089": decision("compliant", "medium", "平车运输、沉淀池清挖及混凝土施工均设置了停稳、防倾倒、盖板和现场管理措施，未发现明确违规。", missing=True),
    "v9_066_chunk094": decision("uncertain", "medium", "瓦斯抽采管回收时移动下风侧50 m内传感器可能削弱监测，但候选未直接规定该回收场景传感器必须保留的位置。", missing=True, flags=["需核查瓦斯抽采管回收专项标准"], uncertain={1: "规定抽采管路的一般安全要求，但不直接规定回收时传感器位置。", 2: "规定抽采泵房和临时泵站监测，与管路回收场景不同。", 5: "规定瓦斯检查和恢复工作一般要求。", 14: "规定若干地点必须设置甲烷传感器，但未明确本回收点。"}),
    "v9_066_chunk100": decision("compliant", "medium", "回收支柱、横梁和安装排水系统均规定先确认支护、固定横梁、防坠落及专职电工作业，未发现明确违规。", missing=True),
    "v9_066_chunk112": decision("compliant", "medium", "液压部件拆装前要求卸载压力，并采取前梁固定、防回落和人员禁入措施，未发现明确违规。", missing=True, uncertain={2: "一般检修安全要求与本块能量释放、防坠落措施相关。", 12: "规定支架牢固和防倒，可辅助核查但不直接覆盖液压部件更换。"}),
    "v9_066_chunk130": decision("compliant", "high", "当前v9块没有历史说明中的20.8%报警值，仅规定传感器维护、闭锁和故障期间人工监测，未发现明确放宽。", flags=["历史v5问题未映射到该v9块"], useful={12: "直接规定甲烷传感器报警、断电和复电浓度，可用于核查本块监控要求。", 14: "直接规定机载设备设置甲烷断电仪。"}, uncertain={1: "规定传感器调校和闭锁测试频率，与本块维护要求相关。", 2: "规定甲烷传感器设置地点，与本块监控系统相关。", 3: "规定传感器对照检查和调校，与本块维护相关。", 4: "规定安全监控故障闭锁功能，与本块故障处理相关。"}),
    "v9_066_chunk141": decision("not_suitable", "high", "该块主体是事故经过、原因和教训，属于反面案例语义，不应按普通允许性作业条款判定合规。", unsuitable=True, missing=True, flags=["事故案例应单独设置语义角色"], uncertain={7: "一般管理文件标题，与事故案例仅背景相关。", 11: "规定企业安全管理制度，可支撑事故教训但不能作为该案例的普通合规证据。", 15: "规定从业人员培训要求，可支撑事故教训但不能给事故案例贴普通合规标签。"}),
}


def compact(text: str, limit: int = 80) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def default_useless_note(case: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    focus = compact(case.get("topic") or case["pending"].get("article") or case["pending"]["content"], 42)
    rule = compact(candidate.get("article") or candidate.get("section") or candidate["content"], 42)
    return f"候选主要规定“{rule}”，未直接规定待审关注点“{focus}”的对象、条件或阈值，不能支撑本样本结论。"


def build_annotations(source: Dict[str, Any]) -> Dict[str, Any]:
    output_cases = []
    for case in source["cases"]:
        if case["case_id"] not in DECISIONS:
            raise KeyError(f"Missing manual decision: {case['case_id']}")
        choice = DECISIONS[case["case_id"]]
        annotations = []
        for candidate in case["candidates"]:
            rank = candidate["rank"]
            if rank in choice["useful"]:
                label = "useful"
                confidence = "high"
                note = choice["useful"][rank]
            elif rank in choice["uncertain"]:
                label = "uncertain"
                confidence = "medium"
                note = choice["uncertain"][rank]
            else:
                label = "useless"
                confidence = "high"
                note = default_useless_note(case, candidate)
            annotations.append({
                "chunk_id": candidate["chunk_id"],
                "doc_name": candidate["doc_name"],
                "kb_chunk_index": candidate["kb_chunk_index"],
                "rank": rank,
                "score": candidate["score"],
                "source": candidate["source"],
                "label": label,
                "confidence": confidence,
                "note": note,
                "content": candidate["content"],
            })
        output_cases.append({
            "case_id": case["case_id"],
            "sample_source": case["sample_source"],
            "topic": case.get("topic", ""),
            "source_note": case.get("source_note", ""),
            "pending": case["pending"],
            "final_label": choice["final_label"],
            "final_confidence": choice["final_confidence"],
            "final_reason": choice["final_reason"],
            "missing_correct_evidence": choice["missing_correct_evidence"],
            "not_eval_suitable": choice["not_eval_suitable"],
            "review_flags": choice["review_flags"],
            "human_note": "Codex逐项预标注；需由领域人员最终确认。",
            "candidate_annotations": annotations,
        })
    return {
        "schema": "coal_rag_codex_manual_preannotations_v9_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_cases": str(DEFAULT_CASES),
        "annotation_method": "Current Codex session manually reviewed each pending chunk and candidate; no external LLM was used.",
        "case_count": len(output_cases),
        "cases": output_cases,
    }


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def build_html(payload: Dict[str, Any]) -> str:
    final_counts = Counter(case["final_label"] for case in payload["cases"])
    candidate_counts = Counter(
        candidate["label"]
        for case in payload["cases"]
        for candidate in case["candidate_annotations"]
    )
    cases_html = []
    for index, case in enumerate(payload["cases"], start=1):
        candidates_html = []
        for candidate in case["candidate_annotations"]:
            candidates_html.append(f"""
            <article class="candidate {esc(candidate['label'])}">
              <div class="candidate-head">
                <span class="rank">#{candidate['rank']}</span>
                <strong>{esc(candidate['label'])}</strong>
                <span>{esc(candidate['chunk_id'])}</span>
              </div>
              <p class="note">{esc(candidate['note'])}</p>
              <details><summary>查看法规全文</summary><pre>{esc(candidate['content'])}</pre></details>
            </article>""")
        flags = "".join(f"<li>{esc(flag)}</li>" for flag in case["review_flags"]) or "<li>无额外标记</li>"
        cases_html.append(f"""
        <section class="case" data-label="{esc(case['final_label'])}" data-missing="{str(case['missing_correct_evidence']).lower()}">
          <header class="case-head">
            <span class="number">{index:02d}</span>
            <div>
              <h2>{esc(case['case_id'])}</h2>
              <p>{esc(case['topic'])}</p>
            </div>
            <div class="verdict {esc(case['final_label'])}">{esc(case['final_label'])}<small>{esc(case['final_confidence'])}</small></div>
          </header>
          <div class="judgment">
            <strong>Codex判断</strong>
            <p>{esc(case['final_reason'])}</p>
            <div class="flags"><span>missing evidence: {str(case['missing_correct_evidence']).lower()}</span><span>not suitable: {str(case['not_eval_suitable']).lower()}</span></div>
            <ul>{flags}</ul>
          </div>
          <details class="pending" open><summary>待审 chunk 全文</summary><pre>{esc(case['pending']['content'])}</pre></details>
          <div class="candidate-list">{''.join(candidates_html)}</div>
        </section>""")
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex v9 黄金集预标注复核</title>
<style>
:root{{--paper:#f4f0e7;--ink:#17211c;--muted:#657168;--line:#c9c2b5;--green:#176b4b;--red:#a33a2b;--amber:#9a641d;--blue:#245b78}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--paper);color:var(--ink);font-family:"Noto Serif SC","Microsoft YaHei",serif;letter-spacing:0}}
.top{{position:sticky;top:0;z-index:10;background:#17211c;color:#fff;border-bottom:4px solid #d3a844;padding:18px 5vw}}
.top h1{{margin:0;font-size:24px}} .top p{{margin:6px 0 12px;color:#d9e0da;font-size:13px}}
.toolbar{{display:flex;flex-wrap:wrap;gap:8px}} button{{border:1px solid #829087;background:transparent;color:#fff;padding:7px 10px;cursor:pointer;border-radius:3px}} button.active{{background:#d3a844;color:#17211c;border-color:#d3a844}}
.stats{{padding:18px 5vw;border-bottom:1px solid var(--line);display:flex;gap:24px;flex-wrap:wrap;font-size:13px}} .stats b{{font-size:20px;margin-right:5px}}
main{{width:min(1500px,94vw);margin:24px auto 80px}} .case{{border-top:3px solid var(--ink);padding:22px 0 36px;margin-bottom:32px}}
.case-head{{display:grid;grid-template-columns:52px 1fr auto;gap:14px;align-items:start}} .number{{font:700 24px Georgia,serif;color:var(--muted)}} h2{{font-size:19px;margin:0 0 4px}} .case-head p{{margin:0;color:var(--muted);font-size:13px}}
.verdict{{min-width:125px;padding:8px 10px;color:#fff;font-weight:700;text-align:center;border-radius:3px}} .verdict small{{display:block;font-weight:400;margin-top:3px}}
.verdict.compliant{{background:var(--green)}} .verdict.non_compliant{{background:var(--red)}} .verdict.uncertain{{background:var(--amber)}} .verdict.not_suitable{{background:var(--blue)}}
.judgment{{margin:18px 0;border-left:5px solid #d3a844;padding:8px 16px}} .judgment p{{margin:6px 0;line-height:1.75}} .judgment ul{{margin:7px 0;padding-left:18px;color:var(--muted)}} .flags{{display:flex;gap:8px;flex-wrap:wrap}} .flags span{{border:1px solid var(--line);padding:3px 7px;font-size:12px}}
details{{border:1px solid var(--line);background:#fffdf8}} summary{{cursor:pointer;padding:9px 12px;font-weight:700}} pre{{white-space:pre-wrap;word-break:break-word;margin:0;padding:12px;line-height:1.7;font-family:"Microsoft YaHei",sans-serif;font-size:13px}}
.candidate-list{{margin-top:18px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}} .candidate{{background:#fffdf8;border:1px solid var(--line);border-top-width:4px;padding:10px;min-width:0}} .candidate.useful{{border-top-color:var(--green)}} .candidate.uncertain{{border-top-color:var(--amber)}} .candidate.useless{{border-top-color:#9da59f}}
.candidate-head{{display:flex;gap:8px;align-items:center;font:12px "Microsoft YaHei",sans-serif;overflow-wrap:anywhere}} .rank{{font-weight:700}} .candidate .note{{font:13px/1.65 "Microsoft YaHei",sans-serif;color:#37433c;min-height:64px}} .candidate details{{margin-top:8px}} .candidate details pre{{max-height:320px;overflow:auto}}
@media(max-width:1000px){{.candidate-list{{grid-template-columns:1fr}}.case-head{{grid-template-columns:38px 1fr}}.verdict{{grid-column:2;justify-self:start}}}}
</style></head>
<body><header class="top"><h1>Codex v9 黄金集预标注复核</h1><p>当前 Codex 会话逐项预标注，无外部模型。点击筛选并展开全文进行最终人工确认。</p>
<div class="toolbar"><button class="active" data-filter="all">全部</button><button data-filter="non_compliant">不合规</button><button data-filter="compliant">合规</button><button data-filter="uncertain">不确定</button><button data-filter="not_suitable">不适合作普通样本</button><button data-filter="missing">缺失正确证据</button></div></header>
<div class="stats"><span><b>{payload['case_count']}</b>待审块</span><span><b>{sum(candidate_counts.values())}</b>候选规则</span><span>最终标签 {esc(dict(final_counts))}</span><span>候选标签 {esc(dict(candidate_counts))}</span></div>
<main>{''.join(cases_html)}</main>
<script>
document.querySelectorAll('button[data-filter]').forEach(btn=>btn.addEventListener('click',()=>{{
 document.querySelectorAll('button[data-filter]').forEach(x=>x.classList.remove('active')); btn.classList.add('active');
 const f=btn.dataset.filter; document.querySelectorAll('.case').forEach(c=>c.style.display=(f==='all'||c.dataset.label===f||(f==='missing'&&c.dataset.missing==='true'))?'block':'none');
}}));
</script></body></html>"""


def validate(payload: Dict[str, Any]) -> None:
    assert payload["case_count"] == 50
    assert len(DECISIONS) == 50
    assert sum(len(case["candidate_annotations"]) for case in payload["cases"]) == 750
    for case in payload["cases"]:
        assert len(case["candidate_annotations"]) == 15
        assert {item["rank"] for item in case["candidate_annotations"]} == set(range(1, 16))
        assert all(item["note"] for item in case["candidate_annotations"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-html", type=Path, default=DEFAULT_HTML)
    args = parser.parse_args()
    source = json.loads(args.cases.read_text(encoding="utf-8"))
    payload = build_annotations(source)
    validate(payload)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_html.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output_html.write_text(build_html(payload), encoding="utf-8")
    print(args.output_json)
    print(args.output_html)


if __name__ == "__main__":
    main()
