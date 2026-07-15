# MongoDB 用户权限与 Milvus Standalone 迁移工业化方案

## 目标

本阶段先完成 Web 知识库路径的向量库迁移：把原来的 ChromaDB 替换为 Docker Milvus Standalone，并把代码写成后续可切换集群版或托管版的服务形态。

用户、角色、组织、知识库权限建议由 MongoDB 管理。Milvus 只负责向量检索和 metadata 过滤，不负责业务权限判断。

## 总体架构

```text
前端
  |
  | 登录态 / 用户请求
  v
FastAPI 后端
  |-- MongoDB：用户、角色、组织、知识库、文档、权限、审计日志
  |-- SQLite：当前原型遗留业务表，迁移期可保留
  |-- Milvus Standalone：chunk 向量、可过滤 metadata
  |-- MinerU：PDF/DOC/DOCX 结构化解析
  |-- LLM：Qwen/OpenAI-compatible
```

关键原则：

1. 前端永远不能直连 Milvus。
2. 后端每次检索都要先从 MongoDB 计算用户权限范围。
3. Milvus 检索必须带 `tenant_id/kb_id/doc_id` filter。
4. Milvus 返回结果后，后端要二次校验 metadata，防止 filter 或数据异常导致越权。
5. 本项目在 Windows 开发环境直接使用 Docker Standalone；Lite 只作为 Linux/macOS 环境的可选轻量形态。

## 角色设计

推荐先做三类角色，不要一开始做过细的权限树。

| 角色 | 定位 | 知识库查看 | 文档上传 | 文档删除 | 知识库管理 | 检索范围 |
| --- | --- | --- | --- | --- | --- | --- |
| system_admin | 系统管理员 | 全部租户或指定租户 | 是 | 是 | 是 | 全部或指定租户 |
| kb_admin | 知识库管理员 | 被授权知识库 | 是 | 是 | 可管理被授权知识库 | 被授权知识库全部文档 |
| normal_user | 普通用户 | 被授权知识库 | 默认否，可单独授权 | 默认否 | 否 | 被授权知识库中允许检索的文档 |

普通用户是否允许上传，建议不要只由“角色”决定，而是由知识库成员关系决定：

```text
role = normal_user
kb_permissions.can_upload = true/false
kb_permissions.can_delete = false
kb_permissions.can_search = true
```

这样同一个普通用户可以在 A 知识库只读，在 B 知识库可上传。

## MongoDB 集合设计

### users

```json
{
  "_id": "ObjectId",
  "tenant_id": "default",
  "username": "zhangsan",
  "display_name": "张三",
  "password_hash": "...",
  "roles": ["normal_user"],
  "status": "active",
  "created_at": "2026-06-30T10:00:00+08:00",
  "updated_at": "2026-06-30T10:00:00+08:00"
}
```

### knowledge_bases

```json
{
  "_id": "ObjectId",
  "tenant_id": "default",
  "kb_id": 3,
  "name": "煤矿安全规程",
  "description": "法规标准库",
  "visibility": "internal",
  "owner_user_id": "ObjectId",
  "status": "active",
  "created_at": "2026-06-30T10:00:00+08:00",
  "updated_at": "2026-06-30T10:00:00+08:00"
}
```

### kb_members

```json
{
  "_id": "ObjectId",
  "tenant_id": "default",
  "kb_id": 3,
  "user_id": "ObjectId",
  "role": "viewer",
  "permissions": {
    "can_view": true,
    "can_search": true,
    "can_upload": false,
    "can_delete_document": false,
    "can_manage_kb": false
  },
  "doc_scope": {
    "mode": "all",
    "doc_ids": []
  },
  "created_at": "2026-06-30T10:00:00+08:00"
}
```

`doc_scope.mode` 建议支持：

- `all`：该知识库下所有文档都可检索。
- `allow_list`：只能检索 `doc_ids` 列表中的文档。
- `deny_list`：可检索全部文档，但排除 `doc_ids`。

第一阶段只实现 `all` 和 `allow_list` 即可。

### documents

