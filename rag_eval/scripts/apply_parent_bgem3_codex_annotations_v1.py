"""Apply progressively reviewed Codex decisions to local BGE-M3 checkpoints.

Add decisions in small reviewed batches. Every case is atomically saved before
the next case is processed, so interrupted annotation work can resume safely.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHECKPOINT_DIR = PROJECT_ROOT / "rag_eval" / "data" / "parent_bgem3_annotation_checkpoints_v1"


def decision(
    final_label: str,
    final_reason: str,
    *,
    useful: tuple[int, ...] = (),
    uncertain: tuple[int, ...] = (),
    missing: bool = False,
    not_suitable: bool = False,
    flags: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "final_label": final_label,
        "final_reason": final_reason,
        "useful": set(useful),
        "uncertain": set(uncertain),
        "missing": missing,
        "not_suitable": not_suitable,
        "flags": list(flags),
    }


# Each entry is added only after the current v6 candidates have been reviewed.
DECISIONS: dict[str, dict[str, Any]] = {
    "v9_004_chunk013": decision(
        "uncertain",
        "待审块采用后退式大采高低位放顶煤综采，候选规则支持正规开采、综采设计及放顶煤专项要求，但不足以验证3.2 m采高、回收率和具体工艺参数是否与支架及设计完全匹配。",
        useful=(4, 7, 13),
        uncertain=(1,),
        missing=True,
        flags=("需结合工作面设计、支架有效支护高度及煤层条件复核具体参数",),
    ),
    "v9_004_chunk038": decision(
        "compliant",
        "待审块要求初采期间加强瓦斯检查、监测闭锁，并规定送电前检查20 m范围瓦斯，0.8%和0.4%的送电阈值未放宽候选规则中的报警、断电及复电要求。",
        useful=(6, 10),
        uncertain=(4, 9),
        missing=False,
    ),
    "v9_004_chunk060": decision(
        "compliant",
        "待审块配置工作面调度电话、刮板输送机通信与启停闭锁、声光报警和视频监控，能够覆盖候选规则对工作面有线调度电话、刮板输送机通信信号及采煤机闭锁的主要要求。",
        useful=(1, 7, 8, 14),
        uncertain=(6,),
        missing=False,
    ),
    "v9_004_chunk061": decision(
        "non_compliant",
        "待审块允许压风自救装置设置在距工作面25~50 m范围，而候选规则对突出煤层、冲击地压煤层规定为25~40 m；该表述允许装置超出规定距离。",
        useful=(13,),
        uncertain=(1, 8),
        missing=False,
        flags=("最终适用性仍需确认该工作面是否属于突出煤层或冲击地压煤层",),
    ),
    "v9_004_chunk076": decision(
        "non_compliant",
        "待审块明确选择支护锚索作为设备起吊点，属于使用永久支护构件承受起吊载荷的高风险做法；当前候选仅包含一般起吊、运输和锚杆安装要求，未召回禁止使用永久支护锚杆或锚索起吊的直接规则。",
        uncertain=(5, 6, 14),
        missing=True,
        flags=("需补充召回永久支护锚杆、锚索不得用于起吊或悬挂重物的直接规定",),
    ),
    "v9_004_chunk087": decision(
        "compliant",
        "待审块对液压支架部件更换规定了停电闭锁、液压缸卸载、敲帮问顶、防坠落和专人指挥等措施，未发现与候选规则直接冲突的内容。候选规则仅能直接验证敲帮问顶要求，未覆盖支架液压系统卸压及部件更换的主要风险。",
        useful=(7,),
        uncertain=(2, 5, 6, 13),
        missing=True,
        flags=("需补充召回液压支架检修、液压系统卸压和承载部件更换的直接规定",),
    ),
    "v9_004_chunk120": decision(
        "non_compliant",
        "待审块允许在机尾上隅角甲烷浓度不超过1.2%时恢复人员进入和开机，可能放宽采掘工作面及其他作业地点1.0%的用电作业控制要求；同时仅规定恢复送风5分钟，未明确必须确认受停风影响地点无危险后方可恢复工作。当前Top15未召回恢复通风和甲烷阈值的直接条款。",
        uncertain=(3, 5, 13),
        missing=True,
        flags=(
            "需补充召回煤矿安全规程第一百九十二条、第一百九十三条、第一百九十六条和第一百九十七条",
            "胶带检修停机闭锁措施本身基本合理，但对应直接规则同样未进入Top15",
        ),
    ),
    "v9_004_chunk131": decision(
        "compliant",
        "待审块设置了架间及放煤喷雾、转载点喷雾、巷道冲洗、个体防护、隔爆水袋巡查和防尘台账，符合综合防尘方向，未发现候选规则能够证明其违规。候选中仅炮采综合防尘条款与其部分措施直接相关，更多关键防尘条款未被召回。",
        useful=(5,),
        uncertain=(6, 10, 11, 14),
        missing=True,
        flags=("需补充召回综合防尘管理、隔爆设施检查和井下转载点喷雾的直接规定",),
    ),
    "v9_004_chunk142": decision(
        "uncertain",
        "待审块要求瓦斯管路与电缆、电气设备分侧布置并采取绝缘、屏蔽和接地措施，能够满足不得接触带电物体的基本要求；但其表述允许因特殊原因距离较近，未明确岔门处确需同侧或交叉时必须制定专项安全技术措施并由煤矿总工程师审批，因此不能仅凭当前文本确认完全合规。",
        useful=(4, 6),
        uncertain=(2,),
        missing=False,
        flags=("需核实同侧或交叉敷设情形是否限定在岔门处，并落实专项措施及总工程师审批",),
    ),
    "v9_004_chunk153": decision(
        "compliant",
        "待审块对矿压监测系统安装维护规定了人员培训、现场监护、登高防坠、胶带闭锁、钻机旋转部位防护和顶帮监护等措施，未发现与候选规则冲突。候选仅能部分支持监测系统及线缆布置要求，未覆盖登高、胶带侧作业和钻机操作的关键安全要求。",
        useful=(7, 10),
        uncertain=(6,),
        missing=True,
        flags=("需补充召回高处作业、带式输送机附近作业及钻机安全操作的直接规定",),
    ),
    "v9_004_chunk159": decision(
        "uncertain",
        "待审块关于浮煤清理、安全出口高度、采高控制、单体液压支柱初撑力及不同工作阻力支柱不得混用等要求与候选规则基本一致；但其强制规定综采工作面超前支护必须使用单体柱，是否违规取决于工作面冲击地压危险等级和超前支护设计，当前文本与Top15均不足以确认适用条件。",
        useful=(2, 4, 6, 7, 8),
        uncertain=(3,),
        missing=True,
        flags=("需确认工作面冲击地压属性，并补充召回冲击地压区域单体液压支柱使用限制",),
    ),
    "v9_004_chunk161": decision(
        "compliant",
        "待审块规定水管使用专用管卡和钢丝绳吊挂、保持平直、分类排列、防腐并避免埋入浮煤，属于正向质量管理要求，未发现明显违规。当前Top15候选均未直接规定井下水管吊挂方式，不能用于充分验证其具体间距和固定方式。",
        missing=True,
        flags=("需补充召回井下压风、供水和排水管路吊挂及布置的直接标准",),
    ),
    "v9_006_chunk003": decision(
        "not_suitable",
        "待审块仅由目录中的章节名称和页码组成，不包含可独立判断合规性的实体要求，不应作为普通合规审查或检索效果黄金样本。",
        missing=True,
        not_suitable=True,
        flags=("目录类待审块应从黄金评测集剔除或单独统计",),
    ),
    "v9_006_chunk008": decision(
        "uncertain",
        "待审块记录高抽巷层位、陷落柱位置、定期标高测量及层位台账，符合掘进前分析地质构造和异常情况的基本方向；但是否属于突出煤层顶底板掘进、是否需要先探后掘，以及每隔60至100 m测量能否满足动态掌握地质变化的要求，需结合矿井属性和完整设计确认。",
        useful=(1,),
        uncertain=(2, 5, 8, 10, 13, 15),
        missing=False,
        flags=("需确认该高抽巷与突出煤层的法向距离、突出危险属性及超前探测设计",),
    ),
    "v9_006_chunk023": decision(
        "compliant",
        "待审块中的锚杆长度和直径计算量纲完整，计算结果分别为2.32 m和0.019 m，选用2.4 m长、22 mm直径锚杆均大于计算值；当前块未出现历史样本所称的1800 m单位错误。候选规则支持锚杆参数设计和理论计算方法，但不能单独证明全部输入参数取值正确。",
        useful=(2, 9, 11),
        uncertain=(4, 5, 6, 14),
        missing=True,
        flags=("历史v5问题未映射到当前v9块；需结合现场地质力学评估和正式支护设计复核输入参数",),
    ),
    "v9_006_chunk024": decision(
        "compliant",
        "待审块根据施工经验和数值模拟选择0.8至1.2 m锚杆间排距，并计算锚索最低长度4.21 m后选用不小于5.3 m锚索，当前文本未出现历史样本中的1800 m单位错误。候选可支持支护参数设计与动态设计方法，但不能验证全部计算输入和正式设计是否一致。",
        useful=(5, 12),
        uncertain=(4, 6, 11, 13),
        missing=True,
        flags=("历史v5问题未映射到当前v9块；需结合地质力学评估、监测反馈和正式支护设计复核参数",),
    ),
    "v9_006_chunk026": decision(
        "not_suitable",
        "待审块的锚固长度公式存在大量OCR符号破坏，例如半径平方、括号和上下标被错误识别；虽然1270 mm、980 mm和1900 mm等结果仍可读取，但无法可靠复算公式或据此作出稳定的合规判断。",
        useful=(5,),
        uncertain=(2, 4, 8, 12, 13),
        missing=True,
        not_suitable=True,
        flags=("公式OCR污染；应先修复文本或使用原始版式后再纳入黄金评测",),
    ),
    "v9_006_chunk030": decision(
        "not_suitable",
        "待审块包含明显异常或残缺数据，例如锚固力要求“200 ~ KN”、150×150×1000 mm锚杆托盘、300×300×1600 mm锚索托盘以及受损的钢筋梯子梁规格。无法确认这些是原文技术错误还是OCR解析错误，因此不适合直接作为合规评测样本。",
        useful=(14,),
        uncertain=(2, 5, 7, 8, 9, 10, 11, 12, 13),
        missing=True,
        not_suitable=True,
        flags=("单位和尺寸OCR污染；需对照原始文档版式确认后重新切块",),
    ),
    "v9_006_chunk040": decision(
        "compliant",
        "待审块完成弱动压危险评价，并规定锚固力、预紧力矩、表面位移、综合测站及顶板离层监测；预紧力矩达到设计值400 N·m方为合格，未采用低于设计值的放宽标准。候选规则能够支持危险评价、测站布置、表面位移监测和观测频度，但未召回预紧力矩验收的更直接条款。",
        useful=(2, 9, 10, 15),
        uncertain=(12,),
        missing=True,
        flags=("需补充召回锚杆预紧力矩抽检合格标准的直接规定",),
    ),
    "v9_006_chunk046": decision(
        "compliant",
        "待审块对掘进局部通风规定了专人管理、专用供电、双风机双电源自动切换、风电和瓦斯电闭锁、风筒管理、无计划停风撤人断电警戒及除尘风机联锁，整体覆盖并部分严于候选规则要求，未发现明确冲突。",
        useful=(2, 5, 8, 9, 12, 13),
        uncertain=(3, 11, 14),
        missing=False,
    ),
    "v9_006_chunk048": decision(
        "not_suitable",
        "待审块整体采用了比规程更严格的局部通风机开机处甲烷浓度0.4%阈值，并规定分级排放、恢复后检查和送电条件；但关键二氧化碳阈值被解析成“<1.5 ~ %”，该文本损坏直接影响阈值判断，因此当前版本不适合作为稳定的普通合规样本。",
        useful=(1, 3, 7, 10, 14, 15),
        uncertain=(4, 6, 8),
        missing=False,
        not_suitable=True,
        flags=("关键二氧化碳阈值存在OCR污染；修复原文后可重新纳入评测",),
    ),
    "v9_006_chunk050": decision(
        "non_compliant",
        "待审块对计划停风规定了撤人、警戒、汇报及恢复前瓦斯检查，但未明确停风时必须切断停风区非本质安全型电气设备电源。仅依赖停风票上的概括性要求，不能替代措施正文中的明确断电要求。",
        useful=(4,),
        uncertain=(1, 5, 11, 15),
        missing=True,
        flags=("需补充召回使用局部通风机的掘进工作面停风时撤人、断电、设置栅栏和警标的直接条款",),
    ),
    "v9_006_chunk053": decision(
        "non_compliant",
        "待审块自行设置五级瓦斯治理区域和W、P、K1、S、Q等阈值，存在残缺条件，并规定巷道掘进30 m后不再测定瓦斯参数；这些表述不能替代依法确定的突出危险性预测、效果检验及持续动态管理要求。当前Top15仅召回部分瓦斯参数和矿井等级规则，未召回K1等直接临界值条款。",
        useful=(5, 8, 10, 11),
        uncertain=(3, 6, 9, 14),
        missing=True,
        flags=("单块包含多个潜在违规点；需补充召回突出危险性预测指标、干湿煤样K1临界值及动态检验要求",),
    ),
    "v9_006_chunk074": decision(
        "non_compliant",
        "待审块明确要求从顶板完好支护中选择一根合格锚杆或锚索作为起吊点，属于使用永久支护构件起吊设备的违规做法。其余钻进异常停钻、不得拔杆、撤人汇报等措施基本合理，但不能消除该明确违规点。",
        useful=(6,),
        uncertain=(9, 12),
        missing=True,
        flags=("Top15未召回永久支护锚杆、锚索不得用于起吊的直接规定；块内包含不同审查主题",),
    ),
    "v9_006_chunk075": decision(
        "compliant",
        "待审块使用专用液压坑道钻机，规定钻头与孔径匹配、异常停钻、旋转部位防护、装卸钻杆站位、瓦斯监测、停电闭锁和专人看守等措施，未发现与候选规则直接冲突。63至75 mm钻杆配不大于94 mm钻孔并不构成明显技术矛盾。",
        useful=(3,),
        uncertain=(6, 8, 12),
        missing=True,
        flags=("需补充召回探放水钻机操作、钻孔异常处置及钻孔作业瓦斯监测的直接规定",),
    ),
    "v9_006_chunk076": decision(
        "compliant",
        "待审块要求两台钻机保留作业空间和畅通退路，并对损坏钻杆、掉钻汇报、打捞、无法打捞后的封孔挂牌及说明书作出规定，未发现明确违规。当前候选主要针对不同用途的超前钻孔参数，不能直接验证掉钻处置要求。",
        uncertain=(2, 3, 8, 15),
        missing=True,
        flags=("需补充召回钻孔事故、遗留钻杆和废弃钻孔封闭管理的直接规定",),
    ),
    "v9_006_chunk088": decision(
        "compliant",
        "待审块要求所有下井人员佩戴定位识别卡，在出入井口检测坏卡并及时更换，在井下设置读卡器并实时上传人员位置、轨迹和考勤信息，能够覆盖候选规则对标识卡、读卡分站及唯一性检测的主要要求。",
        useful=(1, 2, 10),
        uncertain=(6, 9),
        missing=False,
    ),
    "v9_006_chunk091": decision(
        "uncertain",
        "待审块对自救器、防护时间和避难硐室操作管理作出规定，其中入井人员携带不低于30分钟隔绝式自救器符合规则；但统一采用距采掘工作面1000 m范围设置避难硐室，仅适用于其他矿井，若属于突出煤层且掘进长度或推进长度超过500 m，则应执行500 m范围的更严格要求。",
        useful=(2, 10),
        uncertain=(1, 4, 6, 13, 14),
        missing=False,
        flags=("需确认该工作面是否属于突出煤层，以及掘进长度或推进长度是否超过500 m",),
    ),
    "v9_006_chunk095": decision(
        "uncertain",
        "待审块明确采用机掘配合人工风镐开口，并规定敲帮问顶、临时支护和监测设施；候选规则明确预测或者认定为突出危险区的采掘工作面严禁使用风镐，但当前块未说明该工作面是否满足该适用条件，因此不能直接判定合规或违规。",
        useful=(5,),
        uncertain=(3, 7, 8),
        missing=False,
        flags=("需确认开口工作面是否预测或认定为突出危险区",),
    ),
    "v9_006_chunk096": decision(
        "compliant",
        "待审块对掘进机专职操作、启动警报、人员避让、急停保护、离机断电、检修闭锁、截割臂下禁人及内外喷雾等作出明确要求，未发现明显冲突，也未出现历史样本关注的风镐使用问题。Top15仅直接召回喷雾要求，普通掘进机操作的关键条款未召回。",
        useful=(14,),
        uncertain=(6,),
        missing=True,
        flags=("历史问题未映射到当前块；需补充召回掘进机启动、站位、离机断电和检修闭锁的直接规定",),
    ),
    "v9_006_chunk097": decision(
        "compliant",
        "待审块对掘进机进退、人员禁区、电缆甩接、瓦斯检查、风筒保护和停机闭锁作出较完整规定，未发现明显违规，也未出现历史样本关注的风镐作业。当前Top15主要召回其他机械或条件性规则，缺少普通掘进机进退和移动作业的直接条款。",
        useful=(5,),
        uncertain=(1, 6),
        missing=True,
        flags=("历史问题未映射到当前块；需补充召回掘进机移动、人员站位和甩接电缆的直接规定",),
    ),
    "v9_006_chunk104": decision(
        "non_compliant",
        "待审块允许瓦斯抽采管路与电缆交叉时采用皮带包裹、金属网隔离和接地处理，但未将该情形限定在岔门处，也未明确制定专项安全技术措施并由煤矿总工程师审批，放宽了候选规则对同侧或交叉敷设的限制。",
        useful=(2, 3, 4),
        uncertain=(1, 5, 6, 7, 8, 12),
        missing=False,
    ),
    "v9_006_chunk113": decision(
        "non_compliant",
        "待审块明确允许起吊设备小于5 t时选择原支护锚杆或锚索进行拖拉、起吊，与候选规则关于永久支护锚杆、组合构件和金属网不得用于起吊设备或悬挂重物的要求直接冲突。",
        useful=(9,),
        uncertain=(4, 5, 6, 12),
        missing=False,
    ),
    "v9_006_chunk118": decision(
        "compliant",
        "待审块要求拆卸和更换掘进机部件前断电闭锁、支稳部件、设置监护、检查顶板支护、使用满足载荷的起吊器具，并称起吊锚杆应打设牢固，语义更接近为起吊专门打设的锚杆，未明确使用永久支护锚杆。",
        useful=(4, 6, 8, 12),
        uncertain=(2, 7),
        missing=True,
        flags=("需人工确认“起吊锚杆”为专用起吊锚杆，而非原永久支护锚杆",),
    ),
    "v9_006_chunk135": decision(
        "uncertain",
        "待审块在胶带钉卡过程中安排受控点动开机以拖带和重新张紧，同时在更换电机、减速机时执行停电闭锁和挂牌。受控点动是否允许以及需要满足哪些附加条件，应结合带式输送机钉卡检修专门规程判断；当前候选仅能支持一般停电和检修安全要求。",
        useful=(4, 7),
        uncertain=(1, 2, 12),
        missing=True,
        flags=("需补充召回带式输送机钉卡、接带和检修期间点动运行的专门标准",),
    ),
    "v9_006_chunk159": decision(
        "compliant",
        "待审块规定锚杆孔深误差0～+30 mm、间排距误差±100 mm、安装角度偏差不超过±5°，并要求托板和组合构件贴面、锚固剂合格、工程质量验收，均与当前直接候选一致，未发现明确冲突。锚索孔距、外露长度、预紧力及喷射混凝土厚度等具体参数仍需结合设计和其他标准复核。",
        useful=(2, 5, 7, 9, 11),
        uncertain=(4, 8, 10, 12, 13, 14, 15),
        missing=True,
        flags=("锚索和喷射混凝土具体参数缺少直接规则或设计值支撑",),
    ),
    "v9_006_chunk161": decision(
        "compliant",
        "待审块要求防止垃圾、铁器、木器、矸石和积水混入煤流，并设置排水、破碎和清理措施；候选关于煤仓防水、防杂物和大块煤矸进入的规定能够支持其主要管理方向，未发现明显违规。",
        useful=(15,),
        uncertain=(3, 9, 12, 13),
        missing=True,
        flags=("煤质管理多数要求不属于当前安全规则库的直接覆盖范围",),
    ),
    "v9_066_chunk035": decision(
        "non_compliant",
        "待审块明确允许设备重量大于5 t时选择合格的原支护锚杆（索），经拉拔试验后作为起吊点；拉拔合格不能改变永久支护用途，仍与永久支护锚杆、组合构件和金属网不得用于起吊设备或悬挂重物的直接规定冲突。",
        useful=(7,),
        uncertain=(11,),
        missing=False,
    ),
    "v9_066_chunk044": decision(
        "compliant",
        "待审块要求移动变电站和多台电气设备设置接地，设备外壳使用50 mm²镀锌钢绞线连接接地母线，高压电缆接线盒连接局部接地极，主要做法与候选关于局部接地极、总接地网及外壳连接线截面的规定一致。",
        useful=(9, 11, 12, 13),
        uncertain=(2, 3, 5, 8),
        missing=True,
        flags=("主辅助接地极间距及辅助接地芯线截面缺少直接候选支撑",),
    ),
    "v9_066_chunk047": decision(
        "non_compliant",
        "待审块一方面要求严禁用铁丝吊挂电缆，另一方面又明确用8号铅丝将设备电缆吊挂在溜槽圆钢上，并允许固定电缆借助原帮、顶锚杆吊挂钢绞线；前者还与候选关于禁止将电缆绑扎、搭挂或固定在金属构件上的规定冲突。其瓦斯管路与电缆距离较近时仅设置屏蔽装置的做法也需专门规则复核。",
        useful=(12,),
        uncertain=(1, 7, 10, 15),
        missing=True,
        flags=("需补充召回井下固定与移动电缆吊挂、瓦斯抽采管路分侧布置的专门条款",),
    ),
    "v9_066_chunk049": decision(
        "non_compliant",
        "待审块规定接地母线及电气设备与接地母线之间均采用35 mm²镀锌钢绞线，低于候选对设备外壳连接所要求的50 mm²耐腐蚀铁线，也低于主接地极母线所要求的100 mm²耐腐蚀铁线；同时又在要求钢管接地极全部埋入地下后允许地表外露100～150 mm，存在内部矛盾。",
        useful=(8, 11, 12, 15),
        uncertain=(10,),
        missing=False,
    ),
    "v9_066_chunk055": decision(
        "non_compliant",
        "待审块要求悬吊锚杆（索）安装完毕仅过3分钟即进行拉拔试验，而直接候选要求锚杆拉拔应在安装后1～24小时内进行，等待时间明显不足；固定30秒搅拌时长也应进一步核对所用树脂药卷说明。",
        useful=(2,),
        uncertain=(1, 7, 8),
        missing=False,
    ),
    "v9_066_chunk066": decision(
        "not_suitable",
        "待审块多个决定停工、停电和撤人的关键瓦斯阈值已被OCR破坏为“21.2%”“21.5%”“20.4%”等文本，无法确认原文究竟是≥1.2%、≥1.5%、≥0.4%还是其他数值。虽然候选能够提供标准阈值，但当前待审文本不适合用于普通合规性黄金评测。",
        useful=(1, 2, 4, 13, 15),
        uncertain=(3, 8, 11, 14),
        missing=False,
        not_suitable=True,
        flags=("应修复关键瓦斯阈值OCR后再进入合规评测",),
    ),
    "v9_066_chunk081": decision(
        "non_compliant",
        "待审块前部要求根据需要打设专用起吊锚索，但后部又允许设备重量大于5 t时选择合格的原支护锚杆（索）经拉拔后作为起吊点；拉拔合格不能改变永久支护用途，该做法与永久支护不得用于起吊设备或悬挂重物的直接候选冲突。",
        useful=(11,),
        uncertain=(5, 6, 15),
        missing=False,
    ),
    "v9_066_chunk089": decision(
        "compliant",
        "待审块对平车装卸、人工推运、沉淀池盖板、停机转运淤煤及混凝土施工设置了停稳、防倾倒、警号、盖板恢复和现场管理措施，当前候选中未发现与其直接冲突的规定。",
        uncertain=(2, 5, 13),
        missing=True,
        flags=("缺少人工推车、平车装卸及沉淀池清挖的直接规则候选",),
    ),
    "v9_066_chunk094": decision(
        "uncertain",
        "待审块要求回收瓦斯抽采管时，将作业点下风侧50 m范围内的瓦斯传感器办理手续后移至所谓安全位置。移动传感器可能削弱管路拆卸期间对泄漏瓦斯的监测，但当前候选仅规定部分传感器设置位置和瓦斯抽采一般要求，未直接覆盖该回收场景，无法据此确定是否允许移动及应移至何处。",
        uncertain=(3, 4, 5, 12),
        missing=True,
        flags=("需补充召回瓦斯抽采管路回收期间传感器布置和连续监测专项标准",),
    ),
    "v9_066_chunk100": decision(
        "non_compliant",
        "待审块在排水系统安装措施中明确使用锚杆螺栓将顶端管路与巷帮锚杆连接吊挂，属于利用永久支护锚杆悬挂其他重物，与候选关于永久支护锚杆不得用于起吊设备或悬挂其他重物的规定直接冲突。回收横梁时将横梁扔向无人处的做法也需要专门安全规则复核。",
        useful=(13,),
        uncertain=(1, 2, 15),
        missing=True,
        flags=("需补充召回支架横梁回收、抛放和排水管路吊挂专项规则",),
    ),
    "v9_066_chunk112": decision(
        "compliant",
        "待审块要求所有液压部件拆装前卸载压力，拆接液管和阀组前释放残压，并在拆卸千斤顶前固定前梁、禁止人员进入回落区域，未发现与当前候选直接冲突的做法。",
        useful=(12,),
        uncertain=(1, 3, 15),
        missing=True,
        flags=("液压支架部件拆装和残压释放的直接专项规则未被召回",),
    ),
    "v9_066_chunk130": decision(
        "compliant",
        "待审块要求工作面电气设备实现瓦斯电闭锁，维护和调校监测装置，确保机载故障闭锁、瓦斯电闭锁有效，并在监控故障期间使用便携仪、增加人工检查和及时更换，未发现明确放宽报警、断电或复电阈值的内容。",
        useful=(2, 4, 5, 11, 14),
        uncertain=(1, 6, 12),
        missing=True,
        flags=("安全监控系统故障期间能否继续作业及人工替代监测要求缺少直接候选",),
    ),
    "v9_066_chunk141": decision(
        "not_suitable",
        "待审块主体是运输事故经过、违规原因和防范教训，其中出现的违规使用连接杆复轨、违规装运锚杆等内容属于反面案例描述，并非允许性操作要求。若按普通作业条款进行合规判断，会把案例中的违规行为误判为待审要求。",
        useful=(3,),
        uncertain=(6, 8, 9, 10),
        missing=True,
        not_suitable=True,
        flags=("事故案例应单独设置语义角色并排除普通合规性评测",),
    ),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def compact(text: str, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def candidate_note(label: str, pending: dict[str, Any], candidate: dict[str, Any]) -> str:
    focus = compact(pending.get("content", ""), 65)
    rule = compact(candidate.get("content", ""), 100)
    if label == "useful":
        return f"该规则直接覆盖待审块中的关键对象、行为、参数或强制要求，可用于判断“{focus}”。对应规则内容：{rule}"
    if label == "uncertain":
        return f"该规则与待审块“{focus}”存在主题或条件关联，但对象、适用条件或关键阈值不完全对应，只能辅助复核。对应内容：{rule}"
    return f"该规则主要涉及“{rule}”，未直接覆盖待审块的核心审查事项，不能作为本案有效证据。"


def annotate_case(case: dict[str, Any], reviewed: dict[str, Any]) -> dict[str, Any]:
    ranks = {candidate["rank"] for candidate in case["candidate_annotations"]}
    requested = reviewed["useful"] | reviewed["uncertain"]
    if requested - ranks:
        raise ValueError(f"{case['case_id']} references missing ranks: {sorted(requested - ranks)}")
    if reviewed["useful"] & reviewed["uncertain"]:
        raise ValueError(f"{case['case_id']} has overlapping useful/uncertain ranks")

    for candidate in case["candidate_annotations"]:
        rank = candidate["rank"]
        if rank in reviewed["useful"]:
            label, confidence = "useful", "high"
        elif rank in reviewed["uncertain"]:
            label, confidence = "uncertain", "medium"
        else:
            label, confidence = "useless", "high"
        candidate["label"] = label
        candidate["confidence"] = confidence
        candidate["note"] = candidate_note(label, case["pending"], candidate)
        candidate["annotation_source"] = "codex_manual_local_bgem3_v6"

    case["final_label"] = reviewed["final_label"]
    case["final_reason"] = reviewed["final_reason"]
    case["missing_correct_evidence"] = reviewed["missing"]
    case["not_eval_suitable"] = reviewed["not_suitable"]
    case["review_flags"] = reviewed["flags"]
    case["status"] = "completed"
    case["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return case


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-dir", type=Path, default=DEFAULT_CHECKPOINT_DIR)
    parser.add_argument("--reapply", action="store_true")
    args = parser.parse_args()

    saved = skipped = 0
    for case_id, reviewed in DECISIONS.items():
        path = args.checkpoint_dir / f"{case_id}.json"
        case = load_json(path)
        if case["status"] == "completed" and not args.reapply:
            skipped += 1
            continue
        atomic_write(path, annotate_case(case, reviewed))
        saved += 1
        print(f"saved {case_id}", flush=True)
    print(f"saved={saved} skipped={skipped} decisions={len(DECISIONS)}")


if __name__ == "__main__":
    main()
