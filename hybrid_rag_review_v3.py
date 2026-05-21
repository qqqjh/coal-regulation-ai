"""
混合检索RAG审核系统 v3
- 知识库：chapter_based_chunking_v4.py 产生的法规文档（含表格内容，无纯标题chunk）
- 待审文档：pending_doc_chunking_v5.py 产生的待审文档
- 搜索方式：混合搜索（向量搜索 + BM25）+ 父子搜索
- 模型：Qwen
- v3改进：
  1. 初审提示词重构（7条核心原则，针对v2所有误判类型）
  2. 双智能体架构：初审（Draft）+ 验证（Verify）
     - 对初审判定为"不合规"的结果进行二次验证
     - 专门过滤5类常见误判
  3. 处理知识库表格数据缺失时不做推测比较
  4. TOP_K 保持 5
  5. 支持指定审核特定文档
"""
import json
import os
import re
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple
from datetime import datetime
import numpy as np

# DashScope
import dashscope
from dashscope import TextEmbedding

# 向量数据库
import chromadb
from chromadb.config import Settings

# BM25 搜索
from rank_bm25 import BM25Okapi
import jieba

# LLM
from openai import OpenAI


# ============ 配置 ============
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
dashscope.api_key = API_KEY

QWEN_MODEL = "qwen-plus"
EMBEDDING_MODEL = "text-embedding-v3"

SIMILARITY_THRESHOLD = 0.1
TOP_K = 5

# 是否对初审"不合规"结果进行二次验证（双智能体）
VERIFY_NON_COMPLIANT = True

# 只审核包含这些关键词的文档（为空则审核全部）
DOC_FILTER = ["004"]

KB_CHUNKS_DIR = Path("chunks_visualization")
PENDING_CHUNKS_DIR = Path("chunks_visualization")
CHROMA_DB_DIR = Path("data/hybrid_rag_chroma")
OUTPUT_DIR = Path("review_results")


