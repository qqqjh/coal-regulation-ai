"""混合检索RAG审核系统 v9
- v8能力全部保留：三并行主链（合规链 ‖ 错别字链 ‖ 文档级重复性链）、
  二次核验、立场分类、断点续跑、HTML报告。
- v9改进：
  14. 检索栈与评估管线统一：本地 BGE-M3（dense + learned sparse，RRF k=60 融合）
      + bge-reranker-v2-m3 重排，替换 DashScope embedding + jieba BM25。
      数据不出本地、无检索限流；KB 编码结果按内容哈希落盘缓存。
  15. 矿井适用性改为检索元数据过滤：规则chunk在加载时打 outburst_only/general
      标签（文档级 + 章节结构级关键词），非突出矿井审查时突出专用条款直接
      不进入检索语料。提示词约束保留作为内容级兜底。
  16. 数值判断"LLM抽取、代码比较"：初审发现的问题先经 numeric_compare 工具
      做确定性数值核验（单位归一化+方向语义编码），工具结论注入核验智能体
      提示词，并参与升级决策。
  17. chunk级并发：检索预计算（Phase A，GPU批量）与LLM审查（Phase B，
      线程池+全局信号量限流）分离，chunk间并行。
  18. 升级队列：初审/核验分歧、LLM与数值工具矛盾、最终"不确定"、调用异常
      四类升级项写入 SQLite 队列（review_queue.py），由主智能体
      （main_agent_v9.py）loop 处理；裁决沉淀为标注飞轮。
  19. 多查询检索（轻量版原子检索）：待审chunk按编号项/句子切成检索片段，
      整块query与各片段query并行召回，候选按"召回它的片段"重排取最高分。
      修复长chunk多主题稀释导致的法规漏召回（如支护锚索起吊、停风1.2%准入
      两例实测从top-10外提升到top-1）。只增加GPU检索调用，不增加LLM调用。
  20. 反向数值核验（防漏报）：初审判"合规"但内容含阈值语义关键词+数值的
      chunk，用数值核验工具对照法规中含数值的句子反查；工具判不合规时
      状态改"不确定"并以 counter_check 类型升级，交主智能体/人工裁决。
      动机：qwen 在"准入/复风阈值方向"上易拿错对比基准（如拿1.5%停工阈值
      论证1.2%准入合规，回避§197的1.0%恢复通风上限），漏报无法靠核验智能体
      纠正（它只删不加），必须用确定性工具反查。
"""
import argparse
import json
import hashlib
import os
import pickle
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from openai import OpenAI

import numeric_compare
from review_queue import (
    ReviewQueue, ESC_DISAGREEMENT, ESC_NUMERIC_CONFLICT,
    ESC_LOW_CONFIDENCE, ESC_ERROR, ESC_COUNTER_CHECK,
)

# ============ 配置 ============
PROJECT_ROOT = Path(__file__).resolve().parent

API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
QWEN_MODEL = "qwen-plus"

BGE_M3_DIR = PROJECT_ROOT / "models" / "bge-m3"
BGE_RERANKER_DIR = PROJECT_ROOT / "models" / "bge-reranker-v2-m3"
KB_CACHE_DIR = PROJECT_ROOT / "data" / "bge_kb_cache"

TOP_K = 5                        # 重排后进入审查的法规条数
RERANK_POOL_PER_CHANNEL = 10     # 整块query：dense/sparse/RRF 各路召回数（取并集重排）
RERANK_SCORE_THRESHOLD = 0.10    # 重排归一化分数低于此值视为无关

# 多查询检索：chunk切分为片段后逐片段召回（v9.19）
SEGMENT_MIN_LEN = 12             # 片段最短长度（更短的多为编号/标题，并入相邻段）
SEGMENT_MAX_LEN = 220            # 片段最长长度（超长按句子二次切分）
SEGMENT_MAX_PER_CHUNK = 12       # 每chunk最多片段数（超出则相邻合并，控制重排成本）
SEGMENT_POOL_PER_CHANNEL = 5     # 片段query：每路召回数
BGE_MAX_LENGTH = 2048            # 与评估管线一致
BGE_BATCH_SIZE = 8

CHUNK_CONCURRENCY = 4            # 同时审查的chunk数
LLM_MAX_CONCURRENCY = 8          # 全局LLM并发上限（防止触发QPM限流）
MAX_NUMERIC_ISSUES = 4           # 每chunk最多对前N个问题做数值核验

# 反向数值核验（v9.20）触发条件：初审合规 + 含阈值语义关键词 + 含数值
COUNTER_CHECK_KEYWORDS = (
    "报警", "断电", "复电", "停工", "停止作业", "停止工作",
    "恢复送风", "恢复通风", "方可", "准入", "撤人", "撤出",
)
_COUNTER_VALUE_PATTERN = re.compile(r"\d+(?:\.\d+)?\s*[%％℃]")

VERIFY_NON_COMPLIANT = True
DOC_FILTER: List[str] = []
MAX_PENDING_DOCS: Optional[int] = 1
TYPO_MIN_CHUNK_LEN = 10
REDUNDANCY_SIMILARITY_THRESHOLD = 0.90
REDUNDANCY_MIN_CHUNK_LEN = 40

MINE_TYPE = os.getenv("PENDING_MINE_TYPE", "non_outburst")  # non_outburst | outburst

KB_CHUNKS_DIR = PROJECT_ROOT / "chunks_visualization"
PENDING_CHUNKS_DIR = PROJECT_ROOT / "chunks_visualization"
OUTPUT_DIR = PROJECT_ROOT / "review_results"
QUEUE_DB = PROJECT_ROOT / "data" / "review_queue_v9.db"

MINE_APPLICABILITY_NOTE = """【矿井适用性约束】
本批待审对象按非突出矿井处理。若参考法规片段明确限定为“突出矿井”“煤与瓦斯突出矿井”“突出煤层”“突出危险区域”等突出矿井专用场景：
- 不得直接作为非突出矿井待审内容的违规依据；
- 只有待审内容本身明确属于突出矿井/突出煤层/突出危险场景时，才可适用该条款；
- 若条款既包含突出矿井专用要求又包含通用要求，只能依据其中明确适用于所有矿井或一般场景的部分判断。"""

# 适用性标签关键词：文档级（整本规章只适用突出矿井）与章节结构级
OUTBURST_DOC_PATTERNS = ("防治煤与瓦斯突出",)
OUTBURST_STRUCT_KEYWORDS = (
    "突出矿井", "煤与瓦斯突出", "突出煤层", "突出危险", "防突", "石门揭煤",
)


def tag_applicability(chunk: Dict, doc_name: str) -> str:
    """规则chunk适用性标签：outburst_only（突出矿井专用）/ general。

    只在文档级或章节结构级命中时标 outburst_only（整块过滤是安全的）；
    正文内容级的零星提及不过滤，由提示词约束兜底。"""
    if any(p in doc_name for p in OUTBURST_DOC_PATTERNS):
        return "outburst_only"
    struct_text = " ".join([
        str(chunk.get("part", "")), str(chunk.get("chapter", "")),
        str(chunk.get("section", "")), str(chunk.get("context_prefix", "") or ""),
        " ".join(chunk.get("parent_context") or []),
    ])
    if any(k in struct_text for k in OUTBURST_STRUCT_KEYWORDS):
        return "outburst_only"
    return "general"


class FatalAPIError(Exception):
    """账户欠费等不可恢复错误，全局停止后续LLM调用。"""


# ============ 待审chunk片段切分（多查询检索用） ============

# 规程文本的条目编号样式："一、" "1、" "1." "（1）" "（一）" "①" "第X条" "3起吊设备前"
_ENUM_PATTERN = re.compile(
    r"^\s*(?:[一二三四五六七八九十]{1,3}、"
    r"|\d{1,3}[、.]"
    r"|（[一二三四五六七八九十\d]{1,3}）"
    r"|\(\d{1,3}\)"
    r"|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]"
    r"|第[一二三四五六七八九十百\d]{1,4}[条节章]"
    r"|\d{1,3}(?=[一-鿿]))"
)


def split_chunk_segments(content: str) -> List[str]:
    """把待审chunk按编号条目切成检索片段；超长段再按句子切。

    目的：长chunk常混多个主题，整块当query会稀释关键句的检索信号，
    导致违规句对应的法规条款挤不进重排top-k。片段级query可消除稀释。"""
    lines = content.splitlines()
    raw_segments: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        if _ENUM_PATTERN.match(line) and current:
            raw_segments.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        raw_segments.append(current)

    refined: List[str] = []
    for seg_lines in raw_segments:
        seg = "\n".join(seg_lines).strip()
        if not seg:
            continue
        if len(seg) <= SEGMENT_MAX_LEN:
            refined.append(seg)
            continue
        buffer = ""
        for sentence in re.split(r"(?<=[。；;])", seg):
            if len(buffer) + len(sentence) <= SEGMENT_MAX_LEN:
                buffer += sentence
            else:
                if buffer.strip():
                    refined.append(buffer.strip())
                buffer = sentence
        if buffer.strip():
            refined.append(buffer.strip())

    # 过短片段（裸编号/标题）并入下一段
    merged: List[str] = []
    pending_short = ""
    for seg in refined:
        if len(seg) < SEGMENT_MIN_LEN:
            pending_short += seg + "\n"
            continue
        merged.append((pending_short + seg).strip())
        pending_short = ""
    if pending_short.strip() and merged:
        merged[-1] = merged[-1] + "\n" + pending_short.strip()
    elif pending_short.strip():
        merged.append(pending_short.strip())

    # 控制片段数上限：相邻两两合并直到达标
    while len(merged) > SEGMENT_MAX_PER_CHUNK:
        merged = ["\n".join(merged[i:i + 2]) for i in range(0, len(merged), 2)]

    return merged if merged else [content[:SEGMENT_MAX_LEN]]


def _is_fatal_api_error(err: str) -> bool:
    lowered = err.lower()
    return "arrearage" in lowered or "overdue" in lowered or "access denied" in lowered


# ============ 本地 BGE-M3 检索（与 rag_eval 评估管线一致） ============

def _top_indices(scores: np.ndarray, top_k: int) -> List[int]:
    if scores.size == 0:
        return []
    limit = min(top_k, scores.size)
    selected = np.argpartition(scores, -limit)[-limit:]
    return selected[np.argsort(scores[selected])[::-1]].tolist()


def _dense_search(query_vector: np.ndarray, document_vectors: np.ndarray,
                  top_k: int) -> List[Tuple[int, float]]:
    scores = document_vectors @ query_vector
    return [(idx, float(scores[idx])) for idx in _top_indices(scores, top_k)]


def _build_sparse_postings(document_weights: Sequence[Dict[str, float]]) -> Dict[str, List[Tuple[int, float]]]:
    postings: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
    for doc_idx, weights in enumerate(document_weights):
        for token_id, weight in weights.items():
            postings[str(token_id)].append((doc_idx, float(weight)))
    return postings


def _sparse_search(query_weights: Dict[str, float],
                   postings: Dict[str, List[Tuple[int, float]]],
                   document_count: int, top_k: int) -> List[Tuple[int, float]]:
    scores = np.zeros(document_count, dtype=np.float32)
    for token_id, q_weight in query_weights.items():
        for doc_idx, d_weight in postings.get(str(token_id), ()):
            scores[doc_idx] += float(q_weight) * d_weight
    return [(idx, float(scores[idx])) for idx in _top_indices(scores, top_k) if scores[idx] > 0]


