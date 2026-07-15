from fastapi import APIRouter

api_router = APIRouter()

# 这些是占位符，稍后我们会实现具体的路由
from app.api.endpoints import admin, auth, chat, knowledge, rag, review, monitor, v9_review

api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])
api_router.include_router(rag.router, prefix="/rag", tags=["rag"])
api_router.include_router(review.router, prefix="/review", tags=["review"])
api_router.include_router(monitor.router, prefix="/monitor", tags=["monitor"])
# v10 继续复用已经稳定的 SQLite/Web 审查传输层；真正的审核版本由
# ``v10_worker.py`` 决定。保留 /v9 作为旧评测脚本兼容入口，前端只使用 /v10。
api_router.include_router(v9_review.router, prefix="/v10", tags=["v10-review"])
api_router.include_router(
    v9_review.router,
    prefix="/v9",
    tags=["v9-review-compat"],
    include_in_schema=False,
)
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
