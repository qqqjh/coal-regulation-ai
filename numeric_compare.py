"""数值合规核验模块 v1 —— "LLM 抽取、代码比较"

设计动机：v4~v8 迭代史上最大的一类误判是数值方向判断
（误差/偏差范围方向、报警/断电/复电阈值方向、上下限方向）。
本模块把数值判断拆成两步：
  1. LLM 只负责"配对抽取"：从待审文本和法规文本中找出同一参数的数值约束，
     输出结构化 JSON（参数名、约束类型、数值、单位、原文引用）；
  2. 数值大小、方向、单位换算的比较完全由确定性代码完成。

约束类型语义（以法规条款的语义为准）：
  upper_limit        上限：不得超过/不大于/最多/≤X      → 待审 ≤ X 合规
  lower_limit        下限：不得低于/不小于/至少/≥X      → 待审 ≥ X 合规
  range              区间：A~B                          → 待审落在[A,B]内合规
  tolerance          误差/偏差/公差：±X 或 误差A~B       → |待审偏差| ≤ 上界 合规（不区分正负）
  trigger_threshold  安全触发阈值：报警≥X/断电≥X/停工≥X  → 越小越早触发越严格，待审 ≤ X 合规
  restore_threshold  复电阈值：<X才复电                  → 越小越严格，待审 ≤ X 合规
  exact              定值要求                            → 相等合规，不等不确定（交人工/智能体）
"""
import json
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

# ============ 单位归一化 ============

# 单位别名 -> 标准写法
UNIT_ALIASES = {
    "％": "%", "百分比": "%", "百分之": "%",
    "‰": "‰", "千分之": "‰",
    "ppm": "ppm",
    "毫米": "mm", "厘米": "cm", "米": "m", "千米": "km", "公里": "km",
    "m/s": "m/s", "米/秒": "m/s", "米每秒": "m/s", "km/h": "km/h",
    "m3/min": "m3/min", "m³/min": "m3/min", "立方米/分": "m3/min", "立方米/分钟": "m3/min",
    "m3/s": "m3/s", "m³/s": "m3/s", "立方米/秒": "m3/s",
    "m3/h": "m3/h", "m³/h": "m3/h", "立方米/小时": "m3/h",
    "m3/d": "m3/d", "m³/d": "m3/d", "立方米/天": "m3/d",
    "m3": "m3", "m³": "m3", "立方米": "m3",
    "m2": "m2", "m²": "m2", "平方米": "m2",
    "cm2": "cm2", "cm²": "cm2", "平方厘米": "cm2",
    "mm2": "mm2", "mm²": "mm2", "平方毫米": "mm2",
    "m3/t": "m3/t", "m³/t": "m3/t", "立方米/吨": "m3/t",
    "l/min": "L/min", "L/min": "L/min", "升/分钟": "L/min",
    "m/min": "m/min", "米/分钟": "m/min",
    "pa": "Pa", "帕": "Pa", "kpa": "kPa", "千帕": "kPa", "mpa": "MPa", "兆帕": "MPa",
    "mg/m3": "mg/m3", "mg/m³": "mg/m3", "毫克/立方米": "mg/m3",
    "g/m3": "g/m3", "g/m³": "g/m3", "克/立方米": "g/m3",
    "°c": "C", "℃": "C", "摄氏度": "C", "度": "deg",
    "秒": "s", "分钟": "min", "分": "min", "小时": "h",
    "天": "d", "日": "d", "周": "week", "月": "month", "年": "year",
    "克": "g", "千克": "kg", "公斤": "kg", "吨": "t",
    "牛": "N", "千牛": "kN",
    "伏": "V", "千伏": "kV", "安": "A", "ω": "Ω", "ohm": "Ω", "欧姆": "Ω",
    "lx": "lx", "勒克斯": "lx",
    "hz": "Hz", "赫兹": "Hz", "r/min": "r/min", "rpm": "r/min", "转/分钟": "r/min",
    "w": "W", "瓦": "W", "kw": "kW", "千瓦": "kW", "mw": "MW", "兆瓦": "MW",
    "j": "J", "焦": "J", "kj": "kJ", "千焦": "kJ", "mj": "MJ", "兆焦": "MJ",
    "db": "dB", "db(a)": "dB", "dB(A)": "dB", "分贝": "dB",
    "人": "person", "个": "count", "次": "count", "台": "count", "根": "count",
    "组": "count", "处": "count", "套": "count", "倍": "multiple",
}

