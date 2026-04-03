import os
import shutil
from typing import List
from langchain_community.document_loaders import PyPDFLoader, TextLoader, Docx2txtLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma
from app.core.config import settings

class VectorStoreService:
    def __init__(self):
        self.embeddings = OpenAIEmbeddings(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL
        )
        self.persist_directory = str(settings.CHROMA_DB_DIR)
        self._collections = {}  # 缓存已创建的collection
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=1000,
            chunk_overlap=200
        )

    def _get_collection(self, kb_id: int):
        """获取或创建指定知识库的collection"""
        collection_name = f"kb_{kb_id}"
        if collection_name not in self._collections:
            self._collections[collection_name] = Chroma(
                persist_directory=self.persist_directory,
                embedding_function=self.embeddings,
                collection_name=collection_name
            )
        return self._collections[collection_name]

    async def process_file(self, file, filename: str, doc_id: int, kb_id: int) -> str:
        """处理文件并存储到向量数据库

        Args:
            file: 上传的文件对象
            filename: 保存的文件名（原始文件名或带后缀的）
            doc_id: 数据库中的文档ID，用于向量数据库标识
            kb_id: 知识库ID，用于确定存储到哪个collection
        """
        # 保存文件到上传目录
        file_path = settings.UPLOAD_DIR / filename
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 加载文档
        ext = filename.split('.')[-1].lower()
        if ext == 'pdf':
            loader = PyPDFLoader(str(file_path))
        elif ext == 'txt':
            loader = TextLoader(str(file_path), encoding='utf-8')
        elif ext in ['docx', 'doc']:
            loader = Docx2txtLoader(str(file_path))
        else:
            return "Unsupported file format"

        documents = loader.load()
        splits = self.text_splitter.split_documents(documents)

        # 添加元数据 - 使用文档ID作为标识
        for split in splits:
            split.metadata['doc_id'] = doc_id  # 使用文档ID标识
            split.metadata['kb_id'] = kb_id   # 添加知识库ID
            split.metadata['filename'] = filename  # 保存实际文件名
            split.metadata['upload_time'] = str(os.path.getmtime(file_path))

        # 获取该知识库的collection并存入向量数据库
        vector_store = self._get_collection(kb_id)
        vector_store.add_documents(splits)
        return "Success"
        
    def search(self, query: str, kb_id: int, k: int = 4):
        """在指定知识库中搜索相关文档"""
        vector_store = self._get_collection(kb_id)
        return vector_store.similarity_search(query, k=k)

    def get_stats(self, kb_id: int = None):
        """获取向量数据库统计信息

        Args:
            kb_id: 知识库ID，如果为None则返回所有知识库的统计
        """
        if kb_id is not None:
            collection_name = f"kb_{kb_id}"
            return {
                "kb_id": kb_id,
                "collection_name": collection_name
            }
        else:
            return {
                "collections": list(self._collections.keys())
            }

    def get_document_chunks(self, doc_id: int, kb_id: int, limit: int = 20):
        """获取文档的所有分块内容

        Args:
            doc_id: 数据库中的文档ID
            kb_id: 知识库ID
            limit: 返回的最大分块数量

        Returns:
            tuple: (chunks列表, 总分块数)
        """
        # 获取该知识库的collection
        vector_store = self._get_collection(kb_id)

        # 先查询总数
        all_results = vector_store.get(
            where={"doc_id": doc_id}
        )
        total_count = len(all_results['ids']) if all_results and all_results.get('ids') else 0

        # 再查询限制数量的分块
        results = vector_store.get(
            where={"doc_id": doc_id},
            limit=limit
        )

        if not results or not results.get('documents'):
            return [], 0

        # 返回分块内容
        chunks = []
        for i, (doc, metadata) in enumerate(zip(results['documents'], results['metadatas'])):
            chunks.append({
                'id': i + 1,
                'content': doc,
                'metadata': metadata,
                'page': metadata.get('page', i + 1)
            })

        return chunks, total_count

    def delete_document(self, doc_id: int, kb_id: int) -> int:
        """从向量数据库中删除指定文档的所有分块

        Args:
            doc_id: 数据库中的文档ID
            kb_id: 知识库ID

        Returns:
            删除的分块数量
        """
        # 获取该知识库的collection
        vector_store = self._get_collection(kb_id)

        # 首先获取该文档的所有向量ID
        results = vector_store.get(
            where={"doc_id": doc_id}
        )

        if not results or not results.get('ids'):
            return 0

        # 删除所有相关的向量数据
        ids_to_delete = results['ids']
        vector_store.delete(ids=ids_to_delete)

        return len(ids_to_delete)

    def delete_knowledge_base(self, kb_id: int) -> bool:
        """删除整个知识库的collection

        Args:
            kb_id: 知识库ID

        Returns:
            是否删除成功
        """
        try:
            collection_name = f"kb_{kb_id}"

            # 1. 获取该collection的所有segment UUIDs（用于删除物理文件夹）
            import chromadb
            import sqlite3
            import time
            import gc

            db_path = os.path.join(self.persist_directory, "chroma.sqlite3")

            # 先查询segment UUIDs
            segment_uuids = []
            try:
                # 创建一个新的临时client，避免使用缓存的对象
                temp_client = chromadb.PersistentClient(path=self.persist_directory)
                collection = temp_client.get_collection(name=collection_name)
                collection_uuid = collection.id

                # 从sqlite3查询该collection的所有segment UUIDs
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM segments WHERE collection = ?", (str(collection_uuid),))
                segment_uuids = [row[0] for row in cursor.fetchall()]
                conn.close()

                print(f"找到 {len(segment_uuids)} 个segment需要删除")

                # 2. 从缓存中移除collection引用（重要！）
                if collection_name in self._collections:
                    del self._collections[collection_name]

                # 3. 删除ChromaDB中的collection（逻辑删除）
                temp_client.delete_collection(name=collection_name)

                # 4. 显式删除所有对象，释放文件句柄
                del collection
                del temp_client

            except Exception as e:
                print(f"获取segment信息或删除collection时出错: {str(e)}")

            # 5. 清空所有缓存的collection对象（关键！）
            self._collections.clear()

            # 6. 强制垃圾回收
            gc.collect()

            # 7. 等待更长时间，确保文件句柄被释放
            time.sleep(1.0)

            # 8. 删除物理segment文件夹
            deleted_dirs = 0
            for segment_uuid in segment_uuids:
                segment_dir = os.path.join(self.persist_directory, segment_uuid)
                if os.path.exists(segment_dir):
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            shutil.rmtree(segment_dir)
                            deleted_dirs += 1
                            print(f"已删除segment目录: {segment_uuid}")
                            break
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"删除segment目录 {segment_uuid} 失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                                gc.collect()
                                time.sleep(0.5)
                            else:
                                print(f"删除segment目录 {segment_uuid} 最终失败: {str(e)}")

            print(f"成功删除 {deleted_dirs} 个segment物理目录")
            return True
        except Exception as e:
            print(f"删除知识库collection失败: {str(e)}")
            return False

