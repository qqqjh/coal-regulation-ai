# Milvus Standalone 与 MongoDB 权限迁移完备性验证

验证日期：2026-06-30，2026-07-01 复验更新  
验证范围：Web 知识库 RAG 路径的 Chroma -> Milvus Standalone 迁移、旧 Chroma 数据迁移脚本、MongoDB 用户权限方案落地状态。  
验证结论：当前已达到“Docker Milvus Standalone 已启动、Python 客户端可连接、真实 Milvus 写入/查询/删除已跑通、旧 Chroma `kb_2` 已迁移 493/493 条”的状态；尚未达到“MongoDB 用户权限闭环已实现”和“前端上传/删除全流程人工验收完成”的状态。

## 1. 当前落地范围

已落代码：

- `backend/app/services/vector_store.py`
  - 已从 Chroma 改为 `pymilvus.MilvusClient`。
  - 使用统一 collection：`coal_regulation_chunks`。
  - 每条 chunk 写入 `tenant_id`、`kb_id`、`doc_id`、`filename`、`source_type`、`visibility`、`chunk_index`、章节字段和 `metadata_json`。
  - 支持 `process_file()`、`add_documents()`、`search()`、`get_document_chunks()`、`delete_document()`、`delete_knowledge_base()`。
  - `search()` 已预留 `user_scope`，可按 `allowed_kb_ids`、`allowed_doc_ids` 生成 Milvus filter。
- `backend/app/core/config.py`
  - 默认 `VECTOR_BACKEND=milvus`。
  - 默认 `MILVUS_URI=http://localhost:19530`，面向 Docker Standalone。
  - 默认 `MILVUS_COLLECTION_PREFIX=coal_regulation`。
- `docker-compose.milvus.yml`
  - 新增 etcd、MinIO、Milvus Standalone 三服务编排。
  - Milvus 端口：`19530`。
  - 健康检查 / WebUI 端口：`9091`。
  - 数据目录：`backend/data/milvus_standalone/`。
- `tools/migrate_chroma_to_milvus.py`
  - 可读取旧 Chroma `kb_*` collection。
  - 可按 `--kb-id` 迁移单知识库。
  - 支持 `--clear` 清理目标 KB 的既有 Milvus 数据。
  - 支持 `--drop-collection` 删除整个 Milvus collection，用于向量维度变化或重建。
  - 优先复用 Chroma 中已有 embeddings，避免迁移时重新调用外部 embedding API。
  - 支持 `--probe-query` 做迁移后 TopK 探测。
- `backend/app/services/chat_service.py`
  - 已移除直接使用 Chroma retriever 的逻辑。
  - RAG 对话改为通过 `vector_store_service.search()` 获取上下文。
- `backend/requirements.txt`
  - 已加入 `pymilvus>=2.6.0,<3.0.0`。
  - 未再要求 `milvus-lite`，避免 Windows 原生环境安装失败。

## 2. 已执行验证

### 2.1 Python 编译检查

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -m py_compile backend\app\services\vector_store.py backend\app\services\chat_service.py backend\app\core\config.py tools\migrate_chroma_to_milvus.py
```

结果：通过。

### 2.2 `pymilvus` 客户端检查

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -c "from pymilvus import MilvusClient; import app.core.config as c; print('pymilvus ok'); print(c.settings.MILVUS_URI)"
```

结果：

```text
pymilvus ok
http://localhost:19530
```

说明：

- 项目实际运行环境 `langchain0.3` 中已安装 `pymilvus 2.6.16`。
- 默认连接地址已是 Docker Standalone 的 `http://localhost:19530`。

### 2.3 MilvusClient API 兼容性检查

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -c "from pymilvus import MilvusClient; print(hasattr(MilvusClient,'create_schema')); print(hasattr(MilvusClient,'prepare_index_params'))"
```

结果：

```text
True
True
```

说明：当前代码中创建 schema 和 index 的 API 在 `pymilvus 2.6.16` 中存在。

### 2.4 伪 MilvusClient 主路径测试

目的：在没有真实 Docker Milvus 的情况下，验证服务层核心逻辑是否闭环。

覆盖内容：

- `add_documents()` 能生成 Milvus entity。
- entity metadata 中保留 `doc_id/kb_id/filename/chunk_index`。
- `search()` 能生成权限 filter。
- `search()` 返回 LangChain `Document`。
- `get_document_chunks()` 能还原 chunk 内容。
- `delete_document()` 能按 `doc_id/kb_id` 查 ID 并删除。

关键输出：

```text
{
  'added': 1,
  'docs': 1,
  'doc_meta': 101,
  'chunks': 1,
  'total': 1,
  'deleted': 1,
  'search_filter': 'tenant_id == "default" and kb_id == 3 and doc_id in [101]',
  'delete_call': (['default_3_101_0_ea159d91ddb7'], None)
}
```

结论：

- 后端服务层的写入、检索、文档 chunk 查询、文档删除主路径在接口层闭合。
- 权限 filter 生成符合预期。
- 验证过程中发现向量主键原先使用 Python `hash(text)`，跨进程不稳定；已改为 SHA1 稳定哈希。

### 2.5 Docker 与 Milvus Standalone 真实启动

命令：

```powershell
docker --version
docker compose version
docker compose -f docker-compose.milvus.yml up -d
```

结果：

```text
Docker version 29.6.1
Docker Compose version v5.1.4
coal-regulation-milvus-etcd        healthy
coal-regulation-milvus-minio       healthy
coal-regulation-milvus-standalone  started
```

说明：

- 首次验证时本机没有 Docker；2026-07-01 已安装 Docker Desktop、WSL，并成功拉取 etcd、MinIO、Milvus 镜像。
- Codex 沙箱用户不能直接访问 Docker engine，但管理员 PowerShell 已成功启动 compose 服务。

### 2.6 真实 Milvus 连接检查

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://localhost:19530'); print(c.list_collections())"
```

