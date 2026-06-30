"""MinerU 文档解析适配层。

用途：
  1. 把 PDF / DOCX / DOC 交给 MinerU 转成项目既有的 MinerU middle JSON。
  2. 将生成的 `{原文件名}_middle.json` 复制为本项目约定的
     `MinerU_<文档名>__<timestamp>.json`。
  3. 可选：对待审文档继续调用 `pending_doc_chunking_v9.py` 的 chunker。

推荐部署方式：
  - 开发期：本地安装 MinerU CLI，模型源用 ModelScope。
  - 生产期：启动常驻 `mineru-api`，本文件通过 `--api-url` 复用服务，避免每次 CLI
    启动临时服务。

注意：
  - MinerU 3.x 支持 PDF / image / DOCX / PPTX / XLSX；`.doc` 不是原生输入，
    本文件会先用 Word COM 转成 `.docx`。
  - 当前项目的 `pending_doc_chunking_v9.py` 依赖 MinerU pipeline/office middle
    JSON 中的 `pdf_info -> para_blocks`。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_MINERU_WORK_DIR = PROJECT_ROOT / "data" / "mineru_work"
DEFAULT_PENDING_JSON_DIR = PROJECT_ROOT / "new_docs" / "test_doc_json"
DEFAULT_RULE_JSON_DIR = PROJECT_ROOT / "new_docs" / "rule_docs_json"
SUPPORTED_INPUT_SUFFIXES = {".pdf", ".docx", ".doc"}


@dataclass
class MinerUConvertResult:
    source_path: str
    normalized_input_path: str
    mineru_output_dir: str
    mineru_middle_json: str
    project_json_path: str
    backend: str
    method: str
    model_source: str
    api_url: str
    doc_kind: str
    chunk_count: int = 0
    chunks_preview_path: str = ""


def _sanitize_doc_name(path: Path) -> str:
    name = path.stem.strip()
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    return name or "document"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _convert_doc_to_docx(source: Path, target: Path) -> None:
    """用本机 Word COM 将 .doc 转 .docx。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(str(source.resolve()), ReadOnly=True, AddToRecentFiles=False)
        try:
            doc.SaveAs2(str(target.resolve()), FileFormat=16)
        except Exception as exc:
            if target.exists() and target.stat().st_size > 0:
                print(f"[mineru] Word SaveAs2 返回异常但 .docx 已生成，继续处理: {exc}")
            else:
                raise RuntimeError(f".doc 转 .docx 失败：{exc}") from exc
        if not target.exists() or target.stat().st_size == 0:
            raise RuntimeError(".doc 转 .docx 失败：未生成有效 .docx")
    finally:
        if doc is not None:
            try:
                doc.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def normalize_input_for_mineru(input_path: Path, work_dir: Path) -> Path:
    input_path = input_path.resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    suffix = input_path.suffix.lower()
    if suffix not in SUPPORTED_INPUT_SUFFIXES:
        raise ValueError(f"暂只接入 PDF / DOCX / DOC: {input_path.name}")

    if suffix != ".doc":
        return input_path

    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"{_sanitize_doc_name(input_path)}__{_timestamp()}.docx"
    _convert_doc_to_docx(input_path, target)
    return target.resolve()


def _mineru_executable() -> str:
    executable = shutil.which("mineru")
    if executable:
        return executable
    scripts_dir = Path(sys.executable).resolve().parent / "Scripts"
    if os.name == "nt":
        candidate = scripts_dir / "mineru.exe"
    else:
        candidate = scripts_dir / "mineru"
    if candidate.exists():
        return str(candidate)
    raise FileNotFoundError(
        "未找到 mineru 命令。请在当前 Python 环境安装："
        "python -m pip install mineru"
    )


