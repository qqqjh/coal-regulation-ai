"""Apply Codex's manual Chinese duplicate judgments to the >=0.90 pair report."""

from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "rag_eval" / "data" / "similar_rule_pairs_bgem3_v6_threshold090.json"
OUTPUT_JSON = (
    PROJECT_ROOT
    / "rag_eval"
    / "data"
    / "similar_rule_pairs_bgem3_v6_threshold090_codex_annotated.json"
)
OUTPUT_HTML = (
    PROJECT_ROOT
    / "rag_eval"
    / "reports"
    / "similar_rule_pairs_bgem3_v6_threshold090_codex_annotated.html"
)


def decision(
    relation: str,
    direction: str,
    overlap: str,
    difference: str,
    reason: str,
    same_evidence_group: bool,
) -> dict:
    return {
        "is_true_duplicate_including_containment": relation != "非重复",
        "duplicate_relation": relation,
        "containment_direction": direction,
        "same_evidence_group": same_evidence_group,
        "overlap_zh": overlap,
        "difference_zh": difference,
        "reason_zh": reason,
    }


# Decisions are keyed by stable pair_id, not by display rank.
DECISIONS = {
    "pair_0013": decision(
        "包含关系",
        "左侧总体更详细，双方仍各有少量独有要求",
        "均规定预抽煤层瓦斯区域防突钻孔应覆盖回采区、巷道条带等目标区域，并对控制边界提出要求。",
        "左侧细则补充定向钻进、分段施工、厚煤层穿透、邻近煤层和高压力区域等大量细节；右侧规程另有非定向钻孔长度、有效抽采时间等独有要求。",
        "核心控制范围高度重合，可视为包含型重复；但两块不是全文等价，不能删除任意一块，也不能用其中一块证明另一块全部细节。",
        True,
    ),
    "pair_0028": decision(
        "基本等价",
        "相互等价",
        "均规定揭开、穿过突出煤层时必须采取安全防护措施，并要求人员携带隔离/隔绝式自救器。",
        "主要是“隔离式”与“隔绝式”等措辞差异，未发现会改变审查结论的实质差别。",
        "两条的对象、动作和强制要求一致，可作为同一证据组。",
        True,
    ),
    "pair_0026": decision(
        "包含关系",
        "左侧包含更多实施细节",
        "均列举采煤工作面可采用的局部防突措施。",
        "左侧要求明确排放时间并给出更完整的选择说明；右侧使用“应当选用”等更强规范措辞。",
        "针对“采煤工作面防突措施选择”属于包含型重复；涉及规范强度或排放时间时仍应分别引用。",
        True,
    ),
    "pair_0012": decision(
        "包含关系",
        "左侧包含更多保护层选择细节",
        "均要求具备条件时优先开采保护层，并规定保护层选择原则。",
        "左侧增加最佳保护效果、煤层间距和特定条件；右侧增加保护区域后续分类等要求。",
        "核心保护层选择要求一致，属于包含型重复，但双方独有要求不能互相替代。",
        True,
    ),
    "pair_0003": decision(
        "包含关系",
        "左侧包含更多巷道布置细节",
        "均要求主要巷道尽量布置在岩层或非突出煤层中，减少揭煤并避开不利地质区域。",
        "左侧进一步规定上、下山布置条件和有效卸压区域等内容。",
        "核心巷道布置原则相同，可归入同一证据组；左侧的附加条件应保留。",
        True,
    ),
    "pair_0032": decision(
        "包含关系",
        "左侧包含右侧并增加安全细节",
        "均规定远距离爆破时的撤人、警戒和安全防护要求。",
        "左侧增加初始施工阶段、全员撤离、300 m/100 m 等距离及火源限制；右侧仅保留总体要求。",
        "右侧是左侧核心要求的概括，可作为包含型重复，但数值和附加条件必须使用左侧核验。",
        True,
    ),
    "pair_0021": decision(
        "包含关系",
        "左侧包含右侧",
        "均涉及煤巷掘进工作面防突措施的选择，并包含超前钻孔措施。",
        "右侧只给出概括性选择要求；左侧增加优先顺序、禁用措施、倾角条件和效果检验等限制。",
        "对“煤巷掘进工作面应选择防突措施”属于包含型重复，具体方法是否合法仍需左侧细则。",
        True,
    ),
    "pair_0022": decision(
        "包含关系",
        "交叉包含",
        "均限制水力冲孔、水力挤出等措施，并涉及上山掘进条件。",
        "右侧覆盖更广的采掘场景并增加断层、钻孔间距和远距离爆破要求；左侧增加措施优先顺序、支护和效果检验要求。",
        "两块存在可独立识别的共同禁限要求，属于局部包含型重复；全文不能互换。",
        True,
    ),
    "pair_0030": decision(
        "包含关系",
        "左侧包含右侧",
        "均要求设置反向风门，并规定关闭状态、设置位置应根据通风系统和突出强度确定。",
        "左侧增加至少两道风门、间距、距工作面距离、墙体施工及反向隔断等具体要求。",
        "右侧是左侧的概括性子集，可作为同一证据组；具体数量和距离必须引用左侧。",
        True,
    ),
    "pair_0004": decision(
        "基本等价",
        "相互等价",
        "均要求编制矿井、采区和工作面的区域综合防突规划并做好开拓、抽采、采掘衔接。",
        "只有表述次序和少量措辞差异，未发现实质适用范围或阈值差异。",
        "两条核心义务一致，可视为基本等价重复。",
        True,
    ),
    "pair_0020": decision(
        "包含关系",
        "左侧包含右侧",
        "均规定井巷揭煤工作面的区域防突措施选择。",
        "右侧主要对应左侧首段；左侧另有立井例外、实施顺序、实际考察和最小距离等细节。",
        "核心选择要求属于包含型重复；左侧附加条件不可由右侧替代。",
        True,
    ),
    "pair_0023": decision(
        "包含关系",
        "左侧包含右侧",
        "均规定首次采用局部防突措施且缺少允许掘进距离时，应采用小直径钻孔并保留安全煤柱。",
        "左侧还规定地质构造破坏带和断层附近的处理要求。",
        "右侧对应左侧的一项完整要求，可作为同一证据组；其他地质条件仍需左侧。",
        True,
    ),
    "pair_0008": decision(
        "非重复",
        "无",
        "均提到钻孔应覆盖或控制目标区域。",
        "左侧重点是施工记录、验收、竣工图和补孔；右侧重点是钻孔几何控制范围和区域边界。",
        "共同措辞不足以使两条互相证明。审查施工记录合规与审查控制范围合规会得到不同证据，不能归为重复。",
        False,
    ),
    "pair_0006": decision(
        "非重复",
        "无",
        "均包含禁止部分不规范采掘方式、风镐作业和支护安全等要求。",
        "两侧对上山掘进和爆破的倾角阈值、允许条件存在明显差异，可能直接导致不同审查结论。",
        "虽然主题相似，但关键阈值和条件不一致，不能合并为同一证据组，应人工核对规范版本及适用范围。",
        False,
    ),
    "pair_0010": decision(
        "基本等价",
        "左侧略详细",
        "均规定区域突出危险性预测及未预测区域按突出危险区管理。",
        "左侧更明确预测资料来源和使用方式，右侧为概括表述。",
        "核心判定规则一致，可视为基本等价重复；需要方法细节时优先引用左侧。",
        True,
    ),
    "pair_0014": decision(
        "非重复",
        "无",
        "均涉及区域防突钻孔应覆盖目标煤层或区域。",
        "左侧重点是钻孔间距、抽采半径、穿透煤层、防水和封孔长度；右侧重点是区域控制边界和覆盖范围。",
        "两条控制对象和审查要点不同，只是共享“覆盖控制”主题，不能互相替代。",
        False,
    ),
    "pair_0002": decision(
        "非重复",
        "无",
        "均涉及突出矿井建设规模、能力和开采深度限制。",
        "不同矿井类型、建设/生产阶段对应的深度阈值和能力限制不同，且一侧包含产能上限等独有要求。",
        "数值和适用对象会改变审查结论，不能按重复处理。",
        False,
    ),
    "pair_0007": decision(
        "包含关系",
        "左侧包含右侧",
        "均要求发现突出预兆时立即停止作业、撤人并报告，且赋予现场人员相应处置权限。",
        "左侧增加每班瓦斯检查工和固定爆破工等人员配置要求。",
        "对“突出预兆应急处置”属于包含型重复；人员配置要求只能由左侧支持。",
        True,
    ),
    "pair_0033": decision(
        "包含关系",
        "左侧包含右侧",
        "均要求突出煤层采掘工作面附近设置直通调度室电话，并设置压风自救装置。",
        "左侧增加压风管路安装、间距、供气能力和流量等具体要求。",
        "右侧是左侧首段要求的概括，可作为同一证据组；设备参数必须引用左侧。",
        True,
    ),
    "pair_0029": decision(
        "包含关系",
        "左侧包含右侧",
        "均规定距离较远时设置临时避难硐室以及一定范围内设置避险设施。",
        "左侧增加永久避难硐室、96 小时保障、隔离门和压风自救等要求。",
        "共同的避难设施布置要求属于包含型重复；左侧扩展要求仍需单独保留。",
        True,
    ),
    "pair_0025": decision(
        "非重复",
        "无",
        "均列举局部防突措施，部分措施名称相近。",
        "左侧适用于采煤工作面，右侧适用于煤巷掘进工作面；对象不同，措施选择和执行条件也不同。",
        "适用作业面不同会改变合规判断，不能作为同一证据组。",
        False,
    ),
    "pair_0016": decision(
        "包含关系",
        "交叉包含，仅局部要求重合",
        "均要求揭煤前通过超前钻孔探明煤层赋存和地质情况。",
        "左侧给出钻孔数量、取芯和距离等具体要求；右侧还包含多项与揭煤相关但不属于超前探查的规定。",
        "对“揭煤前超前探查”可视为包含型重复；两块全文不能互换。",
        True,
    ),
    "pair_0034": decision(
        "包含关系",
        "交叉包含，仅压风自救要求重合",
        "均规定突出煤层相关作业地点压风自救装置的布置距离、使用人数、供气能力或流量。",
        "左侧另含电话、压风管路和长距离巷道布置要求；右侧适用范围扩展到冲击地压煤层及其他掘进工作面。",
        "压风自救参数可作为同一证据组，但不同适用范围和左侧独有要求不能互相替代。",
        True,
    ),
    "pair_0027": decision(
        "非重复",
        "无",
        "均涉及采煤工作面防突措施及相关限制。",
        "左侧主要规定措施选择和排放时间；右侧主要规定突出煤层采掘禁限条件、钻孔参数及远距离爆破。",
        "两条回答的是不同规范问题，不能因措施名相近而判为重复。",
        False,
    ),
    "pair_0005": decision(
        "包含关系",
        "左侧包含右侧",
        "均规定新水平首次开拓接近一定厚度煤层时的参数测定和突出危险性管理。",
        "左侧另含非突出、高瓦斯煤层持续参数测定和预兆观察要求。",
        "右侧对应左侧的一项要求，属于包含型重复；左侧其他监测义务需保留。",
        True,
    ),
    "pair_0011": decision(
        "非重复",
        "无",
        "均属于综合防突管理，涉及区域预测、效果检验或措施落实。",
        "左侧重点是区域预测/效果检验后的处置、审批、验证和异常区管理；右侧是综合防突体系的总则性要求。",
        "总则与具体处置流程不能互相替代，不应归入同一证据组。",
        False,
    ),
    "pair_0035": decision(
        "非重复",
        "无",
        "均包含突出煤层或突出矿井的定义性内容。",
        "左侧专门定义岩石与二氧化碳气体突出及参照执行；右侧定义煤/岩突出并规定煤层鉴定触发和评估。",
        "定义对象、触发条件和执行要求不同，不能作为重复规则。",
        False,
    ),
    "pair_0001": decision(
        "包含关系",
        "右侧包含左侧",
        "左侧列出的突出危险性鉴定触发条件及鉴定前管理要求，在右侧规则中有直接对应内容。",
        "右侧还包含突出定义、新建矿井评估等更广内容；当前抽取文本可能遗漏部分数值，不宜用右侧核验具体阈值。",
        "对鉴定触发条件属于包含型重复；涉及具体数值时应回查原文。",
        True,
    ),
    "pair_0015": decision(
        "包含关系",
        "交叉包含",
        "均规定工作面突出危险性预测应划分危险/无危险，并将未预测工作面按危险管理。",
        "一侧更强调预测分类和流程，另一侧增加必须采取措施及效果检验等后续义务。",
        "核心预测判定规则相同，属于包含型重复；后续措施义务仍需单独引用。",
        True,
    ),
    "pair_0009": decision(
        "非重复",
        "无",
        "均涉及区域突出危险性管理。",
        "左侧是区域预测方法、分类和未预测区域管理；右侧是综合防突框架和总体管理要求。",
        "具体预测规则与总则性框架不能互相证明，不应判为重复。",
        False,
    ),
    "pair_0019": decision(
        "包含关系",
        "右侧包含左侧",
        "均规定超前探查确认煤层厚度小于 0.3 m 时，可在采取安全措施后直接采用远距离爆破揭煤。",
        "右侧还包含其他揭煤流程要求，左侧对该特定情形描述更集中。",
        "对“薄煤层可直接远距离爆破揭煤”属于同一证据组，整体规则仍不能互换。",
        True,
    ),
    "pair_0024": decision(
        "非重复",
        "无",
        "均列举局部防突措施。",
        "左侧适用于采煤工作面，措施包括注水、松动爆破等；右侧适用于井巷揭煤工作面，措施包括金属骨架、固化、水力冲孔等。",
        "适用对象和措施集合不同，不能作为同一证据组。",
        False,
    ),
    "pair_0017": decision(
        "非重复",
        "无",
        "均属于揭煤过程控制，并出现若干距离要求。",
        "左侧重点是工作面预测、验证和危险处置；右侧重点是超前探查、局部措施与远距离爆破的流程距离。",
        "距离数字相近但对应动作不同，合并会产生错误证据匹配。",
        False,
    ),
    "pair_0031": decision(
        "非重复",
        "无",
        "均涉及远距离爆破和撤人安全。",
        "左侧是远距离爆破的具体安全程序、撤人距离和火源限制；右侧是揭煤作业的总体流程要求。",
        "只有局部主题重合，不能互相替代，也不宜归入同一证据组。",
        False,
    ),
    "pair_0018": decision(
        "包含关系",
        "右侧包含左侧",
        "均规定揭煤工作面从距煤层一定距离至穿过煤层后的远距离爆破要求，并禁止震动爆破。",
        "右侧还包含超前探查、局部防突措施等其他揭煤要求；左侧补充安全防护和支护说明。",
        "远距离爆破和禁止震动爆破部分属于包含型重复；其余内容不能互换。",
        True,
    ),
}


