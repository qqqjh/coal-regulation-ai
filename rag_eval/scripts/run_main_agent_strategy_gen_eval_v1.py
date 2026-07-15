"""6.3 主智能体策略生成时间与决策质量评测（真实 LLM 策略节点）。

调用 main_agent_v9.MainAgent.generate_strategy（先规划后执行的策略节点），
对每个金标任务评测：
  - 策略生成响应时间（mean / P95）
  - 任务识别准确率：生成的 task_type 是否等于金标 task_type
  - 工具选择正确率：selected_tools 对 required_tools 的 F1
  - 策略执行匹配度：steps 是否覆盖金标 key_actions（同义用'/'命中其一）
  - 升级决策准确率：needs_escalation 是否与金标一致

与旧的 run_main_agent_strategy_eval_v1.py（工具循环+mock）互补：该脚本评测的是
真实的“策略生成”输出，任务识别为真实分类而非工具召回代理。

需要 DASHSCOPE_API_KEY。运行：
  python rag_eval/scripts/run_main_agent_strategy_gen_eval_v1.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_TASKS = PROJECT_ROOT / "rag_eval" / "config" / "main_agent_strategy_gold_v1.json"
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "main_agent_strategy_gen_eval_v1.json"
OUTPUT_MD = PROJECT_ROOT / "rag_eval" / "reports" / "main_agent_strategy_gen_eval_v1.md"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def tool_f1(selected: list[str], required: list[str], allowed_extra: list[str]) -> dict[str, Any]:
    selected_set = {str(s) for s in selected}
    required_set = set(required)
    allowed_set = required_set | set(allowed_extra)
    relevant = selected_set & required_set
    unexpected = sorted(selected_set - allowed_set)
    precision = len(relevant) / len(selected_set) if selected_set else 0.0
    recall = len(relevant) / len(required_set) if required_set else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tool_precision": precision,
        "tool_recall": recall,
        "tool_f1": f1,
        "unexpected_tools": unexpected,
        "all_required_selected": required_set.issubset(selected_set),
    }


def step_match(steps: list[str], key_actions: list[str]) -> float:
    """金标 key_actions 覆盖率：每个 action 用'/'拆同义词，命中其一即算覆盖。"""
    if not key_actions:
        return 1.0
    joined = " ".join(str(s) for s in steps)
    hit = 0
    for action in key_actions:
        if any(alt and alt in joined for alt in str(action).split("/")):
            hit += 1
    return hit / len(key_actions)


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, int((len(ordered) * 0.95 + 0.999999) - 1))]


def build_markdown(payload: dict[str, Any]) -> str:
    agg = payload["aggregate"]
    lines = [
        "# 6.3 主智能体策略生成时间与决策质量评测",
        "",
        f"生成时间：`{payload['generated_at']}`",
        f"金标任务：`{payload['tasks_path']}`（{agg['count']} 个，覆盖五类任务）",
        f"评测对象：`MainAgent.generate_strategy`（真实 qwen-plus 策略节点）",
        "",
        "| 范围 | 平均生成时间/s | P95/s | 任务识别准确率 | 工具选择F1 | 策略执行匹配度 | 升级决策准确率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| 全部任务 | "
        f"{agg['mean_elapsed_sec']:.3f} | {agg['p95_elapsed_sec']:.3f} | "
        f"{agg['task_recognition_accuracy']:.2%} | {agg['mean_tool_f1']:.2%} | "
        f"{agg['mean_step_match']:.2%} | {agg['escalation_decision_accuracy']:.2%} |",
    ]
    lines += ["", "## 分任务类型", "",
              "| 任务类型 | 个数 | 平均时间/s | 任务识别 | 工具F1 | 步骤匹配 | 升级准确 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for ttype, row in payload["by_type"].items():
        lines.append(
            f"| {ttype} | {row['count']} | {row['mean_elapsed_sec']:.3f} | "
            f"{row['task_recognition_accuracy']:.2%} | {row['mean_tool_f1']:.2%} | "
            f"{row['mean_step_match']:.2%} | {row['escalation_decision_accuracy']:.2%} |"
        )
    lines += ["", "## 明细", "",
              "| 任务ID | 金标类型 | 识别类型 | 耗时/s | 工具F1 | 步骤匹配 | 升级(标/判) | 意外工具 |",
              "|---|---|---|---:|---:|---:|---|---|"]
    for c in payload["cases"]:
        esc = f"{c['gold_needs_escalation']}/{c['pred_needs_escalation']}"
        esc_mark = "" if c["escalation_correct"] else " ⚠"
        type_mark = "" if c["task_recognized"] else " ⚠"
        lines.append(
            f"| {c['task_id']} | {c['gold_task_type']} | {c['pred_task_type']}{type_mark} | "
            f"{c['elapsed_sec']:.3f} | {c['tool_f1']:.2%} | {c['step_match']:.2%} | "
            f"{esc}{esc_mark} | {', '.join(c['unexpected_tools']) or '-'} |"
        )
    lines += [
        "",
        "## 口径说明",
        "",
        "- 任务识别准确率＝生成 task_type 与金标精确一致的比例（真实分类，非工具召回代理）。",
        "- 工具选择 F1：selected_tools 对 required_tools 的精确率/召回率调和平均；allowed_extra 不罚。",
        "- 策略执行匹配度＝金标 key_actions 被 steps 文本覆盖的比例。",
        "- 升级决策准确率＝needs_escalation 与金标一致的比例。",
        "- 评测的是“先规划”的策略节点，不实际执行工具，故无副作用、可重复跑。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--report", type=Path, default=OUTPUT_MD)
    args = parser.parse_args()

    if not os.getenv("DASHSCOPE_API_KEY"):
        raise SystemExit("DASHSCOPE_API_KEY is required (real strategy-generation LLM call).")

    from main_agent_v9 import MainAgent

    tasks_payload = load_json(args.tasks)
    agent = MainAgent()
    cases: list[dict[str, Any]] = []
    for task in tasks_payload["tasks"]:
        start = time.perf_counter()
        result = agent.generate_strategy(task["task"])
        elapsed = time.perf_counter() - start
        strategy = result["strategy"] or {}
        pred_type = str(strategy.get("task_type", "")).strip()
        selected = strategy.get("selected_tools") or []
        if not isinstance(selected, list):
            selected = [selected]
        steps = strategy.get("steps") or []
        if not isinstance(steps, list):
            steps = [str(steps)]
        pred_esc = bool(strategy.get("needs_escalation", False))

        scores = tool_f1(selected, task["required_tools"], task.get("allowed_extra_tools", []))
        sm = step_match(steps, task.get("key_actions", []))
        recognized = pred_type == task["task_type"]
        esc_correct = pred_esc == bool(task["needs_escalation"])
        print(f"  [{task['task_id']}] 类型 {pred_type or '∅'} "
              f"({'✓' if recognized else '✗'}) F1={scores['tool_f1']:.2f} "
              f"step={sm:.2f} esc={'✓' if esc_correct else '✗'} {elapsed:.1f}s")
        cases.append({
            "task_id": task["task_id"],
            "gold_task_type": task["task_type"],
            "pred_task_type": pred_type,
            "task_recognized": recognized,
            "elapsed_sec": elapsed,
            "selected_tools": selected,
            "steps": steps,
            "tool_f1": scores["tool_f1"],
            "tool_precision": scores["tool_precision"],
            "tool_recall": scores["tool_recall"],
            "unexpected_tools": scores["unexpected_tools"],
            "step_match": sm,
            "gold_needs_escalation": bool(task["needs_escalation"]),
            "pred_needs_escalation": pred_esc,
            "escalation_correct": esc_correct,
            "raw": result["raw"],
        })

    def agg_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "count": len(rows),
            "mean_elapsed_sec": mean(r["elapsed_sec"] for r in rows),
            "p95_elapsed_sec": p95([r["elapsed_sec"] for r in rows]),
            "task_recognition_accuracy": mean(float(r["task_recognized"]) for r in rows),
            "mean_tool_f1": mean(r["tool_f1"] for r in rows),
            "mean_step_match": mean(r["step_match"] for r in rows),
            "escalation_decision_accuracy": mean(float(r["escalation_correct"]) for r in rows),
        }

    aggregate = agg_rows(cases)
    by_type_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in cases:
        by_type_rows[c["gold_task_type"]].append(c)
    by_type = {t: agg_rows(rows) for t, rows in by_type_rows.items()}

    payload = {
        "schema": "coal_main_agent_strategy_gen_eval_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tasks_path": str(args.tasks.resolve()),
        "aggregate": aggregate,
        "by_type": by_type,
        "cases": cases,
    }
    atomic_write(args.output, payload)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_markdown(payload), encoding="utf-8")
    print("\n=== 6.3 策略生成评测 ===")
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.report.resolve())


if __name__ == "__main__":
    main()