# 标准单位 -> (量纲, 换算到基准单位的系数)
UNIT_TABLE = {
    "%": ("ratio", 1.0), "‰": ("ratio", 0.1), "ppm": ("ratio", 1e-4),
    "mm": ("length", 0.001), "cm": ("length", 0.01), "m": ("length", 1.0), "km": ("length", 1000.0),
    "m/s": ("speed", 1.0), "km/h": ("speed", 1.0 / 3.6),
    "m3/min": ("airflow", 1.0), "m3/s": ("airflow", 60.0),
    "m3/h": ("airflow", 1.0 / 60.0), "m3/d": ("airflow", 1.0 / 1440.0),
    "m3": ("volume", 1.0),
    "mm2": ("area", 1e-6), "cm2": ("area", 1e-4), "m2": ("area", 1.0),
    "m3/t": ("gas_content", 1.0), "L/min": ("liquid_flow", 1.0),
    "m/min": ("speed", 1.0 / 60.0),
    "Pa": ("pressure", 1.0), "kPa": ("pressure", 1000.0), "MPa": ("pressure", 1e6),
    "mg/m3": ("density", 1.0), "g/m3": ("density", 1000.0),
    "C": ("temperature", 1.0), "deg": ("angle", 1.0),
    "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0),
    "d": ("time", 86400.0), "week": ("time", 604800.0),
    "month": ("calendar_month", 1.0), "year": ("calendar_year", 1.0),
    "g": ("mass", 0.001), "kg": ("mass", 1.0), "t": ("mass", 1000.0),
    "N": ("force", 1.0), "kN": ("force", 1000.0),
    "V": ("voltage", 1.0), "kV": ("voltage", 1000.0), "A": ("current", 1.0),
    "Ω": ("resistance", 1.0),
    "lx": ("illuminance", 1.0),
    "Hz": ("frequency", 1.0), "r/min": ("rotation_speed", 1.0),
    "W": ("power", 1.0), "kW": ("power", 1000.0), "MW": ("power", 1e6),
    "J": ("energy", 1.0), "kJ": ("energy", 1000.0), "MJ": ("energy", 1e6),
    "dB": ("sound_level", 1.0),
    "person": ("count", 1.0), "count": ("count", 1.0),
    "multiple": ("ratio_multiple", 1.0),
    "": ("dimensionless", 1.0),  # 无单位（个数、倍数等）
}

VALID_TYPES = {
    "upper_limit", "lower_limit", "range", "tolerance",
    "trigger_threshold", "restore_threshold", "exact",
}


def normalize_unit(unit: Optional[str]) -> Optional[Tuple[str, float]]:
    """返回 (量纲, 换算系数)；无法识别返回 None。"""
    if unit is None:
        unit = ""
    u = str(unit).strip().replace(" ", "")
    u = UNIT_ALIASES.get(u, UNIT_ALIASES.get(u.lower(), u))
    if u in UNIT_TABLE:
        return UNIT_TABLE[u]
    if u.lower() in UNIT_TABLE:
        return UNIT_TABLE[u.lower()]
    return None


def _to_base(value: float, factor: float) -> float:
    return float(value) * factor


def _fmt(value: Optional[float], unit: str) -> str:
    if value is None:
        return "?"
    text = f"{value:g}"
    return f"{text}{unit}" if unit else text


