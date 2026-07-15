"""Capture provider-reported LLM token usage and persist monitor records.

The OpenAI-compatible response ``usage`` object is the source of truth.  This
module deliberately does not estimate tokens from text length when a provider
does not return usage, because an estimate must not be presented as real usage.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from openai import OpenAI


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MONITOR_DB = PROJECT_ROOT / "backend" / "data" / "app.db"


def _int_attr(value: Any, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        raw = value.get(name, 0)
    else:
        raw = getattr(value, name, 0)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


class UsageTracker:
    """Thread-safe aggregate for one review job or one auxiliary LLM action."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls = 0
        self._usage_responses = 0
        self._responses_without_usage = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._cached_tokens = 0
        self._reasoning_tokens = 0

    def record_response(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        with self._lock:
            self._calls += 1
            if usage is None:
                self._responses_without_usage += 1
                return

            prompt_tokens = _int_attr(usage, "prompt_tokens")
            completion_tokens = _int_attr(usage, "completion_tokens")
            total_tokens = _int_attr(usage, "total_tokens")
            if total_tokens <= 0 and (prompt_tokens or completion_tokens):
                total_tokens = prompt_tokens + completion_tokens

            prompt_details = (
                usage.get("prompt_tokens_details")
                if isinstance(usage, dict)
                else getattr(usage, "prompt_tokens_details", None)
            )
            completion_details = (
                usage.get("completion_tokens_details")
                if isinstance(usage, dict)
                else getattr(usage, "completion_tokens_details", None)
            )

            self._usage_responses += 1
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._total_tokens += total_tokens
            self._cached_tokens += _int_attr(prompt_details, "cached_tokens")
            self._reasoning_tokens += _int_attr(completion_details, "reasoning_tokens")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "calls": self._calls,
                "usage_responses": self._usage_responses,
                "responses_without_usage": self._responses_without_usage,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._total_tokens,
                "cached_tokens": self._cached_tokens,
                "reasoning_tokens": self._reasoning_tokens,
                "usage_source": (
                    "provider"
                    if self._usage_responses > 0
                    else "provider_unavailable"
                ),
            }


class _TrackedCompletions:
    def __init__(self, completions: Any, tracker: UsageTracker) -> None:
        self._completions = completions
        self._tracker = tracker

    def create(self, *args, **kwargs):
        response = self._completions.create(*args, **kwargs)
        self._tracker.record_response(response)
        return response


class _TrackedChat:
    def __init__(self, chat: Any, tracker: UsageTracker) -> None:
        self._chat = chat
        self.completions = _TrackedCompletions(chat.completions, tracker)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class TrackedOpenAI:
    """Small proxy that preserves the OpenAI client API while tracking usage."""

    def __init__(self, *, tracker: UsageTracker, client: Any = None, **client_kwargs) -> None:
        self._client = client or OpenAI(**client_kwargs)
        self.chat = _TrackedChat(self._client.chat, tracker)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def write_monitor_usage(
    tracker: UsageTracker,
    *,
    module: str,
    operation: str,
    status: str,
    latency: float,
    metadata: Optional[Dict[str, Any]] = None,
    error_msg: str = "",
    cost: float = 0.0,
    db_path: Path = DEFAULT_MONITOR_DB,
) -> int:
    """Write one aggregated usage row into the backend monitor SQLite DB."""

    usage = tracker.snapshot()
    extra = dict(metadata or {})
    extra.update(usage)
    # Standard OpenAI-compatible usage does not include money.  Cost remains
    # zero until a profile-specific input/output price is configured.
    extra.setdefault("cost_calculated", bool(cost))

    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path), timeout=30) as conn:
        cursor = conn.execute(
            """
            INSERT INTO monitor_logs
                (module, operation, status, tokens, latency, cost,
                 timestamp, error_msg, extra_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                module,
                operation,
                status,
                int(usage["total_tokens"]),
                float(latency or 0.0),
                float(cost or 0.0),
                datetime.utcnow(),
                (error_msg or None),
                json.dumps(extra, ensure_ascii=False),
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
