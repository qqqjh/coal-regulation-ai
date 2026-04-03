# 煤炭规程智能体后端API文档

## 项目概述

本项目是煤炭规程智能体的后端服务，基于 FastAPI + LangChain 0.3 + LangGraph 构建，提供对话、知识库管理、RAG检索、文档审查和用量监控等功能。

## 技术栈

- **框架**: FastAPI
- **AI框架**: LangChain 0.3, LangGraph 0.2
- **向量数据库**: ChromaDB
- **数据库**: SQLite (异步)
- **LLM**: OpenAI API (可配置)

## 快速启动

### 1. 安装依赖

```bash
cd backend
pip install -r requirements.txt
```

### 2. 配置环境变量

编辑 `backend/.env` 文件:

```env
OPENAI_API_KEY=your_api_key_here
OPENAI_BASE_URL=https://api.openai.com/v1
```

### 3. 启动服务

```bash
# Windows
python main.py

# Linux/Mac
python3 main.py
```

服务将在 `http://localhost:8000` 启动

## API 端点

### 根端点

#### GET /
获取API基本信息

**响应示例:**
```json
{
  "message": "Coal Regulation AI Backend is running",
  "version": "1.0.0",
  "docs": "/api/docs"
}
```

#### GET /health
健康检查

**响应示例:**
```json
{
  "status": "healthy"
}
```

---

## 1. 对话模块 (Chat)

### 流式聊天补全

**POST** `/api/chat/completions`

**请求体:**
```json
{
  "messages": [
    {"role": "user", "content": "什么是瓦斯检测?"},
    {"role": "assistant", "content": "瓦斯检测是..."},
    {"role": "user", "content": "检测频率是多少?"}
  ],
  "stream": true,
  "useRAG": true
}
```

**响应:** Server-Sent Events 流式输出

### 会话管理

#### 创建会话

**POST** `/api/chat/sessions?name=新对话&user_id=user123`

**响应:**
```json
{
  "id": 1,
  "name": "新对话",
  "created_at": "2024-01-27T10:00:00",
  "updated_at": "2024-01-27T10:00:00"
}
```

#### 获取会话列表

**GET** `/api/chat/sessions?user_id=user123`

#### 获取会话详情

**GET** `/api/chat/sessions/{session_id}`

#### 更新会话（重命名）

**PUT** `/api/chat/sessions/{session_id}?name=新名称`

#### 删除会话

**DELETE** `/api/chat/sessions/{session_id}`

### 消息管理

#### 添加消息到会话

**POST** `/api/chat/sessions/{session_id}/messages?role=user&content=消息内容`

#### 获取会话的消息历史

**GET** `/api/chat/sessions/{session_id}/messages?limit=100`

#### 清空会话的消息

**DELETE** `/api/chat/sessions/{session_id}/messages`

---

## 2. 知识库模块 (Knowledge)

### 知识库管理

#### 创建知识库

**POST** `/api/knowledge/bases?name=煤矿安全规程&description=描述`

**响应:**
```json
{
  "id": 1,
  "name": "煤矿安全规程",
  "description": "描述",
  "created_at": "2024-01-27T10:00:00"
}
```

#### 获取知识库列表

**GET** `/api/knowledge/bases`

**响应:**
```json
[
  {
    "id": 1,
    "name": "煤矿安全规程",
    "description": "描述",
    "created_at": "2024-01-27T10:00:00",
    "document_count": 5
  }
]
```

#### 获取知识库详情

**GET** `/api/knowledge/bases/{kb_id}`

#### 删除知识库

**DELETE** `/api/knowledge/bases/{kb_id}`

### 文档管理

#### 上传文档到知识库

**POST** `/api/knowledge/bases/{kb_id}/documents`

**Content-Type:** `multipart/form-data`

**参数:**
- `file`: 文档文件 (PDF, TXT, DOCX)

**响应:**
```json
{
  "id": 1,
  "filename": "煤矿安全规程.pdf",
  "status": "indexed",
  "message": "Success"
}
```

#### 获取知识库的文档列表

**GET** `/api/knowledge/bases/{kb_id}/documents`

#### 获取文档详情

**GET** `/api/knowledge/documents/{doc_id}`

#### 删除文档

**DELETE** `/api/knowledge/documents/{doc_id}`

#### 上传文档（简化版）

**POST** `/api/knowledge/upload`

自动上传到"默认知识库"

---

## 3. RAG配置模块 (RAG)

### 测试RAG检索和生成

**POST** `/api/rag/test?query=瓦斯检测频率`