# ============ 确定性比较 ============

EPS = 1e-9


def compare_pair(pair: Dict[str, Any]) -> Dict[str, Any]:
    """对一条 LLM 配对结果做确定性比较。

    pair 结构：
      {"parameter": str, "constraint_type": str,
       "pending": {"value": float, "value_high": float|None, "unit": str, "quote": str},
       "rule":    {"value": float, "value_high": float|None, "unit": str, "quote": str}}
    返回：
      {"parameter", "constraint_type", "verdict": 合规/不合规/不确定,
       "relation", "explanation", "pending_quote", "rule_quote"}
    """
    param = str(pair.get("parameter", "")).strip() or "未命名参数"
    ctype = str(pair.get("constraint_type", "")).strip()
    pending = pair.get("pending") or {}
    rule = pair.get("rule") or {}

    rule_quote = str(rule.get("quote", ""))[:120]
    table_header = str(rule.get("table_header", "")).strip()
    if table_header:
        rule_quote = f"{table_header}\n{rule_quote}"
    base = {
        "parameter": param,
        "constraint_type": ctype,
        "pending_quote": str(pending.get("quote", ""))[:120],
        "rule_quote": rule_quote,
    }

    def out(verdict: str, relation: str, explanation: str) -> Dict[str, Any]:
        return {**base, "verdict": verdict, "relation": relation, "explanation": explanation}

    if ctype not in VALID_TYPES:
        return out("不确定", "unknown_type", f"约束类型无法识别: {ctype}")

    try:
        p_val = pending.get("value")
        p_high = pending.get("value_high")
        r_val = rule.get("value")
        r_high = rule.get("value_high")
        p_val = None if p_val is None else float(p_val)
        p_high = None if p_high is None else float(p_high)
        r_val = None if r_val is None else float(r_val)
        r_high = None if r_high is None else float(r_high)
    except (TypeError, ValueError):
        return out("不确定", "bad_value", "数值字段无法解析为数字")

    if p_val is None or r_val is None:
        return out("不确定", "missing_value", "待审或法规数值缺失")

    p_unit_info = normalize_unit(pending.get("unit"))
    r_unit_info = normalize_unit(rule.get("unit"))
    p_unit_str = str(pending.get("unit") or "")
    r_unit_str = str(rule.get("unit") or "")
    if p_unit_info is None or r_unit_info is None:
        return out("不确定", "unknown_unit",
                   f"单位无法识别（待审:{p_unit_str or '无'} / 法规:{r_unit_str or '无'}），不做数值结论")
    if p_unit_info[0] != r_unit_info[0]:
        return out("不确定", "unit_mismatch",
                   f"量纲不一致（待审:{p_unit_str or '无'} / 法规:{r_unit_str or '无'}），不可直接比较")

    # 统一换算到基准单位
    pv = _to_base(p_val, p_unit_info[1])
    ph = _to_base(p_high, p_unit_info[1]) if p_high is not None else None
    rv = _to_base(r_val, r_unit_info[1])
    rh = _to_base(r_high, r_unit_info[1]) if r_high is not None else None

    p_desc = _fmt(p_val, p_unit_str) + (f"~{_fmt(p_high, p_unit_str)}" if p_high is not None else "")
    r_desc = _fmt(r_val, r_unit_str) + (f"~{_fmt(r_high, r_unit_str)}" if r_high is not None else "")

    if ctype == "upper_limit":
        worst = ph if ph is not None else pv
        if worst <= rv + EPS:
            return out("合规", "within_upper",
                       f"{param}：待审 {p_desc} ≤ 法规上限 {r_desc}，合规（更严或相等）")
        return out("不合规", "exceeds_upper",
                   f"{param}：待审 {p_desc} 超过法规上限 {r_desc}")

    if ctype == "lower_limit":
        worst = pv  # 区间时下端最不利
        if worst >= rv - EPS:
            return out("合规", "within_lower",
                       f"{param}：待审 {p_desc} ≥ 法规下限 {r_desc}，合规（更严或相等）")
        return out("不合规", "below_lower",
                   f"{param}：待审 {p_desc} 低于法规下限 {r_desc}")

    if ctype == "range":
        if rh is None:
            return out("不确定", "missing_range_high", f"{param}：法规区间缺少上界")
        lo, hi = (pv, ph) if ph is not None else (pv, pv)
        if lo >= rv - EPS and hi <= rh + EPS:
            return out("合规", "within_range",
                       f"{param}：待审 {p_desc} 落在法规区间 {r_desc} 内")
        return out("不合规", "out_of_range",
                   f"{param}：待审 {p_desc} 超出法规区间 {r_desc}")

    if ctype == "tolerance":
        bound = rh if rh is not None else rv  # 法规"误差A~B"取B；"±X"取X
        p_abs = max(abs(pv), abs(ph)) if ph is not None else abs(pv)
        if p_abs <= bound + EPS:
            return out("合规", "within_tolerance",
                       f"{param}：待审偏差绝对值 {p_desc} ≤ 法规允许上界 {_fmt(r_high if r_high is not None else r_val, r_unit_str)}，"
                       "偏差范围不区分正负，合规")
        return out("不合规", "exceeds_tolerance",
                   f"{param}：待审偏差 {p_desc} 超出法规允许误差上界 {r_desc}")

    if ctype in ("trigger_threshold", "restore_threshold"):
        kind = "触发阈值（报警/断电/停工）" if ctype == "trigger_threshold" else "复电阈值"
        if pv <= rv + EPS:
            return out("合规", "stricter_trigger",
                       f"{param}：待审 {p_desc} ≤ 法规 {r_desc}，{kind}越小越早触发/越严格，"
                       "严于法规即合规，不得以'低于法规值'判违规")
        return out("不合规", "later_trigger",
                   f"{param}：待审 {p_desc} > 法规 {r_desc}，触发更晚/更宽松，违规")

    if ctype == "exact":
        if abs(pv - rv) <= EPS:
            return out("合规", "exact_match", f"{param}：待审 {p_desc} 与法规定值 {r_desc} 一致")
        return out("不确定", "exact_differ",
                   f"{param}：待审 {p_desc} 与法规定值 {r_desc} 不一致，定值类约束需结合上下文人工确认")

    return out("不确定", "unhandled", "未覆盖的约束类型")


