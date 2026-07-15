"""Run serial/parallel performance experiments for hybrid_rag_review_v9.py.

This script launches the existing review pipeline for one or more concurrency
settings and records wall-clock metrics needed by paper section 6.4. It can be
expensive because it calls the actual LLM review chain.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_JSON = PROJECT_ROOT / "rag_eval" / "data" / "parallel_performance_eval_v1.json"
OUTPUT_MD = PROJECT_ROOT / "rag_eval" / "reports" / "parallel_performance_eval_v1.md"
LOG_DIR = PROJECT_ROOT / "rag_eval" / "reports" / "parallel_logs"


def atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_review_result_before_after(before: set[Path]) -> Path | None:
    after = set(PROJECT_ROOT.glob("review_results/review_result_v9_20*.json"))
    created = sorted(after - before, key=lambda path: path.stat().st_mtime, reverse=True)
    if created:
        return created[0]
    all_files = sorted(after, key=lambda path: path.stat().st_mtime, reverse=True)
    return all_files[0] if all_files else None


def iter_review_chunks(review: dict[str, Any]):
    for _doc, rows in review.items():
        if isinstance(rows, list):
            yield from rows


def summarize_result(path: Path | None, total_elapsed: float, concurrency: int) -> dict[str, Any]:
    if not path or not path.exists():
        return {
            "concurrency": concurrency,
            "result_path": None,
            "chunk_count": 0,
            "total_elapsed_sec": total_elapsed,
            "throughput_chunks_per_min": None,
            "first_issue_latency_sec": None,
            "error_rate": None,
            "status_counts": {},
        }
    data = load_json(path)
    chunks = list(iter_review_chunks(data))
    status_counts = {}
    errors = 0
    first_issue_latency = None
    for row in chunks:
        review = row.get("review_result") or {}
        status = str(review.get("compliance_status", ""))
        status_counts[status] = status_counts.get(status, 0) + 1
        if review.get("error") or review.get("parse_error"):
            errors += 1
        if first_issue_latency is None and (review.get("issues") or row.get("escalations")):
            first_issue_latency = float((row.get("timings") or {}).get("chunk_total", 0.0))
    chunk_count = len(chunks)
    return {
        "concurrency": concurrency,
        "result_path": str(path.resolve()),
        "chunk_count": chunk_count,
        "total_elapsed_sec": total_elapsed,
        "throughput_chunks_per_min": chunk_count / total_elapsed * 60 if total_elapsed > 0 else None,
        "first_issue_latency_sec": first_issue_latency,
        "error_rate": errors / chunk_count if chunk_count else None,
        "status_counts": status_counts,
    }


def compare_consistency(serial: dict[str, Any], other: dict[str, Any]) -> float | None:
    serial_path = serial.get("result_path")
    other_path = other.get("result_path")
    if not serial_path or not other_path:
        return None
    serial_data = load_json(Path(serial_path))
    other_data = load_json(Path(other_path))

    def index(data: dict[str, Any]) -> dict[tuple[str, int], str]:
        out = {}
        for doc, rows in data.items():
            if not isinstance(rows, list):
                continue
            for row in rows:
                out[(doc, int(row.get("chunk_index", -1)))] = str(
                    (row.get("review_result") or {}).get("compliance_status", "")
                )
        return out

    a = index(serial_data)
    b = index(other_data)
    keys = sorted(set(a) & set(b))
    if not keys:
        return None
    return sum(a[key] == b[key] for key in keys) / len(keys)


def build_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 多智能体异步并行性能评估",
        "",
        f"生成时间：`{payload['generated_at']}`",
        "",
        "| 执行模式 | 文档规模/块数 | 总审查时间/s | 首条问题返回时间/s | 吞吐量/块每分钟 | 加速比 | 结果一致率 | 异常率 | 备注 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload["runs"]:
        lines.append(
            f"| concurrency={row['concurrency']} | {row['chunk_count']} | "
            f"{row['total_elapsed_sec']:.1f} | "
            f"{row['first_issue_latency_sec'] if row['first_issue_latency_sec'] is not None else '-'} | "
            f"{row['throughput_chunks_per_min']:.2f} | "
            f"{row.get('speedup_vs_serial') if row.get('speedup_vs_serial') is not None else '-'} | "
            f"{row.get('result_consistency') if row.get('result_consistency') is not None else '-'} | "
            f"{row['error_rate'] if row['error_rate'] is not None else '-'} | "
            f"`{Path(row['log_path']).name}` |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--doc-filter", default="", help="Passed to hybrid_rag_review_v9.py --doc-filter.")
    parser.add_argument("--max-docs", type=int, default=1)
    parser.add_argument("--mine-type", default="non_outburst", choices=["non_outburst", "outburst"])
    parser.add_argument("--fresh", action="store_true", help="Force fresh review. Recommended for real comparisons.")
    parser.add_argument("--no-report", action="store_true", default=True)
    parser.add_argument("--output", type=Path, default=OUTPUT_JSON)
    parser.add_argument("--report", type=Path, default=OUTPUT_MD)
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    runs = []
    for concurrency in args.concurrency:
        before = set(PROJECT_ROOT.glob("review_results/review_result_v9_20*.json"))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = LOG_DIR / f"v9_concurrency_{concurrency}_{timestamp}.log"
        cmd = [
            sys.executable,
            str(PROJECT_ROOT / "hybrid_rag_review_v9.py"),
            "--concurrency",
            str(concurrency),
            "--max-docs",
            str(args.max_docs),
            "--mine-type",
            args.mine_type,
        ]
        if args.doc_filter:
            cmd += ["--doc-filter", args.doc_filter]
        if args.fresh:
            cmd.append("--fresh")
        if args.no_report:
            cmd.append("--no-report")

        start = time.perf_counter()
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.perf_counter() - start
        log_path.write_text(
            "COMMAND: " + " ".join(cmd) + "\n\nSTDOUT:\n" + (proc.stdout or "")
            + "\n\nSTDERR:\n" + (proc.stderr or ""),
            encoding="utf-8",
        )
        result_path = latest_review_result_before_after(before)
        row = summarize_result(result_path, elapsed, concurrency)
        row.update({
            "returncode": proc.returncode,
            "log_path": str(log_path.resolve()),
            "command": cmd,
        })
        runs.append(row)

    serial = next((row for row in runs if row["concurrency"] == min(args.concurrency)), None)
    if serial and serial["total_elapsed_sec"]:
        for row in runs:
            row["speedup_vs_serial"] = serial["total_elapsed_sec"] / row["total_elapsed_sec"]
            row["result_consistency"] = compare_consistency(serial, row)

    payload = {
        "schema": "coal_parallel_performance_eval_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "doc_filter": args.doc_filter,
        "max_docs": args.max_docs,
        "fresh": args.fresh,
        "runs": runs,
    }
    atomic_write(args.output, payload)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_markdown(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(args.output.resolve())
    print(args.report.resolve())


if __name__ == "__main__":
    main()
