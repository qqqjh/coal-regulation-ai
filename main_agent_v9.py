"""煤矿审查系统主智能体 v9

职责边界（设计原则：主智能体管路由、管例外、管决策，不管高频常规操作）：
  1. 任务理解与文档路由：根据用户指令判断上传文档是规则文档还是待审文档，
     调度对应的切分/入库/审查流程（通过 skills 注册的脚本，CLI 方式调用）。
  2. 升级队列 loop 处理：审查引擎并行链产生的分歧/数值矛盾/低置信/异常项，
     逐个用**独立上下文**处理（防止上下文污染），每项限定最大工具调用步数，
     处理失败自动重试，3 次失败转 dead-letter 待人工。
  3. 裁决沉淀：每个队列项的最终裁决写入 annotations 表（数据飞轮）。

用法：
  python main_agent_v9.py "把 new_docs 里的待审文档切分并审查"   # 自然语言任务
  python main_agent_v9.py --process-queue                        # 处理升级队列
  python main_agent_v9.py --process-queue --max-items 5
  python main_agent_v9.py --queue-stats                          # 查看队列状态
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

import numeric_compare
from review_queue import ReviewQueue

PROJECT_ROOT = Path(__file__).resolve().parent
SKILLS_DIR = PROJECT_ROOT / "skills"
QUEUE_DB = PROJECT_ROOT / "data" / "review_queue_v9.db"
OUTPUT_DIR = PROJECT_ROOT / "review_results"

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AGENT_MODEL = "qwen-plus"

MAX_AGENT_STEPS = 12          # 单任务最大工具调用轮数
MAX_QUEUE_ITEM_STEPS = 6      # 单个队列项最大工具调用轮数
SCRIPT_TIMEOUT = 3600         # 子脚本超时（秒）

# 除 skills 注册的脚本外，允许直接调用的脚本白名单
BUILTIN_ALLOWED_SCRIPTS = {
    "chapter_based_chunking_v6.py",
    "pending_doc_chunking_v9.py",
    "hybrid_rag_review_v9.py",
}


# ============ 工具实现 ============

class AgentTools:
    def __init__(self, queue: ReviewQueue):
        self.queue = queue
        self._engine = None  # 懒加载：检索工具需要 BGE 模型 + KB 索引

    # ---- skills ----

    @staticmethod
    def _parse_skill_md(path: Path) -> Dict[str, str]:
        text = path.read_text(encoding="utf-8")
        meta = {"name": path.parent.name, "description": "", "script": ""}
        for key in ("name", "description", "script"):
            match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
            if match:
                meta[key] = match.group(1).strip()
        return meta

    def list_skills(self) -> Dict:
        skills = []
        if SKILLS_DIR.is_dir():
            for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
                skills.append(self._parse_skill_md(skill_md))
        return {"skills": skills, "skills_dir": str(SKILLS_DIR)}

    def read_skill(self, skill_name: str) -> Dict:
        skill_md = SKILLS_DIR / skill_name / "SKILL.md"
        if not skill_md.is_file():
            return {"error": f"skill 不存在: {skill_name}",
                    "available": [s["name"] for s in self.list_skills()["skills"]]}
        return {"name": skill_name, "content": skill_md.read_text(encoding="utf-8")}

    def _allowed_scripts(self) -> set:
        allowed = set(BUILTIN_ALLOWED_SCRIPTS)
        for skill in self.list_skills()["skills"]:
            if skill.get("script"):
                allowed.add(skill["script"])
        return allowed

    def run_script(self, script: str, args: Optional[List[str]] = None) -> Dict:
        """在白名单内以当前解释器运行项目脚本（CLI 工具调用能力）。"""
        script_name = Path(script).name
        if script_name not in self._allowed_scripts():
            return {"error": f"脚本不在白名单内: {script_name}",
                    "allowed": sorted(self._allowed_scripts())}
        script_path = PROJECT_ROOT / script_name
        if not script_path.is_file():
            return {"error": f"脚本文件不存在: {script_path}"}
        cmd = [sys.executable, str(script_path)] + [str(a) for a in (args or [])]
        print(f"  [run_script] {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd, cwd=str(PROJECT_ROOT), capture_output=True,
                timeout=SCRIPT_TIMEOUT, encoding="utf-8", errors="replace",
            )
            tail = lambda s: (s or "")[-4000:]
            return {"returncode": proc.returncode,
                    "stdout_tail": tail(proc.stdout), "stderr_tail": tail(proc.stderr)}
        except subprocess.TimeoutExpired:
            return {"error": f"脚本超时（>{SCRIPT_TIMEOUT}s）"}

    # ---- 文档路由 ----

    def list_input_files(self) -> Dict:
        """列出输入目录中的文档，供路由决策。"""
        listing = {}
        for label, rel in (("rule_docs_json", "new_docs/rule_docs_json"),
                           ("pending_docs_json", "new_docs/test_doc_json"),
                           ("new_docs_root", "new_docs")):
            directory = PROJECT_ROOT / rel
            if directory.is_dir():
                listing[label] = sorted(
                    p.name for p in directory.iterdir() if p.is_file()
                )[:50]
        return listing

    def inspect_document(self, file_path: str, max_chars: int = 1500) -> Dict:
        """读取文档开头内容，辅助判断规则文档/待审文档。"""
        path = Path(file_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if not path.is_file():
            return {"error": f"文件不存在: {path}"}
        suffix = path.suffix.lower()
        if suffix in (".json", ".txt", ".md"):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except Exception as exc:
                return {"error": str(exc)}
            return {"file": str(path), "head": text}
        return {"file": str(path), "head": "",
                "note": f"二进制格式({suffix})，请依据文件名和用户提示判断；"
                        "PDF/Word 需先经 MinerU 等工具转为 JSON 再切分"}

    # ---- 审查引擎（懒加载，仅队列处理需要检索时初始化）----

    def _ensure_engine(self):
        if self._engine is None:
            from hybrid_rag_review_v9 import HybridRAGReviewerV9
            print("  [engine] 初始化检索引擎（BGE模型+KB索引，首次较慢）...")
            engine = HybridRAGReviewerV9()
            engine.load_knowledge_base()
            engine.build_index()
            self._engine = engine
        return self._engine

    def retrieve_regulations(self, query: str, top_k: int = 5) -> Dict:
        """用 BGE 混合检索+重排在规则库中检索（主智能体复核时换角度重查）。"""
        engine = self._ensure_engine()
        q_dense, q_lex = engine.retriever.encode([query])
        results = engine.search_one(query, q_dense[0], q_lex[0], top_k=top_k)
        return {"query": query, "results": [
            {"doc": r["chunk"]["doc_name"],
             "chapter": r["chunk"].get("chapter", ""),
             "section": r["chunk"].get("section", ""),
             "score": r["score"],
             "content": r["chunk"]["content"][:1000]}
            for r in results
        ]}

    def numeric_compare_tool(self, pending_text: str, rule_text: str) -> Dict:
        """确定性数值核验工具（单位归一化+方向语义编码）。"""
        client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        return numeric_compare.numeric_check(client, AGENT_MODEL, pending_text, rule_text)

    # ---- 队列与结果 ----

    def queue_stats(self) -> Dict:
        return self.queue.stats()

    def get_chunk_review(self, doc_name: str, chunk_index: int) -> Dict:
        """从最新增量结果中读取某chunk的完整审查记录。"""
        candidates = sorted(OUTPUT_DIR.glob("review_result_v9_incremental*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            try:
                with path.open(encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception:
                continue
            for name, doc_results in data.items():
                if doc_name in name or name in doc_name:
                    for r in doc_results:
                        if r.get("chunk_index") == chunk_index:
                            return {"source_file": path.name, "result": r}
        return {"error": f"未找到 {doc_name} chunk#{chunk_index} 的审查记录"}

    def resolve_queue_item(self, item_id: int, verdict: str, reason: str,
                           needs_human: bool = False) -> Dict:
        """裁决一个队列项。verdict: 合规/不合规/不确定。needs_human=true 表示转人工。"""
        item = self.queue.get_item(item_id)
        if item is None:
            return {"error": f"队列项不存在: {item_id}"}
        resolution = {"verdict": verdict, "reason": reason,
                      "needs_human": bool(needs_human)}
        self.queue.resolve(item_id, resolution, resolved_by="agent")
        payload = item.get("payload", {})
        self.queue.add_annotation(
            doc_name=item["doc_name"],
            chunk_index=item["chunk_index"],
            pending_content=payload.get("chunk_content", ""),
            final_verdict=verdict if not needs_human else f"{verdict}(待人工确认)",
            source="agent",
            kb_refs=payload.get("kb_refs", []),
            model_verdict=str(payload.get("review_result", {}).get("compliance_status", "")),
            reason=reason,
            escalation_id=item_id,
            extra={"item_type": item["item_type"]},
        )
        return {"ok": True, "item_id": item_id, "resolution": resolution}


# ============ 工具 schema（OpenAI function calling 格式） ============

def _tool(name: str, description: str, properties: Dict, required: List[str]) -> Dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required},
    }}


TASK_TOOLS = [
    _tool("list_skills", "列出 skills 目录中注册的全部技能（流程/脚本说明）", {}, []),
    _tool("read_skill", "读取某个技能的完整 SKILL.md 内容（含脚本用法、输入输出路径）",
          {"skill_name": {"type": "string"}}, ["skill_name"]),
    _tool("list_input_files", "列出 new_docs 输入目录中的文档文件", {}, []),
    _tool("inspect_document", "读取文档开头内容，辅助判断它是规则文档还是待审文档",
          {"file_path": {"type": "string"}}, ["file_path"]),
    _tool("run_script", "运行白名单内的项目脚本（切分/审查等）。args 为命令行参数列表",
          {"script": {"type": "string"},
           "args": {"type": "array", "items": {"type": "string"}}}, ["script"]),
    _tool("queue_stats", "查看升级队列状态统计", {}, []),
]

QUEUE_TOOLS = [
    _tool("retrieve_regulations", "在规则库中检索法规条款（BGE混合检索+重排），可换角度改写查询词复查",
          {"query": {"type": "string"}, "top_k": {"type": "integer"}}, ["query"]),
    _tool("numeric_compare_tool", "确定性数值核验：抽取两段文本中同参数的数值约束并按方向语义比较（单位自动归一化）",
          {"pending_text": {"type": "string"}, "rule_text": {"type": "string"}},
          ["pending_text", "rule_text"]),
    _tool("get_chunk_review", "读取某chunk的完整审查记录（初审/核验/数值工具/错别字结果）",
          {"doc_name": {"type": "string"}, "chunk_index": {"type": "integer"}},
          ["doc_name", "chunk_index"]),
    _tool("resolve_queue_item", "对当前队列项给出最终裁决并沉淀标注。证据不足时 verdict 填'不确定'且 needs_human=true",
          {"item_id": {"type": "integer"},
           "verdict": {"type": "string", "enum": ["合规", "不合规", "不确定"]},
           "reason": {"type": "string"},
           "needs_human": {"type": "boolean"}},
          ["item_id", "verdict", "reason"]),
]

TASK_SYSTEM_PROMPT = """你是煤矿文档审查系统的主智能体（统筹调度者）。

