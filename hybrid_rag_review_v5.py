"""
混合检索RAG审核系统 v5
- v4改进：
  1. 检索立场分类智能体（分类结果仅用于前端展示，不传入审核智能体）
  2. 审核结果增加"不确定"状态（合规/不合规/不确定）
  3. 修复v3遗留误判：并列条款、固定数值、幻觉引用等
  4. 前端：每个chunk卡片固定高度+滚动条；KB参考可点击弹窗查看全文
  5. 前端：KB参考显示立场分类标签（支持/反对/例外/无关）
- v5改进：
  6. 修复误差/偏差/公差范围方向误判：法规"误差在A~B"表示偏差绝对值范围，
     与待审"±X"（X≤B）等价，不应判为违规
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


class HybridRAGReviewerV5:
    """混合检索RAG审核器 v5（三智能体：分类+初审+验证）"""

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

【审核流程——按步骤执行】

第一步：逐条阅读知识库中每条法规，提取其约束类型：
  - 禁止行为（严禁/不得/禁止）
  - 数值下限（不得低于/至少/≥）
  - 数值上限（不得超过/最多/≤）
  - 数值区间（A~B，同时含上限和下限）

第二步：判断待审内容是否涉及该约束，注意：
  - 行为判断：关注待审行为的实质，不受表述措辞影响，若本质相同则适用该条款
  - 禁止行为：待审内容**实施**了被禁止的行为 → 违规；待审内容**禁止**了该行为（写了"严禁/不得"）→ 合规，不得反向判违规
  - 数值下限：待审值 < 法规值 → 违规；待审值 ≥ 法规值 → 合规（加严也合规）
  - 数值上限：待审值 > 法规值 → 违规；待审值 ≤ 法规值 → 合规（加严也合规）
  - 数值区间：下限和上限分别判断，任一端违规即整体违规
  - 安全触发阈值（如气体浓度达到X时必须停工/断电/撤人）：本质是上限约束，阈值越低越严。
    待审阈值 < 法规阈值 → 更早触发 → 更严 → 合规，不得以"低于法规值"为由判违规
  - 误差/偏差/公差：若法规以"误差在A~B"或"偏差±X"形式规定，表示偏差绝对值范围，
    不区分正负方向。判断时只需待审偏差绝对值 ≤ 法规上限即合规，不得以"允许负偏差"为由判违规

第三步：确定结论
  - 知识库中无相关条款 → 合规，注明"知识库中无对应条款"
  - 知识库有条款但关键数值缺失/空白 → 不确定，注明"知识库数据不完整，无法核验"，issues为空
  - 待审内容违反某条款 → 不合规，issues记录该条款及冲突说明
  - issues为空 → compliance_status必须为"合规"；issues非空 → "不合规"
  - 证据不足以确定（双向解读均合理）→ "不确定"

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

请逐条检查，判断是否属于以下5类误判，如是则删除该问题：

误判类型1：严于法规被判违规（含禁止行为方向误判）
表现A：待审内容的数值要求比法规更严格，但被认定为违规。
识别方法：必须区分上限和下限的方向性——
  - 对于上限（不得超过X）：待审上限 < 法规上限 = 更严 = 误判；待审上限 > 法规上限 = 更宽松 = 真实违规，不能删除
  - 对于下限（不得低于X）：待审下限 > 法规下限 = 更严 = 误判；待审下限 < 法规下限 = 更宽松 = 真实违规，不能删除
  - 对于区间[A,B]：必须两端分别判断，不能因"包含法规区间"就认为合规
→ 仅当待审值确实在安全方向上比法规更严格时，删除该问题。
表现B：待审内容本身是在**禁止**某行为（写有"严禁/不得"），但被误判为"实施了该违规行为"。
识别方法：检查待审原文——若是用"严禁/不得"修饰该行为，则待审是在约束/禁止，而非实施。
→ 删除该问题（方向判反了）。
表现C：**安全触发阈值**（如气体浓度/温度达到X时必须停工、断电、撤人）。
法规设定阈值是上限，待审阈值更低意味着更早触发应急措施，是更严格的做法。
识别方法：确认法规是"达到X%必须采取措施"的触发型要求，且待审阈值 < 法规阈值。
→ 删除该问题（阈值更低 = 更严格 = 合规）。

误判类型2：引用了知识库外的条款，或将数据缺失误判为冲突
表现A：问题中引用的条款编号或具体数值，在上方知识库文本中完全没有出现。
表现B：问题类型被标为"数值冲突"，但法规一栏写的是"知识库数据不完整"——这说明根本没有比较依据，不构成冲突。
识别方法：在知识库原文中检索该条款/数值，确认不存在；或确认法规内容字段为空/不完整。
→ 删除该问题（依据不足），整体结果改为"不确定"。

误判类型3：并列要求被误读为"任择其一"
表现：待审内容中有两个并列条件，初审认为"可能只执行其一"或"表述歧义"导致判违规。
→ 删除该问题。

误判类型4：固定数值因"刚性"被判违规
表现：仅因为企业给出了固定数值（而非动态确定方法）就判违规，但该固定值本身在法规允许范围内。
识别方法：检查该固定值是否本身违反了法规的数值限制，若未违反则属误判。
→ 删除该问题。

误判类型5：等待后检查被判违反"先检查"原则
表现：待审内容规定"等待X时间后再检查再恢复操作"，初审认为违反"必须先检查"要求。
→ 删除该问题。

误判类型6：误差/偏差范围方向误判
表现：法规规定"误差/偏差在A~B mm"，待审以"±X mm"表示，初审认为"允许负偏差"违规。
识别方法：确认法规用词含"误差/偏差/公差"，且待审偏差绝对值 ≤ 法规上限B。
→ 删除该问题（误差范围表示偏差绝对值，不区分正负方向）。

【重要约束】
- 真实的数值冲突（待审数值确实宽于法规限制）：保留
- 真实的规则冲突（待审内容确实违反法规明文禁止事项）：保留
- 不确定是否误判：保留（宁可保守，不要误删真实问题）

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

    # ============ 审核待审文档 ============

    def review_pending_document(self, pending_json_path: str = None,
                                doc_filter: List[str] = None) -> Dict:
        if pending_json_path is None:
            for version in ['v5', 'v4', 'v3', 'v2']:
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

        all_results = {}
        total_chunks = sum(len(c) for c in pending_data.values())
        processed = 0
        doc_timings = {}
        total_start = time.time()

        for doc_name, chunks in pending_data.items():
            doc_start = time.time()
            print(f"\n审核文档: {doc_name} ({len(chunks)} chunks)")
            doc_results = []

            for i, chunk in enumerate(chunks):
                processed += 1
                chunk_start = time.time()
                print(f"  [{processed}/{total_chunks}] chunk {i + 1}/{len(chunks)}...", end=" ", flush=True)

                retrieval_start = time.time()
                query = chunk['content']
                kb_results = self.search_relevant(query, k=TOP_K, threshold=SIMILARITY_THRESHOLD)
                retrieval_elapsed = time.time() - retrieval_start

                if not kb_results:
                    chunk_elapsed = time.time() - chunk_start
                    print(f"未找到相关法规，耗时 {chunk_elapsed:.1f}s（检索 {retrieval_elapsed:.1f}s）")
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
                    })
                    continue

                print(f"找到 {len(kb_results)} 条相关法规，调用LLM...", end=" ", flush=True)
                review_result, classifications = self.review_chunk_complete(chunk, kb_results)
                chunk_elapsed = time.time() - chunk_start
                print(f"耗时 {chunk_elapsed:.1f}s（检索 {retrieval_elapsed:.1f}s）")

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
                })

            doc_elapsed = time.time() - doc_start
            doc_timings[doc_name] = doc_elapsed
            print(f"  文档审核完成，耗时 {doc_elapsed:.1f}s")
            all_results[doc_name] = doc_results

        total_elapsed = time.time() - total_start
        print(f"\n全部文档审核完成，总耗时 {total_elapsed:.1f}s")
        for name, t in doc_timings.items():
            print(f"  {name}: {t:.1f}s")

        return all_results

    # ============ 保存与报告 ============

    def save_results(self, results: Dict, output_path: str = None):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_result_v5_{timestamp}.json"
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
        """生成HTML审核报告 v4（立场分类 + 可点击KB弹窗 + 滚动卡片）"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_report_v5_{timestamp}.html"

        # ---- 统计 ----
        total_chunks = 0
        status_counts = {'合规': 0, '不合规': 0, '不确定': 0}
        verified_count = 0
        corrected_count = 0

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
            "<title>煤矿作业规程合规审核报告 v5</title>",
            f"<style>{style}</style>",
            "</head><body>",
            # 头部
            "<div class='page-header'>",
            "<h1>煤矿作业规程合规审核报告 v5</h1>",
            f"<div class='meta'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K} | "
            f"三智能体模式（分类+初审+验证）</div>",
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
    print("煤矿作业规程合规审核系统 v5 - 三智能体混合检索RAG")
    print(f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K}")
    print(f"文档过滤: {DOC_FILTER if DOC_FILTER else '全部'}")
    print(f"二次验证: {'开启' if VERIFY_NON_COMPLIANT else '关闭'}")
    print("=" * 70)

    reviewer = HybridRAGReviewerV5()
    reviewer.load_knowledge_base()
    reviewer.load_or_build_index()
    results = reviewer.review_pending_document()
    reviewer.save_results(results)
    reviewer.generate_report(results)

    total = non_compliant = uncertain = verified = corrected = 0
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

    print("\n" + "=" * 70)
    print("[OK] 审核完成！")
    print(f"  总计审核: {total} 个chunks")
    print(f"  不合规: {non_compliant} 个 | 不确定: {uncertain} 个")
    print(f"  经二次验证: {verified} 个 | 验证后纠正误判: {corrected} 个")
    print("=" * 70)


if __name__ == "__main__":
    main()
