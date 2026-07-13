"""v9 常驻 worker —— 前后端与 v9 引擎之间的桥

必须在 langchain0.3 环境运行（BGE + torch CUDA）：
    D:\\Anaconda\\envs\\langchain0.3\\python.exe v9_worker.py

职责：
  1. BGE 模型 + KB 索引只加载一次，常驻内存。
  2. 轮询 jobs 表：取待审任务 → docx 适配为段落模型 + 带段落索引的块 →
     跑引擎（检索 + 合规/错别字/数值核验）→ 边审边把问题写入 issues 表
     （每条带 block_indices，前端可精确定位高亮）。
  3. 轮询人工反馈：accept/custom 的问题交主智能体生成最终替换文本，
     直接改写该任务的工作副本 docx 对应段落，并刷新段落模型。

人工反馈本身（合规结论层面）在后端写入飞轮（annotations, source=human），
是数据飞轮的最高层；worker 这里只负责"把人的意见落到 Word 上"。
"""
import json
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List

from docx import Document
from openai import OpenAI

import docx_adapter
from review_queue import ReviewQueue

PROJECT_ROOT = Path(__file__).resolve().parent
QUEUE_DB = PROJECT_ROOT / "data" / "review_queue_v9.db"
WORK_DIR = PROJECT_ROOT / "data" / "v9_web_work"      # 段落模型 / 工作副本 docx
WORK_DIR.mkdir(parents=True, exist_ok=True)
FEEDBACK_SNAPSHOT_DIR = WORK_DIR / "feedback_snapshots"
FEEDBACK_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
DEMO_SKIP_RETRIEVAL = os.getenv("V9_DEMO_SKIP_RETRIEVAL", "0") == "1"
DISABLE_ESCALATION_LOOP = os.getenv("V9_DISABLE_ESCALATION_LOOP", "0") == "1"
USE_MINERU_FOR_PENDING = os.getenv("V9_USE_MINERU_FOR_PENDING", "1") != "0"
MINERU_API_URL = os.getenv("V9_MINERU_API_URL", os.getenv("MINERU_API_URL", "http://127.0.0.1:51071"))


def _env_int(name: str, default: int, min_value: int = 1, max_value: int = 16) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(max_value, value))


WEB_CHUNK_CONCURRENCY = _env_int("V9_WEB_CHUNK_CONCURRENCY", 4, 1, 12)


