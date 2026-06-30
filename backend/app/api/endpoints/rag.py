from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from app.models.schemas import RAGConfig as RAGConfigSchema
from app.models.database import RAGConfig as RAGConfigDB
from app.services.vector_store import vector_store_service
from app.services.monitor_service import monitor_service
from app.db.database import get_db
from langchain_openai import ChatOpenAI
from app.core.config import settings
import time

router = APIRouter()
QWEN_MODELS = {"qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"}


def _normalize_qwen_model(model: str = None) -> str:
    return model if model in QWEN_MODELS else "qwen-plus"

# 请求模型
class RetrieveRequest(BaseModel):
    query: str
    kb_id: int
    top_k: int = 5
    similarity_threshold: float = 0.0

class GenerateRequest(BaseModel):
    query: str
    kb_id: int
    model: str = "qwen-plus"
    temperature: float = 0.7
    top_k: int = 5

@router.post("/test")
async def test_rag(
    query: str,
    config: RAGConfigSchema,
    db: AsyncSession = Depends(get_db)
):
    """测试RAG检索和生成"""
    start_time = time.time()

    try:
        # 模拟 RAG 测试：检索 + 生成
        docs = vector_store_service.search(query, k=config.retrieval_k)

        context_text = "\n\n".join([d.page_content for d in docs])

        llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=_normalize_qwen_model(config.model_name),
            temperature=config.temperature
        )

        response = await llm.ainvoke([
            ("system", f"基于以下内容回答：\n{context_text}"),
            ("user", query)
        ])

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="test_retrieval",
            status="success",
            tokens=len(response.content) // 4,
            latency=time.time() - start_time
        )

        return {
            "retrieved_docs": [
                {
                    "content": d.page_content,
                    "source": d.metadata.get("source"),
                    "score": 0.9  # 模拟相似度分数
                }
                for d in docs
            ],
            "answer": response.content
        }

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="test_retrieval",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/config")
async def get_config(
    user_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """获取RAG配置"""
    query = select(RAGConfigDB).where(RAGConfigDB.is_active == True)

    if user_id:
        query = query.where(RAGConfigDB.user_id == user_id)

    result = await db.execute(query)
    config = result.scalar_one_or_none()

    if not config:
        # 返回默认配置
        return RAGConfigSchema()

        return {
            "retrieval_k": config.retrieval_k,
            "temperature": config.temperature,
            "similarity_threshold": config.similarity_threshold,
            "model_name": _normalize_qwen_model(config.model_name)
        }

@router.post("/config")
async def save_config(
    config: RAGConfigSchema,
    user_id: str = None,
    db: AsyncSession = Depends(get_db)
):
    """保存RAG配置"""
    # 查找现有配置
    query = select(RAGConfigDB).where(RAGConfigDB.is_active == True)
    if user_id:
        query = query.where(RAGConfigDB.user_id == user_id)

    result = await db.execute(query)
    existing_config = result.scalar_one_or_none()

    if existing_config:
        # 更新现有配置
        existing_config.retrieval_k = config.retrieval_k
        existing_config.temperature = config.temperature
        existing_config.similarity_threshold = config.similarity_threshold
        existing_config.model_name = _normalize_qwen_model(config.model_name)
        existing_config.retrieval_method = config.retrieval_method if hasattr(config, 'retrieval_method') else "similarity"
    else:
        # 创建新配置
        new_config = RAGConfigDB(
            user_id=user_id,
            retrieval_k=config.retrieval_k,
            temperature=config.temperature,
            similarity_threshold=config.similarity_threshold,
            model_name=_normalize_qwen_model(config.model_name),
            retrieval_method="similarity",
            is_active=True
        )
        db.add(new_config)

    await db.commit()

    config.model_name = _normalize_qwen_model(config.model_name)
    return {"status": "success", "config": config}

@router.post("/retrieve")
async def retrieve_documents(
    request: RetrieveRequest,
    db: AsyncSession = Depends(get_db)
):
    """检索文档片段（用于RAG测试界面）"""
    start_time = time.time()

    try:
        # 从指定知识库检索文档
        docs = vector_store_service.search(request.query, kb_id=request.kb_id, k=request.top_k)

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="retrieve_documents",
            status="success",
            latency=time.time() - start_time
        )

        return {
            "query": request.query,
            "kb_id": request.kb_id,
            "results": [
                {
                    "id": i + 1,
                    "document": d.metadata.get("filename", "unknown"),
                    "content": d.page_content,
                    "page": d.metadata.get("page", 0),
                    "similarity": 0.95 - i * 0.05,  # 模拟相似度分数，实际应该从检索结果获取
                }
                for i, d in enumerate(docs)
            ],
            "total": len(docs)
        }

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="retrieve_documents",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
async def generate_answer(
    request: GenerateRequest,
    db: AsyncSession = Depends(get_db)
):
    """基于检索结果生成答案（用于RAG测试界面）"""
    start_time = time.time()

    try:
        # 1. 检索相关文档
        docs = vector_store_service.search(request.query, kb_id=request.kb_id, k=request.top_k)

        if not docs:
            return {
                "query": request.query,
                "answer": "抱歉，没有找到相关文档来回答您的问题。",
                "retrieved_count": 0
            }

        # 2. 构建上下文
        context_parts = []
        for i, doc in enumerate(docs, 1):
            filename = doc.metadata.get("filename", "unknown")
            page = doc.metadata.get("page", 0)
            context_parts.append(f"[文档{i}: {filename}, 第{page}页]\n{doc.page_content}")

        context_text = "\n\n".join(context_parts)

        # 3. 调用LLM生成答案
        llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=_normalize_qwen_model(request.model),
            temperature=request.temperature
        )

        prompt = f"""基于以下参考文档回答用户的问题。

参考文档：
{context_text}

用户问题：{request.query}

请基于参考文档内容回答问题，并在回答中标注引用的文档来源。如果参考文档中没有相关信息，请明确说明。"""

        response = await llm.ainvoke(prompt)

        # 记录监控日志
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="generate_answer",
            status="success",
            tokens=len(response.content) // 4,
            latency=time.time() - start_time
        )

        return {
            "query": request.query,
            "answer": response.content,
            "retrieved_count": len(docs),
            "model": _normalize_qwen_model(request.model),
            "temperature": request.temperature
        }

    except Exception as e:
        await monitor_service.log_operation(
            db=db,
            module="rag",
            operation="generate_answer",
            status="failed",
            latency=time.time() - start_time,
            error_msg=str(e)
        )
        raise HTTPException(status_code=500, detail=str(e))