你的职责（只管路由、例外和决策，不做高频常规操作）：
1. 理解用户任务，判断涉及的文档是【规则文档】（法规/规程/标准，需切分后入规则库）
   还是【待审文档】（作业规程等，需切分后做合规性/错别字/重复性三链并行审查）。
2. 通过 list_skills / read_skill 了解可用流程，再用 run_script 以 CLI 方式调度脚本。
3. 处理流程的标准顺序：
   - 规则文档：MinerU JSON 放入 new_docs/rule_docs_json → 运行规则切分脚本
     （新KB会在下次审查时自动重建索引）
   - 待审文档：MinerU JSON 放入 new_docs/test_doc_json → 运行待审切分脚本
     → 运行审查引擎 hybrid_rag_review_v9.py（可加 --doc-filter/--max-docs 参数）
4. 审查引擎内部已自带初审→数值核验→二次核验流水线和并行调度，你不要干预；
   引擎产生的分歧/矛盾/低置信项会进入升级队列，由你在队列处理模式下逐个裁决。

约束：
- 只能通过提供的工具操作；脚本调用仅限白名单。
- 信息不足时先用 list_input_files / inspect_document 调查，不要凭空假设路径。
- 任务完成后用中文简要总结做了什么、产出文件在哪。"""

QUEUE_SYSTEM_PROMPT = """你是煤矿合规审查系统的主智能体，正在复核审查流水线升级上来的争议项。

