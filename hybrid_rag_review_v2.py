"""
混合检索RAG审核系统 v2
- 知识库：chapter_based_chunking_v3.py 切分的法规文档
- 待审文档：pending_doc_chunking_v5.py 切分的待审文档
- 检索方式：混合检索（向量检索 + BM25）+ 父子检索
- 模型：Qwen
- v2改进：
  1. 优化提示词 - 聚焦数值判断和规则冲突，减少误报
  2. TOP_K 改为 5
  3. 支持指定审核特定文档（如只审核004）
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

# BM25 检索
from rank_bm25 import BM25Okapi
import jieba

# LLM
from openai import OpenAI


# ============ 配置 ============
# 使用 DashScope API
API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
dashscope.api_key = API_KEY

# Qwen 模型
QWEN_MODEL = "qwen-plus"
EMBEDDING_MODEL = "text-embedding-v3"

# 检索参数
SIMILARITY_THRESHOLD = 0.1
TOP_K = 5

# 只审核包含这些关键词的文档（为空则审核全部）
DOC_FILTER = ["004"]

# 路径
KB_CHUNKS_DIR = Path("chunks_visualization")
PENDING_CHUNKS_DIR = Path("chunks_visualization")
CHROMA_DB_DIR = Path("data/hybrid_rag_chroma")
OUTPUT_DIR = Path("review_results")


class HybridRAGReviewer:
    """混合检索RAG审核器"""

    def __init__(self):
        if not API_KEY:
            raise ValueError("请设置环境变量 DASHSCOPE_API_KEY")

        # 初始化 LLM (Qwen)
        self.llm_client = OpenAI(
            api_key=API_KEY,
            base_url=BASE_URL
        )

        # 知识库数据
        self.kb_chunks: List[Dict] = []
        self.kb_parent_map: Dict[str, Dict] = {}
        self.kb_texts: List[str] = []
        self.kb_tokenized: List[List[str]] = []

        # BM25 索引
        self.bm25: BM25Okapi = None

        # ChromaDB 客户端
        self.chroma_client = None
        self.collection = None

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """使用 DashScope 获取文本向量"""
        # DashScope 每次最多处理 10 个文本
        batch_size = 10
        all_embeddings = []

        for i in range(0, len(texts), batch_size):
            batch = texts[i:i+batch_size]
            # 过滤空文本
            batch = [t if t.strip() else " " for t in batch]

            resp = TextEmbedding.call(
                model=EMBEDDING_MODEL,
                input=batch
            )

            if resp.status_code == 200:
                for item in resp.output['embeddings']:
                    all_embeddings.append(item['embedding'])
            else:
                raise Exception(f"Embedding 失败: {resp.code} - {resp.message}")

        return all_embeddings

    def load_knowledge_base(self, kb_json_path: str = None):
        """加载知识库chunks"""
        if kb_json_path is None:
            v3_files = sorted(KB_CHUNKS_DIR.glob("chunks_v3_*.json"), reverse=True)
            if not v3_files:
                raise FileNotFoundError("未找到知识库chunks文件")
            kb_json_path = v3_files[0]

        print(f"加载知识库: {kb_json_path}")
        with open(kb_json_path, 'r', encoding='utf-8') as f:
            kb_data = json.load(f)

        chunk_id = 0
        for doc_name, chunks in kb_data.items():
            parent_chunk = None

            for chunk in chunks:
                chunk['id'] = f"kb_{chunk_id}"
                chunk['doc_name'] = doc_name
                chunk_id += 1

                level = chunk.get('chunk_level', '')
                if level in ['chapter', 'part']:
                    parent_chunk = chunk
                elif level in ['section', 'subsection', 'article']:
                    if parent_chunk:
                        self.kb_parent_map[chunk['id']] = parent_chunk

                self.kb_chunks.append(chunk)
                self.kb_texts.append(chunk['content'])

        print(f"  加载 {len(self.kb_chunks)} 个知识库chunks")
        print(f"  父子关系: {len(self.kb_parent_map)} 个子chunk有父chunk")

    def build_bm25_index(self):
        """构建BM25索引"""
        print("构建BM25索引...")
        self.kb_tokenized = [list(jieba.cut(text)) for text in self.kb_texts]
        self.bm25 = BM25Okapi(self.kb_tokenized)
        print(f"  BM25索引构建完成")

    def build_vector_index(self):
        """构建向量索引（使用 DashScope + ChromaDB）"""
        print("构建向量索引...")
        CHROMA_DB_DIR.mkdir(parents=True, exist_ok=True)

        # 初始化 ChromaDB
        self.chroma_client = chromadb.PersistentClient(path=str(CHROMA_DB_DIR))

        # 删除旧 collection（如果存在）
        try:
            self.chroma_client.delete_collection("kb_chunks")
        except:
            pass

        self.collection = self.chroma_client.create_collection(
            name="kb_chunks",
            metadata={"hnsw:space": "cosine"}
        )

        # 准备数据
        valid_chunks = [(i, c) for i, c in enumerate(self.kb_chunks) if c['content'].strip()]
        print(f"  准备 {len(valid_chunks)} 个有效文档...")

        # 批量处理（DashScope 限制每次10个）
        batch_size = 10
        for batch_start in range(0, len(valid_chunks), batch_size):
            batch = valid_chunks[batch_start:batch_start+batch_size]
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

            # 获取向量
            embeddings = self.get_embeddings(texts)

            # 添加到 ChromaDB
            self.collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=texts,
                metadatas=metadatas
            )

            time.sleep(0.5)  # 避免请求过快

        print(f"  向量索引构建完成，共 {len(valid_chunks)} 个文档")

    def load_or_build_index(self):
        """加载或构建索引"""
        # 检查是否已有向量数据库
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

        # BM25 每次都需要重建（内存索引）
        self.build_bm25_index()

    def vector_search(self, query: str, k: int = TOP_K) -> List[Tuple[Dict, float]]:
        """向量检索"""
        # 获取查询向量
        query_embedding = self.get_embeddings([query])[0]

        # ChromaDB 查询
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=k
        )

        matched = []
        if results and results['ids'] and results['ids'][0]:
            for i, chunk_id in enumerate(results['ids'][0]):
                # 找到对应的chunk
                for chunk in self.kb_chunks:
                    if chunk['id'] == chunk_id:
                        # ChromaDB 返回的距离，转换为相似度
                        distance = results['distances'][0][i] if results.get('distances') else 0
                        similarity = 1 - distance  # cosine 距离转相似度
                        matched.append((chunk, similarity))
                        break

        return matched

    def bm25_search(self, query: str, k: int = TOP_K) -> List[Tuple[Dict, float]]:
        """BM25关键词检索"""
        query_tokens = list(jieba.cut(query))
        scores = self.bm25.get_scores(query_tokens)

        # 获取top-k
        top_indices = np.argsort(scores)[::-1][:k]

        matched = []
        for idx in top_indices:
            if scores[idx] > 0:
                # 归一化分数到0-1
                normalized_score = scores[idx] / (scores[idx] + 1)
                matched.append((self.kb_chunks[idx], normalized_score))

        return matched

    def hybrid_search(self, query: str, k: int = TOP_K,
                     vector_weight: float = 0.5) -> List[Tuple[Dict, float]]:
        """混合检索：结合向量检索和BM25"""
        # 向量检索
        vector_results = self.vector_search(query, k=k*2)
        # BM25检索
        bm25_results = self.bm25_search(query, k=k*2)

        # 合并结果（RRF - Reciprocal Rank Fusion）
        rrf_scores = {}
        similarity_scores = {}  # 保存原始相似度
        chunk_map = {}

        # 向量检索结果
        for rank, (chunk, similarity) in enumerate(vector_results):
            chunk_id = chunk['id']
            chunk_map[chunk_id] = chunk
            rrf_score = vector_weight / (rank + 60)  # RRF公式
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + rrf_score
            # 保存向量相似度（用于阈值过滤）
            if chunk_id not in similarity_scores:
                similarity_scores[chunk_id] = similarity

        # BM25检索结果
        for rank, (chunk, score) in enumerate(bm25_results):
            chunk_id = chunk['id']
            chunk_map[chunk_id] = chunk
            rrf_score = (1 - vector_weight) / (rank + 60)
            rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0) + rrf_score

        # 排序并返回top-k
        sorted_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)[:k]

        results = []
        for chunk_id in sorted_ids:
            chunk = chunk_map[chunk_id]
            # 返回原始相似度分数（用于阈值判断）
            sim_score = similarity_scores.get(chunk_id, 0.5)
            results.append((chunk, sim_score))

        return results

    def get_parent_context(self, chunk: Dict) -> str:
        """获取父chunk的上下文（父子检索）"""
        chunk_id = chunk.get('id', '')
        if chunk_id in self.kb_parent_map:
            parent = self.kb_parent_map[chunk_id]
            return f"\n【上级章节】{parent.get('chapter', '')} {parent.get('section', '')}\n{parent['content'][:500]}..."
        return ""

    def search_with_parent(self, query: str, k: int = TOP_K,
                          threshold: float = SIMILARITY_THRESHOLD) -> List[Dict]:
        """带父子检索的混合检索"""
        results = self.hybrid_search(query, k=k)

        enriched_results = []
        for chunk, score in results:
            if score >= threshold:
                # 获取父上下文
                parent_context = self.get_parent_context(chunk)

                enriched_results.append({
                    'chunk': chunk,
                    'score': score,
                    'parent_context': parent_context,
                    'full_context': chunk['content'] + parent_context
                })

        return enriched_results

    def review_chunk_with_llm(self, pending_chunk: Dict,
                              kb_results: List[Dict]) -> Dict:
        """使用LLM审核单个待审chunk"""
        # 构建知识库上下文
        kb_context = ""
        for i, result in enumerate(kb_results, 1):
            chunk = result['chunk']
            kb_context += f"\n【参考法规{i}】来源: {chunk['doc_name']}\n"
            if chunk.get('chapter'):
                kb_context += f"章节: {chunk['chapter']}\n"
            if chunk.get('section'):
                kb_context += f"小节: {chunk['section']}\n"
            kb_context += f"内容: {chunk['content']}\n"
            if result.get('parent_context'):
                kb_context += f"上级章节参考: {result['parent_context'][:300]}...\n"
            kb_context += "-" * 50

        # 待审内容
        pending_content = pending_chunk['content']
        pending_meta = f"章: {pending_chunk.get('chapter', '')} | 节: {pending_chunk.get('section', '')}"

        prompt = f"""你是一位煤矿安全法规审核专家。请根据提供的法规知识库内容，审核以下待审文档内容。

