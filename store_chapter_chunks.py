"""
将章节chunk存储到独立的向量库
- 不覆盖现有向量库
- 使用新的collection: chapter_based_kb
- 记录到独立的数据库表
"""
import sys
import asyncio
from pathlib import Path
from typing import List, Dict
import json
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent / "backend"))

from app.core.config import settings
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from langchain.schema import Document


class ChapterBasedVectorStore:
    """章节向量库管理器"""

    def __init__(self, collection_name: str = "chapter_based_kb"):
        self.collection_name = collection_name
        self.embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )

        # 使用独立的持久化目录
        self.persist_directory = settings.CHROMA_DB_DIR / "chapter_based"
        self.persist_directory.mkdir(parents=True, exist_ok=True)

        self.vector_store = Chroma(
            collection_name=collection_name,
            embedding_function=self.embeddings,
            persist_directory=str(self.persist_directory)
        )

    def add_chunks(self, doc_name: str, chunks: List[Dict]) -> int:
        """添加文档的chunks到向量库"""
        documents = []

        for i, chunk in enumerate(chunks):
            # 构建元数据
            metadata = {
                'doc_name': doc_name,
                'chunk_id': i,
                'chapter': chunk.get('chapter', ''),
                'section': chunk.get('section', ''),
                'page_range': chunk.get('page_range', ''),
                'chunk_type': chunk.get('chunk_type', 'content'),
                'char_count': len(chunk.get('content', '')),
                'timestamp': datetime.now().isoformat()
            }

            # 创建Document对象
            doc = Document(
                page_content=chunk.get('content', ''),
                metadata=metadata
            )
            documents.append(doc)

        # 批量添加到向量库
        if documents:
            self.vector_store.add_documents(documents)

        return len(documents)

    def search(self, query: str, k: int = 5, filter_dict: Dict = None) -> List[Document]:
        """检索相关内容"""
        if filter_dict:
            results = self.vector_store.similarity_search(query, k=k, filter=filter_dict)
        else:
            results = self.vector_store.similarity_search(query, k=k)
        return results

    def get_stats(self) -> Dict:
        """获取向量库统计信息"""
        collection = self.vector_store._collection
        count = collection.count()

        return {
            'collection_name': self.collection_name,
            'total_chunks': count,
            'persist_directory': str(self.persist_directory)
        }


async def main():
    print("=" * 70)
    print("章节Chunk向量库存储工具")
    print("=" * 70)

    # 1. 加载章节分块结果
    chunks_dir = Path("chunks_visualization")
    json_files = sorted(chunks_dir.glob("chunks_*.json"))

    if not json_files:
        print("\n错误: 未找到chunks JSON文件")
        print("请先运行 chapter_based_chunking.py 生成chunks")
        return

    latest_chunks_file = json_files[-1]
    print(f"\n加载chunks数据: {latest_chunks_file.name}")

    with open(latest_chunks_file, 'r', encoding='utf-8') as f:
        all_chunks = json.load(f)

    print(f"  - 文档数: {len(all_chunks)}")
    print(f"  - 总chunks: {sum(len(chunks) for chunks in all_chunks.values())}")

    # 2. 初始化向量库
    print("\n初始化章节向量库...")
    vector_store = ChapterBasedVectorStore()
    print(f"  - Collection: {vector_store.collection_name}")
    print(f"  - 存储路径: {vector_store.persist_directory}")

    # 3. 逐个文档入库
    print("\n开始入库...")
    total_added = 0

    for doc_name, chunks in all_chunks.items():
        print(f"\n  处理: {doc_name}")
        print(f"    chunks数: {len(chunks)}")

        try:
            added = vector_store.add_chunks(doc_name, chunks)
            total_added += added
            print(f"    [OK] 已入库 {added} 个chunks")
        except Exception as e:
            print(f"    [ERROR] 入库失败: {e}")
            import traceback
            traceback.print_exc()

    # 4. 统计信息
    print("\n" + "=" * 70)
    stats = vector_store.get_stats()
    print("入库完成！")
    print(f"  - Collection: {stats['collection_name']}")
    print(f"  - 总chunks: {stats['total_chunks']}")
    print(f"  - 存储位置: {stats['persist_directory']}")

    # 5. 快速验证检索
    print("\n验证检索功能...")
    test_queries = ["煤矿顶板", "瓦斯突出", "锚杆支护"]

    for query in test_queries:
        results = vector_store.search(query, k=2)
        print(f"\n  查询[{query}] => 找到 {len(results)} 条")
        if results:
            first = results[0]
            print(f"    文档: {first.metadata.get('doc_name')}")
            print(f"    章节: {first.metadata.get('chapter')} {first.metadata.get('section')}")
            print(f"    内容: {first.page_content[:80]}...")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