def esc(value: object) -> str:
    return html.escape(str(value or ""))


def render_chunk(chunk: dict, side: str) -> str:
    return (
        f"<article class='rule'><h3>{side}：{esc(chunk['chunk_id'])}</h3>"
        f"<div class='meta'>文档：{esc(chunk['doc_name'])}　页码：{esc(chunk['page_range'])}　"
        f"稳定ID：{esc(chunk['canonical_rule_id'])}</div>"
        f"<div class='meta'>章：{esc(chunk['chapter'])}　节：{esc(chunk['section'])}　"
        f"条：{esc(chunk['article'])}</div>"
        f"<details open><summary>规则全文</summary><pre>{esc(chunk['content'])}</pre></details>"
        "</article>"
    )


def render_html(data: dict) -> None:
    cards = []
    for rank, pair in enumerate(data["pairs"], start=1):
        ann = pair["codex_annotation"]
        true_text = "真重复（含包含）" if ann["is_true_duplicate_including_containment"] else "非重复"
        true_class = "true" if ann["is_true_duplicate_including_containment"] else "false"
        evidence_text = "可归入同一证据组" if ann["same_evidence_group"] else "不可归入同一证据组"
        cards.append(
            f"<section class='pair {true_class}' data-relation='{esc(ann['duplicate_relation'])}' "
            f"data-true='{str(ann['is_true_duplicate_including_containment']).lower()}'>"
            f"<div class='pair-head'><h2>#{rank} · {esc(pair['pair_id'])} · "
            f"余弦相似度 {pair['cosine_similarity']:.4f}</h2>"
            f"<span class='badge {true_class}'>{true_text}</span>"
            f"<span class='badge relation'>{esc(ann['duplicate_relation'])}</span>"
            f"<span class='badge evidence'>{evidence_text}</span></div>"
            "<div class='judgment'>"
            f"<p><b>包含方向：</b>{esc(ann['containment_direction'])}</p>"
            f"<p><b>共同内容：</b>{esc(ann['overlap_zh'])}</p>"
            f"<p><b>关键差异：</b>{esc(ann['difference_zh'])}</p>"
            f"<p><b>判定理由：</b>{esc(ann['reason_zh'])}</p>"
            "</div>"
            f"<div class='grid'>{render_chunk(pair['left'], '左侧')}{render_chunk(pair['right'], '右侧')}</div>"
            "</section>"
        )

    counts = Counter(pair["codex_annotation"]["duplicate_relation"] for pair in data["pairs"])
    true_count = sum(
        pair["codex_annotation"]["is_true_duplicate_including_containment"] for pair in data["pairs"]
    )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>≥0.90 相似规则 Codex 中文判定</title>
