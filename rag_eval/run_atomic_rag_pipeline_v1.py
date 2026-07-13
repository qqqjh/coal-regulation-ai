"""Run the atomic-query RAG evaluation pipeline step by step.

Run this file with the langchain0.3 environment:

    conda run -n langchain0.3 python rag_eval/run_atomic_rag_pipeline_v1.py

The default mode is a 3-claim smoke test. Use --mode full after the smoke test
succeeds. Existing atomic claims are reused by default so manual edits are not
overwritten. Reranker models are downloaded from ModelScope by default and
then passed to FlagEmbedding as a complete local directory.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RAG_EVAL_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = RAG_EVAL_DIR / "scripts"
DATA_DIR = RAG_EVAL_DIR / "data"
REPORT_DIR = RAG_EVAL_DIR / "reports"

CLAIMS_PATH = DATA_DIR / "gold_atomic_claims_v9_v1.json"
FULL_RETRIEVAL_PATH = DATA_DIR / "atomic_retrieval_results_v9_v1.json"
SMOKE_RETRIEVAL_PATH = DATA_DIR / "atomic_retrieval_bge_smoke.json"
DEFAULT_MODELSCOPE_CACHE = (
    Path("D:/models/modelscope")
    if Path("D:/").exists()
    else Path.home() / ".cache" / "modelscope"
)


def run_step(
    name: str,
    script: Path,
    arguments: Sequence[object] = (),
    environment: dict[str, str] | None = None,
) -> None:
    command = [sys.executable, str(script), *map(str, arguments)]
    print("\n" + "=" * 68, flush=True)
    print(f"STEP: {name}", flush=True)
    print("=" * 68, flush=True)
    print(" ".join(command), flush=True)
    subprocess.run(command, cwd=PROJECT_ROOT, check=True, env=environment)


def verify_environment(skip_dense: bool) -> None:
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("PyTorch is not installed in the current Python environment.") from exc

    print(f"Python: {sys.executable}")
    print(f"Torch: {torch.__version__}")
    print(f"CUDA available: {torch.cuda.is_available()}")
    print(f"Device: {torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable. Run this pipeline in the configured langchain0.3 GPU environment.")
    if not skip_dense and not os.getenv("DASHSCOPE_API_KEY"):
        raise RuntimeError("DASHSCOPE_API_KEY is required for Dense retrieval. Set it or use --skip-dense.")


def validate_local_reranker_model(model_path: Path) -> str:
    if not model_path.is_dir():
        raise FileNotFoundError(f"Local reranker directory does not exist: {model_path}")

    required = ("config.json", "model.safetensors")
    missing = [name for name in required if not (model_path / name).is_file()]
    tokenizer_exists = any(
        (model_path / name).is_file()
        for name in ("tokenizer.json", "tokenizer_config.json", "sentencepiece.bpe.model")
    )
    if missing or not tokenizer_exists:
        missing_text = ", ".join(missing + ([] if tokenizer_exists else ["tokenizer files"]))
        raise FileNotFoundError(
            f"Local reranker directory is incomplete: {model_path}\n"
            f"Missing: {missing_text}"
        )
    return str(model_path.resolve())


def resolve_reranker_model(
    reranker: str,
    model: str,
    provider: str,
    modelscope_cache_dir: str,
) -> str:
    if reranker == "none":
        return model

    looks_like_local_path = (
        Path(model).is_absolute()
        or "\\" in model
        or model.startswith(".")
    )
    if looks_like_local_path:
        return validate_local_reranker_model(Path(model).expanduser())

    if provider != "modelscope":
        raise ValueError(
            "Only ModelScope or a complete local model directory is allowed. "
            "Use --model-provider modelscope or pass a local path."
        )

    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "ModelScope SDK is required. Install it with: "
            "conda run -n langchain0.3 python -m pip install modelscope"
        ) from exc

    print(f"Resolving reranker from ModelScope: {model}", flush=True)
    download_kwargs = {"model_id": model}
    if modelscope_cache_dir:
        download_kwargs["cache_dir"] = str(Path(modelscope_cache_dir).expanduser())
    local_directory = Path(snapshot_download(**download_kwargs))
    print(f"ModelScope local snapshot: {local_directory}", flush=True)
    return validate_local_reranker_model(local_directory)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "full"), default="smoke")
    parser.add_argument(
        "--reranker",
        choices=("flagembedding", "cross-encoder", "qwen3", "none"),
        default="flagembedding",
    )
    parser.add_argument("--reranker-model", default="AI-ModelScope/bge-reranker-v2-m3")
    parser.add_argument("--model-provider", choices=("modelscope", "local"), default="modelscope")
    parser.add_argument(
        "--modelscope-cache-dir",
        default=os.getenv("MODELSCOPE_CACHE", str(DEFAULT_MODELSCOPE_CACHE)),
        help="ModelScope cache root. Defaults to D:/models/modelscope on this machine.",
    )
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--final-claim-top-k", type=int, default=15)
    parser.add_argument("--parent-top-k", type=int, default=25)
    parser.add_argument("--per-claim-pool", type=int, default=3)
    parser.add_argument("--smoke-claims", type=int, default=3)
    parser.add_argument("--regenerate-claims", action="store_true")
    parser.add_argument("--skip-dense", action="store_true")
    parser.add_argument("--skip-gap-registry", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    retrieval_path = FULL_RETRIEVAL_PATH if args.mode == "full" else SMOKE_RETRIEVAL_PATH
    parent_output = DATA_DIR / (
        "parent_chunk_evidence_v9_v1.json"
        if args.mode == "full"
        else "parent_chunk_evidence_smoke_v9_v1.json"
    )
    parent_html = REPORT_DIR / (
        "parent_chunk_evidence_review_v9_v1.html"
        if args.mode == "full"
        else "parent_chunk_evidence_smoke_review_v9_v1.html"
    )

    print("Atomic RAG pipeline v1")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Mode: {args.mode}")
    print(f"Dense enabled: {not args.skip_dense}")
    print(f"Reranker: {args.reranker}")
    print(f"Reranker model: {args.reranker_model}")
    print(f"Model provider: {args.model_provider}")
    if args.model_provider == "modelscope":
        print(f"ModelScope cache: {args.modelscope_cache_dir}")
    verify_environment(args.skip_dense)
    args.reranker_model = resolve_reranker_model(
        args.reranker,
        args.reranker_model,
        args.model_provider,
        args.modelscope_cache_dir,
    )
    child_environment = os.environ.copy()
    child_environment["HF_HUB_OFFLINE"] = "1"
    child_environment["TRANSFORMERS_OFFLINE"] = "1"

    if args.regenerate_claims:
        run_step("Regenerate retrieval query units", SCRIPT_DIR / "prepare_atomic_gold_v1.py")
    elif not CLAIMS_PATH.exists():
        raise FileNotFoundError(
            f"Atomic claim dataset not found: {CLAIMS_PATH}. Run with --regenerate-claims."
        )
    else:
        print(f"Using existing retrieval query units: {CLAIMS_PATH}")
        print("Use --regenerate-claims only when automatic extraction should overwrite this file.")

    run_step(
        "Build retrieval-query review HTML",
        SCRIPT_DIR / "build_atomic_claim_review_html_v1.py",
        ("--input", CLAIMS_PATH, "--output", REPORT_DIR / "atomic_claim_review_v9_v1.html"),
    )

    retrieval_arguments: list[str | Path] = [
        "--claims", CLAIMS_PATH,
        "--output", retrieval_path,
        "--top-k", str(args.top_k),
        "--final-top-k", str(args.final_claim_top_k),
        "--reranker", args.reranker,
    ]
    if args.reranker != "none":
        retrieval_arguments.extend(("--reranker-model", args.reranker_model))
    if not args.skip_dense:
        retrieval_arguments.append("--dense")
    if args.mode == "smoke":
        retrieval_arguments.extend(("--max-claims", str(args.smoke_claims)))

    run_step(
        "Retrieve and rerank atomic query units",
        SCRIPT_DIR / "retrieve_atomic_claims_v1.py",
        retrieval_arguments,
        environment=child_environment,
    )

    run_step(
        "Merge evidence back to parent chunks",
        SCRIPT_DIR / "merge_claim_evidence_to_parent_v1.py",
        (
            "--claims", CLAIMS_PATH,
            "--retrieval", retrieval_path,
            "--output", parent_output,
            "--html", parent_html,
            "--per-claim-pool", str(args.per_claim_pool),
            "--final-top-k", str(args.parent_top_k),
        ),
    )

    if not args.skip_gap_registry:
        run_step(
            "Build knowledge-base gap registry",
            SCRIPT_DIR / "build_kb_gap_registry_v1.py",
            ("--input", CLAIMS_PATH, "--output", DATA_DIR / "kb_gap_registry_v1.json"),
        )

    summary = {
        "retrieval_output": str(retrieval_path),
        "parent_evidence_output": str(parent_output),
        "parent_evidence_review": str(parent_html),
        "atomic_query_review": str(REPORT_DIR / "atomic_claim_review_v9_v1.html"),
    }
    print("\nPipeline completed.")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
