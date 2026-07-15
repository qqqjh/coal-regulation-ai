"""Download the BGE reranker from ModelScope with visible terminal progress.

Recommended command:

    conda run --no-capture-output -n langchain0.3 python rag_eval/download_reranker_modelscope.py

The download is resumable. Files are placed in a normal local model directory
that can be passed directly to FlagEmbedding and Transformers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = "AI-ModelScope/bge-reranker-v2-m3"
DEFAULT_OUTPUT = PROJECT_ROOT / "models" / "bge-reranker-v2-m3"
DEFAULT_CACHE = PROJECT_ROOT / "models" / "modelscope-cache"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def inspect_model_directory(path: Path) -> dict[str, Any]:
    required = ["config.json", "model.safetensors"]
    tokenizer_candidates = [
        "tokenizer.json",
        "tokenizer_config.json",
        "sentencepiece.bpe.model",
    ]
    missing = [name for name in required if not (path / name).is_file()]
    tokenizer_files = [name for name in tokenizer_candidates if (path / name).is_file()]
    if not tokenizer_files:
        missing.append("tokenizer files")

    files = [
        {
            "name": str(file.relative_to(path)),
            "size": file.stat().st_size,
        }
        for file in path.rglob("*")
        if file.is_file()
    ]
    return {
        "path": str(path.resolve()),
        "file_count": len(files),
        "total_size": sum(file["size"] for file in files),
        "missing": missing,
        "files": sorted(files, key=lambda item: item["size"], reverse=True),
    }


def print_inspection(result: dict[str, Any]) -> None:
    print("\nModel directory inspection:", flush=True)
    print(f"  Path: {result['path']}", flush=True)
    print(f"  Files: {result['file_count']}", flush=True)
    print(f"  Total size: {human_size(result['total_size'])}", flush=True)
    print("  Largest files:", flush=True)
    for file in result["files"][:8]:
        print(f"    {human_size(file['size']):>10}  {file['name']}", flush=True)

    if result["missing"]:
        print(f"  Missing: {', '.join(result['missing'])}", flush=True)
    else:
        print("  Status: complete", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Only inspect the output directory; do not download.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    cache_dir = args.cache_dir.expanduser().resolve()

    print("ModelScope reranker downloader", flush=True)
    print(f"Python: {sys.executable}", flush=True)
    print(f"Model ID: {args.model_id}", flush=True)
    print(f"Output directory: {output_dir}", flush=True)
    print(f"Download cache: {cache_dir}", flush=True)
    print(f"Workers: {args.max_workers}", flush=True)
    print("Existing partial files will be reused when ModelScope supports resume.", flush=True)

    if args.inspect_only:
        if not output_dir.is_dir():
            print(f"\nModel directory does not exist: {output_dir}", flush=True)
            raise SystemExit(2)
        result = inspect_model_directory(output_dir)
        print_inspection(result)
        if result["missing"]:
            raise SystemExit(2)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MODELSCOPE_CACHE"] = str(cache_dir)
    os.environ.setdefault("MODELSCOPE_DOMAIN", "www.modelscope.cn")

    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "ModelScope is not installed. Run: "
            "conda run -n langchain0.3 python -m pip install modelscope"
        ) from exc

    print("\nStarting ModelScope download. Progress should appear below:\n", flush=True)
    downloaded_path = snapshot_download(
        model_id=args.model_id,
        cache_dir=str(cache_dir),
        local_dir=str(output_dir),
        max_workers=args.max_workers,
    )
    print(f"\nModelScope returned: {downloaded_path}", flush=True)

    result = inspect_model_directory(output_dir)
    print_inspection(result)
    inspection_path = output_dir / "download_inspection.json"
    inspection_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Inspection report: {inspection_path}", flush=True)

    if result["missing"]:
        raise RuntimeError(
            "Download finished but the model directory is incomplete. "
            f"Missing: {', '.join(result['missing'])}. Run this script again to resume."
        )

    print("\nDownload complete. Use this pipeline command:", flush=True)
    print(
        "conda run --no-capture-output -n langchain0.3 python "
        "rag_eval/run_atomic_rag_pipeline_v1.py "
        f"--reranker-model \"{output_dir}\" --model-provider local",
        flush=True,
    )


if __name__ == "__main__":
    main()
