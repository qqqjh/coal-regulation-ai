"""Automatically evaluate the S1302 manual-error benchmark against V9 review.

Default mode submits the blind DOCX through the same V9 API used by the React
review page, waits for the worker, downloads ``issues``, compares them with the
30-case gold manifest, and writes JSON/CSV/HTML reports.

The script has three input modes:

1. default: upload the benchmark document and run a new review job;
2. ``--job-id``: evaluate an existing V9 job;
3. ``--issues-json``: evaluate an exported issue list without any services.

Use ``--manage-services`` for a one-command local run.  It starts a non-reload
FastAPI process and the V9 worker, then stops only the processes it started.
"""

from __future__ import annotations

import argparse
import ctypes
import csv
import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = (
    PROJECT_ROOT / "new_docs" / "test_doc" / "S1302人工错误注入测试集_v1"
)
DEFAULT_DOCUMENT = BENCHMARK_DIR / "004 S1302工作面作业规程（综采）_人工错误注入版_v1.docx"
DEFAULT_GOLD = BENCHMARK_DIR / "004 S1302工作面作业规程（综采）_人工错误金标_v1.json"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "rag_eval" / "reports" / "s1302_manual_error_eval_v1"
DEFAULT_BASE_URL = os.environ.get("V9_REVIEW_BASE_URL", "http://127.0.0.1:8000/api/v9")
TERMINAL_STATUSES = {"done", "failed", "cancelled"}
EXPECTED_TYPES = ("compliance", "typo", "redundancy")


def configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except Exception:
                pass


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("％", "%").replace("﹪", "%")
    return re.sub(r"[\s\u3000]+", "", text).lower()


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(flatten_text(value[key]) for key in sorted(value))
    if isinstance(value, (list, tuple, set)):
        return " ".join(flatten_text(item) for item in value)
    return str(value)


def char_ngrams(value: str, size: int = 3) -> set[str]:
    value = normalize_text(value)
    if not value:
        return set()
    if len(value) <= size:
        return {value}
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def ratio(left: str, right: str) -> float:
    left_n = normalize_text(left)
    right_n = normalize_text(right)
    if not left_n or not right_n:
        return 0.0
    return SequenceMatcher(None, left_n, right_n, autojunk=False).ratio()


def pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def safe_div(numerator: int | float, denominator: int | float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def f1_score(precision: float, recall: float) -> float:
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def normalize_issue_type(issue: dict[str, Any]) -> str:
    raw_type = normalize_text(issue.get("issue_type"))
    status = normalize_text(issue.get("status"))
    title = normalize_text(issue.get("title"))
    combined = f"{raw_type}|{status}|{title}"
    if raw_type in EXPECTED_TYPES:
        return raw_type
    if raw_type == "numeric":
        return "compliance"
    if any(token in combined for token in ("错别字", "typo")):
        return "typo"
    if any(token in combined for token in ("重复", "redundancy", "duplicate")):
        return "redundancy"
    if any(token in combined for token in ("不合规", "compliance", "numeric")):
        return "compliance"
    if raw_type == "escalation" or "不确定" in status:
        return "escalation"
    return raw_type or "unknown"


def expected_blocks(gold: dict[str, Any], all_cases: list[dict[str, Any]]) -> list[int]:
    destinations = sorted(
        int(case["inserted_after_block_index"])
        for case in all_cases
        if case.get("type") == "redundancy"
    )

    def shifted(block_index: int) -> int:
        return block_index + sum(destination < block_index for destination in destinations)

    if gold["type"] in {"compliance", "typo"}:
        return [shifted(int(gold["baseline_block_index"]))]

    source = shifted(int(gold["source_block_index"]))
    destination = int(gold["inserted_after_block_index"])
    inserted = destination + sum(item < destination for item in destinations) + 1
    return [source, inserted]


def block_similarity(gold_blocks: Iterable[int], predicted_blocks: Iterable[Any]) -> float:
    gold_values = [int(value) for value in gold_blocks]
    predicted_values: list[int] = []
    for value in predicted_blocks or []:
        try:
            predicted_values.append(int(value))
        except (TypeError, ValueError):
            continue
    if not gold_values or not predicted_values:
        return 0.0
    distance = min(abs(left - right) for left in gold_values for right in predicted_values)
    if distance == 0:
        return 1.0
    if distance <= 2:
        return 0.8
    if distance <= 8:
        return 0.5
    if distance <= 20:
        return 0.2
    return 0.0


def gold_target(case: dict[str, Any]) -> str:
    if case["type"] == "redundancy":
        return str(case.get("duplicated_text") or "")
    return str(case.get("mutated_text") or "")


def gold_context(case: dict[str, Any]) -> str:
    if case["type"] == "redundancy":
        return " ".join(
            str(case.get(key) or "")
            for key in ("source_anchor", "destination_anchor", "duplicated_text")
        )
    return str(case.get("mutated_paragraph") or case.get("anchor") or gold_target(case))


def prediction_blob(issue: dict[str, Any]) -> str:
    return " ".join(
        flatten_text(issue.get(key))
        for key in (
            "original_text",
            "title",
            "status",
            "suggestion",
            "regulation",
            "reason",
            "detail",
        )
    )


def target_similarity(
    target: str, issue: dict[str, Any], min_containment: int = 6
) -> float:
    target_n = normalize_text(target)
    original = normalize_text(issue.get("original_text"))
    blob = normalize_text(prediction_blob(issue))
    if not target_n or not blob:
        return 0.0
    if target_n in blob:
        return 1.0
    if len(original) >= min_containment and original in target_n:
        return 1.0
    if len(target_n) >= min_containment and target_n in original:
        return 1.0
    return max(
        ratio(target_n, original),
        jaccard(char_ngrams(target_n), char_ngrams(original)),
        jaccard(char_ngrams(target_n), char_ngrams(blob)),
    )


def context_similarity(context: str, issue: dict[str, Any]) -> float:
    original = str(issue.get("original_text") or "")
    blob = prediction_blob(issue)
    context_n = normalize_text(context)
    original_n = normalize_text(original)
    blob_n = normalize_text(blob)
    if not context_n or not blob_n:
        return 0.0
    if context_n in blob_n:
        return 1.0
    if len(original_n) >= 10 and original_n in context_n:
        return 0.95
    return max(
        ratio(context_n, original_n),
        jaccard(char_ngrams(context_n), char_ngrams(original_n)),
        jaccard(char_ngrams(context_n), char_ngrams(blob_n)),
    )


def candidate_score(
    gold: dict[str, Any], issue: dict[str, Any], all_cases: list[dict[str, Any]]
) -> dict[str, float]:
    # 错别字智能体通常只返回最小替换片段（如“通疯”），而金标目标可能包含
    # 完整词组（“通疯系统”）；允许 2 字片段包含，其他类型仍要求较长文本。
    min_containment = 2 if gold.get("type") == "typo" else 6
    target_score = target_similarity(
        gold_target(gold), issue, min_containment=min_containment
    )
    context_score = context_similarity(gold_context(gold), issue)
    block_score = block_similarity(expected_blocks(gold, all_cases), issue.get("block_indices") or [])
    type_score = 1.0 if normalize_issue_type(issue) == gold["type"] else 0.0
    score = 0.55 * target_score + 0.32 * context_score + 0.10 * block_score + 0.03 * type_score
    return {
        "score": min(1.0, score),
        "target_score": target_score,
        "context_score": context_score,
        "block_score": block_score,
        "type_score": type_score,
    }


def verdict_is_correct(gold: dict[str, Any], issue: dict[str, Any]) -> bool:
    predicted_type = normalize_issue_type(issue)
    if predicted_type != gold["type"]:
        return False
    status = normalize_text(issue.get("status"))
    if gold["type"] == "compliance":
        return "不合规" in status
    if gold["type"] == "typo":
        return "错别字" in status or status == "typo"
    if gold["type"] == "redundancy":
        return "重复" in status or status in {"redundancy", "duplicate"}
    return False


def match_cases(
    gold_cases: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    threshold: float = 0.56,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[tuple[float, int, int, dict[str, float]]] = []
    for gold_index, gold in enumerate(gold_cases):
        for issue_index, issue in enumerate(issues):
            detail = candidate_score(gold, issue, gold_cases)
            if detail["score"] >= threshold:
                candidates.append((detail["score"], gold_index, issue_index, detail))
    candidates.sort(key=lambda item: (item[0], item[3]["type_score"], item[3]["block_score"]), reverse=True)

    assigned_gold: set[int] = set()
    assigned_issues: set[int] = set()
    assignments: dict[int, tuple[int, dict[str, float]]] = {}
    # 两阶段分配：先让同类型预测完成一对一匹配，再允许剩余预测跨类型匹配为
    # detected_not_strict。避免长 chunk 的“重复内容”抢占正确的短错别字结果。
    for same_type_only in (True, False):
        for _score, gold_index, issue_index, detail in candidates:
            is_same_type = bool(detail["type_score"])
            if same_type_only != is_same_type:
                continue
            if gold_index in assigned_gold or issue_index in assigned_issues:
                continue
            assigned_gold.add(gold_index)
            assigned_issues.add(issue_index)
            assignments[gold_index] = (issue_index, detail)

    rows: list[dict[str, Any]] = []
    for gold_index, gold in enumerate(gold_cases):
        matched = assignments.get(gold_index)
        issue = issues[matched[0]] if matched else None
        detail = matched[1] if matched else {
            "score": 0.0,
            "target_score": 0.0,
            "context_score": 0.0,
            "block_score": 0.0,
            "type_score": 0.0,
        }
        type_correct = bool(issue and normalize_issue_type(issue) == gold["type"])
        verdict_correct = bool(issue and verdict_is_correct(gold, issue))
        strict_hit = bool(issue and type_correct and verdict_correct)
        outcome = "strict_hit" if strict_hit else "detected_not_strict" if issue else "missed"
        rows.append(
            {
                "error_id": gold["error_id"],
                "gold_type": gold["type"],
                "gold_subtype": gold.get("subtype", ""),
                "gold_target": gold_target(gold),
                "gold_original": gold.get("original_text", ""),
                "gold_blocks": expected_blocks(gold, gold_cases),
                "matched": bool(issue),
                "strict_hit": strict_hit,
                "type_correct": type_correct,
                "verdict_correct": verdict_correct,
                "outcome": outcome,
                "match_score": round(detail["score"], 6),
                "score_detail": {key: round(value, 6) for key, value in detail.items()},
                "predicted_issue_id": issue.get("id") if issue else None,
                "predicted_type": normalize_issue_type(issue) if issue else "",
                "predicted_raw_type": issue.get("issue_type", "") if issue else "",
                "predicted_status": issue.get("status", "") if issue else "",
                "predicted_title": issue.get("title", "") if issue else "",
                "predicted_original_text": issue.get("original_text", "") if issue else "",
                "predicted_blocks": issue.get("block_indices", []) if issue else [],
                "predicted_reason": issue.get("reason", "") if issue else "",
                "predicted_suggestion": issue.get("suggestion", "") if issue else "",
            }
        )

    unmatched = [
        {
            **issue,
            "normalized_issue_type": normalize_issue_type(issue),
        }
        for index, issue in enumerate(issues)
        if index not in assigned_issues
    ]
    return rows, unmatched


def compute_metrics(
    rows: list[dict[str, Any]], issues: list[dict[str, Any]], unmatched: list[dict[str, Any]]
) -> dict[str, Any]:
    gold_count = len(rows)
    predicted_count = len(issues)
    detected = sum(bool(row["matched"]) for row in rows)
    strict_tp = sum(bool(row["strict_hit"]) for row in rows)
    detection_precision = safe_div(detected, predicted_count)
    detection_recall = safe_div(detected, gold_count)
    strict_precision = safe_div(strict_tp, predicted_count)
    strict_recall = safe_div(strict_tp, gold_count)

    categories: dict[str, Any] = {}
    for category in EXPECTED_TYPES:
        category_rows = [row for row in rows if row["gold_type"] == category]
        predicted_category = sum(normalize_issue_type(issue) == category for issue in issues)
        category_detected = sum(bool(row["matched"]) for row in category_rows)
        category_strict = sum(bool(row["strict_hit"]) for row in category_rows)
        category_precision = safe_div(category_strict, predicted_category)
        category_recall = safe_div(category_strict, len(category_rows))
        categories[category] = {
            "gold_count": len(category_rows),
            "predicted_count": predicted_category,
            "detected_count": category_detected,
            "strict_tp": category_strict,
            "detection_recall": safe_div(category_detected, len(category_rows)),
            "strict_precision": category_precision,
            "strict_recall": category_recall,
            "strict_f1": f1_score(category_precision, category_recall),
        }

    return {
        "gold_count": gold_count,
        "predicted_count": predicted_count,
        "detected_count": detected,
        "strict_tp": strict_tp,
        "missed_count": gold_count - detected,
        "detected_not_strict_count": detected - strict_tp,
        "unmatched_prediction_count": len(unmatched),
        "detection_precision": detection_precision,
        "detection_recall": detection_recall,
        "detection_f1": f1_score(detection_precision, detection_recall),
        "strict_precision": strict_precision,
        "strict_recall": strict_recall,
        "strict_f1": f1_score(strict_precision, strict_recall),
        "categories": categories,
    }


def evaluate(
    gold: dict[str, Any], issues: list[dict[str, Any]], threshold: float = 0.56
) -> dict[str, Any]:
    cases = list(gold.get("cases") or [])
    if not cases:
        raise ValueError("金标文件没有 cases")
    rows, unmatched = match_cases(cases, issues, threshold=threshold)
    metrics = compute_metrics(rows, issues, unmatched)
    return {
        "schema": "coal_manual_error_injection_evaluation_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark_id": gold.get("benchmark_id", ""),
        "matching_threshold": threshold,
        "metric_definitions": {
            "detection": "文本或位置与金标匹配，不要求问题类型和最终判定完全正确",
            "strict": "一对一匹配成功，且问题类型与最终判定均正确",
            "precision_denominator": "系统输出的全部问题数，包括无法匹配金标的误报",
        },
        "metrics": metrics,
        "case_results": rows,
        "unmatched_predictions": unmatched,
        "issues": issues,
    }


class ApiError(RuntimeError):
    pass


class V9ApiClient:
    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            headers={"Accept": "application/json", "User-Agent": "coal-rag-eval/1.0", **(headers or {})},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"HTTP {exc.code} {method} {path}: {detail[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise ApiError(
                f"无法连接 {self.base_url}：{exc.reason}。请启动 backend/main.py，"
                "或使用 --manage-services 自动启动。"
            ) from exc
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ApiError(f"接口返回非 JSON：{payload[:300]!r}") from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, data: bytes = b"", headers: Optional[dict[str, str]] = None) -> Any:
        return self._request("POST", path, data=data, headers=headers)

    def preprocess(self, document: Path) -> dict[str, Any]:
        boundary = f"----CoalEval{uuid.uuid4().hex}"
        filename = document.name.replace('"', "")
        body = b"".join(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8"),
                b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n",
                document.read_bytes(),
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        return self._request(
            "POST",
            "/preprocess",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
            timeout=600.0,
        )

    def start(
        self,
        preprocess_id: str,
        *,
        mine_type: str,
        user_id: str,
        kb_id: Optional[int],
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"mine_type": mine_type, "user_id": user_id}
        if kb_id is not None:
            params["kb_id"] = kb_id
        query = urllib.parse.urlencode(params)
        return self.post(f"/start/{urllib.parse.quote(preprocess_id)}?{query}")

    def status(self, job_id: str) -> dict[str, Any]:
        return self.get(f"/status/{urllib.parse.quote(job_id)}")

    def issues(self, job_id: str) -> list[dict[str, Any]]:
        payload = self.get(f"/issues/{urllib.parse.quote(job_id)}")
        return list(payload.get("issues") or [])

    def jobs(self) -> Any:
        return self.get("/jobs?limit=1")

    def cancel(self, job_id: str) -> None:
        try:
            self.post(f"/cancel/{urllib.parse.quote(job_id)}")
        except Exception:
            pass


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[Any]
    log_handle: Any
    log_path: Path


class ServiceManager:
    def __init__(
        self,
        python_exe: Path,
        report_dir: Path,
        base_url: str,
        *,
        use_mineru: bool = True,
        worker_script: Path | str = "v9_worker.py",
    ):
        self.python_exe = python_exe
        self.report_dir = report_dir
        self.base_url = base_url
        self.use_mineru = use_mineru
        candidate = Path(worker_script)
        self.worker_script = candidate if candidate.is_absolute() else PROJECT_ROOT / candidate
        if not self.worker_script.is_file():
            raise FileNotFoundError(f"Worker 脚本不存在: {self.worker_script}")
        self.processes: list[ManagedProcess] = []
        self.peak_rss_bytes: dict[str, int] = {}

    def _spawn(
        self,
        name: str,
        command: list[str],
        cwd: Path,
        *,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.report_dir / f"service_{name}.log"
        log_handle = log_path.open("w", encoding="utf-8", errors="replace")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            env=env,
        )
        self.processes.append(ManagedProcess(name, process, log_handle, log_path))
        self.peak_rss_bytes[name] = 0
        print(f"[服务] 已启动 {name}: PID={process.pid}, 日志={log_path}")

    def start(self) -> None:
        if self.use_mineru:
            mineru_executable = detect_mineru_executable(self.python_exe)
            self._spawn(
                "mineru",
                [
                    str(mineru_executable),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "51071",
                ],
                PROJECT_ROOT,
            )
            self.wait_url("http://127.0.0.1:51071/openapi.json", "MinerU", 180.0)

        self._spawn(
            "backend",
            [
                str(self.python_exe),
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
            ],
            PROJECT_ROOT / "backend",
        )
        worker_env = os.environ.copy()
        worker_env["V9_USE_MINERU_FOR_PENDING"] = "1" if self.use_mineru else "0"
        # 自动评测只测审核主链。关闭同进程内的主智能体升级队列，避免历史待办和
        # 新升级项在评测期间额外调用 LLM，污染耗时、限流和准确率基线。
        worker_env["V9_DISABLE_ESCALATION_LOOP"] = "1"
        worker_env.setdefault("V9_MINERU_API_URL", "http://127.0.0.1:51071")
        self._spawn(
            "worker",
            [str(self.python_exe), str(self.worker_script)],
            PROJECT_ROOT,
            env=worker_env,
        )
        print(f"[服务] Worker入口: {self.worker_script.name}")
        if self.worker_script.name.lower() == "v10_worker.py":
            self.wait_log_marker(
                "worker", "REVIEW_ENGINE_VERSION=v10", "v10 Worker", 30.0
            )

    def wait_log_marker(
        self, process_name: str, marker: str, display_name: str, timeout_seconds: float
    ) -> None:
        managed = next(
            (item for item in self.processes if item.name == process_name), None
        )
        if managed is None:
            raise RuntimeError(f"未找到受管进程: {process_name}")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            code = managed.process.poll()
            if code is not None:
                raise RuntimeError(
                    f"{display_name} 启动后退出（code={code}），请查看 {managed.log_path}"
                )
            try:
                text = managed.log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if marker in text:
                print(f"[服务] {display_name} 已确认: {marker}")
                return
            time.sleep(0.25)
        raise TimeoutError(
            f"{display_name} 未在 {timeout_seconds:.0f}s 内输出标记 {marker}；"
            f"请查看 {managed.log_path}"
        )

    def wait_url(self, url: str, name: str, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            for managed in self.processes:
                code = managed.process.poll()
                if code is not None:
                    raise RuntimeError(
                        f"{managed.name} 启动后退出（code={code}），请查看 {managed.log_path}"
                    )
            try:
                with urllib.request.urlopen(url, timeout=5.0) as response:
                    if 200 <= response.status < 500:
                        print(f"[服务] {name} 已就绪")
                        return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1.0)
        raise TimeoutError(f"{name} 在 {timeout_seconds:.0f}s 内未就绪：{last_error}")

    def wait_backend(self, timeout_seconds: float = 120.0) -> None:
        client = V9ApiClient(self.base_url, timeout=5.0)
        deadline = time.monotonic() + timeout_seconds
        last_error = ""
        while time.monotonic() < deadline:
            for managed in self.processes:
                code = managed.process.poll()
                if code is not None:
                    raise RuntimeError(
                        f"{managed.name} 启动后退出（code={code}），请查看 {managed.log_path}"
                    )
            try:
                client.jobs()
                print("[服务] 后端已就绪")
                return
            except Exception as exc:
                last_error = str(exc)
                time.sleep(1.0)
        raise TimeoutError(f"后端在 {timeout_seconds:.0f}s 内未就绪：{last_error}")

    def sample_memory(self) -> None:
        for managed in self.processes:
            rss = process_rss_bytes(managed.process.pid)
            if rss is not None:
                self.peak_rss_bytes[managed.name] = max(self.peak_rss_bytes.get(managed.name, 0), rss)

    def memory_report(self) -> dict[str, Any]:
        self.sample_memory()
        items = {
            managed.name: {
                "pid": managed.process.pid,
                "peak_rss_bytes": self.peak_rss_bytes.get(managed.name, 0),
                "peak_rss_mb": round(self.peak_rss_bytes.get(managed.name, 0) / 1024 / 1024, 2),
            }
            for managed in self.processes
        }
        total = sum(item["peak_rss_bytes"] for item in items.values())
        return {
            "scope": (
                "由评测脚本启动的 MinerU、backend 与 "
                f"{self.worker_script.name} 进程工作集，不含独立数据库和其他外部服务"
            ),
            "processes": items,
            "sum_of_process_peaks_bytes": total,
            "sum_of_process_peaks_mb": round(total / 1024 / 1024, 2),
        }

    def stop(self) -> None:
        for managed in reversed(self.processes):
            if managed.process.poll() is None:
                try:
                    managed.process.terminate()
                    managed.process.wait(timeout=10)
                except Exception:
                    try:
                        managed.process.kill()
                    except Exception:
                        pass
            managed.log_handle.close()
            print(f"[服务] 已停止 {managed.name}")


def detect_service_python(explicit: Optional[Path]) -> Path:
    if explicit:
        return explicit.resolve()
    env_path = os.environ.get("V9_SERVICE_PYTHON", "").strip()
    if env_path:
        return Path(env_path).resolve()
    preferred = Path(r"D:\Anaconda\envs\langchain0.3\python.exe")
    if preferred.exists():
        return preferred
    return Path(sys.executable).resolve()


def detect_mineru_executable(python_exe: Path) -> Path:
    candidates = []
    if os.name == "nt":
        candidates.extend(
            [
                python_exe.parent / "Scripts" / "mineru-api.exe",
                python_exe.parent / "mineru-api.exe",
            ]
        )
    else:
        candidates.extend(
            [
                python_exe.parent / "mineru-api",
                python_exe.parent / "bin" / "mineru-api",
            ]
        )
    discovered = shutil.which("mineru-api")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        f"未找到 mineru-api（服务 Python: {python_exe}）。"
        "可安装 MinerU，或增加 --local-docx-parser 使用本地 DOCX 解析兜底。"
    )


