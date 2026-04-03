"""生成文档预览JSON数据"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
from app.db.database import AsyncSessionLocal
from app.models.database import Document
from app.services.vector_store import vector_store_service
from sqlalchemy import select

async def generate_preview_data():
    async with AsyncSessionLocal() as db:
        # 获取文档
        result = await db.execute(
            select(Document).where(Document.id == 1)
        )
        doc = result.scalar_one_or_none()

        if not doc:
            print("文档不存在")
            return

        # 获取分块
        chunks = vector_store_service.get_document_chunks(doc.name, limit=20)

        preview_data = {
            "id": doc.id,
            "name": doc.name,
            "type": doc.file_type,
            "size": doc.file_size,
            "uploadTime": doc.upload_time.isoformat(),
            "chunks": chunks,
            "total_chunks": len(chunks)
        }

        # 保存到public目录供前端访问
        output_file = Path(__file__).parent.parent / "public" / "document_preview.json"
        output_file.parent.mkdir(exist_ok=True)

        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(preview_data, f, ensure_ascii=False, indent=2)

        print(f"预览数据已生成: {output_file}")
        print(f"包含 {len(chunks)} 个文本分块")

if __name__ == "__main__":
    asyncio.run(generate_preview_data())
