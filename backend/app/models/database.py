from sqlalchemy import Column, Integer, String, Float, DateTime, Text, ForeignKey, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class Session(Base):
    """对话会话"""
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    user_id = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    messages = relationship("Message", back_populates="session", cascade="all, delete-orphan")

class Message(Base):
    """对话消息"""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False)
    role = Column(String(50), nullable=False)  # user, assistant
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    tokens = Column(Integer, default=0)

    # 关系
    session = relationship("Session", back_populates="messages")

class KnowledgeBase(Base):
    """知识库"""
    __tablename__ = "knowledge_bases"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关系
    documents = relationship("Document", back_populates="knowledge_base", cascade="all, delete-orphan")

class Document(Base):
    """文档"""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    kb_id = Column(Integer, ForeignKey("knowledge_bases.id"), nullable=False)
    name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_type = Column(String(50), nullable=False)  # pdf, txt, docx
    file_size = Column(Integer, nullable=False)  # bytes
    status = Column(String(50), default="pending")  # pending, indexed, failed
    chunk_count = Column(Integer, default=0)
    upload_time = Column(DateTime, default=datetime.utcnow)
    indexed_time = Column(DateTime, nullable=True)

    # 关系
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")

class MonitorLog(Base):
    """监控日志"""
    __tablename__ = "monitor_logs"

    id = Column(Integer, primary_key=True, index=True)
    module = Column(String(100), nullable=False)  # chat, knowledge, rag, review
    operation = Column(String(100), nullable=False)  # send_message, upload_doc, retrieve, etc
    status = Column(String(50), nullable=False)  # success, failed
    tokens = Column(Integer, default=0)
    latency = Column(Float, default=0.0)  # seconds
    cost = Column(Float, default=0.0)  # USD
    timestamp = Column(DateTime, default=datetime.utcnow)
    error_msg = Column(Text, nullable=True)
    extra_data = Column(Text, nullable=True)  # JSON string for additional data

class ReviewTask(Base):
    """审查任务"""
    __tablename__ = "review_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(100), unique=True, nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    status = Column(String(50), default="pending")  # pending, processing, completed, failed
    progress = Column(Integer, default=0)  # 0-100
    current_agent = Column(String(100), nullable=True)
    kb_id = Column(Integer, default=3)  # 知识库ID
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    result = Column(Text, nullable=True)  # JSON string
    error_msg = Column(Text, nullable=True)

class RAGConfig(Base):
    """RAG配置"""
    __tablename__ = "rag_configs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(100), nullable=True)
    retrieval_method = Column(String(50), default="similarity")  # similarity, mmr
    retrieval_k = Column(Integer, default=4)
    similarity_threshold = Column(Float, default=0.5)
    model_name = Column(String(100), default="gpt-3.5-turbo")
    temperature = Column(Float, default=0.7)
    max_tokens = Column(Integer, default=2000)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