class HybridRAGReviewerV3:
    """混合检索RAG审核器 v3（双智能体）"""

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

    # ============ 向量 & 搜索（与v2相同）============

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """使用 DashScope 获取文本向量"""
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
        """加载知识库chunks"""
        if kb_json_path is None:
            v4_files = sorted(KB_CHUNKS_DIR.glob("chunks_v4_*.json"), reverse=True)
            if not v4_files:
                raise FileNotFoundError("未找到知识库chunks文件（需先运行 chapter_based_chunking_v4.py）")
            kb_json_path = v4_files[0]

        print(f"加载知识库: {kb_json_path}")
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
        print("  BM25索引构建完成")

    def build_vector_index(self):
        print("构建向量索引...")
        CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)
        self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
        try:
            self.chroma_client.delete_collection("kb_chunks")
        except:
            pass
        self.collection = self.chroma_client.create_collection(
            name="kb_chunks",
            metadata={"hnsw:space": "cosine"}
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
        if (CHROMA_DB_DIR / "chroma.sqlite3").exists():
            print("加载已有向量索引...")
            self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))
            try:
                self.collection = self.chroma_client.get_collection("kb_chunks")
                count = self.collection.count()
                if count > 0:
                    print(f"  已加载 {count} 个向量")
                else:
                    self.build_vector_index()
            except:
                self.build_vector_index()
        else:
            self.build_vector_index()
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
                        similarity = 1 - distance
                        matched.append((chunk, similarity))
                        break
        return matched

    def bm25_search(self, query: str, k: int = TOP_K) -> List[Tuple[Dict, float]]:
        query_tokens = list(jieba.cut(query))
        scores = self.bm25.get_scores(query_tokens)
        top_indices = np.argsort(scores)[::-1][:k]
        matched = []
        for idx in top_indices:
            if scores[idx] > 0:
                normalized_score = scores[idx] / (scores[idx] + 1)
                matched.append((self.kb_chunks[idx], normalized_score))
        return matched

    def hybrid_search(self, query: str, k: int = TOP_K,
                      vector_weight: float = 0.5) -> List[Tuple[Dict, float]]:
        vector_results = self.vector_search(query, k=k * 2)
        bm25_results = self.bm25_search(query, k=k * 2)
        rrf_scores = {}
        similarity_scores = {}
        chunk_map = {}
        for rank, (chunk, similarity) in enumerate(vector_results):
            chunk_id = chunk['id']
            chunk_map[chunk_id] = chunk
            rrf_score = vector_weight / (rank + 60)
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + rrf_score
            if chunk_id not in similarity_scores:
                similarity_scores[chunk_id] = similarity
        for rank, (chunk, score) in enumerate(bm25_results):
            chunk_id = chunk['id']
            chunk_map[chunk_id] = chunk
            rrf_score = (1 - vector_weight) / (rank + 60)
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + rrf_score
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)[:k]
        results = []
        for chunk_id in sorted_ids:
            chunk = chunk_map[chunk_id]
            sim_score = similarity_scores.get(chunk_id, 0.5)
            results.append((chunk, sim_score))
        return results

    def search_relevant(self, query: str, k: int = TOP_K,
                        threshold: float = SIMILARITY_THRESHOLD) -> List[Dict]:
        """混合搜索，返回超过相似度阈值的结果（v4起不再做父子检索）"""
        results = self.hybrid_search(query, k=k)
        return [
            {'chunk': chunk, 'score': score}
            for chunk, score in results
            if score >= threshold
        ]

    # ============ 核心：知识库上下文构建 ============

    def _build_kb_context(self, kb_results: List[Dict]) -> str:
        """构建知识库上下文字符串（供提示词使用）"""
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

    # ============ 初审智能体（Draft Agent）============

    def review_chunk_with_llm(self, pending_chunk: Dict, kb_results: List[Dict]) -> Dict:
        """
        初审智能体：使用重构后的提示词进行初步合规审核。
        7条核心原则专门针对v2的误判问题。
        """
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk['content']
        pending_meta = f"章: {pending_chunk.get('chapter', '')} | 节: {pending_chunk.get('section', '')}"

        prompt = f"""你是一位煤矿安全法规审核专家。请根据下方提供的法规知识库内容，审核待审文档内容是否违反法规要求。

【核心审核原则——必须严格遵守，违反将导致误判】

原则一：严于法规 = 合规（最重要）
如果待审内容比法规要求更严格（例如：设定的危险阈值更小、安全距离更短、检测频率更高、分类更细），这是企业自主加严管理，必须判定为合规。
只有当待审内容比法规更宽松（危险阈值更高、安全距离更短、频率更低）时才可能违规。
错误示例：法规要求≥6m，待审要求≥15m → 这是加严，应判合规，不能因"无法规依据"判违规。

原则二：只使用知识库中明确出现的内容
你只能根据下方"参考法规知识库"中明确提供的文字和数值进行判断。
严禁：引用知识库中未出现的法规条款编号、数值或要求。
严禁：用自己记忆中的法规内容补充知识库的空白。
如果知识库中某条款的数值部分显示为空（如"不得小于 ，"、"不超过 ，"），说明表格数据未被提取，请注明"知识库数据不完整"并跳过该项比较，不得推测填充数值。
如果需要某条款判断但知识库中没有，直接判为合规并注明"知识库中无对应条款"。

原则三：并列条款 = 两者都执行（AND关系）
当待审内容中两个条件并列出现时（如"条件A和条件B"、"情形1...情形2..."），理解为两者都必须满足，而非任择其一。
不要因为"两个并列条件可能导致现场只执行其一"而判违规——这是执行层面的问题，不是法规冲突。

原则四：固定数值是合理的操作规程
企业在作业规程中给出固定具体数值（如"探20m采10m"、"每50m布置一个"、"每5分钟检查一次"），是将法规要求转化为可操作的现场指令，不应因"过于刚性"或"未体现动态调整"而判违规。
只有当该固定值本身超出法规允许范围（如固定值大于法规上限、小于法规下限）时才判违规。

原则五：等待后再检查不违反"先检查"原则
若待审内容规定"先等待X时间/完成某准备步骤，再进行检查，然后恢复操作"，这不违反"必须先检查后操作"的要求。
等待或准备是检查前的工序，检查仍在恢复操作之前进行，时序正确。

原则六：结论必须与最终分析严格一致
在分析过程中，如果你逐步得出某个疑似问题"实际上不构成违规"的结论，就不要把它写入issues列表。
issues列表只包含你最终确认存在的违规项。
compliance_status必须与issues列表严格对应：issues为空 → 必须为"合规"；issues有内容 → "不合规"。

原则七：缺少内容不等于违规
除非法规明确规定作业规程"必须包含"该内容，否则待审文档未写某项内容不判违规。

【待审文档信息】
{pending_meta}

【待审内容】
{pending_content}

【参考法规知识库】
{kb_context}

【审核要点】
1. 数值冲突：待审内容中的数值是否超出法规允许范围（注意：比法规更严不是冲突）
2. 规则冲突：待审内容是否与法规规定直接矛盾（注意：要有明确的冲突依据，不是推测）

请以JSON格式输出审核结果：
{{
    "compliance_status": "合规/不合规",
    "issues": [
        {{
            "type": "数值冲突/规则冲突",
            "pending_content": "待审文档中的具体内容（原文引用）",
            "regulation_content": "知识库中对应的法规要求（必须是知识库原文，不得自行补充）",
            "description": "冲突说明（简洁，不超过100字）",
            "suggestion": "修改建议"
        }}
    ],
    "summary": "一句话审核结论"
}}

注意：没有发现问题则issues为空数组[]，compliance_status为"合规"。
"""

        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system",
                     "content": "你是煤矿安全法规审核专家。只关注数值冲突和规则冲突，严于法规的要求视为合规。严格按照JSON格式输出，不引用知识库以外的内容。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )
            result_text = response.choices[0].message.content
            try:
                json_match = re.search(r'\{[\s\S]*\}', result_text)
                if json_match:
                    return json.loads(json_match.group())
                else:
                    return {"raw_response": result_text, "parse_error": "无法提取JSON"}
            except json.JSONDecodeError:
                return {"raw_response": result_text, "parse_error": "JSON解析失败"}
        except Exception as e:
            return {"error": str(e)}

    # ============ 验证智能体（Verify Agent）============

    def verify_review_result(self, pending_chunk: Dict, kb_results: List[Dict],
                             initial_result: Dict) -> Dict:
        """
        验证智能体：对初审"不合规"结果进行二次核查，专门过滤5类常见误判。
        只在初审有issues时调用，目的是减少误报，不会引入新的违规项。
        """
        kb_context = self._build_kb_context(kb_results)
        pending_content = pending_chunk['content']
        initial_issues_text = json.dumps(
            initial_result.get('issues', []),
            ensure_ascii=False, indent=2
        )

        prompt = f"""你是一位资深合规审核质量控制专家。你的任务是检查一份初步审核结果，识别并纠正其中的误判。
你只能删除误判项，不能新增问题，不能修改正确的违规判断。

【待审内容】
{pending_content}

【参考法规知识库（这是唯一允许引用的依据）】
{kb_context}

【初步审核发现的问题（需要逐条核查）】
{initial_issues_text}

请逐条检查上述每个问题，判断是否属于以下5类误判，如是则删除该问题：

误判类型1：严于法规被判违规
表现：待审内容的要求实际上比知识库中的法规更严格（阈值更小、距离更短、频率更高），
      但被认定为违规或"无法规依据"。
识别方法：比较待审数值和法规数值的宽严方向，若待审更严则判误。
→ 处理：删除该问题。

误判类型2：引用了知识库外的条款
表现：问题中引用的法规条款编号或具体数值，在上方知识库文本中完全没有出现。
识别方法：在知识库原文中检索该条款/数值，确认不存在。
→ 处理：删除该问题（依据不足）。

误判类型3：并列要求被误读为"任择其一"
表现：待审内容中有两个并列条件，初审认为"可能只执行其一"或"表述歧义"导致判违规。
识别方法：并列条件在操作规程中通常表示都要执行，这是表述风格问题，不是违规。
→ 处理：删除该问题。

误判类型4：固定数值因"刚性"被判违规
表现：仅因为企业给出了固定数值（而非动态确定方法）就判违规，但该固定值本身在法规
      允许范围内（未超出上限或下限）。
识别方法：检查该固定值是否本身违反了法规的数值限制，若未违反则属误判。
→ 处理：删除该问题。

误判类型5：等待后检查被判违反"先检查"原则
表现：待审内容规定"等待X时间后再检查再恢复操作"，初审认为违反"必须先检查"要求。
识别方法：确认检查动作仍在恢复操作之前，等待只是检查前的准备步骤。
→ 处理：删除该问题。

【重要约束】
- 对于真实的数值冲突（待审数值确实宽于法规限制）：保留
- 对于真实的规则冲突（待审内容确实违反法规明文禁止事项）：保留
- 如不确定是否误判：保留（宁可保守，不要误删真实问题）

请输出修正后的完整审核结果：
{{
    "compliance_status": "合规/不合规",
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
            try:
                json_match = re.search(r'\{[\s\S]*\}', result_text)
                if json_match:
                    verified = json.loads(json_match.group())
                    # 确保compliance_status与issues一致
                    if not verified.get('issues'):
                        verified['compliance_status'] = '合规'
                    return verified
                else:
                    return initial_result
            except json.JSONDecodeError:
                return initial_result
        except Exception as e:
            print(f"    验证失败: {e}，使用初审结果")
            return initial_result

    # ============ 完整双智能体审核流程 ============

    def review_chunk_complete(self, pending_chunk: Dict, kb_results: List[Dict]) -> Dict:
        """
        完整审核流程：初审 → （如有问题）验证
        返回最终审核结果，包含是否经过验证的标记。
        """
        # Stage 1: 初审
        draft = self.review_chunk_with_llm(pending_chunk, kb_results)

        # 如果初审出错或没有issues，直接返回
        if draft.get('error') or draft.get('parse_error'):
            draft['verified'] = False
            return draft

        has_issues = bool(draft.get('issues'))
        is_non_compliant = '不合规' in draft.get('compliance_status', '')

        # Stage 2: 只对"不合规"且有issues的结果进行验证
        if VERIFY_NON_COMPLIANT and is_non_compliant and has_issues:
            print("（二次验证）", end=" ", flush=True)
            verified = self.verify_review_result(pending_chunk, kb_results, draft)
            verified['verified'] = True
            verified['draft_issue_count'] = len(draft.get('issues', []))
            return verified

        draft['verified'] = False
        return draft

    # ============ 审核待审文档 ============

    def review_pending_document(self, pending_json_path: str = None,
                                doc_filter: List[str] = None) -> Dict:
        """审核待审文档"""
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
            filtered_data = {}
            for doc_name, chunks in pending_data.items():
                if any(f in doc_name for f in doc_filter):
                    filtered_data[doc_name] = chunks
            pending_data = filtered_data
            print(f"  过滤后文档数: {len(pending_data)} (过滤条件: {doc_filter})")

        all_results = {}
        total_chunks = sum(len(chunks) for chunks in pending_data.values())
        processed = 0

        for doc_name, chunks in pending_data.items():
            print(f"\n审核文档: {doc_name} ({len(chunks)} chunks)")
            doc_results = []

            for i, chunk in enumerate(chunks):
                processed += 1
                print(f"  [{processed}/{total_chunks}] chunk {i + 1}/{len(chunks)}...", end=" ", flush=True)

                query = chunk['content']
                kb_results = self.search_relevant(query, k=TOP_K, threshold=SIMILARITY_THRESHOLD)

                if not kb_results:
                    print("未找到相关法规")
                    doc_results.append({
                        'chunk_index': i,
                        'chunk_content': chunk['content'][:200] + "...",
                        'chunk_meta': {
                            'chapter': chunk.get('chapter', ''),
                            'section': chunk.get('section', ''),
                            'page_range': chunk.get('page_range', '')
                        },
                        'kb_matches': 0,
                        'review_result': {"status": "无法判断", "reason": "未找到相关法规"},
                        'verified': False
                    })
                    continue

                print(f"找到 {len(kb_results)} 条相关法规，调用LLM...", end=" ", flush=True)

                review_result = self.review_chunk_complete(chunk, kb_results)
                print()  # 换行

                doc_results.append({
                    'chunk_index': i,
                    'chunk_content': chunk['content'][:200] + "...",
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
                            'score': round(r['score'], 4)
                        }
                        for r in kb_results[:3]
                    ],
                    'review_result': review_result,
                    'verified': review_result.get('verified', False)
                })

            all_results[doc_name] = doc_results

        return all_results

    # ============ 保存与报告 ============

    def save_results(self, results: Dict, output_path: str = None):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_result_v3_{timestamp}.json"
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n审核结果已保存: {output_path}")
        return output_path

    def generate_report(self, results: Dict, output_path: str = None):
        """生成HTML审核报告（v3版，含验证状态标记）"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_report_v3_{timestamp}.html"

        html_parts = [
            "<!DOCTYPE html>",
            "<html><head>",
            "<meta charset='utf-8'>",
            "<title>煤矿作业规程合规审核报告 v3</title>",
            "<style>",
            "body { font-family: 'Microsoft YaHei', Arial; margin: 20px; background: #f5f5f5; }",
            "h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }",
            "h2 { color: #34495e; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
            ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
            ".chunk-review { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #3498db; }",
            ".chunk-review.compliant { border-left-color: #27ae60; }",
            ".chunk-review.non-compliant { border-left-color: #e74c3c; }",
            ".chunk-review.unknown { border-left-color: #95a5a6; }",
            ".status-badge { display: inline-block; padding: 4px 12px; border-radius: 4px; color: white; font-weight: bold; font-size: 0.9em; }",
            ".status-compliant { background: #27ae60; }",
            ".status-non-compliant { background: #e74c3c; }",
            ".status-unknown { background: #95a5a6; }",
            ".verified-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; background: #8e44ad; color: white; font-size: 0.75em; margin-left: 6px; vertical-align: middle; }",
            ".corrected-badge { display: inline-block; padding: 2px 8px; border-radius: 3px; background: #e67e22; color: white; font-size: 0.75em; margin-left: 6px; vertical-align: middle; }",
            ".issue { background: #fff5f5; padding: 10px; margin: 5px 0; border-radius: 4px; border: 1px solid #ffcccc; }",
            ".verification-note { background: #f0f0ff; padding: 8px; margin: 5px 0; border-radius: 4px; border-left: 3px solid #8e44ad; font-size: 0.9em; color: #555; }",
            ".kb-ref { background: #f0f8ff; padding: 8px; margin: 5px 0; border-radius: 4px; font-size: 0.9em; }",
            ".meta { color: #7f8c8d; font-size: 0.9em; }",
            ".content-preview { background: #fafafa; padding: 10px; border-radius: 4px; white-space: pre-wrap; font-size: 0.9em; max-height: 200px; overflow-y: auto; }",
            ".stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin: 15px 0; }",
            ".stat-card { background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 15px; border-radius: 8px; text-align: center; }",
            ".stat-value { font-size: 1.8em; font-weight: bold; }",
            ".stat-label { font-size: 0.85em; opacity: 0.9; margin-top: 4px; }",
            "</style>",
            "</head><body>",
            "<h1>煤矿作业规程合规审核报告 v3</h1>",
            f"<p class='meta'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K} | "
            f"双智能体验证: {'开启' if VERIFY_NON_COMPLIANT else '关闭'}</p>"
        ]

        # 统计
        total_chunks = 0
        status_counts = {'合规': 0, '不合规': 0, '无法判断': 0}
        verified_count = 0
        corrected_count = 0

        for doc_name, doc_results in results.items():
            for r in doc_results:
                total_chunks += 1
                review = r.get('review_result', {})
                status = review.get('compliance_status', '无法判断')
                if '合规' in status and '不' not in status:
                    status_counts['合规'] += 1
                elif '不合规' in status:
                    status_counts['不合规'] += 1
                else:
                    status_counts['无法判断'] += 1

                if review.get('verified'):
                    verified_count += 1
                    draft_count = review.get('draft_issue_count', 0)
                    final_count = len(review.get('issues', []))
                    if final_count < draft_count:
                        corrected_count += 1

        html_parts.append("<div class='summary'>")
        html_parts.append("<h2>审核概览</h2>")
        html_parts.append("<div class='stats-grid'>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{len(results)}</div><div class='stat-label'>审核文档数</div></div>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{total_chunks}</div><div class='stat-label'>审核chunks数</div></div>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{status_counts['合规']}</div><div class='stat-label'>合规</div></div>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{status_counts['不合规']}</div><div class='stat-label'>不合规</div></div>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{verified_count}</div><div class='stat-label'>经二次验证</div></div>")
        html_parts.append(f"<div class='stat-card'><div class='stat-value'>{corrected_count}</div><div class='stat-label'>验证后纠正误判</div></div>")
        html_parts.append("</div>")
        html_parts.append("</div>")

        for doc_name, doc_results in results.items():
            html_parts.append(f"<h2>📄 {doc_name}</h2>")

            for r in doc_results:
                review = r.get('review_result', {})
                status = review.get('compliance_status', '无法判断')
                is_verified = review.get('verified', False)
                draft_count = review.get('draft_issue_count', 0)
                final_count = len(review.get('issues', []))
                was_corrected = is_verified and final_count < draft_count

                if '合规' in status and '不' not in status:
                    status_class = 'compliant'
                    status_badge_class = 'status-compliant'
                elif '不合规' in status:
                    status_class = 'non-compliant'
                    status_badge_class = 'status-non-compliant'
                else:
                    status_class = 'unknown'
                    status_badge_class = 'status-unknown'

                html_parts.append(f"<div class='chunk-review {status_class}'>")
                html_parts.append(f"<p><strong>Chunk #{r['chunk_index'] + 1}</strong> "
                                   f"<span class='status-badge {status_badge_class}'>{status}</span>")
                if is_verified:
                    html_parts.append("<span class='verified-badge'>已二次验证</span>")
                if was_corrected:
                    html_parts.append(f"<span class='corrected-badge'>误判纠正 ({draft_count}→{final_count}项)</span>")
                html_parts.append("</p>")

                meta = r.get('chunk_meta', {})
                html_parts.append(f"<p class='meta'>章: {meta.get('chapter', '')} | 节: {meta.get('section', '')} | 页码: {meta.get('page_range', '')}</p>")
                html_parts.append(f"<div class='content-preview'>{r.get('chunk_content', '')}</div>")

                if r.get('kb_refs'):
                    html_parts.append("<p><strong>参考法规:</strong></p>")
                    for ref in r['kb_refs']:
                        html_parts.append(f"<div class='kb-ref'>📚 {ref['doc']} - {ref['chapter']} (相似度: {ref['score']})</div>")

                issues = review.get('issues', [])
                if issues:
                    html_parts.append("<p><strong>发现问题:</strong></p>")
                    for issue in issues:
                        html_parts.append("<div class='issue'>")
                        html_parts.append(f"<p><strong>[{issue.get('type', '')}]</strong> {issue.get('description', '')}</p>")
                        if issue.get('pending_content'):
                            html_parts.append(f"<p class='meta'>待审内容: {issue.get('pending_content', '')}</p>")
                        if issue.get('regulation_content'):
                            html_parts.append(f"<p class='meta'>法规要求: {issue.get('regulation_content', '')}</p>")
                        if issue.get('suggestion'):
                            html_parts.append(f"<p>建议: {issue.get('suggestion', '')}</p>")
                        html_parts.append("</div>")

                # 验证注释
                if review.get('verification_notes') and review['verification_notes'] != '初审结果准确，无误判':
                    html_parts.append(f"<div class='verification-note'>🔍 <strong>验证说明:</strong> {review['verification_notes']}</div>")

                if review.get('summary'):
                    html_parts.append(f"<p><strong>审核意见:</strong> {review['summary']}</p>")

                html_parts.append("</div>")

        html_parts.append("</body></html>")

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(html_parts))

        print(f"审核报告已生成: {output_path}")
        return output_path


