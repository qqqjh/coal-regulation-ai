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
import shutil
import sys
import time
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
DEMO_SKIP_RETRIEVAL = os.getenv("V9_DEMO_SKIP_RETRIEVAL", "0") == "1"


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
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(str(p.resolve()))
            doc.SaveAs2(str(out.resolve()), FileFormat=16)  # 16 = wdFormatXMLDocument(.docx)
            doc.Close(False)
            return str(out)
        finally:
            if word is not None:
                word.Quit()
            pythoncom.CoUninitialize()

    def _working_docx(self, job_id: str, original: str) -> Path:
        """每个任务一份可被主智能体改写的工作副本。"""
        work = WORK_DIR / f"{job_id}_working.docx"
        if not work.exists():
            shutil.copy(original, work)
        return work

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
        try:
            docx_path = self._ensure_docx(docx_path)   # .doc → .docx 自动转换
            if docx_path != job["docx_path"]:
                self.queue.update_job(job_id, docx_path=docx_path)
            parsed, chunks = docx_adapter.adapt(docx_path)
            if self._stop_if_cancelled(job_id, "文档解析后"):
                return
            para_path = self._save_paragraphs(job_id, parsed)
            self._working_docx(job_id, docx_path)  # 预建工作副本
            if self._stop_if_cancelled(job_id, "工作副本创建后"):
                return
            self.queue.update_job(job_id, status="reviewing", n_chunks=len(chunks),
                                  paragraphs_path=para_path, progress=2,
                                  agent_status="主智能体已派发审查子任务，三链并行审查中")

            engine = self.engine
            engine.mine_type = mine_type  # 适用性过滤按本任务矿井类型
            if DEMO_SKIP_RETRIEVAL:
                self.queue.update_job(
                    job_id, progress=3,
                    agent_status=f"已命中本地向量缓存（共 {len(chunks)} 段），正在法规召回…",
                )
                kb_results_all, q_dense = engine.prepare_dense_retrieval(chunks)
                if self._stop_if_cancelled(job_id, "法规召回后"):
                    return
                self.queue.update_job(
                    job_id, progress=15,
                    agent_status="主智能体已派发审查子任务，逐块合规/错别字/数值核验中",
                )
            else:
                encoding_cache_hit = engine.has_pending_encoding_cache(chunks)
                if encoding_cache_hit:
                    encoding_status = f"已命中本地向量缓存（共 {len(chunks)} 段），正在法规召回与重排…"
                else:
                    encoding_status = f"BGE-M3 首次编码中（共 {len(chunks)} 段），完成后将保存本地缓存…"
                self.queue.update_job(job_id, progress=3, agent_status=encoding_status)
                kb_results_all, q_dense = engine.prepare_retrieval(chunks)
                if self._stop_if_cancelled(job_id, "法规召回与重排后"):
                    return
                self.queue.update_job(
                    job_id, progress=15,
                    agent_status=("已复用本地向量缓存，逐块合规/错别字/数值核验中"
                                  if encoding_cache_hit else
                                  "向量编码与检索完成，逐块合规/错别字/数值核验中"),
                )

            n_issues = 0
            for i, chunk in enumerate(chunks):
                if self._stop_if_cancelled(job_id, f"chunk#{i + 1} 前"):
                    return
                kb_results = kb_results_all[i]
                source_blocks = chunk.get("source_blocks", [])
                n_issues += self._review_one_chunk(
                    job_id, job["doc_name"], i, chunk, kb_results, parsed, source_blocks
                )
                if self._stop_if_cancelled(job_id, f"chunk#{i + 1} 后"):
                    return
                progress = 15 + int((i + 1) / max(1, len(chunks)) * 80)
                self.queue.update_job(job_id, progress=progress, n_done=i + 1)

            # 文档级重复性（一次性，复用 Phase A 向量）
            try:
                if DEMO_SKIP_RETRIEVAL or q_dense is None:
                    log("Demo 快速模式：跳过文档级重复性检查")
                    raise RuntimeError("Demo 快速模式跳过重复性检查")
                if self._stop_if_cancelled(job_id, "重复性检查前"):
                    return
                rep_by_chunk, _ = engine.check_document_repetition(job["doc_name"], chunks, q_dense)
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
                self.queue.update_job(job_id, status="done", progress=100,
                                      agent_status=f"审查完成，共发现 {n_issues} 处问题，等待人工裁决")
            log(f"任务 {job_id} 完成，问题 {n_issues} 条")
        except Exception as exc:
            if self._is_cancelled(job_id):
                log(f"任务 {job_id} 已取消，忽略异常: {exc}")
                return
            import traceback
            traceback.print_exc()
            self.queue.update_job(job_id, status="failed", error=str(exc)[:500],
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
            self.queue.add_issue(
                job_id, idx, "escalation", status or "不确定",
                block_indices=source_blocks,
                title="需人工裁决",
                original_text=chunk["content"][:120],
                reason="审查链已将该项加入主智能体升级队列",
                detail={"numeric": numeric_detail, "summary": review.get("summary", "")},
                escalation_type=escalations[0],
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
        for bi in block_indices:
            if docx_adapter.apply_edit_at_block(doc, bi, original, new_text):
                applied = True
                break
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
        while True:
            try:
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








