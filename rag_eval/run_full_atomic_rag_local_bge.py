"""Run the complete atomic RAG pipeline with the downloaded local BGE model.

Open this file in the IDE and click Run. No command-line arguments are needed.

Required Python interpreter:
    D:\Anaconda\envs\langchain0.3\python.exe

Required local model:
    models\bge-reranker-v2-m3
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_SCRIPT = PROJECT_ROOT / "rag_eval" / "run_atomic_rag_pipeline_v1.py"
LOCAL_RERANKER = PROJECT_ROOT / "models" / "bge-reranker-v2-m3"


def validate_runtime() -> None:
    expected_environment = "langchain0.3"
    if expected_environment.lower() not in sys.executable.lower():
        raise RuntimeError(
            "当前IDE未使用langchain0.3环境。\n"
            "请将Python解释器切换为：\n"
            r"D:\Anaconda\envs\langchain0.3\python.exe"
        )

    required_files = (
        "config.json",
        "model.safetensors",
        "tokenizer_config.json",
    )
    missing = [name for name in required_files if not (LOCAL_RERANKER / name).is_file()]
    if missing:
        raise FileNotFoundError(
            f"本地BGE模型不完整：{LOCAL_RERANKER}\n"
            f"缺少文件：{', '.join(missing)}"
        )

    if not os.getenv("DASHSCOPE_API_KEY"):
        raise RuntimeError(
            "当前IDE运行环境中没有DASHSCOPE_API_KEY，无法执行Dense检索。\n"
            "请在IDE运行配置中设置DASHSCOPE_API_KEY。"
        )


def main() -> None:
    validate_runtime()

    # Prevent Transformers/FlagEmbedding from accessing Hugging Face.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    sys.argv = [
        str(PIPELINE_SCRIPT),
        "--mode",
        "full",
        "--model-provider",
        "local",
        "--reranker-model",
        str(LOCAL_RERANKER),
        "--per-claim-pool",
        "3",
    ]

    print("启动完整原子RAG流程")
    print(f"Python解释器：{sys.executable}")
    print(f"本地BGE模型：{LOCAL_RERANKER}")
    print("模型提供方：local，不访问ModelScope或Hugging Face")
    print("运行范围：完整354个检索单元\n")
    print("父级合并池：每个原子主张仅保留 Top3\n")

    runpy.run_path(str(PIPELINE_SCRIPT), run_name="__main__")


if __name__ == "__main__":
    main()