升级类型说明：
- disagreement: 初审判不合规、核验智能体删除了全部问题，但数值核验工具支持初审（判不合规）
- numeric_conflict: LLM最终判不合规，但数值核验工具判所有配对数值合规（疑似方向误判）
- low_confidence: 最终结论"不确定"
- error: 流水线调用异常
- counter_check: 初审判合规，但反向数值核验工具发现疑似超限数值（漏报嫌疑）。
  重点核对工具配对的参数与场景是否一致（如掘进面条款误配综采面），
  场景一致且待审值确实更宽松则判不合规；场景存疑则"不确定"转人工

裁决方法论：
1. 先读队列项中的审查记录、数值核验工具结论和法规依据。
2. 数值类争议：优先采信 numeric_compare_tool 的确定性结论（单位换算和方向语义由代码完成，
   不会犯心算错误）。必要时用不同文本片段再次调用该工具交叉验证。
3. 检索类疑问：用 retrieve_regulations 改写查询词重新检索（如用参数名+场景词），
   确认是否漏检了更相关的条款。
4. 牢记领域规则：严于法规即合规；报警/断电/停工阈值越小越严格；复电阈值越小越严格；
   误差范围不区分正负；突出矿井专用条款不适用于非突出矿井。
5. 证据充分才下结论；证据不足时 verdict="不确定" 且 needs_human=true，写清缺什么证据。