# ============ LLM 配对抽取 ============

EXTRACTION_SYSTEM = (
    "你是煤矿安全法规数值约束抽取专家。你只负责抽取和配对数值，"
    "绝对不做合规判断（合规判断由程序完成）。严格按JSON格式输出。"
)

EXTRACTION_PROMPT = """从下面的【待审内容】和【法规内容】中，找出针对**同一参数、同一场景**的数值约束并配对。

【待审内容】
{pending_text}

【法规内容】
{rule_text}

约束类型定义（按**法规条款**的语义选择）：
- upper_limit: 上限（不得超过/不大于/最多/≤X）
- lower_limit: 下限（不得低于/不小于/至少/≥X）
- range: 区间（A~B，value=A，value_high=B）
- tolerance: 误差/偏差/公差（法规"误差A~B"：value=A，value_high=B；"±X"：value=X；待审"±X"：value=X）
- trigger_threshold: 安全触发阈值，到达即报警/断电/停工/撤人（如"报警浓度≥1.0%""断电浓度≥1.5%""≥X时停止作业"）
- restore_threshold: 复电阈值（"<X才可复电"）
- exact: 定值要求（必须等于X）

配对规则：
1. 只配对参数语义相同且适用场景相同的数值（如同为"掘进工作面回风流甲烷断电浓度"）
2. 场景不同（如采煤工作面 vs 掘进工作面、不同传感器位置）不得配对
3. 控制阶段必须相同："达到X就报警/断电/停工/撤人"是触发阶段；
   "低于X方可复电/恢复/送电/开机/开启/进入"是恢复准入阶段，二者不得互相配对
4. 必须穷举待审内容中的每一项独立数值约束。同一句有多个阈值或动作时逐项输出，
   不得只选择其中一个看似相等的阈值而忽略其余阈值
5. 待审或法规中找不到对应数值的参数，不要输出；严禁根据常识补写法规中缺失的数值
6. 数值只填阿拉伯数字，单位单独填（如"1.5"和"%"，不要填"1.5%"）
7. pending.quote 和 rule.quote 必须分别逐字来自待审原文和法规原文，且各自必须包含所填数值；
   法规原文数值为空时不得生成配对
8. 表格一行含报警、断电、复电多个数值时，必须结合表头选择与待审动作一致的唯一列：
   恢复/复电/送电/开机/开启只允许配"复电浓度"列；报警配报警列；断电/停工配断电列。
   rule.quote 应同时包含表头字段和对应行，禁止因为数值恰好相同而跨列配对
9. 每个待审数值约束最多输出一个最具体配对，不得用同一待审句与多个近义法规行做笛卡尔积重复配对

输出JSON（没有可配对数值时 pairs 为空数组）：
{{
  "pairs": [
    {{
      "parameter": "参数名（含场景，如：掘进工作面甲烷断电浓度）",
      "constraint_type": "上述7类之一",
      "pending": {{"value": 数字, "value_high": 数字或null, "unit": "单位", "quote": "待审原文片段"}},
      "rule": {{"value": 数字, "value_high": 数字或null, "unit": "单位", "quote": "法规原文片段"}}
    }}
  ]
}}"""


