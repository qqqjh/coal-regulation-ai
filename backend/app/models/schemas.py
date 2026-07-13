from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime

# --- Chat Models ---
class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    stream: bool = True
    sessionId: Optional[str] = None
    useRAG: bool = True
    kbId: Optional[int] = None  # 知识库ID，使用RAG时必须指定
    model: Optional[str] = "qwen-plus"  # 模型选择，默认 Qwen Plus
    userId: Optional[str] = None
    role: Optional[str] = "user"

# --- Knowledge Base Models ---
class DocumentUploadResponse(BaseModel):
    filename: str
    file_id: str
    message: str

class DocumentInfo(BaseModel):
    id: str
    name: str
    type: str # pdf, txt, etc
    size: int
    uploadTime: datetime
    status: str # indexed, pending

# --- RAG Config Models ---
class RAGConfig(BaseModel):
    retrieval_k: int = 4
    temperature: float = 0.7
    similarity_threshold: float = 0.5
    model_name: str = "qwen-plus"

# --- Review Models ---
class ReviewRequest(BaseModel):
    content: str
    requirements: Optional[str] = None

class ReviewResponse(BaseModel):
    original_text: str
    suggestions: List[str]
    revised_text: str

# --- Monitor Models ---
class MonitorStats(BaseModel):
    total_calls: int
    total_tokens: int
    avg_latency: float
    error_rate: float
