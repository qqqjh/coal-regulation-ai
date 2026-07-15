"""升级队列与标注飞轮存储 v1（SQLite，跨进程/跨线程安全）

两张核心表：
  escalations  审查链产生的"分歧/低置信/数值冲突/异常"升级项。
               引擎并行写入 -> 主智能体 loop 逐个取出处理 -> 解决后落 resolution。
  annotations  人工/智能体最终裁决沉淀（数据飞轮）。
               每条都可导出为黄金标注集格式，评估集随使用持续增长。

状态机：pending -> processing -> resolved
                         \\-> (失败 attempts+1) -> pending（重试）-> dead（≥MAX_ATTEMPTS）
"""
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_DB = Path("data/review_queue_v9.db")
MAX_ATTEMPTS = 3

# 升级类型
ESC_DISAGREEMENT = "disagreement"        # 初审与核验意见相反，且数值工具支持初审
ESC_NUMERIC_CONFLICT = "numeric_conflict"  # LLM 最终结论与数值工具结论矛盾
ESC_LOW_CONFIDENCE = "low_confidence"    # 最终结论"不确定"
ESC_ERROR = "error"                      # 调用异常/解析失败
ESC_COUNTER_CHECK = "counter_check"      # 初审判合规，但反向数值核验疑似超限（漏报嫌疑）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS escalations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    item_type TEXT NOT NULL,
    doc_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    dedupe_key TEXT UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    processing_since TEXT,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_esc_status ON escalations(status, id);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    doc_name TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    pending_content TEXT NOT NULL,
    kb_refs TEXT,
    model_verdict TEXT,
    final_verdict TEXT NOT NULL,
    reason TEXT,
    source TEXT NOT NULL,
    escalation_id INTEGER,
    extra TEXT,
    job_id TEXT,
    issue_id INTEGER,
    user_id TEXT,
    action TEXT,
    issue_type TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    reused_count INTEGER NOT NULL DEFAULT 0,
    last_reused_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ann_doc ON annotations(doc_name, chunk_index);

-- ====== Web 集成（后端 ↔ v9 worker 进程间通道）======
-- 一次文档审查任务。后端写 pending，worker 取走执行并回写进度/状态。
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    doc_name TEXT NOT NULL,
    docx_path TEXT NOT NULL,
    source_format TEXT NOT NULL DEFAULT 'docx',
    mineru_source_path TEXT,
    original_pdf_path TEXT,
    paragraphs_path TEXT,          -- 段落模型 JSON 落盘路径（前端中间面板用）
    mine_type TEXT NOT NULL DEFAULT 'non_outburst',
    kb_id INTEGER,
    user_id TEXT DEFAULT 'guest',
    status TEXT NOT NULL DEFAULT 'pending',  -- pending/parsing/reviewing/done/failed/cancelled
    progress INTEGER NOT NULL DEFAULT 0,
    n_chunks INTEGER DEFAULT 0,
    n_done INTEGER DEFAULT 0,
    agent_stage TEXT,              -- parsing/vectorizing/retrieval_rerank/reviewing/repetition/done
    phase_done INTEGER DEFAULT 0,
    phase_total INTEGER DEFAULT 0,
    timings TEXT,                  -- JSON: 各阶段耗时秒数
    agent_status TEXT,             -- 主智能体当前状态文本（前端顶部展示）
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);

-- 审查产出的单条问题（边审边写，前端 SSE 流式拉取）。
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    block_indices TEXT,            -- JSON: 该问题对应的段落 block_index 列表（定位高亮）
    issue_type TEXT NOT NULL,      -- compliance/typo/redundancy/numeric/escalation
    status TEXT NOT NULL,          -- 不合规/不确定/错别字/重复/...
    title TEXT,
    original_text TEXT,            -- 待审原文片段（用于精确高亮 + 改字定位）
    suggestion TEXT,
    regulation TEXT,
    reason TEXT,
    detail TEXT,                   -- JSON: 数值核验明细/升级信息等
    escalation_type TEXT,
    human_action TEXT NOT NULL DEFAULT 'pending',  -- pending/accept/reject/custom
    human_text TEXT,               -- 人工自定义意见
    agent_applied INTEGER NOT NULL DEFAULT 0,      -- 0未处理/1已应用/2无需改/-1失败
    agent_note TEXT,               -- 主智能体处理说明
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_job ON issues(job_id, id);
CREATE INDEX IF NOT EXISTS idx_issues_feedback ON issues(human_action, agent_applied);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _feedback_ngrams(text: str, width: int = 3) -> set[str]:
    normalized = re.sub(r"\s+", "", str(text or "")).lower()
    normalized = re.sub(r"[^\w\u4e00-\u9fff.%％]+", "", normalized)
    if not normalized:
        return set()
    if len(normalized) <= width:
        return {normalized}
    return {normalized[i:i + width] for i in range(len(normalized) - width + 1)}