def _parse_json_loose(text: str) -> Optional[Dict]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None


def extract_pairs(llm_client, model: str, pending_text: str, rule_text: str,
                  temperature: float = 0.0, pending_max_chars: int = 1500,
                  rule_max_chars: int = 1500) -> List[Dict[str, Any]]:
    """调用 LLM 抽取配对数值约束。失败返回空列表（降级为纯 LLM 审查）。"""
    prompt = EXTRACTION_PROMPT.format(
        pending_text=pending_text[:pending_max_chars],
        rule_text=rule_text[:rule_max_chars],
    )
    response = llm_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
    )
    parsed = _parse_json_loose(response.choices[0].message.content or "")
    if not parsed:
        return []
    pairs = parsed.get("pairs", [])
    return pairs if isinstance(pairs, list) else []


_NUMBER_IN_QUOTE = re.compile(r"-?\d+(?:\.\d+)?")
_RESTORE_PHASE = re.compile(
    r"(?:方可|才可|允许).{0,16}(?:复电|恢复|送电|开机|开启|启动|进入|作业)"
    r"|(?:复电|恢复|送电|开机|开启|启动|进入).{0,16}(?:方可|才可|允许)"
    r"|复电浓度"
)
_TRIGGER_PHASE = re.compile(
    r"(?:达到|超过|超限|≥|>=|大于).{0,24}(?:报警|断电|停工|停止|撤出|切断)"
    r"|(?:报警|断电|停工|停止|撤出|切断).{0,16}(?:浓度|阈值)"
)
_RELATIVE_PERCENT = re.compile(
    r"(?:上调|下调|提高|降低|增加|减少|浮动|偏差)\s*\d+(?:\.\d+)?\s*%"
)


def _compact_source_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", "", text)


def _quote_is_grounded(source_text: str, quote: Any) -> bool:
    source = _compact_source_text(source_text)
    candidate = _compact_source_text(quote)
    return bool(candidate) and candidate in source


def _quote_contains_value(quote: Any, value: Any) -> bool:
    try:
        expected = float(value)
    except (TypeError, ValueError):
        return False
    for raw in _NUMBER_IN_QUOTE.findall(unicodedata.normalize("NFKC", str(quote or ""))):
        try:
            if abs(float(raw) - expected) <= EPS:
                return True
        except ValueError:
            continue
    return False


