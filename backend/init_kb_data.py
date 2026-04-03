"""
初始化知识库数据 - 创建知识库并关联已加载的PDF
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

from app.db.database import AsyncSessionLocal
from app.models.database import KnowledgeBase, Document
from sqlalchemy import select
from sqlalchemy.orm import selectinload

async def init_knowledge_base():
    async with AsyncSessionLocal() as session:
        # 检查是否已存在煤矿安全规程知识库
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.name == "煤矿安全规程")
        )
        kb = result.scalar_one_or_none()

        if not kb:
            # 创建知识库
            kb = KnowledgeBase(
                name="煤矿安全规程",
                description="包含煤矿安全相关规程文件，用于RAG检索",
                created_at=datetime.utcnow()
            )
            session.add(kb)
            await session.commit()
            await session.refresh(kb)
            print(f"[OK] 创建知识库: {kb.name} (ID: {kb.id})")
        else:
            print(f"[OK] 知识库已存在: {kb.name} (ID: {kb.id})")

        # 检查PDF文档记录是否存在
        pdf_path = Path(__file__).parent.parent / "煤矿安全规程.pdf"

        result = await session.execute(
            select(Document).where(Document.name == "煤矿安全规程.pdf")
        )
        doc = result.scalar_one_or_none()

        if not doc:
            # 创建文档记录
            file_size = pdf_path.stat().st_size if pdf_path.exists() else 0
            doc = Document(
                kb_id=kb.id,
                name="煤矿安全规程.pdf",
                file_path=str(pdf_path),
                file_type="pdf",
                file_size=file_size,
                status="indexed",
                upload_time=datetime.utcnow(),
                indexed_time=datetime.utcnow(),
                chunk_count=0  # 可以稍后计算实际分块数
            )
            session.add(doc)
            await session.commit()
            await session.refresh(doc)
            print(f"[OK] 创建文档记录: {doc.name}")
            print(f"  - 文件大小: {file_size / 1024 / 1024:.2f} MB")
            print(f"  - 状态: {doc.status}")
        else:
            print(f"[OK] 文档记录已存在: {doc.name}")

        # 统计
        result = await session.execute(
            select(KnowledgeBase)
            .options(selectinload(KnowledgeBase.documents))
        )
        all_kbs = result.scalars().all()
        print(f"\n当前知识库总数: {len(all_kbs)}")

        for kb_item in all_kbs:
            doc_count = len(kb_item.documents)
            print(f"  - {kb_item.name}: {doc_count} 个文档")

if __name__ == "__main__":
    print("=" * 60)
    print("初始化知识库数据")
    print("=" * 60)
    asyncio.run(init_knowledge_base())
    print("\n初始化完成！")