vector_store_service = VectorStoreService()

def cleanup_orphan_segments():
    """清理孤立的segment目录（在应用启动时调用）"""
    import chromadb
    import sqlite3

    persist_directory = str(settings.CHROMA_DB_DIR)

    try:
        # 1. 获取所有活跃的segment UUIDs
        db_path = os.path.join(persist_directory, "chroma.sqlite3")
        if not os.path.exists(db_path):
            print("ChromaDB数据库不存在，跳过清理")
            return

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM segments")
        active_segments = set(row[0] for row in cursor.fetchall())
        conn.close()

        print(f"找到 {len(active_segments)} 个活跃的segment")

        # 2. 扫描目录中的所有UUID文件夹
        all_dirs = [d for d in os.listdir(persist_directory)
                    if os.path.isdir(os.path.join(persist_directory, d)) and d not in ['.', '..']]

        # 3. 找出并删除孤立的目录
        deleted_count = 0
        for d in all_dirs:
            if d not in active_segments:
                dir_path = os.path.join(persist_directory, d)
                try:
                    shutil.rmtree(dir_path)
                    deleted_count += 1
                    print(f"已清理孤立segment目录: {d}")
                except Exception as e:
                    print(f"清理孤立segment目录 {d} 失败: {str(e)}")

        if deleted_count > 0:
            print(f"成功清理 {deleted_count} 个孤立segment目录")
        else:
            print("没有发现孤立的segment目录")

    except Exception as e:
        print(f"清理孤立segment时出错: {str(e)}")