class ReviewQueue:
    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._ensure_job_columns()
            self._ensure_annotation_columns()
            self._conn.commit()

    def _ensure_job_columns(self):
        rows = self._conn.execute("PRAGMA table_info(jobs)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "user_id": "TEXT DEFAULT 'guest'",
            "kb_id": "INTEGER",
            "source_format": "TEXT NOT NULL DEFAULT 'docx'",
            "mineru_source_path": "TEXT",
            "original_pdf_path": "TEXT",
            "agent_stage": "TEXT",
            "phase_done": "INTEGER DEFAULT 0",
            "phase_total": "INTEGER DEFAULT 0",
            "timings": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")

    def _ensure_annotation_columns(self):
        rows = self._conn.execute("PRAGMA table_info(annotations)").fetchall()
        existing = {row["name"] for row in rows}
        additions = {
            "job_id": "TEXT",
            "issue_id": "INTEGER",
            "user_id": "TEXT",
            "action": "TEXT",
            "issue_type": "TEXT",
            "lifecycle_status": "TEXT NOT NULL DEFAULT 'active'",
            "reused_count": "INTEGER NOT NULL DEFAULT 0",
            "last_reused_at": "TEXT",
        }
        for name, ddl in additions.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE annotations ADD COLUMN {name} {ddl}")

        # 兼容已经沉淀的人工反馈：从 extra 中回填 issue/action/type。
        legacy = self._conn.execute(
            "SELECT id, extra FROM annotations "
            "WHERE source='human' AND (issue_id IS NULL OR action IS NULL OR issue_type IS NULL)"
        ).fetchall()
        for row in legacy:
            try:
                extra = json.loads(row["extra"] or "{}")
            except (json.JSONDecodeError, TypeError):
                extra = {}
            self._conn.execute(
                "UPDATE annotations SET issue_id=COALESCE(issue_id, ?), "
                "action=COALESCE(action, ?), issue_type=COALESCE(issue_type, ?) WHERE id=?",
                (
                    extra.get("issue_id"),
                    extra.get("action"),
                    extra.get("issue_type"),
                    int(row["id"]),
                ),
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ann_feedback "
            "ON annotations(source, lifecycle_status, issue_id, id)"
        )

    def close(self):
        with self._lock:
            self._conn.close()

    # ============ 升级队列 ============

    def add_escalation(self, item_type: str, doc_name: str, chunk_index: int,
                       payload: Dict[str, Any], dedupe_key: Optional[str] = None) -> Optional[int]:
        """入队。dedupe_key 重复时忽略（断点续跑不产生重复项），返回 None。"""
        if dedupe_key is None:
            dedupe_key = f"{doc_name}#{chunk_index}#{item_type}#{int(time.time()*1000)}"
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO escalations "
                "(created_at, item_type, doc_name, chunk_index, dedupe_key, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_now(), item_type, doc_name, int(chunk_index), dedupe_key,
                 json.dumps(payload, ensure_ascii=False)),
            )
            self._conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def fetch_next(self) -> Optional[Dict[str, Any]]:
        """取出最早的 pending 项并标记 processing。无项返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM escalations WHERE status='pending' "
                "AND attempts < ? ORDER BY id LIMIT 1", (MAX_ATTEMPTS,)
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE escalations SET status='processing', processing_since=? WHERE id=?",
                (_now(), row["id"]),
            )
            self._conn.commit()
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def resolve(self, item_id: int, resolution: Dict[str, Any], resolved_by: str = "agent"):
        with self._lock:
            self._conn.execute(
                "UPDATE escalations SET status='resolved', resolution=?, "
                "resolved_by=?, resolved_at=? WHERE id=?",
                (json.dumps(resolution, ensure_ascii=False), resolved_by, _now(), int(item_id)),
            )
            self._conn.commit()

    def release(self, item_id: int, error: str = ""):
        """处理失败：attempts+1，未达上限回 pending，达上限转 dead。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM escalations WHERE id=?", (int(item_id),)
            ).fetchone()
            if row is None:
                return
            attempts = row["attempts"] + 1
            status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
            self._conn.execute(
                "UPDATE escalations SET status=?, attempts=?, processing_since=NULL, "
                "resolution=COALESCE(resolution, ?) WHERE id=?",
                (status, attempts,
                 json.dumps({"last_error": error[:300]}, ensure_ascii=False) if error else None,
                 int(item_id)),
            )
            self._conn.commit()

    def requeue_stale(self, minutes: int = 30) -> int:
        """把卡死的 processing 项放回 pending（进程崩溃恢复）。"""
        cutoff = datetime.fromtimestamp(time.time() - minutes * 60).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE escalations SET status='pending', processing_since=NULL "
                "WHERE status='processing' AND processing_since < ?", (cutoff,)
            )
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM escalations GROUP BY status"
            ).fetchall()
            type_rows = self._conn.execute(
                "SELECT item_type, COUNT(*) AS n FROM escalations "
                "WHERE status IN ('pending','processing') GROUP BY item_type"
            ).fetchall()
        result = {r["status"]: r["n"] for r in rows}
        result["pending_by_type"] = {r["item_type"]: r["n"] for r in type_rows}
        return result

    def list_items(self, status: str = "pending", limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM escalations WHERE status=? ORDER BY id LIMIT ?",
                (status, int(limit)),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            if item.get("resolution"):
                item["resolution"] = json.loads(item["resolution"])
            items.append(item)
        return items

    def get_item(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM escalations WHERE id=?", (int(item_id),)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        if item.get("resolution"):
            item["resolution"] = json.loads(item["resolution"])
        return item

    # ============ 标注飞轮 ============

    def add_annotation(self, doc_name: str, chunk_index: int, pending_content: str,
                       final_verdict: str, source: str,
                       kb_refs: Optional[List[Dict]] = None,
                       model_verdict: str = "", reason: str = "",
                       escalation_id: Optional[int] = None,
                       extra: Optional[Dict] = None,
                       job_id: str = "", issue_id: Optional[int] = None,
                       user_id: str = "", action: str = "",
                       issue_type: str = "") -> int:
        """沉淀一条最终裁决。source: human / agent / frontend。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO annotations (created_at, doc_name, chunk_index, pending_content, "
                "kb_refs, model_verdict, final_verdict, reason, source, escalation_id, extra, "
                "job_id, issue_id, user_id, action, issue_type, lifecycle_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (_now(), doc_name, int(chunk_index), pending_content,
                 json.dumps(kb_refs or [], ensure_ascii=False),
                 model_verdict, final_verdict, reason, source,
                 escalation_id, json.dumps(extra or {}, ensure_ascii=False),
                 job_id, issue_id, user_id, action, issue_type),
            )
            self._conn.commit()
            return cur.lastrowid

    def upsert_human_annotation(
        self,
        *,
        doc_name: str,
        chunk_index: int,
        pending_content: str,
        final_verdict: str,
        issue_id: int,
        job_id: str = "",
        user_id: str = "",
        action: str,
        issue_type: str = "",
        kb_refs: Optional[List[Dict]] = None,
        model_verdict: str = "",
        reason: str = "",
        extra: Optional[Dict] = None,
    ) -> int:
        """同一问题只保留一条当前有效人工裁决，重新裁决时更新原样本。"""
        payload = json.dumps(extra or {}, ensure_ascii=False)
        refs = json.dumps(kb_refs or [], ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM annotations WHERE source='human' AND issue_id=? "
                "ORDER BY id DESC LIMIT 1",
                (int(issue_id),),
            ).fetchone()
            if row:
                ann_id = int(row["id"])
                self._conn.execute(
                    "UPDATE annotations SET created_at=?, doc_name=?, chunk_index=?, "
                    "pending_content=?, kb_refs=?, model_verdict=?, final_verdict=?, "
                    "reason=?, extra=?, job_id=?, user_id=?, action=?, issue_type=?, "
                    "lifecycle_status='active', reused_count=0, last_reused_at=NULL WHERE id=?",
                    (
                        _now(), doc_name, int(chunk_index), pending_content, refs,
                        model_verdict, final_verdict, reason, payload, job_id, user_id,
                        action, issue_type, ann_id,
                    ),
                )
            else:
                cur = self._conn.execute(
                    "INSERT INTO annotations (created_at, doc_name, chunk_index, pending_content, "
                    "kb_refs, model_verdict, final_verdict, reason, source, extra, job_id, "
                    "issue_id, user_id, action, issue_type, lifecycle_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'human', ?, ?, ?, ?, ?, ?, 'active')",
                    (
                        _now(), doc_name, int(chunk_index), pending_content, refs,
                        model_verdict, final_verdict, reason, payload, job_id,
                        int(issue_id), user_id, action, issue_type,
                    ),
                )
                ann_id = int(cur.lastrowid)
            self._conn.commit()
            return ann_id

    def deactivate_human_annotation(self, issue_id: int) -> bool:
        """撤回人工裁决时同步撤回飞轮样本，避免错误标签继续被复用。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE annotations SET lifecycle_status='revoked' "
                "WHERE source='human' AND issue_id=? AND lifecycle_status='active'",
                (int(issue_id),),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_annotations(self, limit: int = 200, source: Optional[str] = None
                         ) -> List[Dict[str, Any]]:
        """列出飞轮记录（前端查看用），最新在前。"""
        with self._lock:
            if source:
                rows = self._conn.execute(
                    "SELECT * FROM annotations WHERE source=? ORDER BY id DESC LIMIT ?",
                    (source, int(limit)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM annotations ORDER BY id DESC LIMIT ?", (int(limit),)
                ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            for key in ("kb_refs", "extra"):
                if item.get(key):
                    try:
                        item[key] = json.loads(item[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            out.append(item)
        return out

    def count_annotations(self) -> Dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, COUNT(*) n FROM annotations GROUP BY source"
            ).fetchall()
        return {r["source"]: r["n"] for r in rows}

    @staticmethod
    def _decode_annotation(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
        item = dict(row)
        for key in ("kb_refs", "extra"):
            if item.get(key):
                try:
                    item[key] = json.loads(item[key])
                except (json.JSONDecodeError, TypeError):
                    item[key] = {} if key == "extra" else []
        return item

    def list_human_feedback(
        self,
        *,
        limit: int = 200,
        action: str = "",
        issue_type: str = "",
        query: str = "",
        include_revoked: bool = False,
    ) -> List[Dict[str, Any]]:
        clauses = ["source='human'"]
        params: List[Any] = []
        if not include_revoked:
            clauses.append("lifecycle_status='active'")
        if action:
            clauses.append("action=?")
            params.append(action)
        if issue_type:
            clauses.append("issue_type=?")
            params.append(issue_type)
        if query:
            clauses.append("(doc_name LIKE ? OR pending_content LIKE ? OR reason LIKE ?)")
            like = f"%{query}%"
            params.extend([like, like, like])
        params.append(max(1, min(int(limit), 2000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM annotations WHERE " + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._decode_annotation(row) for row in rows]

    def flywheel_stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM annotations WHERE source='human' "
                "AND lifecycle_status='active' ORDER BY id"
            ).fetchall()
        samples = [self._decode_annotation(row) for row in rows]
        actions = Counter(str(item.get("action") or "unknown") for item in samples)
        issue_types = Counter(str(item.get("issue_type") or "unknown") for item in samples)
        reuse_events = sum(int(item.get("reused_count") or 0) for item in samples)
        reused_samples = sum(1 for item in samples if int(item.get("reused_count") or 0) > 0)
        doc_count = len({str(item.get("doc_name") or "") for item in samples})

        today = datetime.now().date()
        day_keys = [(today - timedelta(days=offset)).isoformat() for offset in range(13, -1, -1)]
        daily_counts = Counter(str(item.get("created_at") or "")[:10] for item in samples)
        daily = [{"date": day, "count": daily_counts.get(day, 0)} for day in day_keys]
        confirmed = actions.get("accept", 0) + actions.get("custom", 0)
        total = len(samples)
        return {
            "total_samples": total,
            "confirmed_samples": confirmed,
            "rejected_samples": actions.get("reject", 0),
            "custom_samples": actions.get("custom", 0),
            "document_count": doc_count,
            "reused_samples": reused_samples,
            "reuse_events": reuse_events,
            "confirmation_rate": round(confirmed / total, 4) if total else 0.0,
            "action_counts": dict(actions),
            "issue_type_counts": dict(issue_types),
            "daily": daily,
        }

    def search_human_feedback(
        self,
        content: str,
        *,
        limit: int = 3,
        issue_types: Optional[tuple[str, ...]] = None,
        min_score: float = 0.24,
        mark_reused: bool = False,
    ) -> List[Dict[str, Any]]:
        """检索相似的有效人工样本，作为后续审查的少样本经验。"""
        query_grams = _feedback_ngrams(content)
        if not query_grams:
            return []
        clauses = ["source='human'", "lifecycle_status='active'", "pending_content!=''"]
        params: List[Any] = []
        if issue_types:
            placeholders = ",".join("?" for _ in issue_types)
            clauses.append(f"issue_type IN ({placeholders})")
            params.extend(issue_types)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM annotations WHERE " + " AND ".join(clauses)
                + " ORDER BY id DESC LIMIT 2000",
                tuple(params),
            ).fetchall()

        ranked = []
        for row in rows:
            sample_grams = _feedback_ngrams(row["pending_content"])
            if not sample_grams:
                continue
            overlap = len(query_grams & sample_grams)
            if overlap == 0:
                continue
            coverage = overlap / len(sample_grams)
            jaccard = overlap / len(query_grams | sample_grams)
            score = 0.8 * coverage + 0.2 * jaccard
            if score < min_score:
                continue
            item = self._decode_annotation(row)
            item["similarity"] = round(score, 4)
            ranked.append(item)
        ranked.sort(key=lambda item: (item["similarity"], item["id"]), reverse=True)
        selected = ranked[:max(1, int(limit))]

        if mark_reused and selected:
            ids = [int(item["id"]) for item in selected]
            placeholders = ",".join("?" for _ in ids)
            with self._lock:
                self._conn.execute(
                    f"UPDATE annotations SET reused_count=reused_count+1, last_reused_at=? "
                    f"WHERE id IN ({placeholders})",
                    (_now(), *ids),
                )
                self._conn.commit()
            for item in selected:
                item["reused_count"] = int(item.get("reused_count") or 0) + 1
                item["last_reused_at"] = _now()
        return selected

    def delete_annotation(self, ann_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM annotations WHERE id=?", (int(ann_id),)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def export_gold_cases(self, out_path: Path,
                          sources: tuple = ("human", "frontend")) -> int:
        """把（默认人工确认的）标注导出为黄金标注集格式，用于扩充评估集。"""
        placeholders = ",".join("?" for _ in sources)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM annotations WHERE source IN ({placeholders}) "
                "AND lifecycle_status='active' ORDER BY id",
                sources,
            ).fetchall()
        cases = []
        for row in rows:
            cases.append({
                "case_id": f"flywheel_{row['id']:05d}",
                "pending": {
                    "doc_name": row["doc_name"],
                    "chunk_index": row["chunk_index"],
                    "content": row["pending_content"],
                },
                "kb_refs": json.loads(row["kb_refs"] or "[]"),
                "model_verdict": row["model_verdict"],
                "gold_verdict": row["final_verdict"],
                "reason": row["reason"],
                "annotated_at": row["created_at"],
                "source": row["source"],
            })
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as handle:
            json.dump({"cases": cases, "exported_at": _now()}, handle,
                      ensure_ascii=False, indent=2)
        return len(cases)

    # ============ Web 任务（jobs）：后端写 / worker 消费 ============

    def create_job(self, job_id: str, doc_name: str, docx_path: str,
                   mine_type: str = "non_outburst",
                   paragraphs_path: Optional[str] = None,
                   user_id: str = "guest",
                   kb_id: Optional[int] = None,
                   source_format: str = "docx",
                   mineru_source_path: str = "",
                   original_pdf_path: str = "") -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO jobs (job_id, created_at, updated_at, doc_name, docx_path, "
                "source_format, mineru_source_path, original_pdf_path, paragraphs_path, "
                "mine_type, user_id, kb_id, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending')",
                (
                    job_id, _now(), _now(), doc_name, docx_path,
                    str(source_format or "docx").lower(), mineru_source_path,
                    original_pdf_path, paragraphs_path, mine_type, user_id, kb_id,
                ),
            )
            self._conn.commit()
            return cur.lastrowid

    def fetch_pending_job(self) -> Optional[Dict[str, Any]]:
        """worker 取一个 pending 任务并置 parsing。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE jobs SET status='parsing', updated_at=? WHERE id=?",
                (_now(), row["id"]),
            )
            self._conn.commit()
        job = dict(row)
        job["status"] = "parsing"  # row 是更新前快照，对齐实际状态
        return job

    def update_job(self, job_id: str, **fields):
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE jobs SET {cols} WHERE job_id=?",
                (*fields.values(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_jobs(self, user_id: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        """列出审查历史，最新在前。user_id 为空时返回全部任务。"""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if user_id:
                rows = self._conn.execute(
                    """
                    SELECT j.*,
                           COUNT(i.id) AS issue_count,
                           SUM(CASE WHEN i.human_action != 'pending' THEN 1 ELSE 0 END) AS decided_count
                    FROM jobs j
                    LEFT JOIN issues i ON i.job_id = j.job_id
                    WHERE COALESCE(j.user_id, 'guest') = ?
                    GROUP BY j.id
                    ORDER BY j.id DESC
                    LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT j.*,
                           COUNT(i.id) AS issue_count,
                           SUM(CASE WHEN i.human_action != 'pending' THEN 1 ELSE 0 END) AS decided_count
                    FROM jobs j
                    LEFT JOIN issues i ON i.job_id = j.job_id
                    GROUP BY j.id
                    ORDER BY j.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return [dict(row) for row in rows]

    def cancel_job(self, job_id: str, reason: str = "用户取消任务") -> bool:
        """取消未完成任务。pending 任务不会再被 worker 领取；运行中任务由 worker 协作停止。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status='cancelled', progress=0, agent_status=?, "
                "error=?, updated_at=? "
                "WHERE job_id=? AND status NOT IN ('done','failed','cancelled')",
                (reason, reason, _now(), job_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_job(self, job_id: str) -> bool:
        """删除 Web 审查任务及其问题记录。文件缓存由上层按需清理。"""
        with self._lock:
            self._conn.execute("DELETE FROM issues WHERE job_id=?", (job_id,))
            cur = self._conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ============ 问题（issues）：边审边写 / SSE 流式读 / 人工反馈回流 ============

    def add_issue(self, job_id: str, chunk_index: int, issue_type: str, status: str,
                  block_indices: Optional[List[int]] = None,
                  title: str = "", original_text: str = "", suggestion: str = "",
                  regulation: str = "", reason: str = "",
                  detail: Optional[Dict] = None, escalation_type: str = "") -> int:
        if escalation_type and not isinstance(escalation_type, str):
            escalation_type = json.dumps(escalation_type, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO issues (job_id, created_at, chunk_index, block_indices, "
                "issue_type, status, title, original_text, suggestion, regulation, reason, "
                "detail, escalation_type, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, _now(), int(chunk_index),
                 json.dumps(block_indices or [], ensure_ascii=False),
                 issue_type, status, title, original_text, suggestion, regulation, reason,
                 json.dumps(detail or {}, ensure_ascii=False), escalation_type, _now()),
            )
            self._conn.commit()
            return cur.lastrowid

    @staticmethod
    def _issue_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["block_indices"] = json.loads(item.get("block_indices") or "[]")
        item["detail"] = json.loads(item.get("detail") or "{}")
        esc = item.get("escalation_type")
        if isinstance(esc, str) and esc.strip().startswith(("{", "[")):
            try:
                item["escalation_type"] = json.loads(esc)
            except json.JSONDecodeError:
                pass
        return item

    def list_issues(self, job_id: str, after_id: int = 0) -> List[Dict[str, Any]]:
        """拉取某任务 id>after_id 的问题（SSE 增量推送用）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM issues WHERE job_id=? AND id>? ORDER BY id",
                (job_id, int(after_id)),
            ).fetchall()
        return [self._issue_to_dict(r) for r in rows]

    def get_issue(self, issue_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issues WHERE id=?", (int(issue_id),)
            ).fetchone()
        return self._issue_to_dict(row) if row else None

    def set_issue_feedback(self, issue_id: int, human_action: str,
                           human_text: str = "") -> bool:
        """前端写入人工反馈（accept/reject/custom），置 agent_applied=0 等 worker 处理。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE issues SET human_action=?, human_text=?, agent_applied=0, "
                "updated_at=? WHERE id=?",
                (human_action, human_text, _now(), int(issue_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def fetch_pending_feedback(self) -> Optional[Dict[str, Any]]:
        """worker 取一条待处理的人工反馈（accept/custom 需要主智能体改 Word）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM issues WHERE human_action IN ('accept','custom') "
                "AND agent_applied=0 ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE issues SET agent_applied=3, updated_at=? WHERE id=?",  # 3=处理中
                (_now(), row["id"]),
            )
            self._conn.commit()
        return self._issue_to_dict(row)

    def finish_feedback(self, issue_id: int, applied: int, agent_note: str = ""):
        with self._lock:
            self._conn.execute(
                "UPDATE issues SET agent_applied=?, agent_note=?, updated_at=? WHERE id=?",
                (int(applied), agent_note, _now(), int(issue_id)),
            )
            self._conn.commit()

    def revoke_issue_feedback(self, issue_id: int) -> bool:
        """撤回尚未安全落盘的人工裁决，恢复为待人工处理。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE issues SET human_action='pending', human_text='', "
                "agent_applied=0, agent_note='', updated_at=? WHERE id=?",
                (_now(), int(issue_id)),
            )
            self._conn.commit()
            return cur.rowcount > 0


if __name__ == "__main__":
    import tempfile, os
    # 自检：完整状态机走一遍
    tmp = Path(tempfile.mkdtemp()) / "queue_test.db"
    q = ReviewQueue(tmp)
    i1 = q.add_escalation(ESC_DISAGREEMENT, "docA", 3, {"draft": "不合规", "verified": "合规"},
                          dedupe_key="docA#3#disagreement")
    dup = q.add_escalation(ESC_DISAGREEMENT, "docA", 3, {"draft": "x"},
                           dedupe_key="docA#3#disagreement")
    assert i1 is not None and dup is None, "去重失败"
    item = q.fetch_next()
    assert item["id"] == i1 and item["payload"]["draft"] == "不合规"
    q.release(item["id"], "模拟失败")
    item = q.fetch_next()
    q.resolve(item["id"], {"verdict": "合规", "reason": "数值工具证实更严"}, "agent")
    q.add_annotation("docA", 3, "断电浓度1.2%", "合规", "agent",
                     model_verdict="不合规", reason="严于法规", escalation_id=item["id"])
    q.add_annotation("docA", 5, "xxx", "不合规", "human", reason="超出上限")
    n = q.export_gold_cases(tmp.parent / "gold_export.json")
    stats = q.stats()
    assert stats.get("resolved") == 1 and n == 1
    assert q.fetch_next() is None

    # ---- Web jobs/issues 自检 ----
    q.create_job("job-1", "docA.docx", "/tmp/docA.docx", "non_outburst", "/tmp/p.json")
    job = q.fetch_pending_job()
    assert job["job_id"] == "job-1" and job["status"] == "parsing"
    q.update_job("job-1", status="reviewing", n_chunks=10, progress=20,
                 agent_status="主智能体待命")
    assert q.get_job("job-1")["n_chunks"] == 10

    iid = q.add_issue("job-1", 5, "compliance", "不合规", block_indices=[12, 13],
                      title="支护锚索起吊", original_text="选择支护锚索作为起吊点",
                      suggestion="改用专用吊装锚索", regulation="GB/T 35056-2018 4.4.4.5")
    assert q.list_issues("job-1") and q.list_issues("job-1", after_id=iid) == []
    q.set_issue_feedback(iid, "custom", "改为'必须使用经计算校核的专用起吊锚杆'")
    fb = q.fetch_pending_feedback()
    assert fb and fb["id"] == iid and fb["human_text"]
    assert q.fetch_pending_feedback() is None  # 已被置处理中
    q.finish_feedback(iid, applied=1, agent_note="已按人工意见改写段落")
    assert q.get_issue(iid)["agent_applied"] == 1

    q.close()
    print("[OK] review_queue 状态机/去重/导出 + jobs/issues/反馈回流 自检通过", stats)