<style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;margin:18px;background:#f3f5f6;color:#20272d;line-height:1.65}}
.summary,.pair{{background:#fff;border:1px solid #cbd3d8;margin-bottom:16px;padding:16px;border-radius:6px}}
.summary{{border-top:5px solid #284b63}} .toolbar{{position:sticky;top:0;z-index:5;background:#f3f5f6;padding:10px 0}}
button{{margin-right:8px;padding:7px 12px;border:1px solid #9aa8b1;background:#fff;cursor:pointer;border-radius:4px}}
.pair.true{{border-left:6px solid #18794e}} .pair.false{{border-left:6px solid #b42318}}
.pair-head{{display:flex;gap:8px;align-items:center;flex-wrap:wrap}} .pair-head h2{{margin:0 10px 0 0;font-size:18px}}
.badge{{padding:3px 8px;border-radius:3px;font-weight:700;font-size:13px}} .badge.true{{background:#d9f4e6;color:#12633f}}
.badge.false{{background:#fde3e0;color:#8f1d14}} .badge.relation{{background:#e4ecf2;color:#284b63}}
.badge.evidence{{background:#fff3cd;color:#694f00}} .judgment{{background:#f7fafb;border:1px solid #d9e0e4;padding:10px 14px;margin:13px 0}}
.judgment p{{margin:5px 0}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
.rule{{border:1px solid #d4dadd;padding:12px;min-width:0}} .rule h3{{font-size:16px;margin:0 0 7px;color:#1c5d84}}
.meta{{color:#65727a;font-size:13px;margin-bottom:4px}} details{{margin-top:8px}} summary{{font-weight:700;cursor:pointer}}
pre{{white-space:pre-wrap;word-break:break-word;font-family:"Microsoft YaHei",Arial,sans-serif;font-size:14px;margin:8px 0 0}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style>
</head>
<body>
<section class="summary">
<h1>≥0.90 相似规则 Codex 中文判定</h1>
<p>共 {len(data['pairs'])} 组：真重复（包含基本等价与包含关系）{true_count} 组，非重复 {len(data['pairs']) - true_count} 组。</p>
<p>分类统计：{esc(dict(counts))}</p>
<p><b>判定口径：</b>“包含关系”表示两块存在可作为同一证据的核心规则，但不代表整块可删除或全文可互换；涉及适用对象、数值阈值、规范强度和独有条件时仍需分别保留。</p>
</section>
<div class="toolbar">
<button onclick="filterPairs('all')">全部</button>
<button onclick="filterPairs('true')">仅真重复（含包含）</button>
<button onclick="filterPairs('false')">仅非重复</button>
<button onclick="filterPairs('基本等价')">仅基本等价</button>
<button onclick="filterPairs('包含关系')">仅包含关系</button>
<span id="visibleCount"></span>
</div>
{''.join(cards)}
<script>
function filterPairs(mode) {{
  const cards = Array.from(document.querySelectorAll('.pair'));
  let shown = 0;
  cards.forEach(card => {{
    const visible = mode === 'all' || card.dataset.true === mode || card.dataset.relation === mode;
    card.style.display = visible ? '' : 'none';
    if (visible) shown++;
  }});
  document.getElementById('visibleCount').textContent = ' 当前显示 ' + shown + ' 组';
}}
filterPairs('all');
</script>
</body>
</html>"""
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_HTML.write_text(page, encoding="utf-8")


def main() -> None:
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    pair_ids = {pair["pair_id"] for pair in data["pairs"]}
    if pair_ids != set(DECISIONS):
        missing = sorted(pair_ids - set(DECISIONS))
        extra = sorted(set(DECISIONS) - pair_ids)
        raise RuntimeError(f"Annotation mismatch. missing={missing}, extra={extra}")

    for rank, pair in enumerate(data["pairs"], start=1):
        pair["review_rank"] = rank
        pair["codex_annotation"] = DECISIONS[pair["pair_id"]]

    counts = Counter(pair["codex_annotation"]["duplicate_relation"] for pair in data["pairs"])
    data["schema"] = "similar_rule_pairs_bgem3_v6_codex_annotated_v1"
    data["annotation_metadata"] = {
        "annotated_at": datetime.now().isoformat(timespec="seconds"),
        "annotator": "Codex",
        "scope": "人工判断余弦相似度大于等于0.90的跨文档规则对",
        "true_duplicate_definition": "基本等价或存在明确包含关系；不代表全文可互换或可直接删除",
        "pair_count": len(data["pairs"]),
        "relation_counts": dict(counts),
        "true_duplicate_including_containment_count": sum(
            pair["codex_annotation"]["is_true_duplicate_including_containment"]
            for pair in data["pairs"]
        ),
        "same_evidence_group_count": sum(
            pair["codex_annotation"]["same_evidence_group"] for pair in data["pairs"]
        ),
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html(data)
    print(json.dumps(data["annotation_metadata"], ensure_ascii=False, indent=2))
    print(f"Written JSON: {OUTPUT_JSON}")
    print(f"Written HTML: {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