def _rrf_hybrid(dense: Sequence[Tuple[int, float]], sparse: Sequence[Tuple[int, float]],
                top_k: int, rrf_k: int = 60) -> List[Tuple[int, float]]:
    scores: Dict[int, float] = defaultdict(float)
    for rows in (dense, sparse):
        for rank, (doc_idx, _score) in enumerate(rows, start=1):
            scores[doc_idx] += 1.0 / (rrf_k + rank)
    ordered = sorted(scores, key=scores.get, reverse=True)[:top_k]
    return [(doc_idx, scores[doc_idx]) for doc_idx in ordered]


class LocalBGERetriever:
    """BGE-M3 dense+sparse 召回 + bge-reranker-v2-m3 重排（全本地，懒加载）。"""

    def __init__(self, embedding_dir: Path = BGE_M3_DIR,
                 reranker_dir: Path = BGE_RERANKER_DIR,
                 device: Optional[str] = None,
                 max_length: int = BGE_MAX_LENGTH,
                 batch_size: int = BGE_BATCH_SIZE):
        self.embedding_dir = Path(embedding_dir)
        self.reranker_dir = Path(reranker_dir)
        self.device = device
        self.max_length = max_length
        self.batch_size = batch_size
        self._embedder = None
        self._reranker = None
        self._rerank_lock = threading.Lock()

    def _ensure_models(self):
        if self._embedder is not None:
            return
        if not self.embedding_dir.is_dir():
            raise FileNotFoundError(f"BGE-M3 模型目录不存在: {self.embedding_dir}")
        if not self.reranker_dir.is_dir():
            raise FileNotFoundError(f"BGE reranker 模型目录不存在: {self.reranker_dir}")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        from FlagEmbedding import BGEM3FlagModel, FlagReranker
        import torch
        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        use_fp16 = device.startswith("cuda")
        print(f"加载本地BGE-M3: {self.embedding_dir} (device={device}, fp16={use_fp16})")
        self._embedder = BGEM3FlagModel(
            str(self.embedding_dir.resolve()),
            normalize_embeddings=True,
            use_fp16=use_fp16,
            devices=device,
            batch_size=self.batch_size,
            passage_max_length=self.max_length,
            query_max_length=self.max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        print(f"加载本地reranker: {self.reranker_dir}")
        self._reranker = FlagReranker(str(self.reranker_dir.resolve()), use_fp16=use_fp16)

    def encode(self, texts: List[str]) -> Tuple[np.ndarray, List[Dict[str, float]]]:
        self._ensure_models()
        encoded = self._embedder.encode(
            texts,
            batch_size=self.batch_size,
            max_length=self.max_length,
            return_dense=True,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        dense = np.asarray(encoded["dense_vecs"], dtype=np.float32)
        lexical = encoded["lexical_weights"]
        return dense, lexical

    def encode_corpus_cached(self, texts: List[str], cache_key: str) -> Tuple[np.ndarray, List[Dict[str, float]]]:
        KB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dense_path = KB_CACHE_DIR / f"{cache_key}_dense.npy"
        lex_path = KB_CACHE_DIR / f"{cache_key}_lex.pkl"
        if dense_path.exists() and lex_path.exists():
            dense = np.load(dense_path)
            with lex_path.open("rb") as handle:
                lexical = pickle.load(handle)
            if dense.shape[0] == len(texts) and len(lexical) == len(texts):
                print(f"  命中KB编码缓存: {dense_path.name} ({dense.shape[0]}条)")
                return dense, lexical
        print(f"  编码知识库 {len(texts)} 条（首次较慢，结果将缓存）...")
        dense, lexical = self.encode(texts)
        np.save(dense_path, dense)
        with lex_path.open("wb") as handle:
            pickle.dump(lexical, handle)
        return dense, lexical

    def rerank_pairs(self, pairs: List[List[str]]) -> List[float]:
        self._ensure_models()
        with self._rerank_lock:  # GPU重排串行，避免多线程争用
            scores = self._reranker.compute_score(
                pairs, batch_size=self.batch_size,
                max_length=self.max_length, normalize=True,
            )
        if not isinstance(scores, (list, tuple, np.ndarray)):
            scores = [scores]
        return [float(s) for s in scores]

    def rerank(self, query_text: str, candidate_texts: List[str]) -> List[float]:
        return self.rerank_pairs([[query_text, text] for text in candidate_texts])


# ============ 审核器 ============

class HybridRAGReviewerV9:
    """v9：BGE本地检索 + 三并行链 + 数值核验工具 + 升级队列。"""

    def __init__(self, mine_type: str = MINE_TYPE):
        if not API_KEY:
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
        self.llm_client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        self.retriever = LocalBGERetriever()
        self.queue = ReviewQueue(QUEUE_DB)
        self.mine_type = mine_type

        self.kb_chunks: List[Dict] = []
        self.kb_texts: List[str] = []
        self.kb_dense: Optional[np.ndarray] = None
        self.kb_postings: Optional[Dict[str, List[Tuple[int, float]]]] = None
        self.kb_json_path: Optional[Path] = None
        self.excluded_outburst_count = 0

        self._llm_semaphore = threading.Semaphore(LLM_MAX_CONCURRENCY)
        self._fatal = threading.Event()
        self._print_lock = threading.Lock()

    # ============ LLM 调用（信号量限流 + 致命错误熔断） ============

    def _chat(self, messages: List[Dict], temperature: float = 0.1,
              max_tokens: Optional[int] = None, retries: int = 2) -> str:
        if self._fatal.is_set():
            raise FatalAPIError("API已标记为不可用（欠费/拒绝访问）")
        last_err = None
        for attempt in range(retries + 1):
            try:
                with self._llm_semaphore:
                    kwargs: Dict[str, Any] = dict(
                        model=QWEN_MODEL, messages=messages, temperature=temperature
                    )
                    if max_tokens:
                        kwargs["max_tokens"] = max_tokens
                    response = self.llm_client.chat.completions.create(**kwargs)
                return response.choices[0].message.content or ""
            except Exception as exc:
                err = str(exc)
                if _is_fatal_api_error(err):
                    self._fatal.set()
                    raise FatalAPIError(err)
                last_err = exc
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise last_err

    def _log(self, message: str):
        with self._print_lock:
            print(message, flush=True)

    # ============ 知识库加载与索引 ============

    def load_knowledge_base(self, kb_json_path: Optional[str] = None):
        if kb_json_path is None:
            latest_v6 = KB_CHUNKS_DIR / "chunks_v6_latest.json"
            v6_files = sorted(KB_CHUNKS_DIR.glob("chunks_v6_20*.json"), reverse=True)
            latest_v5 = KB_CHUNKS_DIR / "chunks_v5_latest.json"
            v5_files = sorted(KB_CHUNKS_DIR.glob("chunks_v5_20*.json"), reverse=True)
            if latest_v6.exists():
                kb_json_path = latest_v6
            elif v6_files:
                kb_json_path = v6_files[0]
            elif latest_v5.exists():
                kb_json_path = latest_v5
            elif v5_files:
                kb_json_path = v5_files[0]
            else:
                raise FileNotFoundError("未找到知识库chunks文件")
        print(f"加载知识库: {kb_json_path}")
        self.kb_json_path = Path(kb_json_path)
        with open(kb_json_path, "r", encoding="utf-8") as f:
            kb_data = json.load(f)

        chunk_id = 0
        total = 0
        for doc_name, chunks in kb_data.items():
            for chunk in chunks:
                if chunk.get("retrievable", True) is False:
                    continue
                total += 1
                applicability = tag_applicability(chunk, doc_name)
                if self.mine_type == "non_outburst" and applicability == "outburst_only":
                    self.excluded_outburst_count += 1
                    continue
                chunk["id"] = f"kb_{chunk_id}"
                chunk["doc_name"] = doc_name
                chunk["applicability"] = applicability
                chunk_id += 1
                self.kb_chunks.append(chunk)
                self.kb_texts.append(chunk.get("retrieval_text") or chunk["content"])
        print(f"  可检索chunks: {len(self.kb_chunks)} / {total}"
              f"（适用性过滤排除突出矿井专用 {self.excluded_outburst_count} 条，"
              f"矿井类型: {self.mine_type}）")

    def build_index(self):
        source_hash = hashlib.sha256(self.kb_json_path.read_bytes()).hexdigest()[:16]
        cache_key = f"kb_{source_hash}_{self.mine_type}"
        self.kb_dense, kb_lexical = self.retriever.encode_corpus_cached(self.kb_texts, cache_key)
        self.kb_postings = _build_sparse_postings(kb_lexical)
        print(f"  索引就绪: dense {self.kb_dense.shape}, sparse postings {len(self.kb_postings)} tokens")

    # ============ 检索（Phase A 预计算） ============

    def _recall_union(self, query_dense: np.ndarray, query_lex: Dict[str, float],
                      pool: int) -> List[int]:
        """dense/sparse/RRF 三路召回取并集，返回KB索引列表。"""
        dense = _dense_search(query_dense, self.kb_dense, pool)
        sparse = _sparse_search(query_lex, self.kb_postings, len(self.kb_chunks), pool)
        hybrid = _rrf_hybrid(dense, sparse, pool)
        union: Dict[int, None] = {}
        for rows in (dense, sparse, hybrid):
            for idx, _score in rows:
                union.setdefault(idx, None)
        return list(union.keys())

    def search_one(self, query_text: str, query_dense: np.ndarray,
                   query_lex: Dict[str, float], top_k: int = TOP_K) -> List[Dict]:
        """单query检索（主智能体 retrieve_regulations 工具等使用）。"""
        indices = self._recall_union(query_dense, query_lex, RERANK_POOL_PER_CHANNEL)
        if not indices:
            return []
        scores = self.retriever.rerank(query_text, [self.kb_texts[i] for i in indices])
        ranked = sorted(zip(indices, scores), key=lambda x: x[1], reverse=True)
        return [
            {"chunk": self.kb_chunks[idx], "score": round(score, 4)}
            for idx, score in ranked[:top_k]
            if score >= RERANK_SCORE_THRESHOLD
        ]

    def search_chunk_multi(self, full_text: str,
                           full_dense: np.ndarray, full_lex: Dict[str, float],
                           segments: List[str],
                           seg_dense: np.ndarray, seg_lex: List[Dict[str, float]],
                           top_k: int = TOP_K) -> List[Dict]:
        """多查询检索（v9.19）：整块query + 各片段query并行召回；
        每个候选按"召回它的query"重排，取所有query下的最高分。
        长chunk里被无关内容稀释的关键句由其所在片段单独召回，不再漏检。"""
        pairs: List[Tuple[str, int]] = []
        for idx in self._recall_union(full_dense, full_lex, RERANK_POOL_PER_CHANNEL):
            pairs.append((full_text, idx))
        for s_i, segment in enumerate(segments):
            for idx in self._recall_union(seg_dense[s_i], seg_lex[s_i],
                                          SEGMENT_POOL_PER_CHANNEL):
                pairs.append((segment, idx))
        if not pairs:
            return []
        unique_pairs = list(dict.fromkeys(pairs))  # 同一(query,候选)只重排一次
        scores = self.retriever.rerank_pairs(
            [[query, self.kb_texts[idx]] for query, idx in unique_pairs]
        )
        best: Dict[int, float] = {}
        for (query, idx), score in zip(unique_pairs, scores):
            if score > best.get(idx, -1.0):
                best[idx] = score
        ranked = sorted(best.items(), key=lambda x: x[1], reverse=True)
        return [
            {"chunk": self.kb_chunks[idx], "score": round(score, 4)}
            for idx, score in ranked[:top_k]
            if score >= RERANK_SCORE_THRESHOLD
        ]

    def prepare_retrieval(self, chunks: List[Dict]) -> Tuple[List[List[Dict]], np.ndarray]:
        """对整篇文档批量编码+多查询检索（GPU串行，无LLM调用）。
        返回 (每chunk的kb_results, 待审chunk整块dense向量[供重复性检查复用])。"""
        texts = [c["content"] for c in chunks]
        segments_per_chunk = [split_chunk_segments(t) for t in texts]
        flat: List[str] = list(texts)
        seg_slices: List[Tuple[int, int]] = []
        for segments in segments_per_chunk:
            start = len(flat)
            flat.extend(segments)
            seg_slices.append((start, len(flat)))
        n_segments = len(flat) - len(texts)
        print(f"  [Phase A] 批量编码 {len(texts)} 个chunk + {n_segments} 个检索片段...")
        all_dense, all_lex = self.retriever.encode(flat)
        q_dense = all_dense[:len(texts)]

        kb_results_all: List[List[Dict]] = []
        retrieve_start = time.time()
        for i in range(len(chunks)):
            lo, hi = seg_slices[i]
            kb_results_all.append(self.search_chunk_multi(
                texts[i], all_dense[i], all_lex[i],
                segments_per_chunk[i], all_dense[lo:hi], all_lex[lo:hi],
            ))
            if (i + 1) % 20 == 0:
                print(f"    检索+重排 {i + 1}/{len(chunks)}")
        print(f"  [Phase A] 多查询检索完成，耗时 {time.time() - retrieve_start:.1f}s")
        return kb_results_all, q_dense

    # ============ 构建知识库上下文 ============

    def _build_kb_context(self, kb_results: List[Dict]) -> str:
        kb_context = ""
        for i, result in enumerate(kb_results, 1):
            chunk = result["chunk"]
            kb_context += f"\n【参考法规{i}】来源: {chunk['doc_name']}\n"
            if chunk.get("chapter"):
                kb_context += f"章节: {chunk['chapter']}\n"
            if chunk.get("section"):
                kb_context += f"小节: {chunk['section']}\n"
            kb_context += f"内容: {chunk['content']}\n"
            kb_context += "-" * 50
        return kb_context

    @staticmethod
    def _parse_json_response(text: str) -> Optional[Dict]:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None

    # ============ 智能体1：立场分类（提示词与v8一致） ============

    def classify_kb_chunks(self, pending_chunk: Dict, kb_results: List[Dict]) -> List[Dict]:
        pending_content = pending_chunk["content"]
        chunk_list_text = ""
        for i, result in enumerate(kb_results, 1):
            chunk = result["chunk"]
            chunk_list_text += f"\n[{i}] 来源: {chunk['doc_name']}"
            if chunk.get("chapter"):
                chunk_list_text += f" / {chunk['chapter']}"
            chunk_list_text += f"\n内容: {chunk['content'][:400]}\n"

        prompt = f"""你是煤矿安全法规专家。请判断下方每条法规片段对于"待审内容是否合规"这一问题的立场。

【待审内容】
{pending_content[:600]}

【待检索到的法规片段列表】
{chunk_list_text}

{MINE_APPLICABILITY_NOTE}

请对每条法规片段进行立场分类，分类定义：
- 支持：该片段表明待审内容符合规定（待审内容不违反此片段要求）
- 反对：该片段表明待审内容存在违规风险（待审内容可能违反此片段要求）
- 例外：该片段是一个例外或特殊条款，可能允许待审内容的做法
- 无关：该片段与待审内容无实质关联

注意：
- "支持"不代表待审内容一定合规，只代表该片段不构成阻碍
- "反对"不代表一定违规，只代表该片段需要关注
- 分类依据仅限于片段内容，不做完整合规判断

请输出JSON：
{{
  "classifications": [
    {{"index": 1, "classification": "支持/反对/例外/无关", "reason": "简要原因（20字内）"}},
    {{"index": 2, "classification": "...", "reason": "..."}},
    ...
  ]
}}
"""
        default = [
            {"index": i + 1, "chunk_id": kb_results[i]["chunk"]["id"],
             "classification": "无关", "reason": "分类失败"}
            for i in range(len(kb_results))
        ]
        try:
            result_text = self._chat(
                [{"role": "system", "content": "你是煤矿安全法规专家，负责判断法规片段对待审内容的立场。严格按JSON格式输出。"},
                 {"role": "user", "content": prompt}],
            )
            parsed = self._parse_json_response(result_text)
            if not parsed:
                return default
            classifications = parsed.get("classifications", [])
            result = []
            for i, kb_r in enumerate(kb_results):
                entry = {"index": i + 1, "chunk_id": kb_r["chunk"]["id"],
                         "classification": "无关", "reason": ""}
                for c in classifications:
                    if c.get("index") == i + 1:
                        entry["classification"] = c.get("classification", "无关")
                        entry["reason"] = c.get("reason", "")
                        break
                result.append(entry)
            return result
        except FatalAPIError:
            raise
        except Exception as exc:
            self._log(f"    分类失败: {exc}")
            return default

    # ============ 智能体2：初审（提示词与v8一致） ============

    def review_chunk_with_llm(self, pending_chunk: Dict, kb_results: List[Dict]) -> Dict:
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk["content"]
        pending_meta = f"章: {pending_chunk.get('chapter', '')} | 节: {pending_chunk.get('section', '')}"

        prompt = f"""你是一位煤矿安全法规审核专家。请根据下方知识库，判断待审内容是否违反法规要求。

【核心判断规则】

A. 禁止行为
  - 待审内容**实施**了被禁止的行为 → 违规
  - 待审内容**禁止**了该行为（写了"严禁/不得"）→ 合规，不得反向判违规

B. 普通数值（尺寸、距离、支护参数等）
  - 数值下限（不得低于/至少/≥X）：待审值 < 法规值 → 违规；待审值 ≥ 法规值 → 合规
  - 数值上限（不得超过/最多/≤X）：待审值 > 法规值 → 违规；待审值 ≤ 法规值 → 合规
  - 数值区间（A~B）：下限和上限分别判断

C. 安全触发阈值（★重要，最常见误判点★）
  定义：到达某浓度/温度时必须停工、断电、报警、撤人的触发类参数。
  典型名称：报警浓度、断电浓度、复电浓度、停工阈值、断电阈值。
  判断逻辑：
    - 报警浓度≥X、断电浓度≥X、停工阈值≥X：X越小 = 越早触发保护 = 越严格
      → 待审X < 法规X → 更早触发 → 合规，绝对不得以"低于法规值"判违规
    - 复电浓度<X（恢复送电的气体浓度上限）：X越小 = 要求浓度降得更低才能复电 = 越严格
      → 待审X < 法规X → 更严格 → 合规
  法规表格示例（合规判断）：
    法规表格"掘进工作面 | 报警≥1.0 | 断电≥1.5 | 复电<1.0"
    待审设定"报警≥0.8，断电≥1.2，复电<0.8"
    → 0.8<1.0，1.2<1.5，0.8<1.0 → 三项均更早/更严触发 → 全部合规
  注意：仅当待审值 > 法规值时（如法规断电≥1.5%，待审断电≥1.8%，更晚断电），才违规。
  注意：若待审在某地点设定了比法规更严格的触发阈值（如0.8%），不得以"无上位依据"判违规。严于法规即合规。

D. 误差/偏差/公差
  法规"误差在A~B"或"偏差±X"表示偏差绝对值范围，不区分正负。
  待审偏差绝对值 ≤ 法规上限 → 合规。

【结论规则】
  - 知识库中无相关条款 → 合规，注明"知识库中无对应条款"
  - 知识库有条款但关键数值缺失 → 不确定，issues为空
  - 待审内容违反某条款 → 不合规，issues记录冲突
  - issues为空 → compliance_status必须为"合规"
  - 证据不足（双向解读均合理）→ "不确定"

【待审文档信息】
{pending_meta}

【待审内容】
{pending_content}

【参考法规知识库】
{kb_context}

{MINE_APPLICABILITY_NOTE}

【审核要点】
1. 禁止行为冲突：待审内容是否实施了法规明确禁止的行为（关注实质，不受措辞限制）
2. 数值冲突：待审数值是否超出法规上限或低于法规下限（区间需两端分别比较）

请以JSON格式输出审核结果：
{{
    "compliance_status": "合规/不合规/不确定",
    "issues": [
        {{
            "type": "数值冲突/规则冲突",
            "pending_content": "待审文档中的具体内容（原文引用）",
            "regulation_content": "知识库中对应的法规要求（必须是知识库原文）",
            "description": "冲突说明（简洁，不超过100字）",
            "suggestion": "修改建议"
        }}
    ],
    "summary": "一句话审核结论"
}}

注意：
- issues为空时compliance_status必须为"合规"
- "不确定"状态仅用于知识库信息不足以判断的情况，issues可以包含待确认项
"""
        result_text = ""
        try:
            result_text = self._chat(
                [{"role": "system",
                  "content": "你是煤矿安全法规审核专家。只关注数值冲突和规则冲突，严于法规的要求视为合规。严格按JSON格式输出，不引用知识库以外的内容。"},
                 {"role": "user", "content": prompt}],
            )
            parsed = self._parse_json_response(result_text)
            if parsed is not None:
                return parsed
            return {"raw_response": result_text, "parse_error": "无法提取JSON"}
        except FatalAPIError:
            raise
        except json.JSONDecodeError:
            return {"raw_response": result_text, "parse_error": "JSON解析失败"}
        except Exception as exc:
            return {"error": str(exc)}

    # ============ 数值核验工具（v9新增：LLM抽取、代码比较） ============

    def run_numeric_checks(self, pending_chunk: Dict, issues: List[Dict]) -> List[Dict]:
        """对初审问题逐条做确定性数值核验。失败不阻塞主流程。"""
        checks = []
        for issue in issues[:MAX_NUMERIC_ISSUES]:
            rule_text = str(issue.get("regulation_content", "")).strip()
            pending_text = str(issue.get("pending_content", "")).strip() \
                or pending_chunk["content"][:800]
            if not rule_text:
                checks.append({"has_pairs": False, "overall": "无可比数值",
                               "details": [], "summary": "问题未引用法规原文"})
                continue
            if self._fatal.is_set():
                break
            with self._llm_semaphore:
                checks.append(numeric_compare.numeric_check(
                    self.llm_client, QWEN_MODEL, pending_text, rule_text
                ))
        return checks

    # ---- 反向数值核验（v9.20：防漏报） ----

    @staticmethod
    def _needs_counter_check(content: str) -> bool:
        """初审合规的chunk是否需要反向数值核验：含阈值语义关键词 + 含数值。"""
        return (bool(_COUNTER_VALUE_PATTERN.search(content))
                and any(kw in content for kw in COUNTER_CHECK_KEYWORDS))

    @staticmethod
    def _numeric_sentences(text: str, max_chars: int) -> str:
        """抽取文本中含数字的句子（紧凑拼接，保证关键数值不被截断丢失）。"""
        sentences = re.split(r"(?<=[。；;])", text)
        out = ""
        for sentence in sentences:
            if not re.search(r"\d", sentence):
                continue
            if len(out) + len(sentence) > max_chars:
                break
            out += sentence
        return out.strip()

    def run_counter_numeric_check(self, pending_chunk: Dict,
                                  kb_results: List[Dict]) -> Dict:
        """初审判合规时的反向核验：待审全文 vs 法规中含数值的句子。
        法规侧只取数值句而非chunk开头，避免关键阈值（如§197的1.0%）
        因截断而进不了抽取窗口。"""
        parts: List[str] = []
        total = 0
        for i, result in enumerate(kb_results, 1):
            chunk = result["chunk"]
            sentences = self._numeric_sentences(chunk["content"], 400)
            if not sentences:
                continue
            block = f"【法规{i}】{chunk['doc_name']}：{sentences}"
            if total + len(block) > 1400:
                break
            parts.append(block)
            total += len(block)
        if not parts:
            return {"has_pairs": False, "overall": "无可比数值", "details": [],
                    "summary": "检索到的法规中无数值句"}
        with self._llm_semaphore:
            return numeric_compare.numeric_check(
                self.llm_client, QWEN_MODEL,
                pending_chunk["content"][:1500], "\n".join(parts)
            )

    @staticmethod
    def _numeric_note_for_prompt(numeric_checks: List[Dict]) -> str:
        notes = [numeric_compare.format_for_prompt(c) for c in numeric_checks]
        notes = [n for n in notes if n]
        if not notes:
            return ""
        return ("\n\n【数值核验工具结果】（逐条对应初审问题，由确定性程序完成单位归一化与方向比较）\n"
                + "\n\n".join(f"问题{i + 1}：\n{n}" for i, n in enumerate(notes))
                + "\n\n使用规则：工具结论为'合规'的数值类问题（待审更严或相等）应删除；"
                  "工具结论为'不合规'的数值类问题应保留；'不确定'按原有规则人工判断。")

    # ============ 智能体3：验证（提示词与v8一致 + 数值工具结论注入） ============

    def verify_review_result(self, pending_chunk: Dict, kb_results: List[Dict],
                             initial_result: Dict,
                             numeric_checks: Optional[List[Dict]] = None) -> Dict:
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk["content"]
        initial_issues_text = json.dumps(
            initial_result.get("issues", []), ensure_ascii=False, indent=2
        )
        numeric_note = self._numeric_note_for_prompt(numeric_checks or [])

        prompt = f"""你是一位资深合规审核质量控制专家。请检查初步审核结果，识别并纠正其中的误判。
你只能删除误判项，不能新增问题，不能修改正确的违规判断。

【待审内容】
{pending_content}

【参考法规知识库（唯一允许引用的依据）】
{kb_context}

{MINE_APPLICABILITY_NOTE}

【初步审核发现的问题（需逐条核查）】
{initial_issues_text}{numeric_note}

请逐条检查以下误判类型，符合则删除：

━━ 误判类型1：严于法规被错判违规 ━━

表现A（普通数值方向误判）
  - 上限约束：待审上限 < 法规上限 → 更严 → 删除；待审上限 > 法规上限 → 真实违规 → 保留
  - 下限约束：待审下限 > 法规下限 → 更严 → 删除；待审下限 < 法规下限 → 真实违规 → 保留

表现B（禁止行为方向误判）
  待审原文写了"严禁/不得"来约束某行为，却被误判为"实施了该违规行为"。
  → 检查待审原文，若是"严禁/不得"修饰，则删除。

表现C（安全触发阈值误判）★最常见错误★
  适用场景：问题涉及"报警浓度""断电浓度""复电浓度""停工阈值""断电阈值"等传感器/应急触发参数。
  判断规则：
    · 报警≥X、断电≥X、停工≥X：X越小=越早触发=越严格。待审X < 法规X → 删除（合规）
    · 复电<X：X越小=要求浓度降得更低才能复电=越严格。待审X < 法规X → 删除（合规）
    · 若理由是"无上位依据"但待审值比法规更严（更早触发）→ 也删除（严于法规即合规）
  关键示例1（法规表格行）：
    法规表格"掘进工作面 | 报警≥1.0 | 断电≥1.5 | 复电<1.0"
    待审"T1：报警≥0.8，断电≥1.2，复电<0.8"
    → 逐项：0.8<1.0 ✓，1.2<1.5 ✓，0.8<1.0 ✓ → 三项全部更早/更严触发 → 全部合规 → 删除
  关键示例2（回风流断电阈值）：
    法规§193"风流中甲烷≥1.0%时停止用电作业"
    待审"回风流≥0.8%时停止工作" → 0.8<1.0，更早停工，严于法规 → 合规 → 删除
  反例（不删除）：法规"报警≥1.0%"，待审"报警≥1.2%"→ 1.2>1.0，更晚报警，真实违规，保留。

━━ 误判类型2：引用知识库外条款或数据缺失 ━━
  问题引用的条款/数值在知识库原文中不存在，或法规字段为空/"数据不完整"。
  → 删除，整体结果改为"不确定"。

━━ 误判类型3：并列条件被误读为"任择其一" ━━
  → 删除。

━━ 误判类型4：固定数值因"刚性"被判违规 ━━
  固定数值本身在法规允许范围内，仅因为是固定值而被判违规。
  → 检查该值是否违反法规数值限制，若未违反则删除。

━━ 误判类型5：等待后检查被判违反"先检查"原则 ━━
  → 删除。

━━ 误判类型6：误差/偏差范围方向误判 ━━
  法规"误差在A~B"，待审"±X"（X≤B），初审认为"允许负偏差"违规。
  → 删除（偏差范围不区分正负方向）。

【保留原则】真实数值冲突（待审数值确实比法规更宽松）和真实规则冲突必须保留。不确定是否误判时，保留。
【数值类问题优先采信数值核验工具结论】工具已完成单位换算和方向判断，可靠性高于心算。

请输出修正后的完整审核结果：
{{
    "compliance_status": "合规/不合规/不确定",
    "issues": [...],
    "summary": "修正后的一句话结论",
    "verification_notes": "说明删除了哪些误判项及原因（若无修正填'初审结果准确，无误判'）"
}}
"""
        try:
            result_text = self._chat(
                [{"role": "system",
                  "content": "你是合规审核质量控制专家，专门纠正初审中的误判。只删除误判项，不新增问题。严格按JSON格式输出。"},
                 {"role": "user", "content": prompt}],
            )
            verified = self._parse_json_response(result_text)
            if verified is not None:
                # 尊重验证智能体返回的状态（如"不确定"）；只在状态缺失时按issues推断
                if "compliance_status" not in verified or not verified["compliance_status"]:
                    verified["compliance_status"] = "合规" if not verified.get("issues") else "不合规"
                elif not verified.get("issues") and verified.get("compliance_status") not in ("不确定",):
                    verified["compliance_status"] = "合规"
                return verified
            return initial_result
        except FatalAPIError:
            raise
        except Exception as exc:
            self._log(f"    验证失败: {exc}，使用初审结果")
            return initial_result

    # ============ 完整合规审核流程（含数值核验与升级判定） ============

    @staticmethod
    def _is_noncompliant_review(review_result: Dict) -> bool:
        status = str(review_result.get("compliance_status", ""))
        return bool(review_result.get("issues")) or ("不合规" in status)

    def review_chunk_complete(self, pending_chunk: Dict, kb_results: List[Dict]
                              ) -> Tuple[Dict, List[Dict], List[Dict], List[Dict], Dict[str, float]]:
        """合规链：初审 -> 数值核验工具 -> 核验智能体 -> 立场分类（仅不合规）。
        返回 (审核结果, 分类, 数值核验, 升级项, 耗时)。"""
        classifications: List[Dict] = []
        numeric_checks: List[Dict] = []
        escalations: List[Dict] = []
        timings: Dict[str, float] = {}

        review_start = time.time()
        draft = self.review_chunk_with_llm(pending_chunk, kb_results)
        timings["compliance_review"] = time.time() - review_start
        if draft.get("error") or draft.get("parse_error"):
            draft["verified"] = False
            escalations.append({
                "type": ESC_ERROR,
                "reason": f"初审失败: {draft.get('error') or draft.get('parse_error')}",
            })
            return draft, classifications, numeric_checks, escalations, timings

        has_issues = bool(draft.get("issues"))
        status = draft.get("compliance_status", "")
        needs_verify = VERIFY_NON_COMPLIANT and has_issues and ("不合规" in status or "不确定" in status)

        # 数值核验工具（仅对有问题的chunk）
        if has_issues:
            numeric_start = time.time()
            numeric_checks = self.run_numeric_checks(pending_chunk, draft.get("issues", []))
            timings["numeric_check"] = time.time() - numeric_start

        if needs_verify:
            verify_start = time.time()
            final_result = self.verify_review_result(
                pending_chunk, kb_results, draft, numeric_checks
            )
            timings["verification"] = time.time() - verify_start
            final_result["verified"] = True
            final_result["draft_issue_count"] = len(draft.get("issues", []))
        else:
            draft["verified"] = False
            final_result = draft

        # ---- 反向数值核验（v9.20）：初审未报问题时用工具反查漏报 ----
        if (not has_issues and kb_results and not self._fatal.is_set()
                and self._needs_counter_check(pending_chunk["content"])):
            counter_start = time.time()
            try:
                counter = self.run_counter_numeric_check(pending_chunk, kb_results)
            except FatalAPIError:
                raise
            except Exception as exc:
                counter = {"has_pairs": False, "overall": "无可比数值", "details": [],
                           "summary": f"反向核验失败: {str(exc)[:60]}"}
            timings["counter_check"] = time.time() - counter_start
            if counter.get("has_pairs"):
                counter["is_counter_check"] = True
                numeric_checks.append(counter)
                if counter.get("overall") == "不合规":
                    final_result["compliance_status"] = "不确定"
                    final_result["summary"] = (
                        str(final_result.get("summary", "")).rstrip("。")
                        + "；反向数值核验发现疑似超限数值，已升级待裁决"
                    ).lstrip("；")
                    escalations.append({
                        "type": ESC_COUNTER_CHECK,
                        "reason": ("初审判合规，但反向数值核验工具发现疑似不合规数值"
                                   f"（{counter.get('summary', '')}）"),
                    })

        # ---- 升级判定 ----
        tool_overalls = [c["overall"] for c in numeric_checks
                         if c.get("has_pairs") and not c.get("is_counter_check")]
        tool_says_bad = "不合规" in tool_overalls
        tool_all_ok = bool(tool_overalls) and all(o == "合规" for o in tool_overalls)
        final_bad = self._is_noncompliant_review(final_result)
        final_status = str(final_result.get("compliance_status", ""))

        if ("不确定" in final_status
                and not any(e["type"] == ESC_COUNTER_CHECK for e in escalations)):
            escalations.append({"type": ESC_LOW_CONFIDENCE,
                                "reason": "最终结论为'不确定'，需主智能体/人工裁决"})
        if has_issues and not final_bad and tool_says_bad:
            escalations.append({"type": ESC_DISAGREEMENT,
                                "reason": "核验智能体删除了全部问题，但数值核验工具判定存在不合规数值"})
        if final_bad and tool_all_ok:
            escalations.append({"type": ESC_NUMERIC_CONFLICT,
                                "reason": "最终结论不合规，但数值核验工具判定所有配对数值均合规（疑似方向误判）"})

        if final_bad:
            classify_start = time.time()
            classifications = self.classify_kb_chunks(pending_chunk, kb_results)
            timings["kb_classification"] = time.time() - classify_start

        return final_result, classifications, numeric_checks, escalations, timings

    # ============ 智能体4：错别字检查（与v8一致） ============

    TYPO_SYSTEM_PROMPT = """你是一名专业的煤矿安全文档审校专家，负责检查作业规程中的错别字和用词错误。

检查范围（只报告以下类型）：
1. 错别字：汉字写错，如"既"与"即"、"做"与"作"、"在"与"再"混用
2. 术语错误：煤矿专业术语写错，如"综采"误写为"综彩"
3. 明显用词错误：在上下文中明显不通顺或语义矛盾的词语

不检查的内容：
- 标点符号、格式、排版问题
- 数字、单位数值（单位符号错误由规程审查负责，此处不报）
- 纯符号数学公式（如仅由字母、数字、运算符、括号组成的表达式，不含汉字）
- 单位格式问题：上标缺失（"m 2"）、单位字母拆分（"m i n"）、斜杠截断（"m 3 /"）等
- 语法不通顺但无错字的句子
- 专有名词、地名、人名的用字习惯差异
- 行业内约定俗成的缩写简称（如"安培中心""通防"等）
- 以下 OCR 扫描噪声，一律不报：
  * 型号/规格中多余的符号或空格（如"MD155-. 30×4"中的"-."）
  * 编号中字母与数字形近的OCR误读（如"S↔5"、"O↔0"）
  * 文本末尾孤立的单个字母（如"200 mm c"末尾的"c"）
  * 重复的间隔符（如"··"）

审查原则：
- 积极报告：在正文汉字句子中发现疑似错别字时，应上报，不要因"可能是专业术语"就放弃
- 煤矿专业术语存疑时仍可上报，但在 reason 中注明"不确定是否为专业术语，建议人工核实"
- 不得以全称替代简称为由报告错别字
- 每个错别字单独报告，不合并

输出格式（严格 JSON 数组，不含任何 Markdown 代码块标记）：
若发现错别字，返回：
[
  {
    "wrong_char": "错误的字或词",
    "correct_char": "正确的字或词",
    "context": "包含错别字的原文片段（前后各约10个字）",
    "reason": "判断依据（简短说明为何是错别字）",
    "confidence": "高|中|低"
  }
]
confidence 说明：高=确定是错别字；中=可能是错别字但有一定不确定性；低=存疑，可能是专业术语或OCR噪声。
若无错别字，返回：[]"""

    TYPO_VERIFY_PROMPT = """判断以下错别字报告是否为误报。

原文上下文：{context}
报告的错误：「{wrong_char}」→「{correct_char}」
判断依据：{reason}

以下情形为误报，回答 false：
- 煤矿专业术语（即使普通词典未收录）
- OCR 扫描噪声（多余符号、字母数字形近误读）
- 单位/公式/编号的格式问题
- 行业缩写简称

只回答 true（确实是错别字）或 false（误报），不输出其他内容。"""

    @staticmethod
    def _strip_json_fence(raw: str) -> str:
        raw = raw.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return raw.strip()

    def _verify_typo_issue(self, issue: Dict) -> bool:
        if issue.get("confidence") == "高":
            return True
        prompt = self.TYPO_VERIFY_PROMPT.format(
            context=issue.get("context", ""),
            wrong_char=issue.get("wrong_char", ""),
            correct_char=issue.get("correct_char", ""),
            reason=issue.get("reason", ""),
        )
        try:
            content = self._chat([{"role": "user", "content": prompt}],
                                 temperature=0, max_tokens=5)
            return content.strip().lower().startswith("true")
        except FatalAPIError:
            raise
        except Exception:
            return True

    def check_typos(self, pending_chunk: Dict) -> Dict:
        content = pending_chunk.get("content", "").strip()
        default = {"has_issues": False, "issues": [], "summary": "未发现错别字"}
        if len(content) < TYPO_MIN_CHUNK_LEN:
            return default

        user_msg = f"请检查以下文本中的错别字：\n\n{content}"
        last_error = ""
        for attempt in range(3):
            try:
                raw = self._strip_json_fence(self._chat(
                    [{"role": "system", "content": self.TYPO_SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
                    temperature=0.0,
                ))
                if not raw:
                    raise ValueError("空响应")
                result = json.loads(raw)
                if not isinstance(result, list):
                    return default

                skip_keywords = ("未出现", "暂不报", "无需报告", "不报告", "不存在", "无错")
                filtered = [
                    item for item in result
                    if isinstance(item, dict)
                    and item.get("wrong_char", "").strip()
                    and item.get("wrong_char", "").strip() != item.get("correct_char", "").strip()
                    and not any(kw in item.get("correct_char", "") for kw in skip_keywords)
                    and not any(kw in item.get("reason", "") for kw in skip_keywords)
                ]
                filtered = [item for item in filtered if self._verify_typo_issue(item)]
                issues = [
                    {**item,
                     "original": item.get("wrong_char", ""),
                     "suggestion": item.get("correct_char", "")}
                    for item in filtered
                ]
                return {
                    "has_issues": bool(issues),
                    "issues": issues,
                    "summary": f"发现 {len(issues)} 处疑似错别字" if issues else "未发现错别字",
                }
            except FatalAPIError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if attempt < 2:
                    time.sleep(1)
        return {**default, "summary": f"检查异常（重试3次）: {last_error[:60]}"}

    # ============ 智能体5：重复性检查（向量改用BGE，逻辑与v8一致） ============

    REDUNDANCY_PROMPT = """你是煤矿作业规程专家。判断以下两段作业规程内容是否存在实质性重复。

【片段A】
位置：{chapter_a} / {section_a}
{content_a}

【片段B】
位置：{chapter_b} / {section_b}
{content_b}

判断规则：
1. 两段内容表述相同要求（哪怕用词不同）→ "重复"
2. 两段内容属于同类操作的不同场景、不同工序或不同层级要求 → "正常"

只输出以下 JSON（无 Markdown 标记）：
{{"type":"重复"|"正常","description":"简要说明（30字以内）"}}"""

    def _verify_redundancy_pair(self, chunk_a: Dict, chunk_b: Dict) -> Dict:
        prompt = self.REDUNDANCY_PROMPT.format(
            chapter_a=chunk_a.get("chapter", ""),
            section_a=chunk_a.get("section", ""),
            content_a=chunk_a.get("content", "")[:600],
            chapter_b=chunk_b.get("chapter", ""),
            section_b=chunk_b.get("section", ""),
            content_b=chunk_b.get("content", "")[:600],
        )
        try:
            content = self._chat([{"role": "user", "content": prompt}],
                                 temperature=0, max_tokens=150)
            return json.loads(self._strip_json_fence(content))
        except FatalAPIError:
            raise
        except Exception:
            return {"type": "正常", "description": ""}

    @staticmethod
    def _empty_repetition_result(summary: str = "无重复内容") -> Dict:
        return {"has_duplicates": False, "duplicates": [], "summary": summary}

    def check_document_repetition(self, doc_name: str, chunks: List[Dict],
                                  dense_vecs: np.ndarray
                                  ) -> Tuple[Dict[int, Dict], Dict[str, float]]:
        """整篇文档重复性检查。向量复用 Phase A 的 BGE dense 编码（零额外编码成本）。"""
        started = time.time()
        results = {i: self._empty_repetition_result() for i in range(len(chunks))}
        valid = [
            (i, {**chunk, "doc_name": doc_name})
            for i, chunk in enumerate(chunks)
            if len(chunk.get("content", "")) >= REDUNDANCY_MIN_CHUNK_LEN
        ]
        if len(valid) < 2:
            return results, {"embedding": 0.0, "verification": 0.0, "total": time.time() - started}

        valid_indices = [i for i, _ in valid]
        vectors = dense_vecs[valid_indices].astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1, norms)
        similarity_matrix = vectors @ vectors.T

        candidates = []
        for i in range(len(valid)):
            for j in range(i + 1, len(valid)):
                similarity = float(similarity_matrix[i, j])
                if similarity >= REDUNDANCY_SIMILARITY_THRESHOLD:
                    candidates.append((i, j, similarity))
        candidates.sort(key=lambda item: -item[2])

        verify_start = time.time()
        for valid_i, valid_j, similarity in candidates:
            if self._fatal.is_set():
                break
            chunk_i, chunk_a = valid[valid_i]
            chunk_j, chunk_b = valid[valid_j]
            verdict = self._verify_redundancy_pair(chunk_a, chunk_b)
            if verdict.get("type", "正常") != "重复":
                continue
            description = verdict.get("description", "")
            pair_data = {
                "redundancy_type": "重复",
                "similarity": round(similarity, 3),
                "description": description,
                "chunk_a": {
                    "doc_name": doc_name,
                    "chapter": chunk_a.get("chapter", ""),
                    "section": chunk_a.get("section", ""),
                    "content": chunk_a.get("content", ""),
                    "page_range": chunk_a.get("page_range", ""),
                },
                "chunk_b": {
                    "doc_name": doc_name,
                    "chapter": chunk_b.get("chapter", ""),
                    "section": chunk_b.get("section", ""),
                    "content": chunk_b.get("content", ""),
                    "page_range": chunk_b.get("page_range", ""),
                },
            }
            for own_idx, other_idx, other_chunk in (
                (chunk_i, chunk_j, chunk_b),
                (chunk_j, chunk_i, chunk_a),
            ):
                duplicate = {
                    **pair_data,
                    "chunk_ref": (
                        f"Chunk#{other_idx + 1}"
                        f"（{other_chunk.get('chapter', '')} / {other_chunk.get('section', '')}）"
                    ),
                    "note": description,
                }
                results[own_idx]["duplicates"].append(duplicate)
                results[own_idx]["has_duplicates"] = True

        for result in results.values():
            if result["has_duplicates"]:
                result["summary"] = f"发现 {len(result['duplicates'])} 处实质性重复"
        return results, {
            "embedding": 0.0,  # 向量复用Phase A结果
            "verification": time.time() - verify_start,
            "total": time.time() - started,
            "candidates": len(candidates),
        }

    # ============ 单chunk全任务（Phase B 工作单元） ============

    @staticmethod
    def _timed_call(fn, *args):
        start = time.time()
        result = fn(*args)
        return result, time.time() - start

    def _process_single_chunk(self, doc_name: str, chunk_index: int,
                              chunk: Dict, kb_results: List[Dict]) -> Dict:
        """单chunk的合规链+错别字链（并行），返回完整结果dict并写升级队列。"""
        chunk_start = time.time()
        escalations: List[Dict] = []
        numeric_checks: List[Dict] = []
        classifications: List[Dict] = []
        agent_timings: Dict[str, float] = {}

        if not kb_results:
            try:
                typo_start = time.time()
                typo_result = self.check_typos(chunk)
                agent_timings["typo"] = time.time() - typo_start
            except FatalAPIError:
                typo_result = {"has_issues": False, "issues": [], "summary": "检查中断（API不可用）"}
            except Exception:
                typo_result = {"has_issues": False, "issues": [], "summary": "检查异常"}
            review_result = {"compliance_status": "不确定", "summary": "未找到相关法规", "issues": []}
        else:
            try:
                with ThreadPoolExecutor(max_workers=2) as exe:
                    f_compliance = exe.submit(self.review_chunk_complete, chunk, kb_results)
                    f_typo = exe.submit(self._timed_call, self.check_typos, chunk)
                    review_result, classifications, numeric_checks, escalations, agent_timings = \
                        f_compliance.result()
                    typo_result, typo_elapsed = f_typo.result()
                    agent_timings["typo"] = typo_elapsed
            except FatalAPIError:
                review_result = {"compliance_status": "不确定", "summary": "审查中断（API不可用）", "issues": []}
                typo_result = {"has_issues": False, "issues": [], "summary": "检查中断"}
            except Exception as exc:
                err = str(exc)
                review_result = {"compliance_status": "不确定",
                                 "summary": f"调用失败: {err[:60]}", "issues": []}
                typo_result = {"has_issues": False, "issues": [], "summary": "检查异常"}
                escalations.append({"type": ESC_ERROR, "reason": f"chunk处理异常: {err[:120]}"})

        # ---- 写升级队列（去重键保证断点续跑不重复入队） ----
        for esc in escalations:
            self.queue.add_escalation(
                esc["type"], doc_name, chunk_index,
                payload={
                    "reason": esc["reason"],
                    "chunk_content": chunk["content"][:1200],
                    "chunk_meta": {
                        "chapter": chunk.get("chapter", ""),
                        "section": chunk.get("section", ""),
                        "page_range": chunk.get("page_range", ""),
                    },
                    "review_result": review_result,
                    "numeric_checks": numeric_checks,
                    "kb_refs": [
                        {"doc": r["chunk"]["doc_name"],
                         "chapter": r["chunk"].get("chapter", ""),
                         "content": r["chunk"]["content"][:800],
                         "score": r["score"]}
                        for r in kb_results
                    ],
                },
                dedupe_key=f"{doc_name}#{chunk_index}#{esc['type']}",
            )

        show_kb_refs = self._is_noncompliant_review(review_result)
        elapsed = time.time() - chunk_start
        status_text = review_result.get("compliance_status", "?")
        esc_text = f" ↑升级{len(escalations)}" if escalations else ""
        numeric_elapsed = (agent_timings.get("numeric_check", 0)
                           + agent_timings.get("counter_check", 0))
        self._log(f"  [完成] chunk#{chunk_index + 1} {status_text}{esc_text} "
                  f"耗时{elapsed:.1f}s "
                  f"(初审{agent_timings.get('compliance_review', 0):.1f}s/"
                  f"数值{numeric_elapsed:.1f}s/"
                  f"核验{agent_timings.get('verification', 0):.1f}s/"
                  f"错别字{agent_timings.get('typo', 0):.1f}s)")

        return {
            "chunk_index": chunk_index,
            "chunk_content": chunk["content"],
            "chunk_meta": {
                "chapter": chunk.get("chapter", ""),
                "section": chunk.get("section", ""),
                "page_range": chunk.get("page_range", ""),
            },
            "kb_matches": len(kb_results) if show_kb_refs else 0,
            "kb_refs": [
                {"doc": r["chunk"]["doc_name"],
                 "chapter": r["chunk"].get("chapter", ""),
                 "section": r["chunk"].get("section", ""),
                 "score": round(r["score"], 4),
                 "chunk_id": r["chunk"]["id"],
                 "content": r["chunk"]["content"]}
                for r in kb_results
            ] if show_kb_refs else [],
            "kb_classifications": classifications if show_kb_refs else [],
            "review_result": review_result,
            "typo_result": typo_result,
            "numeric_checks": numeric_checks,
            "escalations": [e["type"] for e in escalations],
            "repetition_result": self._empty_repetition_result("等待文档级重复性检查回填"),
        }

    # ============ 审核待审文档（Phase A 检索预计算 + Phase B 并发审查） ============

    def review_pending_document(self, pending_json_path: Optional[str] = None,
                                doc_filter: Optional[List[str]] = None,
                                max_documents: Optional[int] = MAX_PENDING_DOCS,
                                chunk_concurrency: int = CHUNK_CONCURRENCY,
                                fresh: bool = False) -> Dict:
        if pending_json_path is None:
            for version in ["v9", "v8", "v7", "v6", "v5", "v4", "v3", "v2"]:
                files = sorted(PENDING_CHUNKS_DIR.glob(f"pending_doc_chunks_{version}_*.json"), reverse=True)
                if files:
                    pending_json_path = files[0]
                    break
            if pending_json_path is None:
                raise FileNotFoundError("未找到待审文档chunks文件")

        print(f"\n加载待审文档: {pending_json_path}")
        with open(pending_json_path, "r", encoding="utf-8") as f:
            pending_data = json.load(f)

        if doc_filter is None:
            doc_filter = DOC_FILTER
        if doc_filter:
            pending_data = {
                k: v for k, v in pending_data.items()
                if any(f in k for f in doc_filter)
            }
            print(f"  过滤后文档数: {len(pending_data)} (过滤条件: {doc_filter})")

        if max_documents and max_documents > 0:
            pending_data = dict(list(pending_data.items())[:max_documents])
            print(f"  限制：仅审核前 {max_documents} 个待审文档: {list(pending_data.keys())}")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        incremental_suffix = f"_first_{max_documents}" if max_documents and max_documents > 0 else ""
        incremental_path = OUTPUT_DIR / f"review_result_v9_incremental{incremental_suffix}.json"
        all_results: Dict = {}
        if fresh:
            print("  [--fresh] 忽略已有增量结果，全部重新审核")
        elif incremental_path.exists():
            try:
                with open(incremental_path, "r", encoding="utf-8") as f:
                    all_results = json.load(f)
                print(f"  [续跑] 已加载增量结果，跳过已完成文档: {list(all_results.keys())}")
            except Exception:
                all_results = {}

        doc_timings = {}
        total_start = time.time()

        for doc_name, chunks in pending_data.items():
            if doc_name in all_results:
                print(f"\n跳过（已有结果）: {doc_name}")
                continue
            if self._fatal.is_set():
                print(f"\n[跳过] API不可用，暂停后续文档: {doc_name}")
                continue

            doc_start = time.time()
            print(f"\n审核文档: {doc_name} ({len(chunks)} chunks, 并发={chunk_concurrency})")

            # ---- Phase A：批量检索（GPU，无LLM）----
            kb_results_all, q_dense = self.prepare_retrieval(chunks)

            # ---- Phase B：三并行主链 ----
            print("  [Phase B] 三并行：文档级重复性 ‖ 合规链 ‖ 错别字链（chunk级并发）")
            repetition_executor = ThreadPoolExecutor(max_workers=1)
            repetition_future = repetition_executor.submit(
                self.check_document_repetition, doc_name, chunks, q_dense
            )

            results_by_index: Dict[int, Dict] = {}
            with ThreadPoolExecutor(max_workers=chunk_concurrency) as pool:
                futures = {
                    pool.submit(self._process_single_chunk, doc_name, i, chunk, kb_results_all[i]): i
                    for i, chunk in enumerate(chunks)
                }
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        results_by_index[i] = future.result()
                    except Exception as exc:
                        self._log(f"  [异常] chunk#{i + 1}: {str(exc)[:80]}")
                        results_by_index[i] = {
                            "chunk_index": i,
                            "chunk_content": chunks[i]["content"],
                            "chunk_meta": {
                                "chapter": chunks[i].get("chapter", ""),
                                "section": chunks[i].get("section", ""),
                                "page_range": chunks[i].get("page_range", ""),
                            },
                            "kb_matches": 0, "kb_refs": [], "kb_classifications": [],
                            "review_result": {"compliance_status": "不确定",
                                              "summary": f"处理异常: {str(exc)[:60]}", "issues": []},
                            "typo_result": {"has_issues": False, "issues": [], "summary": "检查异常"},
                            "numeric_checks": [], "escalations": [ESC_ERROR],
                            "repetition_result": self._empty_repetition_result("未取得结果"),
                        }

            print("  等待文档级重复性检查并回填结果...", end=" ", flush=True)
            try:
                repetition_by_chunk, repetition_timings = repetition_future.result()
                print(f"候选对 {repetition_timings.get('candidates', 0)}，"
                      f"耗时 {repetition_timings.get('total', 0.0):.1f}s")
            except Exception as exc:
                print(f"失败({str(exc)[:60]})")
                repetition_by_chunk = {
                    i: self._empty_repetition_result(f"检查异常: {str(exc)[:60]}")
                    for i in range(len(chunks))
                }
            finally:
                repetition_executor.shutdown(wait=True)

            doc_results = [results_by_index[i] for i in sorted(results_by_index)]
            for result in doc_results:
                result["repetition_result"] = repetition_by_chunk.get(
                    result["chunk_index"],
                    self._empty_repetition_result("未取得重复性检查结果"),
                )

            doc_elapsed = time.time() - doc_start
            doc_timings[doc_name] = doc_elapsed

            if self._fatal.is_set():
                # API熔断的文档不保存，续跑时整篇重审，避免半截结果被跳过
                print(f"  ⚠️ 文档审核中断（API不可用），不保存该文档结果，耗时 {doc_elapsed:.1f}s")
            else:
                all_results[doc_name] = doc_results
                self._save_incremental(all_results, incremental_path)
                print(f"  文档审核完成，耗时 {doc_elapsed:.1f}s  [已增量保存]")

        total_elapsed = time.time() - total_start
        print(f"\n全部处理完毕，总耗时 {total_elapsed:.1f}s")
        for name, t in doc_timings.items():
            print(f"  {name}: {t:.1f}s")
        queue_stats = self.queue.stats()
        pending_count = queue_stats.get("pending", 0)
        if pending_count:
            print(f"  ⚠️ 升级队列待处理 {pending_count} 项"
                  f"（按类型: {queue_stats.get('pending_by_type', {})}），"
                  f"可运行: python main_agent_v9.py --process-queue")
        if self._fatal.is_set():
            print("  ⚠️ 因API不可用提前终止，请检查账户后重新运行（将自动续跑）")

        return all_results

    # ============ 保存与报告 ============

    @staticmethod
    def _slim_results(results: Dict) -> Dict:
        slim = {}
        for doc_name, doc_results in results.items():
            slim[doc_name] = []
            for r in doc_results:
                r_slim = dict(r)
                r_slim["kb_refs"] = [
                    {k: v for k, v in ref.items() if k != "content"}
                    for ref in r.get("kb_refs", [])
                ]
                slim[doc_name].append(r_slim)
        return slim

    def _save_incremental(self, results: Dict, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._slim_results(results), f, ensure_ascii=False, indent=2)

    def save_results(self, results: Dict, output_path: Optional[str] = None):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = OUTPUT_DIR / f"review_result_v9_{timestamp}.json"
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self._slim_results(results), f, ensure_ascii=False, indent=2)
        print(f"\n审核结果已保存: {output_path}")
        return output_path

    def generate_report(self, results: Dict, output_path: Optional[str] = None):
        """HTML审核报告 v9（v8样式 + 数值核验工具结论 + 升级标记）"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = OUTPUT_DIR / f"review_report_v9_{timestamp}.html"

        total_chunks = 0
        status_counts = {"合规": 0, "不合规": 0, "不确定": 0}
        verified_count = 0
        corrected_count = 0
        typo_chunk_count = 0
        rep_chunk_count = 0
        escalated_count = 0
        numeric_checked_count = 0

        for doc_results in results.values():
            for r in doc_results:
                total_chunks += 1
                review = r.get("review_result", {})
                status = review.get("compliance_status", "不确定")
                if "合规" in status and "不" not in status:
                    status_counts["合规"] += 1
                elif "不合规" in status:
                    status_counts["不合规"] += 1
                else:
                    status_counts["不确定"] += 1
                if review.get("verified"):
                    verified_count += 1
                    if len(review.get("issues", [])) < review.get("draft_issue_count", 0):
                        corrected_count += 1
                if r.get("typo_result", {}).get("has_issues"):
                    typo_chunk_count += 1
                if r.get("repetition_result", {}).get("has_duplicates"):
                    rep_chunk_count += 1
                if r.get("escalations"):
                    escalated_count += 1
                if any(c.get("has_pairs") for c in r.get("numeric_checks", [])):
                    numeric_checked_count += 1

        clf_colors = {"支持": "#27ae60", "反对": "#e74c3c", "例外": "#e67e22", "无关": "#95a5a6"}
        verdict_colors = {"合规": "#27ae60", "不合规": "#e74c3c", "不确定": "#f39c12"}

        kb_full_texts: Dict[str, str] = {}
        for doc_results in results.values():
            for r in doc_results:
                for ref in r.get("kb_refs", []):
                    cid = ref.get("chunk_id", "")
                    if cid and cid not in kb_full_texts:
                        kb_full_texts[cid] = ref.get("content", "")

        def esc(s) -> str:
            return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;")
                    .replace("'", "&#39;").replace("\n", "<br>"))

        style = """
body { font-family: 'Microsoft YaHei', Arial, sans-serif; margin: 0; background: #f0f2f5; }
.page-header { background: #2c3e50; color: white; padding: 20px 30px; }
.page-header h1 { margin: 0 0 6px 0; font-size: 1.4em; }
.page-header .meta { font-size: 0.85em; opacity: 0.8; }
.main { max-width: 1200px; margin: 20px auto; padding: 0 20px; }
.summary-card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 12px; margin-top: 12px; }
.stat-card { padding: 14px; border-radius: 8px; text-align: center; color: white; }
.stat-card.blue { background: linear-gradient(135deg,#3498db,#2980b9); }
.stat-card.green { background: linear-gradient(135deg,#27ae60,#219a52); }
.stat-card.red { background: linear-gradient(135deg,#e74c3c,#c0392b); }
.stat-card.gray { background: linear-gradient(135deg,#95a5a6,#7f8c8d); }
.stat-card.purple { background: linear-gradient(135deg,#8e44ad,#7d3c98); }
.stat-card.orange { background: linear-gradient(135deg,#e67e22,#ca6f1e); }
.stat-card.teal { background: linear-gradient(135deg,#16a085,#1abc9c); }
.stat-card.dark { background: linear-gradient(135deg,#34495e,#2c3e50); }
.stat-value { font-size: 2em; font-weight: bold; }
.stat-label { font-size: 0.8em; margin-top: 4px; opacity: 0.9; }
.doc-header { background: #34495e; color: white; padding: 10px 16px; border-radius: 6px;
  margin: 24px 0 10px 0; font-size: 1.05em; }
.chunk-card { background: white; border-radius: 8px; margin-bottom: 12px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08); overflow: hidden; }
.chunk-card-header { display: flex; align-items: center; gap: 10px; padding: 12px 16px;
  border-bottom: 1px solid #f0f0f0; flex-wrap: wrap; }
.chunk-num { font-weight: bold; color: #2c3e50; font-size: 1em; }
.chunk-level-tag { background: #ecf0f1; color: #555; font-size: 0.75em;
  padding: 2px 8px; border-radius: 10px; }
.status-badge { padding: 4px 12px; border-radius: 4px; color: white;
  font-weight: bold; font-size: 0.85em; }
.badge-compliant { background: #27ae60; }
.badge-noncompliant { background: #e74c3c; }
.badge-uncertain { background: #f39c12; }
.badge-verified { background: #8e44ad; font-size: 0.75em; padding: 2px 8px; }
.badge-corrected { background: #e67e22; font-size: 0.75em; padding: 2px 8px; }
.badge-typo { background: #d35400; font-size: 0.75em; padding: 2px 8px; }
.badge-rep { background: #16a085; font-size: 0.75em; padding: 2px 8px; }
.badge-escalated { background: #2c3e50; font-size: 0.75em; padding: 2px 8px; }
.badge-numeric { background: #2980b9; font-size: 0.75em; padding: 2px 8px; }
.typo-list, .rep-list, .numeric-list { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
.typo-item { background: #fef9e7; border: 1px solid #f9ca24; border-radius: 4px;
  padding: 7px 10px; font-size: 0.85em; }
.typo-wrong { font-weight: bold; color: #c0392b; }
.typo-fix { color: #27ae60; }
.typo-ctx { color: #7f8c8d; font-size: 0.82em; }
.rep-item { background: #eafaf1; border: 1px solid #82e0aa; border-radius: 4px;
  padding: 7px 10px; font-size: 0.85em; }
.rep-ref { font-weight: bold; color: #1a5276; }
.rep-note { color: #555; }
.numeric-item { background: #ebf5fb; border: 1px solid #aed6f1; border-radius: 4px;
  padding: 7px 10px; font-size: 0.85em; }
.numeric-verdict { font-weight: bold; padding: 1px 8px; border-radius: 10px;
  color: white; font-size: 0.85em; margin-right: 6px; }
.chunk-meta { color: #7f8c8d; font-size: 0.82em; padding: 6px 16px 0 16px; }
.chunk-body { padding: 12px 16px; max-height: 360px; overflow-y: auto; }
.content-box { background: #fafafa; border: 1px solid #eee; border-radius: 4px;
  padding: 10px; white-space: pre-wrap; font-size: 0.88em; line-height: 1.6;
  max-height: 180px; overflow-y: auto; color: #333; }
.section-title { font-weight: bold; color: #2c3e50; margin: 10px 0 6px 0; font-size: 0.9em; }
.kb-refs-list { display: flex; flex-direction: column; gap: 6px; }
.kb-ref-item { display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px;
  background: #f8f9fa; border-radius: 4px; border: 1px solid #e9ecef; font-size: 0.85em; }
.kb-ref-info { flex: 1; min-width: 0; }
.kb-ref-title { color: #2c3e50; font-weight: 500; margin-bottom: 2px; }
.kb-ref-sub { color: #7f8c8d; font-size: 0.85em; }
.clf-badge { padding: 2px 8px; border-radius: 10px; color: white;
  font-size: 0.78em; white-space: nowrap; font-weight: bold; }
.clf-btn { background: none; border: 1px solid #bdc3c7; border-radius: 4px;
  padding: 2px 8px; font-size: 0.78em; cursor: pointer; color: #555;
  white-space: nowrap; }
.clf-btn:hover { background: #ecf0f1; }
.issues-list { margin-top: 8px; }
.issue-item { background: #fff5f5; border: 1px solid #ffcccc; border-radius: 4px;
  padding: 10px 12px; margin-bottom: 6px; }
.issue-type { font-weight: bold; color: #c0392b; font-size: 0.85em; }
.issue-desc { color: #333; margin: 4px 0; font-size: 0.88em; }
.issue-quote { color: #7f8c8d; font-size: 0.82em; margin: 2px 0; }
.issue-suggestion { color: #27ae60; font-size: 0.85em; margin-top: 4px; }
.verify-note { background: #f3e5f5; border-left: 3px solid #8e44ad;
  padding: 8px 10px; margin-top: 8px; border-radius: 0 4px 4px 0;
  font-size: 0.85em; color: #555; }
.summary-text { color: #2c3e50; font-size: 0.9em; margin-top: 8px;
  padding: 8px 10px; background: #f8f9fa; border-radius: 4px; }
.modal-overlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0,0,0,0.5); z-index: 1000; justify-content: center; align-items: center; }
.modal-overlay.active { display: flex; }
.modal-box { background: white; border-radius: 8px; max-width: 700px; width: 90%;
  max-height: 80vh; display: flex; flex-direction: column; box-shadow: 0 8px 32px rgba(0,0,0,0.2); }
.modal-header { padding: 16px 20px; border-bottom: 1px solid #eee;
  display: flex; justify-content: space-between; align-items: flex-start; }
.modal-title { font-weight: bold; color: #2c3e50; font-size: 0.95em; flex: 1; margin-right: 12px; }
.modal-close { background: none; border: none; font-size: 1.4em; cursor: pointer;
  color: #7f8c8d; line-height: 1; padding: 0; }
.modal-close:hover { color: #e74c3c; }
.modal-body { padding: 16px 20px; overflow-y: auto; flex: 1; }
.modal-content { white-space: pre-wrap; font-size: 0.88em; line-height: 1.7;
  color: #333; background: #fafafa; padding: 12px; border-radius: 4px; }
"""

        js = """
const kbTexts = %s;

function showKB(chunkId, title) {
  const text = kbTexts[chunkId] || '（内容不可用）';
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-content').textContent = text;
  document.getElementById('kb-modal').classList.add('active');
}

function closeModal() {
  document.getElementById('kb-modal').classList.remove('active');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeModal();
});
""" % json.dumps(kb_full_texts, ensure_ascii=False)

        parts = [
            "<!DOCTYPE html>",
            "<html lang='zh-CN'><head>",
            "<meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>煤矿作业规程合规审核报告 v9</title>",
            f"<style>{style}</style>",
            "</head><body>",
            "<div class='page-header'>",
            "<h1>煤矿作业规程合规审核报告 v9</h1>",
            f"<div class='meta'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"检索: BGE-M3 dense+sparse RRF + bge-reranker-v2-m3（本地） | TopK: {TOP_K} | "
            f"重排阈值: {RERANK_SCORE_THRESHOLD} | 矿井类型: {self.mine_type} | "
            f"三并行+chunk级并发 | 数值核验: LLM抽取·代码比较</div>",
            "</div>",
            "<div class='main'>",
            "<div class='summary-card'>",
            "<h2 style='margin:0 0 8px 0;color:#2c3e50;font-size:1.1em;'>审核概览</h2>",
            "<div class='stats-grid'>",
            f"<div class='stat-card blue'><div class='stat-value'>{len(results)}</div><div class='stat-label'>审核文档数</div></div>",
            f"<div class='stat-card blue'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>审核chunks数</div></div>",
            f"<div class='stat-card green'><div class='stat-value'>{status_counts['合规']}</div><div class='stat-label'>合规</div></div>",
            f"<div class='stat-card red'><div class='stat-value'>{status_counts['不合规']}</div><div class='stat-label'>不合规</div></div>",
            f"<div class='stat-card gray'><div class='stat-value'>{status_counts['不确定']}</div><div class='stat-label'>不确定</div></div>",
            f"<div class='stat-card purple'><div class='stat-value'>{verified_count}</div><div class='stat-label'>经二次验证</div></div>",
            f"<div class='stat-card orange'><div class='stat-value'>{corrected_count}</div><div class='stat-label'>验证纠正误判</div></div>",
            f"<div class='stat-card' style='background:linear-gradient(135deg,#2980b9,#3498db)'>"
            f"<div class='stat-value'>{numeric_checked_count}</div><div class='stat-label'>数值工具核验</div></div>",
            f"<div class='stat-card dark'><div class='stat-value'>{escalated_count}</div><div class='stat-label'>升级待裁决</div></div>",
            f"<div class='stat-card' style='background:linear-gradient(135deg,#d35400,#e67e22)'>"
            f"<div class='stat-value'>{typo_chunk_count}</div><div class='stat-label'>含错别字chunks</div></div>",
            f"<div class='stat-card teal'><div class='stat-value'>{rep_chunk_count}</div><div class='stat-label'>含重复内容chunks</div></div>",
            "</div></div>",
        ]

        for doc_name, doc_results in results.items():
            visible_results = [
                r for r in doc_results
                if self._is_noncompliant_review(r.get("review_result", {}))
                or r.get("typo_result", {}).get("has_issues")
                or r.get("repetition_result", {}).get("has_duplicates")
                or r.get("escalations")
            ]
            if not visible_results:
                continue
            parts.append(f"<div class='doc-header'>📄 {esc(doc_name)}</div>")

            for r in visible_results:
                review = r.get("review_result", {})
                status = review.get("compliance_status", "不确定")
                is_verified = review.get("verified", False)
                draft_count = review.get("draft_issue_count", 0)
                final_count = len(review.get("issues", []))
                was_corrected = is_verified and final_count < draft_count
                typo = r.get("typo_result", {})
                rep = r.get("repetition_result", {})
                numeric_checks = [c for c in r.get("numeric_checks", []) if c.get("has_pairs")]
                escalation_types = r.get("escalations", [])

                if "合规" in status and "不" not in status:
                    badge_cls = "badge-compliant"
                elif "不合规" in status:
                    badge_cls = "badge-noncompliant"
                else:
                    badge_cls = "badge-uncertain"

                meta = r.get("chunk_meta", {})
                chunk_idx = r["chunk_index"]

                parts.append("<div class='chunk-card'>")
                parts.append("<div class='chunk-card-header'>")
                parts.append(f"<span class='chunk-num'>Chunk #{chunk_idx + 1}</span>")
                if meta.get("section"):
                    parts.append("<span class='chunk-level-tag'>section</span>")
                elif meta.get("chapter"):
                    parts.append("<span class='chunk-level-tag'>chapter</span>")
                parts.append(f"<span class='status-badge {badge_cls}'>{esc(status)}</span>")
                if is_verified:
                    parts.append("<span class='status-badge badge-verified'>已二次验证</span>")
                if was_corrected:
                    parts.append(f"<span class='status-badge badge-corrected'>误判纠正 {draft_count}→{final_count}项</span>")
                if numeric_checks:
                    parts.append("<span class='status-badge badge-numeric'>数值工具核验</span>")
                if escalation_types:
                    parts.append(f"<span class='status-badge badge-escalated'>已升级: {esc('/'.join(escalation_types))}</span>")
                if typo.get("has_issues"):
                    n = len(typo.get("issues", []))
                    parts.append(f"<span class='status-badge badge-typo'>错别字 {n}处</span>")
                if rep.get("has_duplicates"):
                    parts.append("<span class='status-badge badge-rep'>重复内容</span>")
                parts.append("</div>")

                parts.append(
                    f"<div class='chunk-meta'>"
                    f"章: {esc(meta.get('chapter', ''))} | "
                    f"节: {esc(meta.get('section', ''))} | "
                    f"页码: {esc(meta.get('page_range', ''))}"
                    f"</div>"
                )

                parts.append("<div class='chunk-body'>")
                parts.append("<div class='section-title'>待审内容</div>")
                parts.append(f"<div class='content-box'>{esc(r.get('chunk_content', ''))}</div>")

                kb_refs = r.get("kb_refs", [])
                kb_clf = {c["index"] - 1: c for c in r.get("kb_classifications", [])}
                if kb_refs:
                    parts.append("<div class='section-title'>参考法规</div>")
                    parts.append("<div class='kb-refs-list'>")
                    for j, ref in enumerate(kb_refs):
                        clf_info = kb_clf.get(j, {})
                        clf_label = clf_info.get("classification", "无关")
                        clf_color = clf_colors.get(clf_label, "#95a5a6")
                        clf_reason = esc(clf_info.get("reason", ""))
                        chunk_id = ref.get("chunk_id", "")
                        title_str = esc(f"{ref.get('doc', '')} / {ref.get('chapter', '')} / {ref.get('section', '')}")

                        parts.append("<div class='kb-ref-item'>")
                        parts.append(
                            f"<span class='clf-badge' style='background:{clf_color}' "
                            f"title='{clf_reason}'>{esc(clf_label)}</span>"
                        )
                        parts.append("<div class='kb-ref-info'>")
                        parts.append(f"<div class='kb-ref-title'>📚 {title_str}</div>")
                        parts.append(
                            f"<div class='kb-ref-sub'>重排分: {ref.get('score', 0)}"
                            + (f" | {clf_reason}" if clf_reason else "") + "</div>"
                        )
                        parts.append("</div>")
                        if chunk_id:
                            parts.append(
                                f"<button class='clf-btn' "
                                f"onclick=\"showKB('{chunk_id}', '{title_str}')\">查看全文</button>"
                            )
                        parts.append("</div>")
                    parts.append("</div>")

                issues = review.get("issues", [])
                if issues:
                    parts.append("<div class='section-title'>发现问题</div>")
                    parts.append("<div class='issues-list'>")
                    for issue in issues:
                        parts.append("<div class='issue-item'>")
                        parts.append(f"<div class='issue-type'>[{esc(issue.get('type', ''))}]</div>")
                        parts.append(f"<div class='issue-desc'>{esc(issue.get('description', ''))}</div>")
                        if issue.get("pending_content"):
                            parts.append(f"<div class='issue-quote'>待审: {esc(issue['pending_content'])}</div>")
                        if issue.get("regulation_content"):
                            parts.append(f"<div class='issue-quote'>法规: {esc(issue['regulation_content'])}</div>")
                        if issue.get("suggestion"):
                            parts.append(f"<div class='issue-suggestion'>建议: {esc(issue['suggestion'])}</div>")
                        parts.append("</div>")
                    parts.append("</div>")

                # v9: 数值核验工具结论
                if numeric_checks:
                    parts.append("<div class='section-title'>🧮 数值核验（LLM抽取 · 代码比较）</div>")
                    parts.append("<div class='numeric-list'>")
                    for nc in numeric_checks:
                        for d in nc.get("details", []):
                            color = verdict_colors.get(d.get("verdict", ""), "#95a5a6")
                            parts.append(
                                "<div class='numeric-item'>"
                                f"<span class='numeric-verdict' style='background:{color}'>{esc(d.get('verdict', ''))}</span>"
                                f"{esc(d.get('explanation', ''))}"
                                "</div>"
                            )
                    parts.append("</div>")

                vn = review.get("verification_notes", "")
                if vn and vn != "初审结果准确，无误判":
                    parts.append(f"<div class='verify-note'>🔍 <strong>验证说明:</strong> {esc(vn)}</div>")

                if review.get("summary"):
                    parts.append(f"<div class='summary-text'>📝 {esc(review['summary'])}</div>")

                if typo.get("has_issues") and typo.get("issues"):
                    parts.append("<div class='section-title'>✏️ 错别字检查</div>")
                    parts.append("<div class='typo-list'>")
                    for ti in typo["issues"]:
                        parts.append("<div class='typo-item'>")
                        parts.append(
                            f"<span class='typo-wrong'>「{esc(ti.get('original', ''))}」</span>"
                            f" → <span class='typo-fix'>「{esc(ti.get('suggestion', ''))}」</span>"
                        )
                        if ti.get("context"):
                            parts.append(f"<div class='typo-ctx'>上下文: …{esc(ti['context'])}…</div>")
                        if ti.get("reason"):
                            parts.append(f"<div class='typo-ctx'>依据: {esc(ti['reason'])}</div>")
                        if ti.get("confidence"):
                            parts.append(f"<div class='typo-ctx'>置信度: {esc(ti['confidence'])}</div>")
                        parts.append("</div>")
                    parts.append("</div>")
                elif typo:
                    parts.append(f"<div class='summary-text' style='color:#7f8c8d;'>✏️ {esc(typo.get('summary', ''))}</div>")

                if rep.get("has_duplicates") and rep.get("duplicates"):
                    parts.append("<div class='section-title'>🔁 重复性检查</div>")
                    parts.append("<div class='rep-list'>")
                    for ri in rep["duplicates"]:
                        parts.append("<div class='rep-item'>")
                        parts.append(f"<span class='rep-ref'>{esc(ri.get('chunk_ref', ''))}</span>")
                        if ri.get("note"):
                            parts.append(f"<span class='rep-note'>：{esc(ri['note'])}</span>")
                        if ri.get("similarity") is not None:
                            parts.append(f"<span class='rep-note'>（相似度 {esc(ri['similarity'])}）</span>")
                        parts.append("</div>")
                    parts.append("</div>")

                parts.append("</div>")  # chunk-body
                parts.append("</div>")  # chunk-card

        parts.append("</div>")  # main

        parts.append("""
<div class='modal-overlay' id='kb-modal' onclick='if(event.target===this)closeModal()'>
  <div class='modal-box'>
    <div class='modal-header'>
      <div class='modal-title' id='modal-title'></div>
      <button class='modal-close' onclick='closeModal()'>×</button>
    </div>
    <div class='modal-body'>
      <div class='modal-content' id='modal-content'></div>
    </div>
  </div>
</div>
""")
        parts.append(f"<script>{js}</script>")
        parts.append("</body></html>")

        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))

        print(f"审核报告已生成: {output_path}")
        return output_path


def main(argv: Optional[List[str]] = None):
    parser = argparse.ArgumentParser(description="煤矿作业规程合规审核系统 v9")
    parser.add_argument("--kb", default=None, help="知识库chunks JSON路径（默认自动找最新）")
    parser.add_argument("--pending", default=None, help="待审chunks JSON路径（默认自动找最新）")
    parser.add_argument("--doc-filter", default="", help="待审文档名过滤，逗号分隔，如 004,006")
    parser.add_argument("--max-docs", type=int, default=MAX_PENDING_DOCS or 0,
                        help="最多审核的文档数，0=全部")
    parser.add_argument("--mine-type", default=MINE_TYPE,
                        choices=["non_outburst", "outburst"], help="矿井类型（适用性过滤）")
    parser.add_argument("--concurrency", type=int, default=CHUNK_CONCURRENCY, help="chunk并发数")
    parser.add_argument("--no-report", action="store_true", help="不生成HTML报告")
    parser.add_argument("--fresh", action="store_true", help="忽略增量结果，全部重新审核")
    args = parser.parse_args(argv)

    doc_filter = [s.strip() for s in args.doc_filter.split(",") if s.strip()]
    max_docs = args.max_docs if args.max_docs > 0 else None

    print("=" * 70)
    print("煤矿作业规程合规审核系统 v9")
    print("检索: 本地BGE-M3 dense+sparse RRF + bge-reranker-v2-m3")
    print(f"TopK: {TOP_K} | 重排阈值: {RERANK_SCORE_THRESHOLD} | "
          f"矿井类型: {args.mine_type} | chunk并发: {args.concurrency}")
    print(f"数值核验: LLM抽取·代码比较 | 升级队列: {QUEUE_DB}")
    print(f"文档过滤: {doc_filter if doc_filter else '全部'} | 最多文档数: {max_docs or '全部'}")
    print("=" * 70)

    reviewer = HybridRAGReviewerV9(mine_type=args.mine_type)
    reviewer.load_knowledge_base(args.kb)
    reviewer.build_index()
    results = reviewer.review_pending_document(
        pending_json_path=args.pending,
        doc_filter=doc_filter,
        max_documents=max_docs,
        chunk_concurrency=args.concurrency,
        fresh=args.fresh,
    )
    if results:
        reviewer.save_results(results)
        if not args.no_report:
            reviewer.generate_report(results)
    return results


if __name__ == "__main__":
    main()