def run_mineru_cli(
    input_path: Path,
    output_dir: Path,
    *,
    backend: str = "pipeline",
    method: str = "auto",
    lang: str = "ch",
    model_source: str = "modelscope",
    api_url: str = "",
    timeout: int = 3600,
) -> Path:
    """运行 MinerU CLI，返回本次输出目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        _mineru_executable(),
        "-p",
        str(input_path),
        "-o",
        str(output_dir),
        "-b",
        backend,
        "-m",
        method,
    ]
    if lang:
        cmd.extend(["-l", lang])
    if api_url:
        cmd.extend(["--api-url", api_url])

    env = os.environ.copy()
    if model_source:
        env["MINERU_MODEL_SOURCE"] = model_source

    marker = datetime.now().timestamp()
    print("[mineru] running:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env, check=True, timeout=timeout)

    candidates = [
        item for item in output_dir.rglob("*_middle.json")
        if item.stat().st_mtime >= marker - 2
    ]
    if not candidates:
        candidates = list(output_dir.rglob("*_middle.json"))
    if not candidates:
        raise FileNotFoundError(f"MinerU 未生成 *_middle.json: {output_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _extract_middle_json_from_api_response(response_json: Dict[str, Any]) -> Dict[str, Any]:
    results = response_json.get("results")
    if not isinstance(results, dict) or not results:
        raise ValueError("MinerU API 响应缺少 results")
    first_result = next(iter(results.values()))
    if not isinstance(first_result, dict):
        raise ValueError("MinerU API results 结构异常")
    middle_json = first_result.get("middle_json")
    if isinstance(middle_json, str):
        middle_json = json.loads(middle_json)
    if not isinstance(middle_json, dict):
        raise ValueError("MinerU API 响应缺少 middle_json")
    if "pdf_info" not in middle_json or not isinstance(middle_json["pdf_info"], list):
        raise ValueError("MinerU API middle_json 缺少 pdf_info")
    return middle_json


def run_mineru_api(
    input_path: Path,
    output_dir: Path,
    *,
    api_url: str,
    backend: str = "pipeline",
    method: str = "auto",
    lang: str = "ch",
    timeout: int = 3600,
) -> Path:
    """直接调用常驻 MinerU API，写出 middle JSON 并返回路径。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    endpoint = api_url.rstrip("/") + "/file_parse"
    output_path = output_dir / f"{input_path.stem}_middle.json"

    data = {
        "backend": backend,
        "parse_method": method,
        "lang_list": lang,
        "return_md": "false",
        "return_middle_json": "true",
        "return_content_list": "false",
        "return_images": "false",
    }
    with input_path.open("rb") as f:
        files = {"files": (input_path.name, f)}
        print("[mineru] posting:", endpoint)
        response = requests.post(endpoint, data=data, files=files, timeout=timeout)
    response.raise_for_status()
    response_json = response.json()
    if response_json.get("status") not in (None, "completed"):
        raise RuntimeError(f"MinerU API 解析失败: {response_json.get('status')} {response_json.get('error')}")

    middle_json = _extract_middle_json_from_api_response(response_json)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(middle_json, f, ensure_ascii=False, indent=2)
    return output_path.resolve()


def _validate_middle_json(json_path: Path) -> Dict[str, Any]:
    with json_path.open(encoding="utf-8") as f:
        data = json.load(f)
    if "pdf_info" not in data or not isinstance(data["pdf_info"], list):
        raise ValueError(f"不是当前 chunker 可消费的 MinerU middle JSON，缺少 pdf_info: {json_path}")
    return data


def copy_to_project_mineru_json(
    middle_json: Path,
    source_path: Path,
    target_dir: Path,
    *,
    overwrite: bool = False,
) -> Path:
    _validate_middle_json(middle_json)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_name = f"MinerU_{_sanitize_doc_name(source_path)}__{_timestamp()}.json"
    target = target_dir / target_name
    if target.exists() and not overwrite:
        raise FileExistsError(f"目标 JSON 已存在: {target}")
    shutil.copy2(middle_json, target)
    return target.resolve()


def chunk_pending_mineru_json(project_json_path: Path, output_dir: Path) -> Dict[str, Any]:
    """把单个待审 MinerU JSON 切成 v9 chunks，返回轻量结果。

    这不调用 pending_doc_chunking_v9.py 的 main，避免顺带处理目录下所有 JSON。
    """
    from pending_doc_chunking_v9 import PendingDocChunkerV9

    chunks = PendingDocChunkerV9(str(project_json_path)).process()
    output_dir.mkdir(parents=True, exist_ok=True)
    doc_name = project_json_path.stem.replace("MinerU_", "").strip()
    chunk_path = output_dir / f"pending_doc_chunks_v9_single_{_timestamp()}.json"
    with chunk_path.open("w", encoding="utf-8") as f:
        json.dump({doc_name: chunks}, f, ensure_ascii=False, indent=2)
    return {"doc_name": doc_name, "chunk_count": len(chunks), "chunks_preview_path": str(chunk_path)}