def _load_env_fallback():
    """新终端里若未设 DASHSCOPE_API_KEY，从 backend/.env 兜底读取。"""
    if os.getenv("DASHSCOPE_API_KEY"):
        return
    for env_path in (PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env_fallback()
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AGENT_MODEL = "qwen-plus"
POLL_INTERVAL = 1.5


def log(msg: str):
    print(f"[worker] {msg}", flush=True)


def _timings_json(timings: Dict[str, float]) -> str:
    return json.dumps(
        {key: round(float(value), 2) for key, value in timings.items()},
        ensure_ascii=False,
    )


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _feedback_snapshot_path(job_id: str, issue_id: int) -> Path:
    return FEEDBACK_SNAPSHOT_DIR / f"{job_id}_issue_{int(issue_id)}_before.docx"


def _feedback_snapshot_meta_path(job_id: str, issue_id: int) -> Path:
    return FEEDBACK_SNAPSHOT_DIR / f"{job_id}_issue_{int(issue_id)}_before.json"


class V9Worker:
    def __init__(self, load_engine: bool = True):
        if not API_KEY:
            raise ValueError("请设置 DASHSCOPE_API_KEY")
        self.queue = ReviewQueue(QUEUE_DB)
        self.llm = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        self.engine = None
        if load_engine:
            log("加载 v9 引擎（BGE 模型 + KB 索引，仅一次）...")
            from hybrid_rag_review_v9 import HybridRAGReviewerV9
            self.engine = HybridRAGReviewerV9()
            self.engine.load_knowledge_base()
            self.engine.build_index()
            log("引擎就绪，进入轮询循环")

    # ---------- 段落模型落盘 ----------

    def _save_paragraphs(self, job_id: str, parsed: Dict[str, Any]) -> str:
        path = WORK_DIR / f"{job_id}_paragraphs.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False)
        return str(path)

    def _ensure_docx(self, path: str) -> str:
        """若是老式 .doc，用本机 Word 自动转 .docx；否则原样返回。"""
        p = Path(path)
        if p.suffix.lower() != ".doc":
            return path
        out = p.with_suffix(".docx")
        if out.exists():
            return str(out)
        log(f"检测到 .doc，调用 Word 转换为 .docx: {p.name}")
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        word = None
        doc = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(str(p.resolve()), ReadOnly=True, AddToRecentFiles=False)
            try:
                doc.SaveAs2(str(out.resolve()), FileFormat=16)  # 16 = wdFormatXMLDocument(.docx)
            except Exception as exc:
                if out.exists() and out.stat().st_size > 0:
                    log(f"Word SaveAs2 返回异常但 .docx 已生成，继续处理: {exc}")
                else:
                    raise RuntimeError(
                        f"Word COM 转换 .doc 失败，请先手动另存为 .docx 后再上传：{exc}"
                    ) from exc
            if not out.exists() or out.stat().st_size == 0:
                raise RuntimeError("Word COM 转换 .doc 失败：未生成有效 .docx 文件")
            return str(out)
        finally:
            if doc is not None:
                try:
                    doc.Close(False)
                except Exception as exc:
                    log(f"Word 文档关闭失败（已忽略）: {exc}")
            if word is not None:
                try:
                    word.Quit()
                except Exception as exc:
                    log(f"Word 退出失败（已忽略）: {exc}")
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _working_docx(self, job_id: str, original: str) -> Path:
        """每个任务一份可被主智能体改写的工作副本。"""
        work = WORK_DIR / f"{job_id}_working.docx"
        if not work.exists():
            shutil.copy(original, work)
        return work

    def _blocks_from_content(self, parsed: Dict[str, Any], content: str) -> List[int]:
        """把 MinerU chunk 内容映射回 docx block_index，供前端高亮/Word 改写使用。"""
        norm = _compact_text(content)
        if not norm:
            return []

        hits: List[int] = []
        for block in parsed.get("blocks", []):
            block_text = _compact_text(block.get("text", ""))
            if not block_text:
                continue
            idx = int(block.get("block_index", -1))
            if idx < 0:
                continue
            if block_text in norm or norm in block_text:
                hits.append(idx)
                continue
            # MinerU 和 python-docx 的换行/表格符号可能不同，用首尾短片段兜底。
            head = block_text[: min(36, len(block_text))]
            tail = block_text[-min(36, len(block_text)) :]
            if len(head) >= 12 and head in norm:
                hits.append(idx)
            elif len(tail) >= 12 and tail in norm:
                hits.append(idx)

        return sorted(set(hits))

    def _best_docx_chunk_blocks(self, content: str, docx_chunks: List[Dict[str, Any]]) -> List[int]:
        norm = _compact_text(content)
        if not norm:
            return []
        sample = norm[:1400]
        best_score = 0.0
        best_blocks: List[int] = []
        for chunk in docx_chunks:
            candidate = _compact_text(chunk.get("content", ""))
            if not candidate:
                continue
            if norm in candidate or candidate in norm:
                score = min(len(norm), len(candidate)) / max(len(norm), len(candidate))
            else:
                score = SequenceMatcher(None, sample, candidate[:1400]).ratio()
            if score > best_score:
                best_score = score
                best_blocks = list(chunk.get("source_blocks") or [])
        return best_blocks if best_score >= 0.20 else []

    def _source_units_for_blocks(self, parsed: Dict[str, Any], blocks: List[int]) -> List[Dict[str, Any]]:
        all_blocks = parsed.get("blocks", [])
        units: List[Dict[str, Any]] = []
        for idx in blocks:
            if 0 <= idx < len(all_blocks):
                block = all_blocks[idx]
                units.append({
                    "text": block.get("text", ""),
                    "source_blocks": [idx],
                    "kind": block.get("kind", "paragraph"),
                })
        return units

    def _attach_source_blocks_to_mineru_chunks(
        self,
        parsed: Dict[str, Any],
        mineru_chunks: List[Dict[str, Any]],
        docx_chunks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        mapped: List[Dict[str, Any]] = []
        for chunk in mineru_chunks:
            content = chunk.get("content", "")
            source_blocks = self._blocks_from_content(parsed, content)
            if not source_blocks:
                source_blocks = self._best_docx_chunk_blocks(content, docx_chunks)
            next_chunk = dict(chunk)
            next_chunk["source_blocks"] = source_blocks
            if source_blocks and not next_chunk.get("source_units"):
                next_chunk["source_units"] = self._source_units_for_blocks(parsed, source_blocks)
            next_chunk.setdefault("char_count", len(content))
            next_chunk.setdefault("chunk_level", "paragraph")
            mapped.append(next_chunk)
        return mapped

    def _build_pending_chunks_with_mineru(
        self,
        docx_path: str,
        parsed: Dict[str, Any],
        docx_chunks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        from mineru_adapter import convert_document_to_mineru_json

        try:
            result = convert_document_to_mineru_json(
                docx_path,
                doc_kind="pending",
                api_url=MINERU_API_URL,
                timeout=7200,
                chunk_pending=True,
            )
        except Exception as exc:
            raise RuntimeError(
                "MinerU 解析待审文档失败。请先启动 MinerU API："
                "D:/Anaconda/envs/langchain0.3/Scripts/mineru-api.exe "
                "--host 127.0.0.1 --port 51071；"
                f"当前 V9_MINERU_API_URL={MINERU_API_URL}；原始错误: {exc}"
            ) from exc
        if not result.chunks_preview_path:
            raise RuntimeError("MinerU 已生成 JSON，但未生成待审文档 chunks")
        with open(result.chunks_preview_path, "r", encoding="utf-8") as f:
            chunk_doc = json.load(f)
        if not isinstance(chunk_doc, dict) or not chunk_doc:
            raise RuntimeError(f"MinerU 待审 chunks 文件为空: {result.chunks_preview_path}")
        mineru_chunks = next(iter(chunk_doc.values()))
        if not isinstance(mineru_chunks, list) or not mineru_chunks:
            raise RuntimeError(f"MinerU 待审 chunks 结构异常: {result.chunks_preview_path}")
        chunks = self._attach_source_blocks_to_mineru_chunks(parsed, mineru_chunks, docx_chunks)
        mapped_count = sum(1 for chunk in chunks if chunk.get("source_blocks"))
        log(
            f"MinerU 待审切分完成: {len(chunks)} 段，"
            f"已映射 Word source_blocks {mapped_count}/{len(chunks)} 段"
        )
        return chunks

    def _persist_review_doc_vectors(
        self,
        job: Dict[str, Any],
        chunks: List[Dict[str, Any]],
        q_dense,
    ) -> Dict[str, Any]:
        """复用 Phase A 的待审文档向量，长期写入 Milvus，并镜像 chunk 元数据到 MongoDB。"""
        if q_dense is None:
            return {"saved": 0, "mongo": False}
        try:
            from review_doc_vector_store import review_doc_vector_store

            records = review_doc_vector_store.upsert_job_chunks(
                job=job,
                chunks=chunks,
                dense_vecs=q_dense,
            )
            mongo_ok = False
            try:
                backend_dir = PROJECT_ROOT / "backend"
                if str(backend_dir) not in sys.path:
                    sys.path.insert(0, str(backend_dir))
                from app.services.mongo_service import mongo_service

                mongo_ok = mongo_service.replace_review_chunks(job=job, vector_records=records)
            except Exception as exc:
                log(f"待审 chunk 元数据写入 MongoDB 失败（不影响审查）: {exc}")
            return {"saved": len(records), "mongo": bool(mongo_ok)}
        except Exception as exc:
            log(f"待审文档向量写入 Milvus 失败（不影响审查）: {exc}")
            return {"saved": 0, "mongo": False, "error": str(exc)[:200]}

    # ---------- 审查任务 ----------

    def _is_cancelled(self, job_id: str) -> bool:
        job = self.queue.get_job(job_id)
        return bool(job and job.get("status") == "cancelled")

    def _stop_if_cancelled(self, job_id: str, stage: str) -> bool:
        if self._is_cancelled(job_id):
            log(f"任务 {job_id} 已取消，停止于 {stage}")
            return True
        return False

    def process_job(self, job: Dict[str, Any]):
        job_id = job["job_id"]
        docx_path = job["docx_path"]
        mine_type = job.get("mine_type", "non_outburst")
        log(f"开始审查任务 {job_id}: {job['doc_name']}")
        timings: Dict[str, float] = {}
        job_started = time.perf_counter()
        try:
            parse_started = time.perf_counter()
            self.queue.update_job(
                job_id,
                agent_stage="parsing",
                phase_done=0,
                phase_total=0,
                agent_status="正在解析文档并生成待审块…",
                timings=_timings_json(timings),
            )
            docx_path = self._ensure_docx(docx_path)   # .doc → .docx 自动转换
            if docx_path != job["docx_path"]:
                self.queue.update_job(job_id, docx_path=docx_path)
            parsed, docx_chunks = docx_adapter.adapt(docx_path)
            if USE_MINERU_FOR_PENDING:
                self.queue.update_job(
                    job_id,
                    agent_stage="parsing",
                    phase_done=0,
                    phase_total=0,
                    agent_status="正在调用 MinerU 结构化解析待审文档…",
                    timings=_timings_json(timings),
                )
                chunks = self._build_pending_chunks_with_mineru(docx_path, parsed, docx_chunks)
            else:
                chunks = docx_chunks
            timings["parse"] = time.perf_counter() - parse_started
            if self._stop_if_cancelled(job_id, "文档解析后"):
                return
            para_path = self._save_paragraphs(job_id, parsed)
            self._working_docx(job_id, docx_path)  # 预建工作副本
            if self._stop_if_cancelled(job_id, "工作副本创建后"):
                return
            self.queue.update_job(job_id, status="reviewing", n_chunks=len(chunks),
                                  paragraphs_path=para_path, progress=2,
                                  n_done=0, phase_done=0, phase_total=len(chunks),
                                  agent_stage="retrieval_rerank",
                                  agent_status="文档解析完成，准备法规召回与重排…",
                                  timings=_timings_json(timings))

            engine = self.engine
            engine.mine_type = mine_type  # 适用性过滤按本任务矿井类型
            if DEMO_SKIP_RETRIEVAL:
                retrieval_started = time.perf_counter()
                self.queue.update_job(
                    job_id, progress=3,
                    agent_stage="retrieval_rerank",
                    phase_done=0,
                    phase_total=len(chunks),
                    agent_status=f"已命中本地向量缓存（共 {len(chunks)} 段），正在法规召回…",
                    timings=_timings_json(timings),
                )
                kb_results_all, q_dense = engine.prepare_dense_retrieval(chunks)
                timings["retrieval_rerank"] = time.perf_counter() - retrieval_started
                if self._stop_if_cancelled(job_id, "法规召回后"):
                    return
                persist_started = time.perf_counter()
                persist_result = self._persist_review_doc_vectors(job, chunks, q_dense)
                timings["persist_review_vectors"] = time.perf_counter() - persist_started
                if persist_result.get("saved"):
                    log(
                        f"任务 {job_id} 待审向量已写入 Milvus: "
                        f"{persist_result['saved']} 段, mongo={persist_result.get('mongo')}"
                    )
                self.queue.update_job(
                    job_id, progress=15,
                    agent_stage="reviewing",
                    phase_done=0,
                    phase_total=len(chunks),
                    agent_status="主智能体已派发审查子任务，逐块合规/错别字/数值核验中",
                    timings=_timings_json(timings),
                )
            else:
                encoding_cache_hit = engine.has_pending_encoding_cache(chunks)
                vector_started = None
                if encoding_cache_hit:
                    encoding_status = f"已命中本地向量缓存（共 {len(chunks)} 段），正在法规召回与重排…"
                    stage = "retrieval_rerank"
                else:
                    encoding_status = f"BGE-M3 首次编码中（共 {len(chunks)} 段），完成后将保存本地缓存…"
                    stage = "vectorizing"
                    vector_started = time.perf_counter()
                self.queue.update_job(
                    job_id,
                    progress=3,
                    n_done=0,
                    agent_stage=stage,
                    phase_done=0,
                    phase_total=len(chunks),
                    agent_status=encoding_status,
                    timings=_timings_json(timings),
                )
                last_encoding_chunk_done = -1

                def _encoding_progress(done: int, total: int) -> None:
                    nonlocal last_encoding_chunk_done
                    if total <= 0:
                        return
                    chunk_done = min(len(chunks), int(done / total * len(chunks)))
                    if chunk_done == last_encoding_chunk_done and done < total:
                        return
                    last_encoding_chunk_done = chunk_done
                    percent = 3 + int(done / total * 10)
                    self.queue.update_job(
                        job_id,
                        progress=percent,
                        n_done=0,
                        n_chunks=len(chunks),
                        agent_stage="vectorizing",
                        phase_done=chunk_done,
                        phase_total=len(chunks),
                        agent_status=(
                            f"BGE-M3 向量化中：已编码 {chunk_done}/{len(chunks)} 段"
                            if done < total else
                            f"BGE-M3 向量化完成（{len(chunks)}/{len(chunks)} 段），正在法规召回与重排…"
                        ),
                        timings=_timings_json(timings),
                    )

                retrieval_started = None
                last_retrieval_done = -1

                def _retrieval_progress(done: int, total: int) -> None:
                    nonlocal last_retrieval_done, retrieval_started
                    if total <= 0:
                        return
                    if done <= 0:
                        retrieval_started = time.perf_counter()
                        if vector_started is not None:
                            timings["vectorize"] = retrieval_started - vector_started
                        self.queue.update_job(
                            job_id,
                            progress=13,
                            n_done=0,
                            n_chunks=len(chunks),
                            agent_stage="retrieval_rerank",
                            phase_done=0,
                            phase_total=total,
                            agent_status=f"BGE-M3 向量化完成，开始法规召回与重排（共 {total} 段）",
                            timings=_timings_json(timings),
                        )
                        return
                    if done == last_retrieval_done and done < total:
                        return
                    last_retrieval_done = done
                    self.queue.update_job(
                        job_id,
                        progress=min(15, 13 + int(done / total * 2)),
                        n_done=0,
                        n_chunks=len(chunks),
                        agent_stage="retrieval_rerank",
                        phase_done=done,
                        phase_total=total,
                        agent_status=f"BGE-M3 向量化完成，法规召回与重排中：{done}/{total} 段",
                        timings=_timings_json(timings),
                    )

                kb_results_all, q_dense = engine.prepare_retrieval(
                    chunks,
                    encoding_progress_cb=None if encoding_cache_hit else _encoding_progress,
                    retrieval_progress_cb=_retrieval_progress,
                )
                if retrieval_started is None:
                    retrieval_started = time.perf_counter()
                    if vector_started is not None:
                        timings["vectorize"] = retrieval_started - vector_started
                timings["retrieval_rerank"] = time.perf_counter() - retrieval_started
                log(f"任务 {job_id} Phase A 完成: "
                    f"向量化 {timings.get('vectorize', 0.0):.1f}s, "
                    f"检索重排 {timings.get('retrieval_rerank', 0.0):.1f}s")
                if self._stop_if_cancelled(job_id, "法规召回与重排后"):
                    return
                persist_started = time.perf_counter()
                persist_result = self._persist_review_doc_vectors(job, chunks, q_dense)
                timings["persist_review_vectors"] = time.perf_counter() - persist_started
                if persist_result.get("saved"):
                    log(
                        f"任务 {job_id} 待审向量已写入 Milvus: "
                        f"{persist_result['saved']} 段, mongo={persist_result.get('mongo')}"
                    )
                self.queue.update_job(
                    job_id, progress=15, n_done=0, n_chunks=len(chunks),
                    agent_stage="reviewing",
                    phase_done=0,
                    phase_total=len(chunks),
                    agent_status=("已复用本地向量缓存，逐块合规/错别字/数值核验中"
                                  if encoding_cache_hit else
                                  "向量编码与检索完成，逐块合规/错别字/数值核验中"),
                    timings=_timings_json(timings),
                )

            n_issues = 0
            review_started = time.perf_counter()
            total_chunks = len(chunks)
            concurrency = min(WEB_CHUNK_CONCURRENCY, max(1, total_chunks))
            log(f"任务 {job_id} Phase B 开始: chunk级并发={concurrency}, chunks={total_chunks}")
            self.queue.update_job(
                job_id,
                progress=15,
                n_done=0,
                n_chunks=total_chunks,
                agent_stage="reviewing",
                phase_done=0,
                phase_total=total_chunks,
                agent_status=f"合规/错别字/数值核验中：并发 {concurrency}，已审查 0/{total_chunks} 段",
                timings=_timings_json(timings),
            )
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {}
                for i, chunk in enumerate(chunks):
                    if self._stop_if_cancelled(job_id, f"chunk#{i + 1} 提交前"):
                        return
                    futures[
                        pool.submit(
                            self._review_one_chunk,
                            job_id,
                            job["doc_name"],
                            i,
                            chunk,
                            kb_results_all[i],
                            parsed,
                            chunk.get("source_blocks", []),
                        )
                    ] = i

                completed = 0
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        n_issues += int(future.result() or 0)
                    except Exception as exc:
                        log(f"  chunk#{idx + 1} worker future异常: {exc}")
                    completed += 1
                    if self._stop_if_cancelled(job_id, f"chunk#{idx + 1} 完成后"):
                        return
                    progress = 15 + int(completed / max(1, total_chunks) * 80)
                    timings["review"] = time.perf_counter() - review_started
                    self.queue.update_job(
                        job_id,
                        progress=progress,
                        n_done=completed,
                        n_chunks=total_chunks,
                        agent_stage="reviewing",
                        phase_done=completed,
                        phase_total=total_chunks,
                        agent_status=(
                            f"合规/错别字/数值核验中：并发 {concurrency}，"
                            f"已审查 {completed}/{total_chunks} 段"
                        ),
                        timings=_timings_json(timings),
                    )

            # 文档级重复性（一次性，复用 Phase A 向量）
            try:
                repetition_started = time.perf_counter()
                if DEMO_SKIP_RETRIEVAL or q_dense is None:
                    log("Demo 快速模式：跳过文档级重复性检查")
                    raise RuntimeError("Demo 快速模式跳过重复性检查")
                if self._stop_if_cancelled(job_id, "重复性检查前"):
                    return
                self.queue.update_job(
                    job_id,
                    progress=96,
                    agent_stage="repetition",
                    phase_done=0,
                    phase_total=len(chunks),
                    agent_status="正在做文档级重复性检查…",
                    timings=_timings_json(timings),
                )
                rep_by_chunk, _ = engine.check_document_repetition(job["doc_name"], chunks, q_dense)
                timings["repetition"] = time.perf_counter() - repetition_started
                if self._stop_if_cancelled(job_id, "重复性检查后"):
                    return
                for ci, rep in rep_by_chunk.items():
                    if rep.get("has_duplicates"):
                        dup = rep["duplicates"][0]
                        self.queue.add_issue(
                            job_id, ci, "redundancy", "重复",
                            block_indices=chunks[ci].get("source_blocks", []),
                            title="重复内容",
                            original_text=chunks[ci]["content"][:80],
                            reason=dup.get("note", ""),
                            detail={"ref": dup.get("chunk_ref", ""),
                                    "similarity": dup.get("similarity")},
                        )
                        n_issues += 1
            except Exception as exc:
                log(f"重复性检查异常（忽略）: {exc}")

            if not self._is_cancelled(job_id):
                timings["total"] = time.perf_counter() - job_started
                self.queue.update_job(job_id, status="done", progress=100,
                                      agent_stage="done",
                                      phase_done=len(chunks),
                                      phase_total=len(chunks),
                                      timings=_timings_json(timings),
                                      agent_status=f"审查完成，共发现 {n_issues} 处问题，等待人工裁决")
            log(f"任务 {job_id} 完成，问题 {n_issues} 条，耗时: "
                f"解析 {timings.get('parse', 0.0):.1f}s / "
                f"向量化 {timings.get('vectorize', 0.0):.1f}s / "
                f"检索重排 {timings.get('retrieval_rerank', 0.0):.1f}s / "
                f"审查 {timings.get('review', 0.0):.1f}s / "
                f"总计 {timings.get('total', 0.0):.1f}s")
        except Exception as exc:
            if self._is_cancelled(job_id):
                log(f"任务 {job_id} 已取消，忽略异常: {exc}")
                return
            import traceback
            traceback.print_exc()
            self.queue.update_job(job_id, status="failed", error=str(exc)[:500],
                                  agent_stage="failed",
                                  timings=_timings_json(timings),
                                  agent_status=f"审查失败: {str(exc)[:80]}")

    def _review_one_chunk(self, job_id: str, doc_name: str, idx: int, chunk: Dict,
                          kb_results: List[Dict], parsed: Dict, source_blocks: List[int]) -> int:
        """单块：合规链 + 错别字，产出 issues。返回新增问题数。"""
        engine = self.engine
        count = 0

        # 复用引擎的合规链与错别字并行处理，并自动写入升级队列
        try:
            result = engine._process_single_chunk(doc_name, idx, chunk, kb_results)
            review = result["review_result"]
            numeric_checks = result["numeric_checks"]
            escalations = result["escalations"]
            typo = result["typo_result"]
        except Exception as exc:
            log(f"  chunk#{idx+1} 并行审查异常: {exc}")
            review, numeric_checks, escalations = {"compliance_status": "不确定", "issues": []}, [], ["error"]
            typo = {"issues": []}

        status = str(review.get("compliance_status", ""))
        numeric_detail = [
            {"verdict": d.get("verdict"), "explanation": d.get("explanation")}
            for nc in numeric_checks if nc.get("has_pairs")
            for d in nc.get("details", [])
        ]

        for issue in review.get("issues", []):
            original = issue.get("pending_content", "") or ""
            blocks = docx_adapter.locate_text_blocks(parsed, original, source_blocks)
            self.queue.add_issue(
                job_id, idx, "compliance", status or "不合规",
                block_indices=blocks or source_blocks,
                title=issue.get("type", "合规问题"),
                original_text=original,
                suggestion=issue.get("suggestion", ""),
                regulation=issue.get("regulation_content", ""),
                reason=issue.get("description", ""),
                detail={"numeric": numeric_detail,
                        "summary": review.get("summary", "")},
            )
            count += 1

        # 有升级但没有具体 issue（如"不确定"/反向核验）时，也产出一条供人工裁决
        if not review.get("issues") and escalations:
            first_escalation = escalations[0]
            escalation_reason = first_escalation.get("reason", "") if isinstance(first_escalation, dict) else str(first_escalation)
            self.queue.add_issue(
                job_id, idx, "escalation", status or "不确定",
                block_indices=source_blocks,
                title="需人工裁决",
                original_text=chunk["content"][:120],
                reason=escalation_reason or "审查链已将该项加入主智能体升级队列",
                detail={
                    "numeric": numeric_detail,
                    "summary": review.get("summary", ""),
                    "escalations": escalations,
                },
                escalation_type=first_escalation,
            )
            count += 1

        # 错别字结果已由引擎与合规链并行产出
        for t in typo.get("issues", []):
            wrong = t.get("original") or t.get("wrong_char", "")
            blocks = docx_adapter.locate_text_blocks(parsed, wrong, source_blocks)
            self.queue.add_issue(
                job_id, idx, "typo", "错别字",
                block_indices=blocks or source_blocks,
                title="错别字",
                original_text=wrong,
                suggestion=t.get("suggestion") or t.get("correct_char", ""),
                reason=t.get("reason", ""),
                detail={"confidence": t.get("confidence", ""),
                        "context": t.get("context", "")},
            )
            count += 1

        return count

    # ---------- 人工反馈 → 主智能体改 Word ----------

    def process_feedback(self, fb: Dict[str, Any]):
        issue_id = fb["id"]
        job_id = fb["job_id"]
        action = fb["human_action"]
        log(f"处理反馈 issue#{issue_id} ({action})")
        job = self.queue.get_job(job_id)
        if not job:
            self.queue.finish_feedback(issue_id, applied=-1, agent_note="任务不存在")
            return

        original = fb.get("original_text", "")
        if not original:
            self.queue.finish_feedback(issue_id, applied=2, agent_note="无原文定位，未改写")
            return

        # 主智能体根据 原文 + 系统建议 + 人工意见 产出最终替换文本
        new_text = self._agent_decide_replacement(
            original, fb.get("suggestion", ""), fb.get("human_text", ""), action
        )
        if not new_text or new_text.strip() == original.strip():
            self.queue.finish_feedback(issue_id, applied=2,
                                       agent_note="主智能体判定无需改写或替换文本为空")
            return

        work = self._working_docx(job_id, job["docx_path"])
        doc = Document(str(work))
        block_indices = fb.get("block_indices") or []
        applied = False
        snapshot_path = _feedback_snapshot_path(job_id, issue_id)
        snapshot_meta_path = _feedback_snapshot_meta_path(job_id, issue_id)
        try:
            shutil.copy2(work, snapshot_path)
            snapshot_meta_path.write_text(
                json.dumps({
                    "issue_id": issue_id,
                    "job_id": job_id,
                    "original_text": original,
                    "new_text": new_text,
                    "suggestion": fb.get("suggestion", ""),
                    "human_text": fb.get("human_text", ""),
                    "human_action": action,
                    "block_indices": block_indices,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            log(f"  issue#{issue_id} 保存撤回快照失败（继续尝试改写）: {exc}")

        # 先按系统建议中的“将 X 改为 Y”做最小替换，适合数值冲突和错别字。
        suggestion = fb.get("suggestion", "")
        if action == "accept" and suggestion:
            applied = docx_adapter.apply_suggestion_at_blocks(doc, block_indices, suggestion)

        # 再按原文片段改写。该函数支持规范化匹配和跨多个 block 的整体改写。
        if not applied:
            applied = docx_adapter.apply_rewrite_at_blocks(doc, block_indices, original, new_text)

        if not applied:
            # 兜底：全文段落范围内尝试
            parsed = docx_adapter.parse_docx(work)
            for bi in docx_adapter.locate_text_blocks(parsed, original):
                if docx_adapter.apply_edit_at_block(doc, bi, original, new_text):
                    applied = True
                    break

        if applied:
            doc.save(str(work))
            try:
                (WORK_DIR / f"{job_id}_preview.pdf").unlink(missing_ok=True)
            except Exception:
                pass
            # 刷新段落模型，前端重新拉取即见改动
            self._save_paragraphs(job_id, docx_adapter.parse_docx(work))
            self.queue.finish_feedback(
                issue_id, applied=1,
                agent_note=f"已将「{original[:20]}…」改写为「{new_text[:20]}…」")
            log(f"  issue#{issue_id} 已改写 Word")
        else:
            try:
                snapshot_path.unlink(missing_ok=True)
                snapshot_meta_path.unlink(missing_ok=True)
            except Exception:
                pass
            self.queue.finish_feedback(issue_id, applied=-1,
                                       agent_note="未能在文档中定位原文，改写失败")

    def _agent_decide_replacement(self, original: str, suggestion: str,
                                  human_text: str, action: str) -> str:
        """主智能体直接产出替换文本（只输出替换后的原文片段）。"""
        human_part = f"\n【人工意见】{human_text}" if human_text else ""
        guidance = ("人工接受系统建议，请据此给出替换文本。" if action == "accept"
                    else "请优先采纳人工意见，给出替换文本。")
        prompt = f"""你是煤矿作业规程审查系统的主智能体，需要把一处问题落实到原文修改。
{guidance}

【原文片段】
{original}

【系统修改建议】
{suggestion or "（无）"}{human_part}

只输出"替换原文片段后的最终文本"，不要解释、不要引号、不要前后缀。
若判断无需修改，原样输出原文片段。"""
        try:
            resp = self.llm.chat.completions.create(
                model=AGENT_MODEL,
                messages=[
                    {"role": "system", "content": "你只输出替换后的文本，不输出任何解释。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            log(f"  替换文本生成失败: {exc}")
            # 退化：accept 用 suggestion，custom 用 human_text
            return (human_text or suggestion or "").strip()

    # ---------- 主循环 ----------

    def _feedback_loop(self):
        """独立线程：持续处理人工反馈，与审查任务并行，保证交互实时响应。"""
        import traceback
        while True:
            try:
                fb = self.queue.fetch_pending_feedback()
                if fb:
                    self.process_feedback(fb)
                else:
                    time.sleep(POLL_INTERVAL)
            except Exception:
                traceback.print_exc()
                time.sleep(POLL_INTERVAL)

    def _escalation_loop(self):
        """持续调用主智能体处理审查链写入的升级队列。"""
        from main_agent_v9 import MainAgent
        agent = MainAgent()
        dead_logged = False
        while True:
            try:
                queue_stats = self.queue.stats()
                pending_total = sum(queue_stats.get("pending_by_type", {}).values())
                if pending_total <= 0:
                    if queue_stats.get("dead") and not dead_logged:
                        log(f"升级队列暂无待处理项；dead-letter {queue_stats['dead']} 项需人工处理")
                        dead_logged = True
                    time.sleep(POLL_INTERVAL * 2)
                    continue
                dead_logged = False
                stats = agent.process_queue(max_items=1)
                if not stats.get("resolved") and not stats.get("failed"):
                    time.sleep(POLL_INTERVAL * 2)
            except Exception as exc:
                log(f"主智能体升级队列处理异常: {exc}")
                time.sleep(POLL_INTERVAL * 2)
    def run(self):
        import threading
        import traceback
        # 反馈处理放独立守护线程：审查长任务进行中也能实时改 Word
        threading.Thread(target=self._feedback_loop, daemon=True).start()
        if DISABLE_ESCALATION_LOOP:
            log("反馈处理线程已启动；主智能体升级队列线程已关闭（V9_DISABLE_ESCALATION_LOOP=1）")
        else:
            threading.Thread(target=self._escalation_loop, daemon=True).start()
            log("反馈处理与主智能体升级队列线程已启动")
        while True:
            try:
                job = self.queue.fetch_pending_job()
                if job:
                    self.process_job(job)
                    continue
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                log("收到中断，退出")
                break
            except Exception:
                traceback.print_exc()
                time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    V9Worker().run()








