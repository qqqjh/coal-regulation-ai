"""
混合检索RAG审核系统 v6
- v4改进：
  1. 检索立场分类智能体（分类结果仅用于前端展示，不传入审核智能体）
  2. 审核结果增加"不确定"状态（合规/不合规/不确定）
  3. 修复v3遗留误判：并列条款、固定数值、幻觉引用等
  4. 前端：每个chunk卡片固定高度+滚动条；KB参考可点击弹窗查看全文
  5. 前端：KB参考显示立场分类标签（支持/反对/例外/无关）
- v5改进：
  6. 修复误差/偏差/公差范围方向误判：法规"误差在A~B"表示偏差绝对值范围，
     与待审"±X"（X≤B）等价，不应判为违规
- v6改进：
  7. 简化两个审核智能体提示词中"安全触发阈值"（表现C）的判断逻辑，
     修复传感器报警/断电/复电浓度阈值被误判为违规的问题：
     - 报警浓度、断电浓度、停工阈值等：待审值 < 法规值 = 更早触发 = 更严格 = 合规
     - 复电浓度（<X才复电）：待审值 < 法规值 = 要求浓度降得更低才复电 = 更严格 = 合规
     - 添加明确示例，避免LLM误将"低于法规值"等同于"不满足下限"
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional
from datetime import datetime
import numpy as np

import dashscope
from dashscope import TextEmbedding
import chromadb
from rank_bm25 import BM25Okapi
import jieba
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============ 配置 ============
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
dashscope.api_key = API_KEY

QWEN_MODEL = "qwen-plus"
EMBEDDING_MODEL = "text-embedding-v3"

SIMILARITY_THRESHOLD = 0.1
TOP_K = 5
VERIFY_NON_COMPLIANT = True
DOC_FILTER = []  # 空列表 = 审核全部文档（004、006、066）

KB_CHUNKS_DIR = Path("chunks_visualization")
PENDING_CHUNKS_DIR = Path("chunks_visualization")
CHROMA_DB_DIR = Path("data/hybrid_rag_chroma")
OUTPUT_DIR = Path("review_results")


class HybridRAGReviewerV6:
    """混合检索RAG审核器 v6（三智能体：分类+初审+验证）"""

    def __init__(self):
        if not API_KEY:
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")
        self.llm_client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
        self.kb_chunks: List[Dict] = []
        self.kb_texts: List[str] = []
        self.kb_tokenized: List[List[str]] = []
        self.bm25: BM25Okapi = None
        self.chroma_client = None
        self.collection = None

    # ============ 向量 & 搜索 ============

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        batch_size = 10
        all_embeddings = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            batch = [t if t.strip() else " " for t in batch]
            resp = TextEmbedding.call(model=EMBEDDING_MODEL, input=batch)
            if resp.status_code == 200:
                for item in resp.output['embeddings']:
                    all_embeddings.append(item['embedding'])
            else:
                raise Exception(f"Embedding 失败: {resp.code} - {resp.message}")
        return all_embeddings

    def load_knowledge_base(self, kb_json_path: str = None):
        if kb_json_path is None:
            v4_files = sorted(KB_CHUNKS_DIR.glob("chunks_v4_*.json"), reverse=True)
            if not v4_files:
                raise FileNotFoundError("未找到知识库chunks文件")
            kb_json_path = v4_files[0]
        print(f"加载知识库: {kb_json_path}")
        self.kb_json_path = kb_json_path
        with open(kb_json_path, 'r', encoding='utf-8') as f:
            kb_data = json.load(f)
        chunk_id = 0
        for doc_name, chunks in kb_data.items():
            for chunk in chunks:
                chunk['id'] = f"kb_{chunk_id}"
                chunk['doc_name'] = doc_name
                chunk_id += 1
                self.kb_chunks.append(chunk)
                self.kb_texts.append(chunk['content'])
        print(f"  加载 {len(self.kb_chunks)} 个知识库chunks")

    def build_bm25_index(self):
        print("构建BM25索引...")
        self.kb_tokenized = [list(jieba.cut(text)) for text in self.kb_texts]
        self.bm25 = BM25Okapi(self.kb_tokenized)

    def build_vector_index(self):
        print("构建向量索引...")
        CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
        self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
        try:
            self.chroma_client.delete_collection("kb_chunks")
        except:
            pass
        self.collection = self.chroma_client.create_collection(
            name="kb_chunks", metadata={"hnsw:space": "cosine"}
        )
        valid_chunks = [(i, c) for i, c in enumerate(self.kb_chunks) if c['content'].strip()]
        print(f"  准备 {len(valid_chunks)} 个有效文档...")
        batch_size = 10
        for batch_start in range(0, len(valid_chunks), batch_size):
            batch = valid_chunks[batch_start:batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            total_batches = (len(valid_chunks) - 1) // batch_size + 1
            print(f"  处理批次 {batch_num}/{total_batches}...")
            texts = [c['content'] for _, c in batch]
            ids = [c['id'] for _, c in batch]
            metadatas = [{
                'doc_name': c['doc_name'],
                'chapter': c.get('chapter', ''),
                'section': c.get('section', ''),
                'chunk_level': c.get('chunk_level', ''),
                'page_range': c.get('page_range', '')
            } for _, c in batch]
            embeddings = self.get_embeddings(texts)
            self.collection.add(ids=ids, embeddings=embeddings, documents=texts, metadatas=metadatas)
            time.sleep(0.5)
        print(f"  向量索引构建完成，共 {len(valid_chunks)} 个文档")

    def load_or_build_index(self):
        marker_file = CHROMA_DB_DIR / "kb_source.txt"
        current_source = str(self.kb_json_path) if hasattr(self, 'kb_json_path') else str(len(self.kb_chunks))
        need_rebuild = True
        if (CHROMA_DB_DIR / "chroma.sqlite3").exists() and marker_file.exists():
            if marker_file.read_text(encoding='utf-8').strip() == current_source:
                need_rebuild = False
        if not need_rebuild:
            print("加载已有向量索引（来源匹配）...")
            self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
            try:
                self.collection = self.chroma_client.get_collection("kb_chunks")
                count = self.collection.count()
                if count > 0:
                    print(f"  已加载 {count} 个向量")
                else:
                    need_rebuild = True
            except:
                need_rebuild = True
        if need_rebuild:
            print("知识库来源已变更或索引不存在，重新构建向量索引...")
            self.build_vector_index()
            CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
            marker_file.write_text(current_source, encoding='utf-8')
        self.build_bm25_index()

    def vector_search(self, query: str, k: int = TOP_K) -> List[Tuple[Dict, float]]:
        query_embedding = self.get_embeddings([query])[0]
        results = self.collection.query(query_embeddings=[query_embedding], n_results=k)
        matched = []
        if results and results['ids'] and results['ids'][0]:
            for i, chunk_id in enumerate(results['ids'][0]):
                for chunk in self.kb_chunks:
                    if chunk['id'] == chunk_id:
                        distance = results['distances'][0][i] if results.get('distances') else 0
                        matched.append((chunk, 1 - distance))
                        break
        return matched

    def bm25_search(self, query: str, k: int = TOP_K) -> List[Tuple[Dict, float]]:
        query_tokens = list(jieba.cut(query))
        scores = self.bm25.get_scores(query_tokens)
        top_indices = np.argsort(scores)[::-1][:k]
        matched = []
        for idx in top_indices:
            if scores[idx] > 0:
                matched.append((self.kb_chunks[idx], scores[idx] / (scores[idx] + 1)))
        return matched

    def hybrid_search(self, query: str, k: int = TOP_K,
                      vector_weight: float = 0.5,
                      vector_query: str = None) -> List[Tuple[Dict, float]]:
        # 向量检索用前500字（避免长文本稀释语义），BM25用全文（覆盖所有关键词）
        vector_results = self.vector_search(vector_query or query[:500], k=k * 2)
        bm25_results = self.bm25_search(query, k=k * 2)
        rrf_scores = {}
        similarity_scores = {}
        chunk_map = {}
        for rank, (chunk, similarity) in enumerate(vector_results):
            cid = chunk['id']
            chunk_map[cid] = chunk
            rrf_scores[cid] = rrf_scores.get(cid, 0) + vector_weight / (rank + 60)
            if cid not in similarity_scores:
                similarity_scores[cid] = similarity
        for rank, (chunk, score) in enumerate(bm25_results):
            cid = chunk['id']
            chunk_map[cid] = chunk
            rrf_scores[cid] = rrf_scores.get(cid, 0) + (1 - vector_weight) / (rank + 60)
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)[:k]
        return [(chunk_map[cid], similarity_scores.get(cid, 0.5)) for cid in sorted_ids]

    def search_relevant(self, query: str, k: int = TOP_K,
                        threshold: float = SIMILARITY_THRESHOLD) -> List[Dict]:
        results = self.hybrid_search(query, k=k)
        return [
            {'chunk': chunk, 'score': score}
            for chunk, score in results
            if score >= threshold
        ]

    # ============ 文档自检索（重复性检查用）============

    def build_doc_bm25(self, doc_chunks: List[Dict]) -> BM25Okapi:
        """为单篇待审文档构建BM25索引，供重复性检查使用。"""
        tokenized = [list(jieba.cut(c['content'])) for c in doc_chunks]
        return BM25Okapi(tokenized)

    def search_doc_similar(self, query_chunk: Dict, query_idx: int,
                           doc_bm25: BM25Okapi, doc_chunks: List[Dict],
                           k: int = 3, min_norm: float = 0.5) -> List[Dict]:
        """在同一文档内找与 query_chunk 最相似的其他 chunks（排除自身）。
        只返回归一化得分 >= min_norm 的结果，避免无关内容进入重复性检查。
        """
        tokens = list(jieba.cut(query_chunk['content']))
        scores = doc_bm25.get_scores(tokens)
        # 排除自身
        others = [(i, s) for i, s in enumerate(scores) if i != query_idx]
        if not others:
            return []
        max_score = max(s for _, s in others)
        if max_score <= 0:
            return []
        results = []
        for idx, score in sorted(others, key=lambda x: x[1], reverse=True)[:k]:
            if score / max_score >= min_norm:
                c = doc_chunks[idx]
                results.append({
                    'chunk_index': idx,
                    'chapter': c.get('chapter', ''),
                    'section': c.get('section', ''),
                    'content': c['content'],
                    'norm_score': round(score / max_score, 3),
                })
        return results

    # ============ 构建知识库上下文 ============

    def _build_kb_context(self, kb_results: List[Dict]) -> str:
        kb_context = ""
        for i, result in enumerate(kb_results, 1):
            chunk = result['chunk']
            kb_context += f"\n【参考法规{i}】来源: {chunk['doc_name']}\n"
            if chunk.get('chapter'):
                kb_context += f"章节: {chunk['chapter']}\n"
            if chunk.get('section'):
                kb_context += f"小节: {chunk['section']}\n"
            kb_context += f"内容: {chunk['content']}\n"
            kb_context += "-" * 50
        return kb_context

    # ============ 智能体1：立场分类 ============

    def classify_kb_chunks(self, pending_chunk: Dict, kb_results: List[Dict]) -> List[Dict]:
        """
        立场分类智能体：对每条检索到的KB chunk判断其立场。
        输出结果仅用于前端展示，不传入审核智能体。
        分类：支持 / 反对 / 例外 / 无关
        """
        pending_content = pending_chunk['content']

        chunk_list_text = ""
        for i, result in enumerate(kb_results, 1):
            chunk = result['chunk']
            chunk_list_text += f"\n[{i}] 来源: {chunk['doc_name']}"
            if chunk.get('chapter'):
                chunk_list_text += f" / {chunk['chapter']}"
            chunk_list_text += f"\n内容: {chunk['content'][:400]}\n"

        prompt = f"""你是煤矿安全法规专家。请判断下方每条法规片段对于"待审内容是否合规"这一问题的立场。