你必须在调查后调用 resolve_queue_item 给出裁决（这是本次任务的唯一出口）。
单项调查不超过 5 次工具调用。"""


# ============ 策略生成（6.3 评测对象：先规划后执行的策略节点） ============

STRATEGY_TASK_TYPES = [
    "规则文档入库", "待审文档审查", "争议项复核", "Word修订", "现场风险问答",
]

STRATEGY_TOOL_VOCAB = [
    "list_skills", "read_skill", "list_input_files", "inspect_document", "run_script",
    "queue_stats", "retrieve_regulations", "numeric_compare_tool",
    "get_chunk_review", "resolve_queue_item",
]

STRATEGY_SYSTEM_PROMPT = """你是煤矿文档审查系统的主智能体。给定一个用户任务，你要先生成结构化执行策略（只规划、不执行）。

系统可处理的任务类型（task_type 必须从中精确选择一个）：
- 规则文档入库：把法规/规程/标准切分后建入规则知识库。
- 待审文档审查：把作业规程等待审文档切分后，做合规/错别字/重复三链并行审查，产生的争议项进入升级队列。
- 争议项复核：对审查流水线升级到队列的分歧/数值矛盾/低置信/异常项逐个复核并裁决。
- Word修订：根据审查问题与人工意见，定位并修改 Word 文档中对应段落。
- 现场风险问答：针对井下现场情况追问关键信息，并给出有法规依据的处置建议。

可用工具（selected_tools 只能从下列名称中选择）：
list_skills, read_skill, list_input_files, inspect_document, run_script,
queue_stats, retrieve_regulations, numeric_compare_tool, get_chunk_review, resolve_queue_item

升级/转人工原则（决定 needs_escalation）：涉及升级队列复核、数值方向存疑、证据不足、
或井下高风险现场处置时，needs_escalation=true；常规切分入库、纯信息检索、确定性文档修改时为 false。

