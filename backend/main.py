from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.api.api import api_router
from app.core.config import settings
from app.db.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时：初始化数据库
    print("正在初始化数据库...")
    await init_db()
    print("数据库初始化完成！")

    # 启动时：清理孤立的向量数据目录
    print("正在清理孤立的向量数据目录...")
    from app.services.vector_store import cleanup_orphan_segments
    cleanup_orphan_segments()
    print("向量数据清理完成！")

    yield

    # 关闭时的清理工作
    print("应用正在关闭...")

app = FastAPI(
    title=settings.PROJECT_NAME,
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境请修改为前端实际地址
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {
        "message": "Coal Regulation AI Backend is running",
        "version": "1.0.0",
        "docs": f"{settings.API_V1_STR}/docs"
    }

@app.get("/health")
def health_check():
    return {"status": "healthy"}

if __name__ == "__main__":
    import uvicorn
    print(f"启动 {settings.PROJECT_NAME}...")
    print(f"API文档地址: http://localhost:8000{settings.API_V1_STR}/docs")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
