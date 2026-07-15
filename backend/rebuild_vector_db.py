"""
重建向量数据库
1. 删除旧的 chroma_db
2. 清空 documents 表的旧记录
3. 将 data/uploads/ 下的 4 个 PDF 重新入库（全部归入 kb_id=2）
"""
import asyncio
import sys
import shutil
import sqlite3
import os
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from app.core.config import settings
from app.services.vector_store import VectorStoreService

# 全部入库到知识库 ID=2（煤矿安全规程）
KB_ID = 2

PDF_FILES = [
    "《防治煤与瓦斯突出细则》.pdf",
    "GB∕T 35056-2018 煤矿巷道锚杆支护技术规范.pdf",
    "煤矿安全规程.pdf",
    "山西省煤矿顶板安全管理规定.pdf",
]


def delete_chroma_db():
    chroma_dir = settings.CHROMA_DB_DIR
    if chroma_dir.exists():
        shutil.rmtree(chroma_dir)
        print(f"✓ 已删除旧向量库: {chroma_dir}")
    chroma_dir.mkdir(parents=True)
    print(f"✓ 已创建空向量库目录: {chroma_dir}")


def reset_documents_table():
    db_path = str(settings.DATA_DIR / "app.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM documents")
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    print(f"✓ 已清空 documents 表（删除 {deleted} 条旧记录）")


def insert_document_record(name: str, file_path: str, file_size: int) -> int:
    """插入文档记录，返回新 doc_id"""
    db_path = str(settings.DATA_DIR / "app.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(
        """INSERT INTO documents (kb_id, name, file_path, file_type, file_size, status, chunk_count, upload_time)
           VALUES (?, ?, ?, 'pdf', ?, 'processing', 0, ?)""",
        (KB_ID, name, file_path, file_size, now)
    )
    doc_id = cur.lastrowid
    conn.commit()
    conn.close()
    return doc_id


def update_document_status(doc_id: int, chunk_count: int):
    db_path = str(settings.DATA_DIR / "app.db")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    now = datetime.now().isoformat()
    cur.execute(
        "UPDATE documents SET status='indexed', chunk_count=?, indexed_time=? WHERE id=?",
        (chunk_count, now, doc_id)
    )
    conn.commit()
    conn.close()


async def load_pdf(service: VectorStoreService, pdf_path: Path, doc_id: int):
    """将单个 PDF 载入向量库"""
    from langchain_community.document_loaders import PyPDFLoader
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    loader = PyPDFLoader(str(pdf_path))
    documents = loader.load()

    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    splits = splitter.split_documents(documents)

    for split in splits:
        split.metadata['doc_id'] = doc_id
        split.metadata['kb_id'] = KB_ID
        split.metadata['filename'] = pdf_path.name
        split.metadata['upload_time'] = str(os.path.getmtime(pdf_path))

    vector_store = service._get_collection(KB_ID)
    vector_store.add_documents(splits)

    return len(splits)


async def main():
    print("=" * 60)
    print("煤矿法规向量数据库重建工具")
    print("=" * 60)

    # 1. 删除旧向量库
    print("\n[1/3] 删除旧向量库...")
    delete_chroma_db()

    # 2. 清空旧文档记录
    print("\n[2/3] 清空旧文档记录...")
    reset_documents_table()

    # 3. 逐个入库
    print(f"\n[3/3] 开始入库（目标知识库 kb_id={KB_ID}）...")
    service = VectorStoreService()
    total_chunks = 0

    for filename in PDF_FILES:
        pdf_path = settings.UPLOAD_DIR / filename
        if not pdf_path.exists():
            print(f"  ✗ 文件不存在，跳过: {filename}")
            continue

        file_size = pdf_path.stat().st_size
        print(f"\n  处理: {filename}  ({file_size / 1024:.0f} KB)")

        doc_id = insert_document_record(filename, str(pdf_path), file_size)
        print(f"    doc_id = {doc_id}")

        try:
            chunk_count = await load_pdf(service, pdf_path, doc_id)
            update_document_status(doc_id, chunk_count)
            total_chunks += chunk_count
            print(f"    ✓ 入库完成，共 {chunk_count} 个分块")
        except Exception as e:
            print(f"    ✗ 入库失败: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"重建完成！共入库 {total_chunks} 个文本分块")
    print(f"向量库位置: {settings.CHROMA_DB_DIR}")
    print("=" * 60)

    # 快速验证
    print("\n验证检索...")
    results = service.search("煤矿安全", KB_ID, k=3)
    print(f"  检索「煤矿安全」=> 找到 {len(results)} 条相关内容")
    if results:
        print(f"  第一条预览: {results[0].page_content[:80].strip()}...")


if __name__ == "__main__":
    asyncio.run(main())