def convert_document_to_mineru_json(
    input_path: str | Path,
    *,
    doc_kind: str = "pending",
    target_json_dir: str | Path | None = None,
    mineru_work_dir: str | Path = DEFAULT_MINERU_WORK_DIR,
    backend: str = "pipeline",
    method: str = "auto",
    lang: str = "ch",
    model_source: str = "modelscope",
    api_url: str = "",
    timeout: int = 3600,
    chunk_pending: bool = False,
) -> MinerUConvertResult:
    """PDF / Word -> MinerU middle JSON -> 项目 JSON。

    doc_kind:
      - pending: 输出到 new_docs/test_doc_json，后续可走 pending_doc_chunking_v9
      - rule: 输出到 new_docs/rule_docs_json，后续可走 chapter_based_chunking_v6
      - custom: 必须传 target_json_dir
    """
    source = Path(input_path).resolve()
    work_dir = Path(mineru_work_dir).resolve()
    normalized = normalize_input_for_mineru(source, work_dir / "normalized")

    if target_json_dir is None:
        if doc_kind == "pending":
            target_json_dir = DEFAULT_PENDING_JSON_DIR
        elif doc_kind == "rule":
            target_json_dir = DEFAULT_RULE_JSON_DIR
        else:
            raise ValueError("doc_kind=custom 时必须传 target_json_dir")
    target_json_dir = Path(target_json_dir).resolve()

    run_dir = work_dir / "raw_outputs" / f"{_sanitize_doc_name(source)}__{_timestamp()}"
    if api_url:
        middle_json = run_mineru_api(
            normalized,
            run_dir,
            api_url=api_url,
            backend=backend,
            method=method,
            lang=lang,
            timeout=timeout,
        )
    else:
        middle_json = run_mineru_cli(
            normalized,
            run_dir,
            backend=backend,
            method=method,
            lang=lang,
            model_source=model_source,
            api_url="",
            timeout=timeout,
        )
    project_json = copy_to_project_mineru_json(middle_json, source, target_json_dir)

    chunk_count = 0
    chunks_preview_path = ""
    if chunk_pending:
        chunk_info = chunk_pending_mineru_json(project_json, PROJECT_ROOT / "chunks_visualization")
        chunk_count = chunk_info["chunk_count"]
        chunks_preview_path = chunk_info["chunks_preview_path"]

    return MinerUConvertResult(
        source_path=str(source),
        normalized_input_path=str(normalized),
        mineru_output_dir=str(run_dir),
        mineru_middle_json=str(middle_json),
        project_json_path=str(project_json),
        backend=backend,
        method=method,
        model_source=model_source,
        api_url=api_url,
        doc_kind=doc_kind,
        chunk_count=chunk_count,
        chunks_preview_path=chunks_preview_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PDF / Word 转 MinerU JSON，并落到本项目 new_docs 目录")
    parser.add_argument("input", help="PDF / DOCX / DOC 文件路径")
    parser.add_argument("--kind", choices=["pending", "rule", "custom"], default="pending")
    parser.add_argument("--target-json-dir", default="")
    parser.add_argument("--work-dir", default=str(DEFAULT_MINERU_WORK_DIR))
    parser.add_argument("--backend", default="pipeline")
    parser.add_argument("--method", default="auto", choices=["auto", "txt", "ocr"])
    parser.add_argument("--lang", default="ch")
    parser.add_argument("--model-source", default="modelscope", choices=["huggingface", "modelscope", "local", ""])
    parser.add_argument("--api-url", default="")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--chunk-pending", action="store_true", help="待审文档转换后立即生成单文档 chunk 预览")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    target_json_dir = args.target_json_dir or None
    try:
        result = convert_document_to_mineru_json(
            args.input,
            doc_kind=args.kind,
            target_json_dir=target_json_dir,
            mineru_work_dir=args.work_dir,
            backend=args.backend,
            method=args.method,
            lang=args.lang,
            model_source=args.model_source,
            api_url=args.api_url,
            timeout=args.timeout,
            chunk_pending=args.chunk_pending,
        )
    except subprocess.CalledProcessError as exc:
        print(f"[mineru] CLI 执行失败，退出码 {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        print(f"[mineru] 转换失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
