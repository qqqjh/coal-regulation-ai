from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
from app.models.database import Base

# 创建异步引擎
engine = create_async_engine(
    settings.SQLITE_URL,
    echo=False,
    future=True
)

# 创建异步会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def init_db():
    """初始化数据库"""
    async with engine.begin() as conn:
        # 创建所有表
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_knowledge_base_owner_columns(conn)


async def _ensure_knowledge_base_owner_columns(conn):
    """SQLite 轻量迁移：旧库表补充知识库归属字段。"""
    result = await conn.exec_driver_sql("PRAGMA table_info(knowledge_bases)")
    existing = {row[1] for row in result.fetchall()}
    additions = {
        "owner_user_id": "VARCHAR(100) NOT NULL DEFAULT 'admin'",
        "owner_name": "VARCHAR(100)",
        "owner_role": "VARCHAR(50) DEFAULT 'admin'",
        "visibility": "VARCHAR(50) DEFAULT 'private'",
    }
    for name, ddl in additions.items():
        if name not in existing:
            await conn.exec_driver_sql(f"ALTER TABLE knowledge_bases ADD COLUMN {name} {ddl}")

async def get_db():
    """获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