**请求体:**
```json
{
  "retrieval_k": 4,
  "temperature": 0.7,
  "similarity_threshold": 0.5,
  "model_name": "gpt-3.5-turbo"
}
```

**响应:**
```json
{
  "retrieved_docs": [
    {
      "content": "文档内容片段...",
      "source": "煤矿安全规程.pdf",
      "score": 0.9
    }
  ],
  "answer": "根据检索到的内容，瓦斯检测频率为..."
}
```

### 获取RAG配置

**GET** `/api/rag/config?user_id=user123`

**响应:**
```json
{
  "retrieval_k": 4,
  "temperature": 0.7,
  "similarity_threshold": 0.5,
  "model_name": "gpt-3.5-turbo"
}
```

### 保存RAG配置

**POST** `/api/rag/config?user_id=user123`

**请求体:**
```json
{
  "retrieval_k": 4,
  "temperature": 0.7,
  "similarity_threshold": 0.5,
  "model_name": "gpt-3.5-turbo"
}
```

### 搜索文档片段

**GET** `/api/rag/search?query=瓦斯检测&k=4`

**响应:**
```json
{
  "query": "瓦斯检测",
  "results": [
    {
      "content": "文档内容...",
      "source": "煤矿安全规程.pdf",
      "upload_time": "2024-01-27T10:00:00",
      "score": 0.85
    }
  ],
  "total": 4
}
```

---

## 4. 审查修正模块 (Review)

**核心功能**: 使用 LangGraph 构建的五个智能体链式审查流程

### 五个智能体流程

1. **全域知识检索智能体**: 从知识库检索相关规程
2. **合规性判别智能体**: 识别参数、程序违规问题
3. **全局依赖分析智能体**: 分析文档各部分依赖关系
4. **标准内容生成智能体**: 生成符合规程的修正内容
5. **安全合规复核智能体**: 最终审核确保所有问题已解决

### 上传文档进行审查

**POST** `/api/review/upload`

**Content-Type:** `multipart/form-data`

**参数:**
- `file`: 待审查文档

**响应:**
```json
{
  "task_id": "uuid-string",
  "filename": "待审查文档.txt",
  "status": "pending",
  "message": "文档已上传，等待审查"
}
```

### 开始审查文档（异步）

**POST** `/api/review/analyze/{task_id}`

**响应:**
```json
{
  "message": "审查任务已启动",
  "task_id": "uuid-string",
  "status": "processing"
}
```

### 获取审查结果

**GET** `/api/review/result/{task_id}`

**响应:**
```json
{
  "task_id": "uuid-string",
  "file_name": "待审查文档.txt",
  "status": "completed",
  "progress": 100,
  "current_agent": "安全合规复核智能体",
  "created_at": "2024-01-27T10:00:00",
  "completed_at": "2024-01-27T10:05:00",
  "result": {
    "is_compliant": true,
    "total_issues": 5,
    "severe_issues": 1,
    "warning_issues": 3,
    "suggestion_issues": 1,
    "issues": [
      {
        "level": "严重",
        "location": "第3段",
        "description": "瓦斯浓度检测频率不符合规程要求",
        "reference": "《煤矿安全规程》第128条",
        "suggestion": "应将检测频率从每4小时调整为每2小时"
      }
    ],
    "dependencies": [
      {
        "from": "瓦斯检测频率",
        "to": "通风系统设计",
        "type": "强依赖",
        "impact": "修改检测频率需同步调整通风系统参数"
      }
    ],
    "knowledge_references": [
      {
        "source": "《煤矿安全规程》第X条",
        "content": "相关规程内容...",
        "relevance": 0.95
      }
    ],
    "revised_content": "修正后的文档内容...",
    "summary": "审查总结...",
    "recommendations": [
      "建议定期复查瓦斯检测记录",
      "建议完善通风系统监测机制"
    ]
  }
}
```

### 同步审查文档（简化版）

**POST** `/api/review/analyze`

**请求体:**
```json
{
  "content": "待审查的文本内容",
  "requirements": "符合《煤矿安全规程》"
}
```

**响应:**
```json
{
  "original_text": "原始文本",
  "suggestions": ["建议1", "建议2"],
  "revised_text": "修正后文本"
}
```

---

## 5. 监控模块 (Monitor)

### 获取监控统计数据

**GET** `/api/monitor/stats?start_time=2024-01-27T00:00:00&end_time=2024-01-27T23:59:59&module=chat`