def process_rss_bytes(pid: int) -> Optional[int]:
    """Read one process working-set RSS using only the Python standard library."""
    if pid <= 0:
        return None
    if os.name == "nt":
        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        process_query_information = 0x0400
        process_vm_read = 0x0010
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_information | process_vm_read, False, int(pid)
        )
        if not handle:
            return None
        try:
            counters = ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            return int(counters.WorkingSetSize) if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    status_path = Path(f"/proc/{pid}/status")
    if status_path.exists():
        try:
            match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status_path.read_text(), re.MULTILINE)
            return int(match.group(1)) * 1024 if match else None
        except Exception:
            return None
    return None


def wait_for_job(
    client: V9ApiClient,
    job_id: str,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    sample_callback: Optional[Any] = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_signature: tuple[Any, ...] | None = None
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError(f"任务 {job_id} 超过 {timeout_seconds:.0f}s 未完成")
        status = client.status(job_id)
        if sample_callback:
            sample_callback()
        signature = (
            status.get("status"),
            status.get("progress"),
            status.get("n_done"),
            status.get("n_chunks"),
            status.get("agent_status"),
        )
        if signature != last_signature:
            print(
                f"[审查] {status.get('status')} {status.get('progress', 0)}% "
                f"({status.get('n_done', 0)}/{status.get('n_chunks', 0)}) "
                f"{status.get('agent_status') or ''}"
            )
            last_signature = signature
        if status.get("status") in TERMINAL_STATUSES:
            if status.get("status") != "done":
                raise RuntimeError(
                    f"任务 {job_id} 状态为 {status.get('status')}：{status.get('error') or status.get('agent_status')}"
                )
            return status
        time.sleep(max(0.2, poll_seconds))


def load_issues_json(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("issues", "items", "predictions"):
            if isinstance(payload.get(key), list):
                return payload[key]
    raise ValueError(f"无法从 {path} 读取问题列表；应为数组或包含 issues 数组的对象")


def outcome_label(value: str) -> str:
    return {
        "strict_hit": "严格命中",
        "detected_not_strict": "已发现但分类/判定不正确",
        "missed": "漏检",
    }.get(value, value)


def short(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "…"


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv_report(path: Path, result: dict[str, Any]) -> None:
    fields = [
        "record_kind",
        "error_id",
        "gold_type",
        "outcome",
        "gold_blocks",
        "gold_original",
        "gold_target",
        "match_score",
        "predicted_issue_id",
        "predicted_type",
        "predicted_status",
        "predicted_blocks",
        "predicted_original_text",
        "predicted_reason",
        "predicted_suggestion",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in result["case_results"]:
            writer.writerow({"record_kind": "gold_case", **row})
        for issue in result["unmatched_predictions"]:
            writer.writerow(
                {
                    "record_kind": "unmatched_prediction",
                    "outcome": "误报/未匹配",
                    "predicted_issue_id": issue.get("id"),
                    "predicted_type": issue.get("normalized_issue_type"),
                    "predicted_status": issue.get("status"),
                    "predicted_blocks": issue.get("block_indices"),
                    "predicted_original_text": issue.get("original_text"),
                    "predicted_reason": issue.get("reason"),
                    "predicted_suggestion": issue.get("suggestion"),
                }
            )


def write_html_report(path: Path, result: dict[str, Any], run_meta: dict[str, Any]) -> None:
    metrics = result["metrics"]
    type_labels = {"compliance": "合规性", "typo": "错别字", "redundancy": "重复"}

    category_rows = []
    for category in EXPECTED_TYPES:
        item = metrics["categories"][category]
        category_rows.append(
            "<tr>"
            f"<td>{type_labels[category]}</td>"
            f"<td>{item['gold_count']}</td><td>{item['predicted_count']}</td>"
            f"<td>{item['detected_count']}</td><td>{item['strict_tp']}</td>"
            f"<td>{pct(item['detection_recall'])}</td>"
            f"<td>{pct(item['strict_precision'])}</td>"
            f"<td>{pct(item['strict_recall'])}</td>"
            f"<td>{pct(item['strict_f1'])}</td>"
            "</tr>"
        )

    case_rows = []
    for row in result["case_results"]:
        css = row["outcome"]
        case_rows.append(
            f"<tr class='{css}'>"
            f"<td>{html.escape(row['error_id'])}</td>"
            f"<td>{type_labels.get(row['gold_type'], row['gold_type'])}</td>"
            f"<td><span class='pill {css}'>{outcome_label(row['outcome'])}</span></td>"
            f"<td>{html.escape(str(row['gold_blocks']))}</td>"
            f"<td>{html.escape(short(row['gold_target']))}</td>"
            f"<td>{html.escape(str(row.get('predicted_issue_id') or ''))}</td>"
            f"<td>{html.escape(row.get('predicted_raw_type') or '')}<br>"
            f"<span class='muted'>{html.escape(row.get('predicted_status') or '')}</span></td>"
            f"<td>{html.escape(short(row.get('predicted_original_text')))}</td>"
            f"<td>{row['match_score']:.3f}</td>"
            "</tr>"
        )

    unmatched_rows = []
    for issue in result["unmatched_predictions"]:
        unmatched_rows.append(
            "<tr>"
            f"<td>{html.escape(str(issue.get('id') or ''))}</td>"
            f"<td>{html.escape(str(issue.get('issue_type') or ''))}</td>"
            f"<td>{html.escape(str(issue.get('status') or ''))}</td>"
            f"<td>{html.escape(str(issue.get('block_indices') or ''))}</td>"
            f"<td>{html.escape(short(issue.get('original_text')))}</td>"
            f"<td>{html.escape(short(issue.get('reason')))}</td>"
            "</tr>"
        )
    if not unmatched_rows:
        unmatched_rows.append("<tr><td colspan='6' class='muted'>无未匹配系统问题</td></tr>")

    timings = run_meta.get("job_status", {}).get("timings") or {}
    timing_text = html.escape(json.dumps(timings, ensure_ascii=False)) if timings else "未提供"
    process_memory = run_meta.get("process_memory") or {}
    memory_text = (
        f"MinerU + backend + worker 峰值工作集之和：{process_memory.get('sum_of_process_peaks_mb')} MB"
        if process_memory
        else "未采样（仅在 --manage-services 模式采样）"
    )
    report = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>S1302 人工错误注入自动评测</title>
<style>
body{{font-family:"Microsoft YaHei",Arial,sans-serif;margin:0;background:#f4f6f8;color:#1f2937}}
.wrap{{max-width:1500px;margin:0 auto;padding:28px}} h1{{margin:0 0 6px;font-size:28px}}
.subtitle,.muted{{color:#6b7280}} .cards{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:22px 0}}
.card{{background:white;border:1px solid #dfe3e8;border-radius:10px;padding:16px;box-shadow:0 1px 2px #00000008}}
.card b{{display:block;font-size:25px;color:#1d4ed8;margin-top:6px}} section{{background:white;border:1px solid #dfe3e8;border-radius:10px;padding:18px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #d8dee6;padding:8px;vertical-align:top;text-align:left}}
th{{background:#e8eef5;color:#1f4d78;position:sticky;top:0}} tr.strict_hit td{{background:#f0fdf4}} tr.detected_not_strict td{{background:#fff7ed}} tr.missed td{{background:#fef2f2}}
.pill{{white-space:nowrap;border-radius:999px;padding:3px 8px;font-weight:700}} .pill.strict_hit{{background:#dcfce7;color:#166534}}
.pill.detected_not_strict{{background:#ffedd5;color:#9a3412}} .pill.missed{{background:#fee2e2;color:#991b1b}}
.meta{{font-size:13px;line-height:1.7;background:#eef2ff;border-left:4px solid #4f46e5;padding:10px 14px;margin-top:14px}}
@media(max-width:1100px){{.cards{{grid-template-columns:repeat(3,1fr)}}}}
</style></head><body><div class="wrap">
<h1>S1302 人工错误注入自动评测</h1>
<div class="subtitle">严格指标要求错误位置、类型和最终判定均正确；发现指标仅要求与金标内容或位置匹配。</div>
<div class="meta">任务：{html.escape(str(run_meta.get('job_id') or '离线结果'))}<br>
文档：{html.escape(str(run_meta.get('document') or ''))}<br>
耗时：{timing_text}<br>内存：{html.escape(memory_text)}<br>
评测墙钟时间：{run_meta.get('evaluation_wall_seconds', 0):.2f}s<br>生成时间：{html.escape(result['generated_at'])}</div>
<div class="cards">
<div class="card">金标错误<b>{metrics['gold_count']}</b></div>
<div class="card">系统问题<b>{metrics['predicted_count']}</b></div>
<div class="card">发现召回率<b>{pct(metrics['detection_recall'])}</b></div>
<div class="card">严格精确率<b>{pct(metrics['strict_precision'])}</b></div>
<div class="card">严格召回率<b>{pct(metrics['strict_recall'])}</b></div>
<div class="card">严格 F1<b>{pct(metrics['strict_f1'])}</b></div>
</div>
<section><h2>分类指标</h2><table><thead><tr><th>类别</th><th>金标数</th><th>系统数</th><th>发现数</th><th>严格命中</th><th>发现召回率</th><th>严格精确率</th><th>严格召回率</th><th>严格F1</th></tr></thead>
<tbody>{''.join(category_rows)}</tbody></table></section>
<section><h2>30项金标逐项核对</h2><table><thead><tr><th>编号</th><th>类别</th><th>结果</th><th>预期块</th><th>注入错误</th><th>系统问题ID</th><th>系统类型/状态</th><th>系统定位原文</th><th>匹配分</th></tr></thead>
<tbody>{''.join(case_rows)}</tbody></table></section>
<section><h2>未匹配系统问题（误报候选）</h2><table><thead><tr><th>ID</th><th>类型</th><th>状态</th><th>块</th><th>原文</th><th>原因</th></tr></thead>
<tbody>{''.join(unmatched_rows)}</tbody></table></section>
</div></body></html>"""
    path.write_text(report, encoding="utf-8")


def save_reports(
    report_dir: Path,
    result: dict[str, Any],
    run_meta: dict[str, Any],
) -> dict[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "s1302_manual_error_eval_v1.json"
    csv_path = report_dir / "s1302_manual_error_eval_v1.csv"
    html_path = report_dir / "s1302_manual_error_eval_v1.html"
    issues_path = report_dir / "s1302_manual_error_issues_raw_v1.json"
    payload = {**result, "run": run_meta}
    write_json(json_path, payload)
    write_csv_report(csv_path, result)
    write_html_report(html_path, result, run_meta)
    write_json(issues_path, {"job_id": run_meta.get("job_id"), "issues": result["issues"]})
    return {
        "json": str(json_path.resolve()),
        "csv": str(csv_path.resolve()),
        "html": str(html_path.resolve()),
        "issues_raw": str(issues_path.resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="提交或读取 V9 审查结果，与 S1302 的30项人工错误金标自动比对。"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--job-id", help="评估已有任务；若任务未完成会等待")
    source.add_argument("--issues-json", type=Path, help="离线评估问题 JSON，不访问后端")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT)
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--mine-type", default="non_outburst")
    parser.add_argument("--user-id", default="manual-eval")
    parser.add_argument("--kb-id", type=int)
    parser.add_argument("--threshold", type=float, default=0.56)
    parser.add_argument("--timeout", type=float, default=7200.0, help="审查最长等待秒数")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--manage-services", action="store_true", help="自动启动并停止 MinerU + backend + worker")
    parser.add_argument("--keep-services", action="store_true", help="自动启动后在评测结束时不停止服务")
    parser.add_argument("--service-python", type=Path, help="启动服务所用 Python")
    parser.add_argument(
        "--worker-script",
        type=Path,
        default=Path("v9_worker.py"),
        help="受管Worker入口脚本（v10使用v10_worker.py）",
    )
    parser.add_argument(
        "--local-docx-parser",
        action="store_true",
        help="不启动 MinerU，令本次自动启动的 worker 使用旧版 docx_adapter 直切路径",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    configure_stdout()
    evaluation_started = time.monotonic()
    args = build_parser().parse_args(argv)
    if not 0.0 < args.threshold <= 1.0:
        raise ValueError("--threshold 必须在 (0, 1] 范围内")
    gold_path = args.gold.resolve()
    if not gold_path.exists():
        raise FileNotFoundError(gold_path)
    gold = json.loads(gold_path.read_text(encoding="utf-8"))
    if len(gold.get("cases") or []) != int(gold.get("counts", {}).get("total", 0)):
        raise ValueError("金标 counts.total 与 cases 数量不一致")

    manager: ServiceManager | None = None
    client = V9ApiClient(args.base_url)
    run_meta: dict[str, Any] = {
        "mode": "offline" if args.issues_json else "existing_job" if args.job_id else "submitted_job",
        "base_url": args.base_url,
        "document": str(args.document.resolve()),
        "gold": str(gold_path),
        "job_id": args.job_id,
    }
    try:
        if args.manage_services:
            manager = ServiceManager(
                detect_service_python(args.service_python),
                args.report_dir.resolve(),
                args.base_url,
                use_mineru=not args.local_docx_parser,
                worker_script=args.worker_script,
            )
            manager.start()
            manager.wait_backend()

        if args.issues_json:
            issues = load_issues_json(args.issues_json.resolve())
            run_meta["issues_json"] = str(args.issues_json.resolve())
        else:
            job_id = args.job_id
            if not job_id:
                document = args.document.resolve()
                if not document.exists():
                    raise FileNotFoundError(document)
                print(f"[上传] {document}")
                preprocess = client.preprocess(document)
                preprocess_id = preprocess.get("preprocess_id")
                if not preprocess_id or not preprocess.get("docx_ready"):
                    raise RuntimeError(f"预处理失败：{preprocess}")
                print(f"[预处理] {preprocess_id} ready")
                started = client.start(
                    preprocess_id,
                    mine_type=args.mine_type,
                    user_id=args.user_id,
                    kb_id=args.kb_id,
                )
                job_id = str(started["job_id"])
                run_meta["preprocess_id"] = preprocess_id
                run_meta["job_id"] = job_id
                print(f"[任务] {job_id}")
            try:
                status = wait_for_job(
                    client,
                    job_id,
                    timeout_seconds=args.timeout,
                    poll_seconds=args.poll_seconds,
                    sample_callback=manager.sample_memory if manager else None,
                )
            except KeyboardInterrupt:
                client.cancel(job_id)
                raise
            run_meta["job_status"] = status
            issues = client.issues(job_id)
            print(f"[结果] 系统返回 {len(issues)} 条问题")

        result = evaluate(gold, issues, threshold=args.threshold)
        run_meta["evaluation_wall_seconds"] = round(time.monotonic() - evaluation_started, 2)
        if manager:
            run_meta["process_memory"] = manager.memory_report()
        paths = save_reports(args.report_dir.resolve(), result, run_meta)
        metrics = result["metrics"]
        print("\n===== S1302 人工错误注入评测 =====")
        print(f"金标/系统问题: {metrics['gold_count']}/{metrics['predicted_count']}")
        print(f"发现召回率: {pct(metrics['detection_recall'])}")
        print(
            f"严格 P/R/F1: {pct(metrics['strict_precision'])} / "
            f"{pct(metrics['strict_recall'])} / {pct(metrics['strict_f1'])}"
        )
        for category in EXPECTED_TYPES:
            item = metrics["categories"][category]
            print(
                f"- {category}: strict={item['strict_tp']}/{item['gold_count']}, "
                f"R={pct(item['strict_recall'])}, P={pct(item['strict_precision'])}"
            )
        print("报告：")
        for kind, path in paths.items():
            print(f"- {kind}: {path}")
        return 0
    finally:
        if manager and not args.keep_services:
            manager.stop()


if __name__ == "__main__":
    raise SystemExit(main())