def main():
    print("=" * 70)
    print("煤矿作业规程合规审核系统 v3 - 双智能体混合检索RAG")
    print(f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K}")
    print(f"文档过滤: {DOC_FILTER if DOC_FILTER else '全部'}")
    print(f"二次验证: {'开启' if VERIFY_NON_COMPLIANT else '关闭'}")
    print("=" * 70)

    reviewer = HybridRAGReviewerV3()

    reviewer.load_knowledge_base()
    reviewer.load_or_build_index()
    results = reviewer.review_pending_document()
    reviewer.save_results(results)
    reviewer.generate_report(results)

    # 统计
    total = non_compliant = verified = corrected = 0
    for doc_results in results.values():
        for r in doc_results:
            total += 1
            review = r.get('review_result', {})
            if '不合规' in review.get('compliance_status', ''):
                non_compliant += 1
            if review.get('verified'):
                verified += 1
                if len(review.get('issues', [])) < review.get('draft_issue_count', 0):
                    corrected += 1

    print("\n" + "=" * 70)
    print("[OK] 审核完成！")
    print(f"  总计审核: {total} 个chunks")
    print(f"  不合规: {non_compliant} 个")
    print(f"  经二次验证: {verified} 个")
    print(f"  验证后纠正误判: {corrected} 个")
    print("=" * 70)


if __name__ == "__main__":
    main()