```json
{
  "_id": "ObjectId",
  "tenant_id": "default",
  "kb_id": 3,
  "doc_id": 101,
  "filename": "煤矿安全规程.pdf",
  "source_type": "regulation",
  "visibility": "internal",
  "file_path": "backend/data/uploads/xxx.pdf",
  "chunk_count": 836,
  "status": "indexed",
  "uploaded_by": "ObjectId",
  "created_at": "2026-06-30T10:00:00+08:00",
  "indexed_at": "2026-06-30T10:05:00+08:00"
}
```

### audit_logs

用于后续追责和排查权限问题。

```json
{
  "_id": "ObjectId",
  "tenant_id": "default",
  "user_id": "ObjectId",
  "action": "rag.search",
  "resource": {
    "kb_id": 3,
    "doc_ids": [101, 102]
  },
  "result": "success",
  "created_at": "2026-06-30T10:00:00+08:00",
  "ip": "127.0.0.1"
}
```

## Milvus Collection 设计

本项目建议用统一 collection：

```text
coal_regulation_chunks
```

每条 chunk 存以下字段：

```json
{
  "id": "default_3_101_35_1234567890",
  "vector": [0.1, 0.2],
  "text": "法规片段正文",
  "tenant_id": "default",
  "kb_id": 3,
  "doc_id": 101,
  "filename": "煤矿安全规程.pdf",
  "doc_name": "煤矿安全规程",
  "source_type": "regulation",
  "visibility": "internal",
  "chunk_index": 35,
  "page": 12,
  "page_range": "12-13",
  "chapter": "第三章",
  "section": "第一节",
  "chunking_strategy": "mineru_chapter_v6",
  "metadata_json": "{...完整元数据...}"
}
```

统一 collection 的优点是跨知识库检索和权限过滤更自然。删除知识库时用：

```text
tenant_id == "default" and kb_id == 3
```

删除文档时用：

```text
tenant_id == "default" and kb_id == 3 and doc_id == 101
```

## 权限到 Milvus Filter 的流程

检索请求：

```json
{
  "query": "便携式甲烷检测报警仪报警浓度",
  "kb_id": 3,
  "top_k": 5
}
```

后端处理：

1. 从登录态拿到 `user_id`。
2. 查 MongoDB `users` 和 `kb_members`。
3. 判断用户是否有 `can_search`。
4. 得到 `tenant_id`、`kb_id`、`doc_scope`。
5. 构造 Milvus filter。

管理员：

```text
tenant_id == "default" and kb_id == 3
```

普通用户，只允许检索两个文档：

```text
tenant_id == "default" and kb_id == 3 and doc_id in [101, 102]
```

无权限：

```text
直接返回 403 或空结果，不允许无 filter 检索
```

## 上传、查看、删除权限建议

### 上传

上传是高风险操作，因为会改变知识库内容和 RAG 结果。

建议策略：

- `system_admin`：可上传到任何租户内知识库。
- `kb_admin`：可上传到自己管理的知识库。
- `normal_user`：默认不可上传；需要 `kb_members.permissions.can_upload=true`。

上传成功后：

1. MongoDB 写 `documents`，状态 `processing`。
2. 文件进入预处理流程：MinerU -> JSON -> v6 chunk。
3. chunk 写入 Milvus，带 `tenant_id/kb_id/doc_id/uploaded_by`。
4. MongoDB 更新 `documents.status=indexed` 和 `chunk_count`。
5. 写审计日志。

### 查看

查看知识库列表时不要返回所有知识库。

- 管理员：返回租户内全部知识库。
- 普通用户：只返回 `kb_members` 中授权的知识库。

查看文档列表时同理：

- `doc_scope=all`：返回该知识库全部文档。
- `doc_scope=allow_list`：只返回允许的文档。

### 删除

删除必须比上传更严格。

- `system_admin`：可删除任意知识库/文档。
- `kb_admin`：可删除自己管理知识库下的文档。
- `normal_user`：默认不可删除；即使允许上传，也不建议允许删除他人文档。

删除顺序：

1. 校验 MongoDB 权限。
2. Milvus 删除 `tenant_id/kb_id/doc_id` 对应 chunk。
3. MongoDB 将文档标记为 `deleted` 或删除记录。
4. 物理文件可延迟清理，避免误删无法恢复。
5. 写审计日志。

## 当前代码落地状态

已完成的工程落点：