结果：

```text
collections= []
```

结论：Windows 侧 Python 客户端可连接 Docker Milvus Standalone。

### 2.7 真实 Milvus 写入 / 查询 / 删除 smoke test

命令逻辑：

- 使用 4 维测试向量创建临时数据。
- 执行 `add_documents()`。
- 执行 `search()`。
- 执行 `get_document_chunks()`。
- 执行 `delete_knowledge_base()` 清理测试数据。

结果：

```text
{'added': 1, 'hits': 1, 'chunks': 1, 'total': 1, 'deleted_kb': True, 'first': 'Milvus standalone smoke test chunk'}
```

结论：真实 Milvus 服务上的写入、检索、chunk 查询、删除主路径已跑通。

### 2.8 旧 Chroma `kb_2` 迁移验证

旧 Chroma 检查：

```text
collections: ['kb_2']
kb_2 count: 493
embedding dim: 1536
```

迁移命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe tools\migrate_chroma_to_milvus.py --kb-id 2 --drop-collection
```

结果：

```text
Dropped Milvus collection: coal_regulation_chunks
[kb_2] using existing Chroma embeddings (493 vectors)
[kb_2] kb_2: migrated 493/493 chunks
Done. Total migrated chunks: 493
```

Milvus 抽样查询：

```text
count 493
《防治煤与瓦斯突出细则》.pdf
防治煤与瓦斯突出细则
第一章  总    则
第一条  为加强防治煤（岩）与瓦斯（二氧化碳）突出...
```

结论：

- 旧 Chroma `kb_2` 已完整迁移到 Milvus。
- 迁移使用 Chroma 原有 1536 维 embeddings，没有重新调用外部 embedding API。
- 迁移后 Milvus 中 `tenant_id == "default" and kb_id == 2` 的 chunk 数量为 493。

## 3. 功能完备性矩阵

| 功能 | 当前状态 | 证据 | 是否完备 |
| --- | --- | --- | --- |
| Milvus Standalone 配置 | 已落代码 | `MILVUS_URI=http://localhost:19530`，`docker-compose.milvus.yml` | 基本完备 |
| Milvus Python 客户端 | 已安装 | `pymilvus ok`，版本 2.6.16 | 完备 |
| collection schema 创建 | 已落代码并检查 API | `create_schema=True`，`prepare_index_params=True` | 基本完备 |
| PDF/DOC/DOCX 上传后入向量库 | 代码路径已接入 | `process_file()` -> MinerU -> v6 chunks -> `add_documents()` | 待前端上传验收 |
| TXT 上传后入向量库 | 代码路径已接入 | `TextLoader` -> splitter -> `add_documents()` | 待前端上传验收 |
| RAG 检索 | 真实 Milvus 已验证底层查询 | smoke test `hits=1`，`kb_2` 迁移后可 query | 基本完备 |
| Chat RAG | 已改统一 search | `chat_service.py` 不再使用 Chroma retriever | 基本完备 |
| 文档 chunk 查看 | 真实 Milvus 已验证 | smoke test `chunks=1` | 基本完备 |
| 文档删除 | 真实 Milvus 已验证 | smoke test `deleted_kb=True` | 基本完备 |
| 知识库删除 | 真实 Milvus 已验证 | `delete_knowledge_base()` smoke test | 基本完备 |
| 旧 Chroma 迁移 | 已真实迁移 `kb_2` | 493/493 chunks | 完备 |
| MongoDB 用户/角色 | 已有工业方案 | `MongoDB用户权限与MilvusStandalone迁移工业化方案.md` | 未落代码 |
| 管理员/普通用户权限 | vector store 预留 `user_scope` | 可生成 doc allow-list filter | API 层未接入，未完备 |
| v9 审查本地 BGE 路径 | 未迁移 | 保持 `hybrid_rag_review_v9.py` 本地检索 | 按当前边界不迁移 |