**响应:**
```json
{
  "total_calls": 1024,
  "total_tokens": 500000,
  "avg_latency": 1.234,
  "total_cost": 5.67,
  "error_rate": 1.5,
  "failed_calls": 15
}
```

### 获取调用趋势数据

**GET** `/api/monitor/trend?hours=24&module=chat`

**响应:**
```json
{
  "data": [
    {
      "time": "2024-01-27 10:00",
      "calls": 45,
      "tokens": 12000
    }
  ]
}
```

### 获取模块使用分布

**GET** `/api/monitor/distribution?start_time=2024-01-20T00:00:00&end_time=2024-01-27T23:59:59`

**响应:**
```json
{
  "data": [
    {
      "name": "chat",
      "value": 450
    },
    {
      "name": "knowledge",
      "value": 200
    },
    {
      "name": "rag",
      "value": 150
    },
    {
      "name": "review",
      "value": 100
    }
  ]
}
```

### 获取最近的日志记录

**GET** `/api/monitor/logs?limit=50&module=chat&status=success`

**响应:**
```json
{
  "logs": [
    {
      "id": 1,
      "module": "chat",
      "operation": "completions",
      "status": "success",
      "tokens": 120,
      "latency": 1.5,
      "cost": 0.001,
      "timestamp": "2024-01-27T10:00:00",
      "error_msg": null
    }
  ],
  "total": 50
}
```

---

## 数据库模型

### Session (会话)
- id: 会话ID
- name: 会话名称
- user_id: 用户ID
- created_at: 创建时间
- updated_at: 更新时间

### Message (消息)
- id: 消息ID
- session_id: 所属会话ID
- role: 角色 (user/assistant)
- content: 消息内容
- timestamp: 时间戳
- tokens: Token数量

### KnowledgeBase (知识库)
- id: 知识库ID
- name: 名称
- description: 描述
- created_at: 创建时间
- updated_at: 更新时间

### Document (文档)
- id: 文档ID
- kb_id: 所属知识库ID
- name: 文档名称
- file_path: 文件路径
- file_type: 文件类型
- file_size: 文件大小
- status: 状态 (pending/indexed/failed)
- upload_time: 上传时间
- indexed_time: 索引时间

### ReviewTask (审查任务)
- id: 任务ID
- task_id: 任务UUID
- file_name: 文件名
- file_path: 文件路径
- status: 状态 (pending/processing/completed/failed)
- progress: 进度 (0-100)
- current_agent: 当前智能体
- created_at: 创建时间
- completed_at: 完成时间
- result: 审查结果 (JSON)

### MonitorLog (监控日志)
- id: 日志ID
- module: 模块名称
- operation: 操作类型
- status: 状态 (success/failed)
- tokens: Token数量
- latency: 延迟 (秒)
- cost: 成本 (USD)
- timestamp: 时间戳
- error_msg: 错误信息

### RAGConfig (RAG配置)
- id: 配置ID
- user_id: 用户ID
- retrieval_method: 检索方法
- retrieval_k: 检索数量
- similarity_threshold: 相似度阈值
- model_name: 模型名称
- temperature: 温度参数
- max_tokens: 最大Token数
- is_active: 是否激活
- created_at: 创建时间
- updated_at: 更新时间

---

## API文档

启动服务后，访问以下地址查看交互式API文档:

- **Swagger UI**: http://localhost:8000/api/docs
- **ReDoc**: http://localhost:8000/api/redoc

---

## 错误处理

所有API端点都会返回标准的HTTP状态码:

- **200**: 成功
- **201**: 创建成功
- **400**: 请求参数错误
- **404**: 资源不存在
- **500**: 服务器内部错误

错误响应示例:
```json
{
  "detail": "错误描述信息"
}
```

---

## 注意事项

1. 所有涉及数据库操作的端点都是异步的
2. 文件上传大小限制取决于FastAPI配置
3. 监控日志会自动记录所有API调用
4. 审查任务使用后台任务处理，避免阻塞
5. 向量数据库使用ChromaDB持久化存储

---

## 开发建议

1. 使用 `.env` 文件管理敏感配置
2. 生产环境请修改CORS配置
3. 建议使用专业的向量数据库（如Pinecone, Weaviate）
4. 建议使用Redis存储审查任务进度
5. 建议使用PostgreSQL替代SQLite

---

## 版本信息

- **版本**: 1.0.0
- **最后更新**: 2024-01-27
- **Python版本**: >= 3.10
- **FastAPI版本**: >= 0.110.0
- **LangChain版本**: >= 0.3.0
- **LangGraph版本**: >= 0.2.0