【审核原则】
1. 只关注待审内容与法规的【直接冲突】，不要过度解读
2. 重点检查【数值参数】是否与法规一致（如距离、尺寸、数量、比例等）
3. 如果待审内容在法规中没有对应要求，直接判定为"合规"
4. 不要因为待审文档"缺少某些内容"就判定不合规，除非法规明确要求必须包含
5. 作业规程是具体操作文档，不需要包含法规的所有内容

【待审文档信息】
{pending_meta}

【待审内容】
{pending_content}

【参考法规知识库】
{kb_context}

【审核要点】
1. 数值冲突：待审内容中的数值是否违反法规要求？（如法规要求≥10m，待审写8m）
2. 规则冲突：待审内容是否与法规规定直接矛盾？（如法规禁止的操作，待审却允许）

请以JSON格式输出审核结果：
{{
    "compliance_status": "合规/不合规",
    "issues": [
        {{
            "type": "数值冲突/规则冲突",
            "pending_content": "待审文档中的具体内容",
            "regulation_content": "法规中的对应要求",
            "description": "问题说明",
            "suggestion": "修改建议"
        }}
    ],
    "summary": "简要审核结论（一句话）"
}}

注意：
- 如果没有发现问题，issues数组为空，compliance_status为"合规"
- 只有确实存在数值冲突或规则冲突时才判定"不合规"
- 不要因为"可能"、"建议"、"最好"等原因判定不合规
"""

        try:
            response = self.llm_client.chat.completions.create(
                model=QWEN_MODEL,
                messages=[
                    {"role": "system", "content": "你是煤矿安全法规审核专家。只关注数值冲突和规则冲突，不要过度解读。请严格按照JSON格式输出。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1
            )

            result_text = response.choices[0].message.content

            # 尝试解析JSON
            try:
                # 提取JSON部分
                json_match = re.search(r'\{[\s\S]*\}', result_text)
                if json_match:
                    result = json.loads(json_match.group())
                else:
                    result = {"raw_response": result_text, "parse_error": "无法提取JSON"}
            except json.JSONDecodeError:
                result = {"raw_response": result_text, "parse_error": "JSON解析失败"}

            return result

        except Exception as e:
            return {"error": str(e)}

    def review_pending_document(self, pending_json_path: str = None,
                                 doc_filter: List[str] = None) -> Dict:
        """审核待审文档

        Args:
            pending_json_path: 待审文档JSON路径
            doc_filter: 文档名过滤列表，只审核包含这些关键词的文档
        """
        if pending_json_path is None:
            # 找最新的 v5 pending chunks 文件（优先），否则找 v4, v3, v2
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

        # 应用文档过滤
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
                print(f"  [{processed}/{total_chunks}] 审核 chunk {i+1}/{len(chunks)}...", end=" ")

                # 混合检索相关法规
                query = chunk['content'][:500]  # 使用前500字符作为查询
                kb_results = self.search_with_parent(query, k=TOP_K, threshold=SIMILARITY_THRESHOLD)

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
                        'review_result': {"status": "无法判断", "reason": "未找到相关法规"}
                    })
                    continue

                print(f"找到 {len(kb_results)} 条相关法规，调用LLM...")

                # LLM审核
                review_result = self.review_chunk_with_llm(chunk, kb_results)

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
                        for r in kb_results[:3]  # 只保存前3个引用
                    ],
                    'review_result': review_result
                })

            all_results[doc_name] = doc_results

        return all_results

    def save_results(self, results: Dict, output_path: str = None):
        """保存审核结果"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_result_{timestamp}.json"

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        print(f"\n审核结果已保存: {output_path}")
        return output_path

    def generate_report(self, results: Dict, output_path: str = None):
        """生成HTML审核报告"""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        if output_path is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            output_path = OUTPUT_DIR / f"review_report_{timestamp}.html"

        html_parts = [
            "<!DOCTYPE html>",
            "<html><head>",
            "<meta charset='utf-8'>",
            "<title>煤矿作业规程合规审核报告</title>",
            "<style>",
            "body { font-family: 'Microsoft YaHei', Arial; margin: 20px; background: #f5f5f5; }",
            "h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }",
            "h2 { color: #34495e; background: #ecf0f1; padding: 10px; border-radius: 5px; }",
            ".summary { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }",
            ".chunk-review { background: white; padding: 15px; margin: 10px 0; border-radius: 5px; border-left: 4px solid #3498db; }",
            ".chunk-review.compliant { border-left-color: #27ae60; }",
            ".chunk-review.partial { border-left-color: #f39c12; }",
            ".chunk-review.non-compliant { border-left-color: #e74c3c; }",
            ".chunk-review.unknown { border-left-color: #95a5a6; }",
            ".status-badge { display: inline-block; padding: 4px 12px; border-radius: 4px; color: white; font-weight: bold; }",
            ".status-compliant { background: #27ae60; }",
            ".status-partial { background: #f39c12; }",
            ".status-non-compliant { background: #e74c3c; }",
            ".status-unknown { background: #95a5a6; }",
            ".issue { background: #fff5f5; padding: 10px; margin: 5px 0; border-radius: 4px; border: 1px solid #ffcccc; }",
            ".kb-ref { background: #f0f8ff; padding: 8px; margin: 5px 0; border-radius: 4px; font-size: 0.9em; }",
            ".meta { color: #7f8c8d; font-size: 0.9em; }",
            ".content-preview { background: #fafafa; padding: 10px; border-radius: 4px; white-space: pre-wrap; font-size: 0.9em; max-height: 200px; overflow-y: auto; }",
            "</style>",
            "</head><body>",
            "<h1>煤矿作业规程合规审核报告</h1>",
            f"<p class='meta'>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 检索阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K}</p>"
        ]

        # 统计
        total_chunks = 0
        status_counts = {'合规': 0, '不合规': 0, '无法判断': 0}

        for doc_name, doc_results in results.items():
            for r in doc_results:
                total_chunks += 1
                status = r.get('review_result', {}).get('compliance_status', '无法判断')
                # 简化状态映射
                if '合规' in status and '不' not in status:
                    status_counts['合规'] += 1
                elif '不合规' in status:
                    status_counts['不合规'] += 1
                else:
                    status_counts['无法判断'] += 1

        html_parts.append("<div class='summary'>")
        html_parts.append(f"<h2>审核概览</h2>")
        html_parts.append(f"<p>审核文档数: {len(results)} | 审核chunks数: {total_chunks}</p>")
        html_parts.append(f"<p>合规: <span class='status-badge status-compliant'>{status_counts['合规']}</span> ")
        html_parts.append(f"不合规: <span class='status-badge status-non-compliant'>{status_counts['不合规']}</span> ")
        html_parts.append(f"无法判断: <span class='status-badge status-unknown'>{status_counts['无法判断']}</span></p>")
        html_parts.append("</div>")

        # 每个文档的详细结果
        for doc_name, doc_results in results.items():
            html_parts.append(f"<h2>📄 {doc_name}</h2>")

            for r in doc_results:
                status = r.get('review_result', {}).get('compliance_status', '无法判断')
                # 简化状态映射
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
                html_parts.append(f"<p><strong>Chunk #{r['chunk_index']+1}</strong> ")
                html_parts.append(f"<span class='status-badge {status_badge_class}'>{status}</span></p>")

                meta = r.get('chunk_meta', {})
                html_parts.append(f"<p class='meta'>章: {meta.get('chapter', '')} | 节: {meta.get('section', '')} | 页码: {meta.get('page_range', '')}</p>")

                html_parts.append(f"<div class='content-preview'>{r.get('chunk_content', '')}</div>")

                # 知识库引用
                if r.get('kb_refs'):
                    html_parts.append("<p><strong>参考法规:</strong></p>")
                    for ref in r['kb_refs']:
                        html_parts.append(f"<div class='kb-ref'>📚 {ref['doc']} - {ref['chapter']} (相似度: {ref['score']})</div>")

                # 问题列表
                review = r.get('review_result', {})
                issues = review.get('issues', [])
                if issues:
                    html_parts.append("<p><strong>发现问题:</strong></p>")
                    for issue in issues:
                        html_parts.append(f"<div class='issue'>")
                        html_parts.append(f"<p><strong>[{issue.get('type', '')}]</strong> {issue.get('description', '')}</p>")
                        if issue.get('reference'):
                            html_parts.append(f"<p class='meta'>参考: {issue.get('reference', '')}</p>")
                        if issue.get('suggestion'):
                            html_parts.append(f"<p>建议: {issue.get('suggestion', '')}</p>")
                        html_parts.append("</div>")

                # 总结
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
    print("煤矿作业规程合规审核系统 v2 - 混合检索RAG")
    print(f"相似度阈值: {SIMILARITY_THRESHOLD} | TopK: {TOP_K}")
    print(f"文档过滤: {DOC_FILTER if DOC_FILTER else '全部'}")
    print("=" * 70)

    reviewer = HybridRAGReviewer()

    # 1. 加载知识库
    reviewer.load_knowledge_base()

    # 2. 构建索引
    reviewer.load_or_build_index()

    # 3. 审核待审文档
    results = reviewer.review_pending_document()

    # 4. 保存结果
    reviewer.save_results(results)

    # 5. 生成报告
    reviewer.generate_report(results)

    print("\n" + "=" * 70)
    print("[OK] 审核完成！")
    print("=" * 70)


if __name__ == "__main__":
    main()
