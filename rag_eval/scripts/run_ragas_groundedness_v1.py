"""6.2 RAGAS groundedness 评测（无金标，只跑不需要 reference 的指标）。

输入 JSONL 每行需含：user_input, retrieved_contexts, response。
指标：Faithfulness（回答是否被上下文支撑）+ ResponseRelevancy（回答是否切题）。
两者都不需要 reference/reference_contexts，因此适用于错别字/重复性这类无人工金标的智能体。

LLM judge 用 DashScope qwen-plus；ResponseRelevancy 所需 embedding 默认用本地 BGE-M3。
必须在 langchain0.3 环境运行（含 ragas + FlagEmbedding + torch）。

运行：
  D:\\Anaconda\\envs\\langchain0.3\\python.exe rag_eval/scripts/run_ragas_groundedness_v1.py \\
      --dataset rag_eval/data/typo_agent_ragas_dataset_v1.jsonl --label typo
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BGE_M3_DIR = PROJECT_ROOT / "models" / "bge-m3"
DEFAULT_OUTPUT = PROJECT_ROOT / "rag_eval" / "data" / "ragas_groundedness_metrics_v1.json"
DEFAULT_REPORT = PROJECT_ROOT / "rag_eval" / "reports" / "agent_groundedness_ragas_v1.md"


class LocalBGEM3Embeddings:
    """Minimal LangChain-compatible embedding wrapper around local BGE-M3."""

    def __init__(self, model_dir: Path, batch_size: int = 8, max_length: int = 2048, device: str | None = None):
        self.model_dir = Path(model_dir)
        self.batch_size = batch_size
        self.max_length = max_length
        self.device = device
        self._model = None

    def _ensure_model(self):
        if self._model is not None:
            return
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f"Local BGE-M3 model directory not found: {self.model_dir}")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from FlagEmbedding import BGEM3FlagModel
        import torch

        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        self._model = BGEM3FlagModel(
            str(self.model_dir.resolve()),
            normalize_embeddings=True,
            use_fp16=device.startswith("cuda"),
            devices=device,
            batch_size=self.batch_size,
            passage_max_length=self.max_length,
            query_max_length=self.max_length,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )

    def _embed(self, texts: list[str]) -> list[list[float]]:
        self._ensure_model()
        encoded = self._model.encode(
            texts, batch_size=self.batch_size, max_length=self.max_length,
            return_dense=True, return_sparse=False, return_colbert_vecs=False,
        )
        return encoded["dense_vecs"].tolist()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]


def load_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
            if 0 < limit <= len(rows):
                break
    return rows


def summarize(scores: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({k for row in scores for k in row if k not in ("user_input",)})
    summary: dict[str, Any] = {}
    for key in keys:
        values = []
        for row in scores:
            v = row.get(key)
            if isinstance(v, (int, float)) and not math.isnan(float(v)):
                values.append(float(v))
        if values:
            summary[key] = {
                "mean": sum(values) / len(values),
                "count": len(values),
                "missing_or_failed": len(scores) - len(values),
            }
    return summary


def append_report(report_path: Path, label: str, payload: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    metrics = payload["metrics"]
    lines = [
        f"## {label} 智能体 groundedness（{payload['row_count']} 样本）",
        "",
        f"生成时间：`{payload['generated_at']}` 数据集：`{Path(payload['dataset']).name}`",
        "",
        "| 指标 | 均值 | 有效样本 | 失败/NaN |",
        "|---|---:|---:|---:|",
    ]
    name_zh = {"faithfulness": "Faithfulness 有据性",
               "answer_relevancy": "ResponseRelevancy 相关性",
               "response_relevancy": "ResponseRelevancy 相关性"}
    for key, row in metrics.items():
        lines.append(f"| {name_zh.get(key, key)} | {row['mean']:.4f} | {row['count']} | {row['missing_or_failed']} |")
    lines.append("")
    existing = report_path.read_text(encoding="utf-8") if report_path.exists() else \
        "# 6.2 错别字/重复性智能体 RAGAS groundedness 评测\n\n" \
        "口径：无人工金标，只跑 Faithfulness + ResponseRelevancy。\n" \
        "Faithfulness 度量纠错/判定是否有原文依据（抓幻觉），ResponseRelevancy 度量回答是否切题。\n\n"
    report_path.write_text(existing + "\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--label", default="agent", help="智能体名（typo/redundancy），用于报告分节")
    parser.add_argument("--limit", type=int, default=0, help="最多样本数，0=全部")
    parser.add_argument("--model", default="qwen-plus")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--bge-model-dir", type=Path, default=BGE_M3_DIR)
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--base-url",
                        default=os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
    args = parser.parse_args()

    rows = load_jsonl(args.dataset, args.limit)
    rows = [r for r in rows if str(r.get("response", "")).strip()]
    if not rows:
        raise SystemExit("数据集无有效 response 行。")

    api_key = os.getenv(args.api_key_env)
    if not api_key:
        raise SystemExit(f"缺少 API key 环境变量：{args.api_key_env}")

    from langchain_openai import ChatOpenAI
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    try:
        from ragas.metrics import Faithfulness, ResponseRelevancy
    except ImportError:  # 兼容旧命名
        from ragas.metrics import Faithfulness
        from ragas.metrics import AnswerRelevancy as ResponseRelevancy

    os.environ.setdefault("OPENAI_API_KEY", api_key)
    os.environ.setdefault("OPENAI_BASE_URL", args.base_url)

    llm = ChatOpenAI(model=args.model, temperature=0, api_key=api_key, base_url=args.base_url)
    evaluator_llm = LangchainLLMWrapper(llm)
    embeddings = LangchainEmbeddingsWrapper(LocalBGEM3Embeddings(args.bge_model_dir))

    # 只保留 RAGAS 需要的列
    clean = [{"user_input": r["user_input"],
              "retrieved_contexts": r["retrieved_contexts"],
              "response": r["response"]} for r in rows]
    dataset = EvaluationDataset.from_list(clean)
    result = evaluate(
        dataset=dataset,
        metrics=[Faithfulness(), ResponseRelevancy()],
        llm=evaluator_llm,
        embeddings=embeddings,
    )
    scores = list(getattr(result, "scores", []))
    payload = {
        "schema": "coal_ragas_groundedness_v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "label": args.label,
        "dataset": str(args.dataset.resolve()),
        "model": args.model,
        "row_count": len(clean),
        "metrics": summarize(scores),
        "scores": scores,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # 多次运行（typo/redundancy）写到带 label 的独立 JSON，避免互相覆盖
    out_path = args.output.with_name(f"ragas_groundedness_{args.label}_v1.json")
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    append_report(args.report, args.label, payload)
    print(json.dumps(payload["metrics"], ensure_ascii=False, indent=2))
    print(out_path.resolve())
    print(args.report.resolve())


if __name__ == "__main__":
    main()
