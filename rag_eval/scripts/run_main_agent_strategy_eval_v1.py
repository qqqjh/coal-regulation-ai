"""Evaluate main-agent strategy generation and tool-selection quality.

This script calls the real main-agent LLM loop, but by default it mocks tool
execution so the evaluation measures planning/tool choice without launching
expensive document review jobs. Set --execute-tools only when you explicitly
want the tools to run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
DEFAULT_TASKS = PROJECT_ROOT / "rag_eval" / "config" / "main_agent_strategy_eval_tasks_v1.json"
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "main_agent_strategy_eval_v1.json"
OUTPUT_MD = PROJECT_ROOT / "rag_eval" / "reports" / "main_agent_strategy_eval_v1.md"


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def tool_prf(called: list[str], required: list[str], allowed_extra: list[str]) -> dict[str, Any]:
    called_set = set(called)
    required_set = set(required)
    allowed_set = required_set | set(allowed_extra)
    relevant_called = called_set & required_set
    unexpected = [name for name in called if name not in allowed_set]
    precision = len(relevant_called) / len(called_set) if called_set else 0.0
    recall = len(relevant_called) / len(required_set) if required_set else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "required_tool_recall": recall,
        "called_tool_precision": precision,
        "tool_selection_f1": f1,
        "all_required_tools_called": required_set.issubset(called_set),
        "unexpected_tools": unexpected,
    }


class StrategyEvalAgent:
    def __init__(self, execute_tools: bool):
        from main_agent_v9 import MainAgent

        self._agent = MainAgent()
        self.execute_tools = execute_tools
        self.tool_call_records: list[dict[str, Any]] = []
        self.mock_outputs = {
            "list_skills": {
                "skills": [
                    {"name": "chunk-pending-doc", "script": "pending_doc_chunking_v9.py"},
                    {"name": "review-engine", "script": "hybrid_rag_review_v9.py"},
                    {"name": "chunk-rule-doc", "script": "chapter_based_chunking_v6.py"},
                ]
            },
            "read_skill": {"content": "script: pending_doc_chunking_v9.py\n用于待审文档切分。"},
            "list_input_files": {
                "pending_docs_json": [
                    "MinerU_004    S1302工作面作业规程（综采）__20260406082118.json",
                    "MinerU_006     总工办S5102高抽巷掘进作业规程（掘进__20260406113122.json",
                ],
                "rule_docs_json": ["MinerU_煤矿安全规程__20260406080656.json"],
            },
            "inspect_document": {"head": "作业规程待审文档片段，包含章节、表格和安全措施。"},
            "run_script": {"returncode": 0, "stdout_tail": "mock: script completed successfully"},
            "queue_stats": {"pending": 3, "processing": 0, "resolved": 12, "dead": 0},
            "retrieve_regulations": {
                "results": [
                    {
                        "doc": "煤矿安全规程",
                        "section": "瓦斯检查与停电撤人",
                        "score": 0.91,
                        "content": "采掘工作面及其他作业地点风流中甲烷浓度达到1.0%时，必须停止用电钻打眼。",
                    }
                ]
            },
            "numeric_compare_tool": {"overall": "不合规", "details": []},
            "get_chunk_review": {"result": {"review_result": {"compliance_status": "不确定"}}},
            "resolve_queue_item": {"status": "resolved"},
        }

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> str:
        validation_error = ""
        if name == "run_script":
            script_name = Path(str(arguments.get("script", ""))).name
            allowed_scripts = {
                "chapter_based_chunking_v6.py",
                "pending_doc_chunking_v9.py",
                "hybrid_rag_review_v9.py",
            }
            if script_name not in allowed_scripts:
                validation_error = f"script not allowed or not registered: {script_name}"
        self.tool_call_records.append(
            {"tool": name, "arguments": arguments, "validation_error": validation_error}
        )
        if validation_error and not self.execute_tools:
            return json.dumps(
                {"mock": True, "tool": name, "error": validation_error},
                ensure_ascii=False,
            )
        if self.execute_tools:
            return self._agent._execute_tool(name, arguments)
        return json.dumps(
            {
                "mock": True,
                "tool": name,
                "arguments": arguments,
                "result": self.mock_outputs.get(name, {"ok": True}),
            },
            ensure_ascii=False,
        )

    def run_task(self, task: str) -> tuple[str, list[str]]:
        from main_agent_v9 import MAX_AGENT_STEPS, QUEUE_TOOLS, TASK_SYSTEM_PROMPT, TASK_TOOLS

        original_execute = self._agent._execute_tool
        self.tool_call_records = []
        self._agent._execute_tool = self._execute_tool  # type: ignore[method-assign]
        try:
            return self._agent._agent_loop(
                TASK_SYSTEM_PROMPT,
                task,
                TASK_TOOLS + QUEUE_TOOLS[:2],
                MAX_AGENT_STEPS,
            )
        finally:
            self._agent._execute_tool = original_execute  # type: ignore[method-assign]


def build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 主智能体策略生成时间与决策质量评估",
        "",
        f"生成时间：`{payload['generated_at']}`",
        f"工具执行模式：`{'execute' if payload['execute_tools'] else 'mock'}`",
        "",
        "| 任务类型 | 平均策略生成时间/s | P95时间/s | 任务识别准确率 | 工具选择准确率 | 步骤有效率 | 升级条件完整率 | 备注 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    aggregate = payload["aggregate"]
    lines.append(
        "| 全部任务 | "
        f"{aggregate['mean_elapsed_sec']:.3f} | {aggregate['p95_elapsed_sec']:.3f} | "
        f"{aggregate['task_recognition_accuracy']:.2%} | "
        f"{aggregate['mean_tool_selection_f1']:.2%} | "
        f"{aggregate['step_validity_rate']:.2%} | "
        f"{aggregate['escalation_condition_completeness']:.2%} | "
        f"{aggregate['case_count']}个任务 |"
    )
    lines.extend(["", "## 明细", "", "| 任务ID | 类型 | 耗时/s | 调用工具 | 必需工具召回 | 工具F1 | 参数错误 | 意外工具 |", "|---|---|---:|---|---:|---:|---|---|"])
    for row in payload["cases"]:
        invalid = "; ".join(record["validation_error"] for record in row.get("invalid_tool_arguments", []))
        lines.append(
            f"| {row['task_id']} | {row['task_type']} | {row['elapsed_sec']:.3f} | "
            f"`{', '.join(row['called_tools'])}` | "
            f"{row['required_tool_recall']:.2%} | {row['tool_selection_f1']:.2%} | "
            f"{invalid or '-'} | "
            f"{', '.join(row['unexpected_tools']) or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--report", type=Path, default=OUTPUT_MD)
    parser.add_argument("--execute-tools", action="store_true", help="Run real tools instead of mock outputs.")
    args = parser.parse_args()

    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("DASHSCOPE_API_KEY is required because this evaluates the real main-agent LLM loop.")

    tasks_payload = load_json(args.tasks)
    agent = StrategyEvalAgent(execute_tools=args.execute_tools)
    cases = []
    for task in tasks_payload["tasks"]:
        start = time.perf_counter()
        reply, called = agent.run_task(task["task"])
        elapsed = time.perf_counter() - start
        scores = tool_prf(called, task["required_tools"], task.get("allowed_extra_tools", []))
        queue_task = "queue" in task["task_id"] or "升级" in task["task_type"]
        invalid_tool_arguments = [
            record for record in agent.tool_call_records if record.get("validation_error")
        ]
        cases.append(
            {
                **task,
                "elapsed_sec": elapsed,
                "called_tools": called,
                "tool_call_records": list(agent.tool_call_records),
                "invalid_tool_arguments": invalid_tool_arguments,
                "final_reply": reply,
                "task_recognized": scores["all_required_tools_called"],
                "step_valid": not scores["unexpected_tools"] and not invalid_tool_arguments and bool(called),
                "escalation_condition_complete": (not queue_task) or ("queue_stats" in called),
                **scores,
            }
        )

    elapsed_values = sorted(row["elapsed_sec"] for row in cases)
    p95 = elapsed_values[max(0, int((len(elapsed_values) * 0.95 + 0.999999) - 1))]
    aggregate = {
        "case_count": len(cases),
        "mean_elapsed_sec": mean(row["elapsed_sec"] for row in cases),
        "p95_elapsed_sec": p95,
        "task_recognition_accuracy": mean(float(row["task_recognized"]) for row in cases),
        "mean_tool_selection_f1": mean(row["tool_selection_f1"] for row in cases),
        "step_validity_rate": mean(float(row["step_valid"]) for row in cases),
        "escalation_condition_completeness": mean(float(row["escalation_condition_complete"]) for row in cases),
    }
    payload = {
        "schema": "coal_main_agent_strategy_eval_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tasks": str(args.tasks.resolve()),
        "execute_tools": args.execute_tools,
        "aggregate": aggregate,
        "cases": cases,
    }
    atomic_write(args.output, payload)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_markdown(payload), encoding="utf-8")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.report.resolve())


if __name__ == "__main__":
    main()