def _values_with_unit(quote: Any, unit: Any) -> List[float]:
    normalized = unicodedata.normalize("NFKC", str(quote or ""))
    normalized_unit = str(unit or "").strip()
    if normalized_unit in ("%", "％"):
        unit_pattern = r"%"
    elif normalized_unit:
        unit_pattern = re.escape(normalized_unit)
    else:
        return []
    pattern = re.compile(rf"(-?\d+(?:\.\d+)?)\s*{unit_pattern}", re.IGNORECASE)
    values: List[float] = []
    for raw in pattern.findall(normalized):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return list(dict.fromkeys(values))


def _repair_unique_value_from_grounded_quote(side: Dict[str, Any]) -> bool:
    """quote 已落地且仅含一个同单位数值时，以原文值纠正 LLM 填错的 value。"""
    values = _values_with_unit(side.get("quote"), side.get("unit"))
    if len(values) != 1 or side.get("value_high") is not None:
        return False
    side["value"] = values[0]
    return True


def _recover_unique_pending_quote(source_text: str, side: Dict[str, Any]) -> str:
    """LLM 改写 quote 时，仅用唯一数值候选回填待审原文，不猜测法规原文。"""
    value = side.get("value")
    if value is None:
        return ""
    candidates: List[str] = []
    for raw in re.split(r"[\r\n]+|(?<=[。；;])", str(source_text or "")):
        candidate = re.sub(r"^【待审数值句\d+】", "", raw).strip()
        if not candidate or not _quote_contains_value(candidate, value):
            continue
        if side.get("value_high") is not None and not _quote_contains_value(
            candidate, side.get("value_high")
        ):
            continue
        candidates.append(candidate)
    unique = list(dict.fromkeys(_compact_source_text(item) for item in candidates))
    if len(unique) != 1:
        return ""
    compact = unique[0]
    return next(
        (item for item in candidates if _compact_source_text(item) == compact), ""
    )


