"""v10 Web Worker 显式入口。

复用 v9 的前后端桥接、任务队列和反馈处理，只强制把审核引擎切换为
``HybridRAGReviewerV10``。使用独立入口可以避免 PowerShell 环境变量在多层
子进程启动时丢失。
"""
from __future__ import annotations

import os

import v9_worker


class V10Worker(v9_worker.V9Worker):
    """v10 审核引擎 + 已有 Web 队列、SSE、反馈回写能力。"""


def configure_runtime() -> bool:
    """配置 v10 运行时，返回是否显式启用v9历史异步升级线程。"""
    legacy_loop_enabled = os.getenv(
        "V10_ENABLE_LEGACY_ESCALATION_LOOP", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    # v10 的初审/复核分歧已在主链内同步交给主智能体裁决。默认不再消费
    # review_queue_v9.db 中积压的历史升级项，避免旧错误重试占用LLM额度。
    v9_worker.DISABLE_ESCALATION_LOOP = not legacy_loop_enabled
    return legacy_loop_enabled


def main() -> None:
    v9_worker.REVIEW_ENGINE_VERSION = "v10"
    legacy_loop_enabled = configure_runtime()
    print(
        "[worker] REVIEW_ENGINE_VERSION=v10; "
        "conflict_adjudication=synchronous-main-agent; "
        f"legacy_escalation_loop={'enabled' if legacy_loop_enabled else 'disabled'}",
        flush=True,
    )
    V10Worker().run()


if __name__ == "__main__":
    main()