## 4. 当前不完备项

### P0：MongoDB 用户/角色/权限仍是方案，尚未接入 API

当前只有 `vector_store_service.search(..., user_scope=...)` 预留入口，但：

- 没有 MongoDB 连接。
- 没有 `users/kb_members/documents/audit_logs` 集合代码。
- 没有登录态 / token / current_user 依赖。
- `knowledge.py`、`rag.py`、`chat.py` 尚未按用户过滤知识库、文档和检索范围。

因此现在所有 Web API 仍接近“管理员式默认权限”。

建议下一步新增：

- `backend/app/db/mongo.py`
- `backend/app/services/auth_service.py`
- `backend/app/services/permission_service.py`
- `backend/app/api/endpoints/auth.py`
- 在 `knowledge.py/rag.py/chat.py` 中注入 `current_user` 和 `user_scope`。

### P1：真实删除和统计在大规模数据下需要补批处理验证

当前 `delete_document()` 和 `get_document_chunks()` 使用 Milvus query 返回 ID 后处理。代码里有 `limit=10000`，对于单文档超过 10000 chunks 的极端情况需要补批量查询或分页策略。

当前煤矿法规/规程文档通常不会单文档超过这个数量，但工业化版本建议补：

- 分页 query。
- 删除批次日志。
- 删除后二次 query 校验为 0。

### P1：前端上传 / 删除全流程仍需人工验收

底层 Milvus 写入、查询、删除已经真实跑通；旧 Chroma `kb_2` 也已迁移。但还需要在浏览器里跑完整业务流程：

- 知识库页面上传 PDF/DOC/DOCX。
- 文档状态从 `processing` 变成 `indexed`。
- 文档详情能看到 chunks。
- RAG 页面能基于该知识库检索。
- 删除文档后，Milvus 中该 `doc_id` 不再返回。
- 删除知识库后，Milvus 中该 `kb_id` 不再返回。

### P1：旧脚本直接使用 `_get_collection()` 的历史路径不再兼容

正式 API 已改走统一 `search()`。但部分旧测试脚本仍可能调用：

```python
vector_store_service._get_collection(kb_id)
```

当前会明确抛错，提示改用：

```python
vector_store_service.search(..., kb_id=...)
```

这属于预期破坏性变更。后续如果还需要旧脚本，应逐个迁移。

### P2：v9 主审查路径未迁移 Milvus

当前边界是只迁移 Web 知识库路径。`hybrid_rag_review_v9.py` 仍使用本地 BGE-M3 dense/sparse/rerank。该路径不影响 Web 知识库 RAG 迁移，但如果未来希望审查和 RAG 共用权限化知识库，需要单独设计。

## 5. 真实端到端验证步骤

Docker Desktop 安装并启动后，按以下顺序执行。

### 5.1 启动 Milvus Standalone

```powershell
cd D:/work/project/coal-regulation-ai
docker compose -f docker-compose.milvus.yml up -d
docker compose -f docker-compose.milvus.yml ps
```

### 5.2 检查连接

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://localhost:19530'); print(c.list_collections())"
```

### 5.3 迁移旧 Chroma

```powershell
D:/Anaconda/envs/langchain0.3/python.exe tools/migrate_chroma_to_milvus.py --kb-id 2 --drop-collection
```

### 5.4 启动后端和前端

```powershell
cd D:/work/project/coal-regulation-ai/backend
D:/Anaconda/envs/langchain0.3/python.exe main.py
```

```powershell
cd D:/work/project/coal-regulation-ai
npm.cmd run dev
```

### 5.5 前端验证点

- 知识库列表能打开。
- RAG 页面选择知识库后可检索。
- 上传 PDF/DOC/DOCX 后，文档状态变为 `indexed`。
- 文档详情能看到 chunks。
- 删除文档后，再检索该文档内容不返回。
- 删除知识库后，该 `kb_id` 内容不再返回。

## 6. 结论

当前迁移不是“完全生产就绪”，但已经完成向量库替换和真实 Milvus 验证：

- Chroma 依赖已从正式 Web RAG 主路径移除。
- Milvus Standalone 默认配置和 Docker 编排已就绪，并已成功启动。
- Milvus 写入、检索、删除、旧数据迁移的服务接口已真实验证。
- 旧 Chroma `kb_2` 已迁移 493/493 chunks。
- 权限 filter 的底层入口已预留。

真正还缺的是两件事：

1. 在前端完成上传、检索、查看 chunk、删除文档/知识库的人工验收。
2. 按方案把 MongoDB 用户、角色、知识库成员关系和 API 权限判断接入后端。