def _constraint_phase(text: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    if _RESTORE_PHASE.search(normalized):
        return "restore"
    if _TRIGGER_PHASE.search(normalized):
        return "trigger"
    return ""


def _repair_restore_pair_from_table(
    pair: Dict[str, Any], rule_text: str
) -> bool:
    """按运行时表头定位复电列，修正模型在同一行中选错报警/断电列。"""
    rule = pair.get("rule") or {}
    rule_quote = str(rule.get("quote", "")).strip()
    if "|" not in rule_quote:
        return False
    lines = [line.strip() for line in str(rule_text or "").splitlines() if line.strip()]
    row_index = next(
        (
            index
            for index, line in enumerate(lines)
            if _compact_source_text(rule_quote) in _compact_source_text(line)
            or _compact_source_text(line) in _compact_source_text(rule_quote)
        ),
        None,
    )
    if row_index is None:
        return False
    header = ""
    for line in reversed(lines[:row_index]):
        if "|" in line and "复电浓度" in line:
            header = line
            break
    if not header:
        return False
    header_cells = [cell.strip() for cell in header.split("|")]
    row_cells = [cell.strip() for cell in lines[row_index].split("|")]
    restore_index = next(
        (index for index, cell in enumerate(header_cells) if "复电浓度" in cell),
        None,
    )
    if restore_index is None or restore_index >= len(row_cells):
        return False
    value_match = _NUMBER_IN_QUOTE.search(row_cells[restore_index])
    if not value_match:
        return False
    try:
        restore_value = float(value_match.group())
    except ValueError:
        return False
    pair["constraint_type"] = "restore_threshold"
    rule["value"] = restore_value
    rule["value_high"] = None
    rule["unit"] = "%" if "%" in header else rule.get("unit", "")
    # 表头与目标行均分别来自法规原文；中间可能夹有其他数据行，不能要求二者相邻。
    rule["table_header"] = header
    return True


def validate_extracted_pair(
    pair: Dict[str, Any], pending_text: str, rule_text: str
) -> Tuple[bool, str]:
    """阻断无原文依据或控制阶段错配的 LLM 数值配对。"""
    pending = pair.get("pending") or {}
    rule = pair.get("rule") or {}
    pending_quote = pending.get("quote", "")
    rule_quote = rule.get("quote", "")
    if not _quote_is_grounded(pending_text, pending_quote):
        recovered = _recover_unique_pending_quote(pending_text, pending)
        if not recovered:
            return False, "pending_quote_not_grounded"
        pending["quote"] = recovered
        pending_quote = recovered
    if not _quote_is_grounded(rule_text, rule_quote):
        return False, "rule_quote_not_grounded"
    if bool(_RELATIVE_PERCENT.search(pending_quote)) != bool(
        _RELATIVE_PERCENT.search(rule_quote)
    ):
        return False, "relative_percentage_mismatch"

    pending_phase = _constraint_phase(pending_quote)
    if pending_phase == "restore":
        _repair_restore_pair_from_table(pair, rule_text)
        rule_quote = rule.get("quote", "")

    for side_name, side in (("pending", pending), ("rule", rule)):
        if not _quote_contains_value(side.get("quote"), side.get("value")):
            if not _repair_unique_value_from_grounded_quote(side):
                return False, f"{side_name}_value_not_in_quote"
        if side.get("value_high") is not None and not _quote_contains_value(
            side.get("quote"), side.get("value_high")
        ):
            return False, f"{side_name}_high_value_not_in_quote"

    ctype = str(pair.get("constraint_type", "")).strip()
    rule_phase = _constraint_phase(rule_quote)
    if pending_phase and rule_phase and pending_phase != rule_phase:
        return False, "control_phase_mismatch"
    if ctype == "trigger_threshold" and "restore" in (pending_phase, rule_phase):
        return False, "type_phase_mismatch"
    if ctype == "restore_threshold" and "trigger" in (pending_phase, rule_phase):
        return False, "type_phase_mismatch"
    return True, ""


def numeric_check(llm_client, model: str, pending_text: str, rule_text: str,
                  pending_max_chars: int = 1500,
                  rule_max_chars: int = 1500) -> Dict[str, Any]:
    """完整数值核验：LLM 配对抽取 + 代码逐对比较。

    返回：
      {"has_pairs": bool,
       "overall": "合规"|"不合规"|"不确定"|"无可比数值",
       "details": [compare_pair 结果...],
       "summary": str}
    """
    try:
        raw_pairs = extract_pairs(
            llm_client,
            model,
            pending_text,
            rule_text,
            pending_max_chars=pending_max_chars,
            rule_max_chars=rule_max_chars,
        )
    except Exception as exc:  # 抽取失败不阻塞主流程
        return {"has_pairs": False, "overall": "无可比数值", "details": [],
                "summary": f"数值抽取调用失败: {str(exc)[:80]}"}

    pairs: List[Dict[str, Any]] = []
    rejected_pairs: List[Dict[str, str]] = []
    for pair in raw_pairs:
        if not isinstance(pair, dict):
            rejected_pairs.append({"reason": "pair_not_object"})
            continue
        valid, reason = validate_extracted_pair(pair, pending_text, rule_text)
        if valid:
            pairs.append(pair)
        else:
            rejected_pairs.append(
                {
                    "reason": reason,
                    "parameter": str(pair.get("parameter", ""))[:80],
                    "pending_quote": str(
                        (pair.get("pending") or {}).get("quote", "")
                    )[:160],
                    "rule_quote": str(
                        (pair.get("rule") or {}).get("quote", "")
                    )[:160],
                }
            )

    if not pairs:
        return {"has_pairs": False, "overall": "无可比数值", "details": [],
                "summary": "未抽取到有双侧原文依据的可配对数值约束",
                "raw_pair_count": len(raw_pairs),
                "rejected_pair_count": len(rejected_pairs),
                "rejected_pairs": rejected_pairs}

    details = [compare_pair(p) for p in pairs if isinstance(p, dict)]
    for detail in details:
        detail["evidence_grounded"] = True
    verdicts = [d["verdict"] for d in details]
    if "不合规" in verdicts:
        overall = "不合规"
    elif verdicts and all(v == "合规" for v in verdicts):
        overall = "合规"
    else:
        overall = "不确定"

    n_bad = verdicts.count("不合规")
    n_ok = verdicts.count("合规")
    n_unsure = verdicts.count("不确定")
    summary = f"数值核验 {len(details)} 对：合规 {n_ok}，不合规 {n_bad}，不确定 {n_unsure}"
    return {"has_pairs": True, "overall": overall, "details": details,
            "summary": summary, "raw_pair_count": len(raw_pairs),
            "rejected_pair_count": len(rejected_pairs),
            "rejected_pairs": rejected_pairs}


def format_for_prompt(result: Dict[str, Any]) -> str:
    """把数值核验结果格式化为可注入验证智能体提示词的文本。"""
    if not result.get("has_pairs"):
        return ""
    lines = ["【数值核验工具结论】（由确定性程序计算，单位已归一化，方向语义已编码，可信度高于人工心算）"]
    for d in result.get("details", []):
        lines.append(f"- [{d['verdict']}] {d['explanation']}")
    lines.append(f"综合：{result.get('overall')}（{result.get('summary')}）")
    return "\n".join(lines)


if __name__ == "__main__":
    # 纯代码部分自检（不调用 LLM）
    cases = [
        # 触发阈值：待审更小 = 更早触发 = 合规
        {"parameter": "掘进工作面甲烷断电浓度", "constraint_type": "trigger_threshold",
         "pending": {"value": 1.2, "value_high": None, "unit": "%", "quote": "断电≥1.2%"},
         "rule": {"value": 1.5, "value_high": None, "unit": "%", "quote": "断电≥1.5%"}},
        # 触发阈值：待审更大 = 违规
        {"parameter": "报警浓度", "constraint_type": "trigger_threshold",
         "pending": {"value": 1.2, "value_high": None, "unit": "%", "quote": "报警≥1.2%"},
         "rule": {"value": 1.0, "value_high": None, "unit": "%", "quote": "报警≥1.0%"}},
        # 误差范围：±0.3 vs 法规误差0.1~0.5 → 合规
        {"parameter": "支柱初撑力误差", "constraint_type": "tolerance",
         "pending": {"value": 0.3, "value_high": None, "unit": "MPa", "quote": "±0.3MPa"},
         "rule": {"value": 0.1, "value_high": 0.5, "unit": "MPa", "quote": "误差0.1~0.5MPa"}},
        # 下限+单位换算：待审500mm vs 法规不低于0.6m → 违规
        {"parameter": "人行道宽度", "constraint_type": "lower_limit",
         "pending": {"value": 500, "value_high": None, "unit": "mm", "quote": "500mm"},
         "rule": {"value": 0.6, "value_high": None, "unit": "m", "quote": "不得低于0.6m"}},
        # 量纲不一致 → 不确定
        {"parameter": "风速", "constraint_type": "upper_limit",
         "pending": {"value": 4, "value_high": None, "unit": "m/s", "quote": "4m/s"},
         "rule": {"value": 8, "value_high": None, "unit": "%", "quote": "8%"}},
    ]
    expected = ["合规", "不合规", "合规", "不合规", "不确定"]
    for case, want in zip(cases, expected):
        got = compare_pair(case)
        flag = "OK " if got["verdict"] == want else "FAIL"
        print(f"[{flag}] want={want} got={got['verdict']} | {got['explanation']}")