只输出如下 JSON（不要任何多余文字、不要解释）：
{
  "task_type": "上述五类之一",
  "selected_tools": ["按调用顺序列出的工具名"],
  "steps": ["有序的执行步骤描述", "..."],
  "needs_escalation": true 或 false,
  "escalation_conditions": ["需要升级/转人工的具体条件，可为空数组"]
}"""


def _parse_strategy_json(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    if not isinstance(data, dict):
        return {}
    return data


# ============ Agent 循环 ============

class MainAgent:
    def __init__(self):
        if not API_KEY:
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
        self.client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        self.queue = ReviewQueue(QUEUE_DB)
        self.tools = AgentTools(self.queue)

    def _execute_tool(self, name: str, arguments: Dict) -> str:
        handler = getattr(self.tools, name, None)
        if handler is None:
            result: Any = {"error": f"未知工具: {name}"}
        else:
            try:
                result = handler(**arguments)
            except TypeError as exc:
                result = {"error": f"参数错误: {exc}"}
            except Exception as exc:
                result = {"error": f"工具执行异常: {str(exc)[:300]}"}
        return json.dumps(result, ensure_ascii=False, default=str)[:12000]

    def _agent_loop(self, system_prompt: str, user_message: str,
                    tool_schemas: List[Dict], max_steps: int) -> Tuple[str, List[str]]:
        """单次独立上下文的 agent 循环。返回 (最终回复, 已调用工具名列表)。"""
        messages: List[Dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        called: List[str] = []
        for _step in range(max_steps):
            response = self.client.chat.completions.create(
                model=AGENT_MODEL, messages=messages,
                tools=tool_schemas, temperature=0.1,
            )
            msg = response.choices[0].message
            if not msg.tool_calls:
                return msg.content or "", called
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })
            for tc in msg.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                print(f"  [tool] {tc.function.name}({tc.function.arguments[:160]})")
                result = self._execute_tool(tc.function.name, arguments)
                called.append(tc.function.name)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return "（达到最大步数限制，任务未完整结束）", called

    # ---- 策略生成（先规划）----

    def generate_strategy(self, task: str) -> Dict[str, Any]:
        """对一个用户任务生成结构化执行策略（task_type/工具/步骤/升级判断），不实际执行。

        返回 {"strategy": <解析后的dict>, "raw": <原始文本>}。
        供 run_task 在执行前调用，也供 6.3 策略生成评测使用。"""
        messages = [
            {"role": "system", "content": STRATEGY_SYSTEM_PROMPT},
            {"role": "user", "content": f"用户任务：{task}\n\n请输出该任务的执行策略（严格 JSON）。"},
        ]
        response = self.client.chat.completions.create(
            model=AGENT_MODEL, messages=messages, temperature=0.1,
        )
        raw = response.choices[0].message.content or ""
        return {"strategy": _parse_strategy_json(raw), "raw": raw}

    # ---- 模式1：自然语言任务 ----

    def run_task(self, task: str) -> str:
        print(f"\n=== 主智能体任务: {task}\n")
        reply, _called = self._agent_loop(
            TASK_SYSTEM_PROMPT, task, TASK_TOOLS + QUEUE_TOOLS[:2], MAX_AGENT_STEPS
        )
        print(f"\n=== 主智能体回复 ===\n{reply}")
        return reply

    # ---- 模式2：升级队列 loop ----

    def process_queue(self, max_items: Optional[int] = None) -> Dict[str, int]:
        requeued = self.queue.requeue_stale(minutes=30)
        if requeued:
            print(f"[队列] 回收卡死项 {requeued} 个")
        stats = {"resolved": 0, "failed": 0, "skipped": 0}
        handled = 0
        while True:
            if max_items is not None and handled >= max_items:
                break
            item = self.queue.fetch_next()
            if item is None:
                break
            handled += 1
            print(f"\n[队列 {handled}] #{item['id']} {item['item_type']} "
                  f"{item['doc_name']} chunk#{item['chunk_index'] + 1} "
                  f"(第{item['attempts'] + 1}次尝试)")

            payload = item["payload"]
            context = {
                "item_id": item["id"],
                "item_type": item["item_type"],
                "doc_name": item["doc_name"],
                "chunk_index": item["chunk_index"],
                "升级原因": payload.get("reason", ""),
                "待审内容": payload.get("chunk_content", ""),
                "chunk位置": payload.get("chunk_meta", {}),
                "流水线审查结果": payload.get("review_result", {}),
                "数值核验工具结论": payload.get("numeric_checks", []),
                "检索到的法规依据": payload.get("kb_refs", []),
            }
            user_message = (
                "请复核以下升级项并给出最终裁决（调用 resolve_queue_item，"
                f"item_id={item['id']}）：\n\n"
                + json.dumps(context, ensure_ascii=False, indent=2)[:9000]
            )
            try:
                # 每个队列项独立上下文，防止跨项污染
                reply, called = self._agent_loop(
                    QUEUE_SYSTEM_PROMPT, user_message, QUEUE_TOOLS, MAX_QUEUE_ITEM_STEPS
                )
                refreshed = self.queue.get_item(item["id"])
                if refreshed and refreshed["status"] == "resolved":
                    stats["resolved"] += 1
                    verdict = (refreshed.get("resolution") or {}).get("verdict", "?")
                    print(f"  → 已裁决: {verdict}")
                else:
                    self.queue.release(item["id"], "agent循环结束但未调用resolve_queue_item")
                    stats["failed"] += 1
                    print("  → 未裁决，已释放重试")
            except Exception as exc:
                self.queue.release(item["id"], str(exc))
                stats["failed"] += 1
                print(f"  → 处理异常已释放: {str(exc)[:120]}")
                time.sleep(2)

        final = self.queue.stats()
        print(f"\n[队列处理完成] 本轮裁决 {stats['resolved']}，失败 {stats['failed']}；"
              f"当前队列状态: {final}")
        if final.get("dead"):
            print(f"  ⚠️ dead-letter {final['dead']} 项需人工处理"
                  "（review_queue.list_items('dead') 查看）")
        return stats


def main():
    parser = argparse.ArgumentParser(description="煤矿审查系统主智能体 v9")
    parser.add_argument("task", nargs="?", default=None, help="自然语言任务描述")
    parser.add_argument("--process-queue", action="store_true", help="处理升级队列")
    parser.add_argument("--max-items", type=int, default=None, help="本轮最多处理的队列项数")
    parser.add_argument("--queue-stats", action="store_true", help="仅查看队列状态")
    args = parser.parse_args()

    if args.queue_stats:
        print(json.dumps(ReviewQueue(QUEUE_DB).stats(), ensure_ascii=False, indent=2))
        return

    agent = MainAgent()
    if args.process_queue:
        agent.process_queue(max_items=args.max_items)
    elif args.task:
        agent.run_task(args.task)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
