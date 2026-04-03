"""直接测试API端点"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
from app.db.database import AsyncSessionLocal
from app.models.database import Document
from app.services.vector_store import vector_store_service
from sqlalchemy import select

async def test_api():
    async with AsyncSessionLocal() as db:
        # 获取文档
        result = await db.execute(
            select(Document).where(Document.id == 1)
        )
        doc = result.scalar_one_or_none()

        if not doc:
            print("文档不存在")
            return

        print(f"文档名称: {doc.name}")
        print(f"文档类型: {doc.file_type}")
        print(f"文档大小: {doc.file_size}")

        # 获取分块
        chunks = vector_store_service.get_document_chunks(doc.name, limit=5)

        print(f"\n找到 {len(chunks)} 个分块\n")

        for chunk in chunks[:3]:  # 只显示前3个
            print(f"========== 分块 {chunk['id']} ==========")
            print(f"页码: {chunk.get('page', 'N/A')}")
            print(f"内容长度: {len(chunk['content'])} 字符")
            print(f"内容预览: {chunk['content'][:150]}...")
            print()

if __name__ == "__main__":
    asyncio.run(test_api())