【待审内容】
{pending_content[:600]}

【待检索到的法规片段列表】
{chunk_list_text}

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
            {"index": i + 1, "chunk_id": kb_results[i]['chunk']['id'],
             "classification": "无关", "reason": "分类失败"}
            for i in range(len(kb_results))
        ]
        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": "你是煤矿安全法规专家，负责判断法规片段对待审内容的立场。严格按JSON格式输出。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            result_text = response.choices[0].message.content
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if not json_match:
                return default
            parsed = json.loads(json_match.group())
            classifications = parsed.get('classifications', [])
            result = []
            for i, kb_r in enumerate(kb_results):
                entry = {"index": i + 1, "chunk_id": kb_r['chunk']['id'],
                         "classification": "无关", "reason": ""}
                for c in classifications:
                    if c.get('index') == i + 1:
                        entry["classification"] = c.get("classification", "无关")
                        entry["reason"] = c.get("reason", "")
                        break
                result.append(entry)
            return result
        except Exception as e:
            print(f"    分类失败: {e}")
            return default

    # ============ 智能体2：初审 ============

    def review_chunk_with_llm(self, pending_chunk: Dict, kb_results: List[Dict]) -> Dict:
        """
        初审智能体：7条核心原则 + 不确定状态支持。
        """
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk['content']
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

        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system",
                     "content": "你是煤矿安全法规审核专家。只关注数值冲突和规则冲突，严于法规的要求视为合规。严格按JSON格式输出，不引用知识库以外的内容。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            result_text = response.choices[0].message.content
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if json_match:
                return json.loads(json_match.group())
            return {"raw_response": result_text, "parse_error": "无法提取JSON"}
        except json.JSONDecodeError:
            return {"raw_response": result_text, "parse_error": "JSON解析失败"}
        except Exception as e:
            return {"error": str(e)}

    # ============ 智能体3：验证 ============

    def verify_review_result(self, pending_chunk: Dict, kb_results: List[Dict],
                             initial_result: Dict) -> Dict:
        """
        验证智能体：对初审"不合规"/"不确定"结果进行二次核查，过滤5类误判。
        只删除误判项，不新增问题。
        """
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk['content']
        initial_issues_text = json.dumps(
            initial_result.get('issues', []), ensure_ascii=False, indent=2
        )

        prompt = f"""你是一位资深合规审核质量控制专家。请检查初步审核结果，识别并纠正其中的误判。
你只能删除误判项，不能新增问题，不能修改正确的违规判断。

【待审内容】
{pending_content}

【参考法规知识库（唯一允许引用的依据）】
{kb_context}

【初步审核发现的问题（需逐条核查）】
{initial_issues_text}

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

请输出修正后的完整审核结果：
{{
    "compliance_status": "合规/不合规/不确定",
    "issues": [...],
    "summary": "修正后的一句话结论",
    "verification_notes": "说明删除了哪些误判项及原因（若无修正填'初审结果准确，无误判'）"
}}
"""

        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system",
                     "content": "你是合规审核质量控制专家，专门纠正初审中的误判。只删除误判项，不新增问题。严格按JSON格式输出。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            result_text = response.choices[0].message.content
            json_match = re.search(r'\{[\s\S]*\}', result_text)
            if json_match:
                verified = json.loads(json_match.group())
                # 尊重验证智能体返回的状态（如"不确定"）；只在状态缺失时按issues推断
                if 'compliance_status' not in verified or not verified['compliance_status']:
                    verified['compliance_status'] = '合规' if not verified.get('issues') else '不合规'
                elif not verified.get('issues') and verified.get('compliance_status') not in ('不确定',):
                    verified['compliance_status'] = '合规'
                return verified
            return initial_result
        except Exception as e:
            print(f"    验证失败: {e}，使用初审结果")
            return initial_result

    # ============ 完整三智能体审核流程 ============

    def review_chunk_complete(self, pending_chunk: Dict,
                              kb_results: List[Dict]) -> Tuple[Dict, List[Dict]]:
        """
        完整审核流程：
        1. 分类智能体（结果仅用于前端）
        2. 初审智能体
        3. 验证智能体（仅对不合规/不确定调用）
        返回 (审核结果, 分类结果列表)
        """
        # Stage 1: 立场分类（独立于审核）
        classifications = self.classify_kb_chunks(pending_chunk, kb_results)

        # Stage 2: 初审
        draft = self.review_chunk_with_llm(pending_chunk, kb_results)
        if draft.get('error') or draft.get('parse_error'):
            draft['verified'] = False
            return draft, classifications

        has_issues = bool(draft.get('issues'))
        status = draft.get('compliance_status', '')
        needs_verify = VERIFY_NON_COMPLIANT and has_issues and ('不合规' in status or '不确定' in status)

        # Stage 3: 验证
        if needs_verify:
            print("（二次验证）", end=" ", flush=True)
            verified = self.verify_review_result(pending_chunk, kb_results, draft)
            verified['verified'] = True
            verified['draft_issue_count'] = len(draft.get('issues', []))
            return verified, classifications

        draft['verified'] = False
        return draft, classifications

    # ============ 智能体4：错别字检查 ============

    def check_typos(self, pending_chunk: Dict) -> Dict:
        """错别字检查智能体：无需RAG，直接LLM分析原文。"""
        content = pending_chunk['content']
        prompt = f"""请检查下方煤矿作业规程文本，找出其中的**汉字错别字**。

只标注汉字的字形相近错误：
- 例：军→均（"回风顺槽军采用"中"军"应为"均"）
- 例：黯→暗（字形相近，用错了字）
- 例：做为→作为（"做"在此处应为"作"）

注意：
- 如果某个词你不100%确定是错别字，就不要标注
- 数字、英文字母、单位符号不在检查范围内（m2不是错别字，MPa大小写不是错别字）
- 矿山专有术语不标注（即使你认为可能写错了）
- OCR扫描中的空格、断行等格式问题不标注

【待检查文本】
{content[:800]}

输出JSON：{{
  "has_issues": true或false,
  "issues": [{{"original": "原文错误汉字（1-3字）", "suggestion": "正确汉字", "context": "前后各5字"}}],
  "summary": "一句话说明"
}}"""
        default = {"has_issues": False, "issues": [], "summary": "检查失败"}
        try:
            resp = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": "你是专业汉字校对员，只标注**汉字**的字形相近错别字（如军→均、黯→暗、做为→作为）。以下绝对不标注：①单位大小写（m2/m²/MPa/MPA/KN/kN均不标注）②矿山术语（逼帮板/蔽帮板/菠萝盖等不标注，即使你认为写错了）③英文字母/数字/符号。如不100%确定是汉字形近错误，不标注。严格按JSON输出。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0
            )
            m = re.search(r'\{[\s\S]*\}', resp.choices[0].message.content)
            if not m:
                return default
            result = json.loads(m.group())
            # Post-filter: keep only issues where both original and suggestion contain Chinese
            def _is_hanzi_error(issue: Dict) -> bool:
                orig = issue.get('original', '')
                sugg = issue.get('suggestion', '')
                return (bool(re.search(r'[\u4e00-\u9fff]', orig)) and
                        bool(re.search(r'[\u4e00-\u9fff]', sugg)))
            filtered = [iss for iss in result.get('issues', []) if _is_hanzi_error(iss)]
            result['issues'] = filtered
            result['has_issues'] = bool(filtered)
            return result
        except Exception as e:
            return {**default, "summary": f"检查异常: {str(e)[:60]}"}

    # ============ 智能体5：重复性检查 ============

    def check_repetition(self, pending_chunk: Dict, similar_chunks: List[Dict]) -> Dict:
        """重复性检查智能体：基于文档自身BM25结果，判断是否存在重复段落。"""
        default = {"has_duplicates": False, "duplicates": [], "summary": "无重复内容"}
        if not similar_chunks:
            return default
        content = pending_chunk['content']
        similar_text = ""
        for sc in similar_chunks:
            loc = f"{sc.get('chapter', '')} / {sc.get('section', '')}"
            similar_text += f"\n【Chunk#{sc['chunk_index']+1}（{loc}）】\n{sc['content'][:400]}\n"
        prompt = f"""你是文档质量审查专家，检查作业规程中是否存在重复段落。

【待检查段落】
{content[:600]}

【文档内相似段落】
{similar_text}

判断标准：
- 重复：两段文字高度相似（>60%文字相同）且表述同一项要求 → has_duplicates=true
- 不重复：内容相似但针对不同设备/位置/工序；前文总述后文细化；规范性引用 → false

输出JSON：{{
  "has_duplicates": true或false,
  "duplicates": [{{"chunk_ref": "Chunk#X（位置）", "note": "重复内容简述"}}],
  "summary": "一句话结论（如'与第X章重复'或'无重复'）"
}}"""
        try:
            resp = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": "你是文档质量审查专家，只有文字高度相似且内容相同才判为重复。严格按JSON格式输出。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0
            )
            m = re.search(r'\{[\s\S]*\}', resp.choices[0].message.content)
            return json.loads(m.group()) if m else default
        except Exception as e:
            return {**default, "summary": f"检查异常: {str(e)[:60]}"}

    # ============ 并行三任务调度 ============

    def review_chunk_all_tasks(self, pending_chunk: Dict, kb_results: List[Dict],
                               doc_bm25: Optional[BM25Okapi], doc_chunks: List[Dict],
                               query_idx: int) -> Tuple[Dict, List[Dict], Dict, Dict]:
        """并行执行三项检查：合规审查（三智能体）、错别字检查、重复性检查。"""
        similar_chunks = (
            self.search_doc_similar(pending_chunk, query_idx, doc_bm25, doc_chunks)
            if doc_bm25 is not None else []
        )
        with ThreadPoolExecutor(max_workers=3) as exe:
            f_compliance = exe.submit(self.review_chunk_complete, pending_chunk, kb_results)
            f_typo = exe.submit(self.check_typos, pending_chunk)
            f_rep = exe.submit(self.check_repetition, pending_chunk, similar_chunks)
            compliance_result, classifications = f_compliance.result()
            typo_result = f_typo.result()
            rep_result = f_rep.result()
        return compliance_result, classifications, typo_result, rep_result

    # ============ 审核待审文档 ============

    def review_pending_document(self, pending_json_path: str = None,
                                doc_filter: List[str] = None) -> Dict:
        if pending_json_path is None:
            for version in ['v6', 'v5', 'v4', 'v3', 'v2']:
                files = sorted(PENDING_CHUNKS_DIR.glob(f"pending_doc_chunks_{version}_*.json"), reverse=True)
                if files:
                    pending_json_path = files[0]
                    break
            if pending_json_path is None:
                raise FileNotFoundError("未找到待审文档chunks文件")

        print(f"\n加载待审文档: {pending_json_path}")
        with open(pending_json_path, 'r', encoding='utf-8') as f:
            pending_data = json.load(f)

        if doc_filter is None:
            doc_filter = DOC_FILTER
        if doc_filter:
            pending_data = {
                k: v for k, v in pending_data.items()
                if any(f in k for f in doc_filter)
            }
            print(f"  过滤后文档数: {len(pending_data)} (过滤条件: {doc_filter})")

        # ---- 断点续跑：加载已有增量结果 ----
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        incremental_path = OUTPUT_DIR / "review_result_v6_incremental.json"
        all_results: Dict = {}
        if incremental_path.exists():
            try:
                with open(incremental_path, 'r', encoding='utf-8') as f:
                    all_results = json.load(f)
                print(f"  [续跑] 已加载增量结果，跳过已完成文档: {list(all_results.keys())}")
            except Exception:
                all_results = {}

        total_chunks = sum(len(c) for c in pending_data.values())
        processed = sum(
            len(all_results[k]) for k in all_results if k in pending_data
        )
        doc_timings = {}
        total_start = time.time()
        api_fatal = False  # 账户欠费等致命错误标志

        for doc_name, chunks in pending_data.items():
            if doc_name in all_results:
                print(f"\n跳过（已有结果）: {doc_name}")
                continue
            if api_fatal:
                print(f"\n[跳过] API不可用，暂停后续文档: {doc_name}")
                continue

            doc_start = time.time()
            print(f"\n审核文档: {doc_name} ({len(chunks)} chunks)")
            doc_results = []

            # 为本文档构建BM25自检索索引（重复性检查用）
            doc_bm25 = self.build_doc_bm25(chunks) if len(chunks) > 1 else None

            for i, chunk in enumerate(chunks):
                if api_fatal:
                    break
                processed += 1
                print(f"  [{processed}/{total_chunks}] chunk {i + 1}/{len(chunks)}...", end=" ", flush=True)

                try:
                    query = chunk['content']
                    kb_results = self.search_relevant(query, k=TOP_K, threshold=SIMILARITY_THRESHOLD)
                except Exception as e:
                    err_str = str(e)
                    if 'Arrearage' in err_str or 'overdue' in err_str.lower() or 'Access denied' in err_str:
                        print(f"\n  [致命] API欠费，保存已有结果后退出: {err_str[:80]}")
                        api_fatal = True
                        break
                    print(f"检索失败({err_str[:60]})，跳过")
                    kb_results = []

                if not kb_results:
                    print("未找到相关法规，执行错别字/重复性检查...", end=" ", flush=True)
                    try:
                        typo_result = self.check_typos(chunk)
                        similar = self.search_doc_similar(chunk, i, doc_bm25, chunks) if doc_bm25 else []
                        rep_result = self.check_repetition(chunk, similar)
                    except Exception as e:
                        err_str = str(e)
                        if 'Arrearage' in err_str or 'Access denied' in err_str:
                            print(f"\n  [致命] API欠费，保存已有结果后退出")
                            api_fatal = True
                            break
                        typo_result = {"has_issues": False, "issues": [], "summary": "检查异常"}
                        rep_result = {"has_duplicates": False, "duplicates": [], "summary": "检查异常"}
                    print()
                    doc_results.append({
                        'chunk_index': i,
                        'chunk_content': chunk['content'],
                        'chunk_meta': {
                            'chapter': chunk.get('chapter', ''),
                            'section': chunk.get('section', ''),
                            'page_range': chunk.get('page_range', '')
                        },
                        'kb_matches': 0,
                        'kb_refs': [],
                        'kb_classifications': [],
                        'review_result': {"compliance_status": "不确定", "summary": "未找到相关法规", "issues": []},
                        'typo_result': typo_result,
                        'repetition_result': rep_result,
                    })
                    continue

                print(f"找到 {len(kb_results)} 条相关法规，并行三任务...", end=" ", flush=True)
                try:
                    review_result, classifications, typo_result, rep_result = \
                        self.review_chunk_all_tasks(chunk, kb_results, doc_bm25, chunks, i)
                except Exception as e:
                    err_str = str(e)
                    if 'Arrearage' in err_str or 'Access denied' in err_str:
                        print(f"\n  [致命] API欠费，保存已有结果后退出")
                        api_fatal = True
                        break
                    print(f"LLM调用失败({err_str[:60]})，标记不确定")
                    review_result = {"compliance_status": "不确定", "summary": f"调用失败: {err_str[:60]}", "issues": []}
                    classifications = []
                    typo_result = {"has_issues": False, "issues": [], "summary": "检查异常"}
                    rep_result = {"has_duplicates": False, "duplicates": [], "summary": "检查异常"}
                print()

                doc_results.append({
                    'chunk_index': i,
                    'chunk_content': chunk['content'],
                    'chunk_meta': {
                        'chapter': chunk.get('chapter', ''),
                        'section': chunk.get('section', ''),
                        'page_range': chunk.get('page_range', '')
                    },
                    'kb_matches': len(kb_results),
                    'kb_refs': [
                        {
                            'doc': r['chunk']['doc_name'],
                            'chapter': r['chunk'].get('chapter', ''),
                            'section': r['chunk'].get('section', ''),
                            'score': round(r['score'], 4),
                            'chunk_id': r['chunk']['id'],
                            'content': r['chunk']['content'],
                        }
                        for r in kb_results
                    ],
                    'kb_classifications': classifications,
                    'review_result': review_result,
                    'typo_result': typo_result,
                    'repetition_result': rep_result,
                })

            doc_elapsed = time.time() - doc_start
            doc_timings[doc_name] = doc_elapsed

            # 每完成一个文档立即保存增量结果
            if doc_results:
                all_results[doc_name] = doc_results
                self._save_incremental(all_results, incremental_path)
                print(f"  文档审核完成，耗时 {doc_elapsed:.1f}s  [已增量保存]")
            else:
                print(f"  文档无结果（可能因API中断），耗时 {doc_elapsed:.1f}s")

        total_elapsed = time.time() - total_start
        print(f"\n全部处理完毕，总耗时 {total_elapsed:.1f}s")
        for name, t in doc_timings.items():
            print(f"  {name}: {t:.1f}s")
        if api_fatal:
            print("  ⚠️  因API欠费提前终止，请充值后重新运行（将自动续跑）")

        return all_results

    def _save_incremental(self, results: Dict, path):
        """将结果保存到增量文件（不含KB全文）。"""
        slim = {}
        for doc_name, doc_results in results.items():
            slim[doc_name] = []
            for r in doc_results:
                r_slim = dict(r)
                r_slim['kb_refs'] = [
                    {k: v for k, v in ref.items() if k != 'content'}
                    for ref in r.get('kb_refs', [])
                ]
                slim[doc_name].append(r_slim)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)

    # ============ 保存与报告 ============

    def save_results(self, results: Dict, output_path: str = None):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_result_v6_{timestamp}.json"
        # 保存时不含kb全文（减小体积）
        slim = {}
        for doc_name, doc_results in results.items():
            slim[doc_name] = []
            for r in doc_results:
                r_slim = dict(r)
                r_slim['kb_refs'] = [
                    {k: v for k, v in ref.items() if k != 'content'}
                    for ref in r.get('kb_refs', [])
                ]
                slim[doc_name].append(r_slim)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        print(f"\n审核结果已保存: {output_path}")
        return output_path

    def generate_report(self, results: Dict, output_path: str = None):
        """生成HTML审核报告 v6（合规审查 + 错别字 + 重复性，并行三任务）"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_report_v6_{timestamp}.html"

        # ---- 统计 ----
        total_chunks = 0
        status_counts = {'合规': 0, '不合规': 0, '不确定': 0}
        verified_count = 0
        corrected_count = 0
        typo_chunk_count = 0
        typo_issue_count = 0
        rep_chunk_count = 0

        for doc_results in results.values():
            for r in doc_results:
                total_chunks += 1
                review = r.get('review_result', {})
                status = review.get('compliance_status', '不确定')
                if '合规' in status and '不' not in status:
                    status_counts['合规'] += 1
                elif '不合规' in status:
                    status_counts['不合规'] += 1
                else:
                    status_counts['不确定'] += 1
                if review.get('verified'):
                    verified_count += 1
                    if len(review.get('issues', [])) < review.get('draft_issue_count', 0):
                        corrected_count += 1
                typo = r.get('typo_result', {})
                if typo.get('has_issues'):
                    typo_chunk_count += 1
                    typo_issue_count += len(typo.get('issues', []))
                if r.get('repetition_result', {}).get('has_duplicates'):
                    rep_chunk_count += 1

        # ---- 分类颜色映射 ----
        clf_colors = {
            '支持': '#27ae60',
            '反对': '#e74c3c',
            '例外': '#e67e22',
            '无关': '#95a5a6',
        }

        # ---- 构建KB弹窗数据 ----
        # 收集所有KB chunk的全文，用于JS弹窗
        kb_full_texts: Dict[str, str] = {}
        for doc_results in results.values():
            for r in doc_results:
                for ref in r.get('kb_refs', []):
                    cid = ref.get('chunk_id', '')
                    if cid and cid not in kb_full_texts:
                        kb_full_texts[cid] = ref.get('content', '')

        # 转义HTML
        def esc(s: str) -> str:
            return (s.replace('&', '&amp;').replace('<', '&lt;')
                    .replace('>', '&gt;').replace('"', '&quot;')
                    .replace("'", '&#39;').replace('\n', '<br>'))

        # ---- CSS & JS ----
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
.typo-list, .rep-list { margin-top: 6px; display: flex; flex-direction: column; gap: 4px; }
.typo-item { background: #fef9e7; border: 1px solid #f9ca24; border-radius: 4px;
  padding: 7px 10px; font-size: 0.85em; }
.typo-wrong { font-weight: bold; color: #c0392b; }
.typo-fix { color: #27ae60; }
.typo-ctx { color: #7f8c8d; font-size: 0.82em; }
.rep-item { background: #eafaf1; border: 1px solid #82e0aa; border-radius: 4px;
  padding: 7px 10px; font-size: 0.85em; }
.rep-ref { font-weight: bold; color: #1a5276; }
.rep-note { color: #555; }
.chunk-meta { color: #7f8c8d; font-size: 0.82em; padding: 6px 16px 0 16px; }
.chunk-body { padding: 12px 16px; max-height: 320px; overflow-y: auto; }
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
/* Modal */
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

        # ---- 构建HTML ----
        parts = [
            "<!DOCTYPE html>",
            "<html lang='zh-CN'><head>",
            "<meta charset='utf-8'>",
            "<meta name='viewport' content='width=device-width, initial-scale=1'>",
            "<title>煤矿作业规程合规审核报告 v6</title>",
            f"<style>{style}</style>",
            "</head><body>",
            # 头部
            "<div class='page-header'>",
            "<h1>煤矿作业规程合规审核报告 v6</h1>",
            f"<div class='meta'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K} | "
            f"五智能体并行模式（分类+初审+验证 ‖ 错别字 ‖ 重复性）</div>",
            "</div>",
            # 主体
            "<div class='main'>",
            # 概览卡片
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
            f"<div class='stat-card' style='background:linear-gradient(135deg,#d35400,#e67e22)'>"
            f"<div class='stat-value'>{typo_chunk_count}</div><div class='stat-label'>含错别字chunks</div></div>",
            f"<div class='stat-card' style='background:linear-gradient(135deg,#16a085,#1abc9c)'>"
            f"<div class='stat-value'>{rep_chunk_count}</div><div class='stat-label'>含重复内容chunks</div></div>",
            "</div></div>",
        ]

        # ---- 各文档 ----
        for doc_name, doc_results in results.items():
            parts.append(f"<div class='doc-header'>📄 {esc(doc_name)}</div>")

            for r in doc_results:
                review = r.get('review_result', {})
                status = review.get('compliance_status', '不确定')
                is_verified = review.get('verified', False)
                draft_count = review.get('draft_issue_count', 0)
                final_count = len(review.get('issues', []))
                was_corrected = is_verified and final_count < draft_count
                typo = r.get('typo_result', {})
                rep = r.get('repetition_result', {})

                if '合规' in status and '不' not in status:
                    badge_cls = 'badge-compliant'
                elif '不合规' in status:
                    badge_cls = 'badge-noncompliant'
                else:
                    badge_cls = 'badge-uncertain'

                meta = r.get('chunk_meta', {})
                chunk_idx = r['chunk_index']

                # 卡片
                parts.append("<div class='chunk-card'>")

                # 卡片头
                parts.append("<div class='chunk-card-header'>")
                parts.append(f"<span class='chunk-num'>Chunk #{chunk_idx + 1}</span>")
                if meta.get('section'):
                    parts.append(f"<span class='chunk-level-tag'>section</span>")
                elif meta.get('chapter'):
                    parts.append(f"<span class='chunk-level-tag'>chapter</span>")
                parts.append(f"<span class='status-badge {badge_cls}'>{esc(status)}</span>")
                if is_verified:
                    parts.append("<span class='status-badge badge-verified'>已二次验证</span>")
                if was_corrected:
                    parts.append(f"<span class='status-badge badge-corrected'>误判纠正 {draft_count}→{final_count}项</span>")
                if typo.get('has_issues'):
                    n = len(typo.get('issues', []))
                    parts.append(f"<span class='status-badge badge-typo'>错别字 {n}处</span>")
                if rep.get('has_duplicates'):
                    parts.append("<span class='status-badge badge-rep'>重复内容</span>")
                parts.append("</div>")

                # 元信息
                parts.append(
                    f"<div class='chunk-meta'>"
                    f"章: {esc(meta.get('chapter', ''))} | "
                    f"节: {esc(meta.get('section', ''))} | "
                    f"页码: {esc(meta.get('page_range', ''))}"
                    f"</div>"
                )

                # 卡片体（带滚动）
                parts.append("<div class='chunk-body'>")

                # 待审内容
                parts.append("<div class='section-title'>待审内容</div>")
                parts.append(f"<div class='content-box'>{esc(r.get('chunk_content', ''))}</div>")

                # KB参考
                kb_refs = r.get('kb_refs', [])
                kb_clf = {c['index'] - 1: c for c in r.get('kb_classifications', [])}
                if kb_refs:
                    parts.append("<div class='section-title'>参考法规</div>")
                    parts.append("<div class='kb-refs-list'>")
                    for j, ref in enumerate(kb_refs):
                        clf_info = kb_clf.get(j, {})
                        clf_label = clf_info.get('classification', '无关')
                        clf_color = clf_colors.get(clf_label, '#95a5a6')
                        clf_reason = esc(clf_info.get('reason', ''))
                        chunk_id = ref.get('chunk_id', '')
                        title_str = esc(f"{ref.get('doc', '')} / {ref.get('chapter', '')} / {ref.get('section', '')}")

                        parts.append("<div class='kb-ref-item'>")
                        parts.append(
                            f"<span class='clf-badge' style='background:{clf_color}' "
                            f"title='{clf_reason}'>{esc(clf_label)}</span>"
                        )
                        parts.append("<div class='kb-ref-info'>")
                        parts.append(f"<div class='kb-ref-title'>📚 {title_str}</div>")
                        parts.append(
                            f"<div class='kb-ref-sub'>相似度: {ref.get('score', 0)}"
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

                # 问题列表
                issues = review.get('issues', [])
                if issues:
                    parts.append("<div class='section-title'>发现问题</div>")
                    parts.append("<div class='issues-list'>")
                    for issue in issues:
                        parts.append("<div class='issue-item'>")
                        parts.append(f"<div class='issue-type'>[{esc(issue.get('type', ''))}]</div>")
                        parts.append(f"<div class='issue-desc'>{esc(issue.get('description', ''))}</div>")
                        if issue.get('pending_content'):
                            parts.append(f"<div class='issue-quote'>待审: {esc(issue['pending_content'])}</div>")
                        if issue.get('regulation_content'):
                            parts.append(f"<div class='issue-quote'>法规: {esc(issue['regulation_content'])}</div>")
                        if issue.get('suggestion'):
                            parts.append(f"<div class='issue-suggestion'>建议: {esc(issue['suggestion'])}</div>")
                        parts.append("</div>")
                    parts.append("</div>")

                # 验证说明
                vn = review.get('verification_notes', '')
                if vn and vn != '初审结果准确，无误判':
                    parts.append(f"<div class='verify-note'>🔍 <strong>验证说明:</strong> {esc(vn)}</div>")

                # 审核意见
                if review.get('summary'):
                    parts.append(f"<div class='summary-text'>📝 {esc(review['summary'])}</div>")

                # 错别字结果
                if typo.get('has_issues') and typo.get('issues'):
                    parts.append("<div class='section-title'>✏️ 错别字检查</div>")
                    parts.append("<div class='typo-list'>")
                    for ti in typo['issues']:
                        parts.append("<div class='typo-item'>")
                        parts.append(
                            f"<span class='typo-wrong'>「{esc(ti.get('original', ''))}」</span>"
                            f" → <span class='typo-fix'>「{esc(ti.get('suggestion', ''))}」</span>"
                        )
                        if ti.get('context'):
                            parts.append(f"<div class='typo-ctx'>上下文: …{esc(ti['context'])}…</div>")
                        parts.append("</div>")
                    parts.append("</div>")
                elif typo:
                    parts.append(f"<div class='summary-text' style='color:#7f8c8d;'>✏️ {esc(typo.get('summary', ''))}</div>")

                # 重复性结果
                if rep.get('has_duplicates') and rep.get('duplicates'):
                    parts.append("<div class='section-title'>🔁 重复性检查</div>")
                    parts.append("<div class='rep-list'>")
                    for ri in rep['duplicates']:
                        parts.append("<div class='rep-item'>")
                        parts.append(f"<span class='rep-ref'>{esc(ri.get('chunk_ref', ''))}</span>")
                        if ri.get('note'):
                            parts.append(f"<span class='rep-note'>：{esc(ri['note'])}</span>")
                        parts.append("</div>")
                    parts.append("</div>")

                parts.append("</div>")  # chunk-body
                parts.append("</div>")  # chunk-card

        parts.append("</div>")  # main

        # ---- Modal ----
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

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(parts))

        print(f"审核报告已生成: {output_path}")
        return output_path


def main():
    print("=" * 70)
    print("煤矿作业规程合规审核系统 v6 - 五智能体并行（合规+错别字+重复性）")
    print(f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K}")
    print(f"文档过滤: {DOC_FILTER if DOC_FILTER else '全部'}")
    print(f"二次验证: {'开启' if VERIFY_NON_COMPLIANT else '关闭'}")
    print("=" * 70)

    reviewer = HybridRAGReviewerV6()
    reviewer.load_knowledge_base()
    reviewer.load_or_build_index()
    results = reviewer.review_pending_document()

    # 只有全部文档都完成才保存正式结果并清理增量文件
    incremental_path = OUTPUT_DIR / "review_result_v6_incremental.json"
    all_done = all(k in results for k in results)  # 总是True，但下面判断文档数
    pending_json_files = sorted(
        (OUTPUT_DIR.parent / "chunks_visualization").glob("pending_doc_chunks_v*_*.json"), reverse=True
    )
    if pending_json_files:
        import json as _json
        with open(pending_json_files[0], 'r', encoding='utf-8') as _f:
            _pending = _json.load(_f)
        all_done = all(k in results for k in _pending)

    if all_done:
        reviewer.save_results(results)
        reviewer.generate_report(results)
        if incremental_path.exists():
            incremental_path.unlink()
            print("  增量文件已清理")
    else:
        # 部分完成：只生成已有结果的报告，增量文件保留供续跑
        reviewer.generate_report(results)
        print(f"  ⚠️  部分完成（{len(results)}/待审文档数），增量文件已保留，充值后重新运行续跑")

    total = non_compliant = uncertain = verified = corrected = 0
    typo_chunks = typo_issues = rep_chunks = 0
    for doc_results in results.values():
        for r in doc_results:
            total += 1
            review = r.get('review_result', {})
            s = review.get('compliance_status', '')
            if '不合规' in s:
                non_compliant += 1
            elif '不确定' in s:
                uncertain += 1
            if review.get('verified'):
                verified += 1
                if len(review.get('issues', [])) < review.get('draft_issue_count', 0):
                    corrected += 1
            typo = r.get('typo_result', {})
            if typo.get('has_issues'):
                typo_chunks += 1
                typo_issues += len(typo.get('issues', []))
            if r.get('repetition_result', {}).get('has_duplicates'):
                rep_chunks += 1

    print("\n" + "=" * 70)
    print("[OK] 审核完成！")
    print(f"  总计审核: {total} 个chunks")
    print(f"  合规审查 → 不合规: {non_compliant} | 不确定: {uncertain} | 经验证: {verified} | 纠正误判: {corrected}")
    print(f"  错别字检查 → 含问题chunks: {typo_chunks} | 总计错误: {typo_issues} 处")
    print(f"  重复性检查 → 含重复chunks: {rep_chunks}")
    print("=" * 70)


if __name__ == "__main__":
    main()
