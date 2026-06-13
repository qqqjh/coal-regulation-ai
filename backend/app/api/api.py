from fastapi import APIRouter

api_router = APIRouter()

# 这些是占位符，稍后我们会实现具体的路由
from app.api.endpoints import chat, knowledge, rag, review, monitor, v9_review

api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])
api_router.include_router(rag.router, prefix="/rag", tags=["rag"])
api_router.include_router(review.router, prefix="/review", tags=["review"])
api_router.include_router(monitor.router, prefix="/monitor", tags=["monitor"])
api_router.include_router(v9_review.router, prefix="/v9", tags=["v9-review"])