- `backend/app/services/vector_store.py`：Web 知识库向量存储已改为 MilvusClient。
- `backend/app/core/config.py`：新增 `VECTOR_BACKEND/MILVUS_URI/MILVUS_TOKEN/MILVUS_COLLECTION_PREFIX`。
- `tools/migrate_chroma_to_milvus.py`：提供旧 Chroma 数据导入 Milvus 的一次性脚本。
- `backend/app/services/chat_service.py`：聊天 RAG 改为调用统一 `search()`，不再直接依赖 Chroma retriever。

当前仍保留的边界：

- v9 多智能体法规审查主路径仍使用本地 BGE-M3 dense/sparse/rerank 流程，暂不迁移。
- 当前业务表仍是 SQLite/SQLAlchemy；MongoDB 用户权限先按本文方案设计，后续分阶段替换或并行接入。
- 当前开发态没有真实登录态时，可使用 `DEFAULT_TENANT_ID=default` 和管理员式默认 scope。

## 开发期运行方式

安装依赖：

```powershell
pip install -r backend/requirements.txt
```

默认配置：

```env
VECTOR_BACKEND=milvus
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=
MILVUS_COLLECTION_PREFIX=coal_regulation
DEFAULT_TENANT_ID=default
```

启动 Standalone：

```powershell
docker compose -f docker-compose.milvus.yml up -d
docker compose -f docker-compose.milvus.yml ps
```

验证连接：

```powershell
python -c "from pymilvus import MilvusClient; c=MilvusClient(uri='http://localhost:19530'); print(c.list_collections())"
```

迁移旧 Chroma 数据：

```powershell
python tools/migrate_chroma_to_milvus.py --clear --probe-query "便携式甲烷检测报警仪报警浓度"
```

只迁移一个知识库：

```powershell
python tools/migrate_chroma_to_milvus.py --kb-id 3 --clear
```

## 后续切换远程 Standalone / 集群版

从本机 Docker Standalone 切到远程 Standalone 后，只改配置：

```env
VECTOR_BACKEND=milvus
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=rag_app:strong_password
MILVUS_COLLECTION_PREFIX=coal_regulation
```

数据迁移建议不要直接复制底层数据目录，而是：

1. 保留原始文档和 MinerU JSON。
2. 用同一套 chunk 规则重建 chunks。
3. 重新 embedding。
4. 写入 Standalone。
5. 用固定问题集对比迁移前后的 TopK。

## 分阶段实施计划

### 第一阶段：Milvus Standalone 替换 Chroma

- 后端 Web 知识库上传、删除、检索跑通。
- 旧 Chroma 数据可通过脚本导入 Milvus。
- 前端 RAG、Chat、知识库管理无需感知向量库变化。

### 第二阶段：MongoDB 权限并行接入

- 新增 MongoDB 连接和 `users/kb_members/documents/audit_logs`。
- 登录后从 token 拿 `user_id`。
- 每个知识库接口增加权限判断。
- `vector_store_service.search()` 传入 `user_scope`。

### 第三阶段：权限闭环

- 知识库列表按用户过滤。
- 上传、删除、查看、检索全部写审计日志。
- 管理员后台支持给用户授权知识库和文档范围。
- 增加越权测试：普通用户不能看到、检索、删除未授权文档。

### 第四阶段：远程 Standalone / 集群版

- Docker 启动 Milvus Standalone。
- 修改 `MILVUS_URI`。
- 重建或迁移向量。
- 固定测试集校验 TopK 和 RAG 引用。
- 观察并发、延迟、磁盘增长和备份恢复。

## 验收清单

- 上传 PDF/DOC/DOCX 后，MinerU JSON、v6 chunks、Milvus chunk 数量一致。
- RAG 检索可以返回带 `filename/chapter/section/page_range` 的来源。
- 删除文档后，Milvus 中对应 `doc_id` 不再能检索到。
- 删除知识库后，Milvus 中对应 `kb_id` 不再能检索到。
- 普通用户只看到授权知识库。
- 普通用户只检索授权文档。
- 无权限请求返回 403 或空结果，不允许退化成全库检索。
- 本机 Standalone 切到远程 Standalone 或集群版时不改业务代码，只改配置和重建数据。
