# Milvus Lite 到 Standalone 迁移指导

## 结论

开发期可以使用 Milvus Lite，但代码必须按 Milvus Standalone 的服务形态封装。

本项目推荐路线：

1. 开发验证阶段：MongoDB 管用户、角色、知识库、文档权限；Milvus Lite 只存向量。
2. 准生产阶段：切换到 Milvus Standalone Docker，保持同一套 `pymilvus` / `MilvusClient` 调用。
3. 生产扩展阶段：数据量、并发或高可用要求上来后，再从 Standalone 升级到 Distributed 或托管版 Zilliz Cloud。

不要把业务代码写成“只能访问本地 `.db` 文件”的形态。Lite 只能作为连接地址的一种配置值。

## 为什么可以先用 Lite

Milvus Lite 的优点：

- 安装简单，适合本地开发、原型验证、RAG 流程调试。
- 客户端 API 与 Milvus 服务端形态接近，后续切换成本低。
- 不需要先部署 Docker、K8s 或独立向量库服务。

Milvus Lite 的限制：

- 不适合作为多人正式生产环境。
- 没有数据库级用户、角色、权限隔离能力。
- 本质是本地文件，如果 `.db` 文件泄露，MongoDB 中的权限控制无法保护向量内容。
- 部分服务端能力不可用或受限，实际能力应以 Milvus 官方文档为准。

官方参考：

- Milvus Lite 文档：<https://milvus.io/docs/milvus_lite.md>
- Milvus 部署方式：<https://milvus.io/docs/install-overview.md>

## 权限边界

使用 MongoDB 管权限是可行的，但要明确它属于应用层权限，不是向量库自身权限。

推荐职责划分：

| 模块 | 职责 |
| --- | --- |
| MongoDB | 用户、角色、组织、知识库、文档、审查任务、权限关系 |
| 后端 API | 登录鉴权、权限判断、构造 Milvus filter、禁止前端直连向量库 |
| Milvus Lite / Standalone | 存储向量、元数据、执行向量相似度检索 |

关键原则：

1. 前端不能直接访问 Milvus。
2. 所有检索必须经过后端。
3. 后端每次向量检索都必须根据 MongoDB 权限生成 filter。
4. Milvus 中的每条 chunk 必须带权限过滤所需的 metadata。

## 推荐元数据字段

向量库中每条 chunk 至少保留以下字段：

```json
{
  "tenant_id": "default",
  "kb_id": 1,
  "doc_id": 123,
  "filename": "煤矿安全规程.pdf",
  "source_type": "regulation",
  "chapter": "第三章",
  "section": "第三节",
  "page": 12,
  "chunk_index": 35,
  "visibility": "internal",
  "created_at": "2026-06-30T10:00:00+08:00"
}
```

本项目当前 `backend/app/services/vector_store.py` 已经有 `doc_id`、`kb_id`、`filename`、`upload_time`。迁移 Milvus 时应补齐：

- `tenant_id`：多组织或多项目隔离。
- `chunk_index`：稳定定位和重新导入对齐。
- `source_type`：区分法规库、待审文档、用户上传资料等。
- `visibility`：配合 MongoDB 权限策略做过滤。

## 推荐配置方式

后端不要区分两套业务代码，只通过配置切换连接目标。

开发期：

```env
VECTOR_BACKEND=milvus
MILVUS_URI=./data/milvus_lite/coal_regulation.db
MILVUS_TOKEN=
MILVUS_COLLECTION_PREFIX=coal_regulation
```

Standalone：

```env
VECTOR_BACKEND=milvus
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=rag_app:strong_password
MILVUS_COLLECTION_PREFIX=coal_regulation
```

Zilliz Cloud：

```env
VECTOR_BACKEND=milvus
MILVUS_URI=https://xxx.api.gcp-us-west1.zillizcloud.com
MILVUS_TOKEN=<zilliz-api-key>
MILVUS_COLLECTION_PREFIX=coal_regulation
```

## 代码封装建议

当前项目的 ChromaDB 入口集中在：

```text
backend/app/services/vector_store.py
```

迁移时不要让业务层直接依赖 Chroma 或 Milvus SDK。建议拆成接口 + 适配器：

```text
backend/app/services/vector_store/
  __init__.py
  base.py
  chroma_store.py
  milvus_store.py
  factory.py
```

业务层只调用统一接口：

```python
class VectorStore:
    async def add_documents(self, documents, kb_id: int) -> str:
        ...

    def search(self, query: str, kb_id: int, user_scope: dict, k: int = 4):
        ...

    def get_document_chunks(self, doc_id: int, kb_id: int, limit: int = 20):
        ...

    def delete_document(self, doc_id: int, kb_id: int) -> int:
        ...

    def delete_knowledge_base(self, kb_id: int) -> bool:
        ...
```

Milvus 适配器内部根据配置连接：

```python
from pymilvus import MilvusClient

client = MilvusClient(
    uri=settings.MILVUS_URI,
    token=settings.MILVUS_TOKEN or None,
)
```

当 `MILVUS_URI` 是本地 `.db` 路径时，就是 Lite；当它是 `http://localhost:19530` 时，就是 Standalone。

## 权限过滤流程

检索流程必须按以下顺序执行：

1. 用户请求后端 API。
2. 后端从登录态拿到 `user_id`。
3. 后端查询 MongoDB，得到用户可访问的 `tenant_id`、`kb_id`、`doc_id` 范围。
4. 后端构造 Milvus filter。
5. 后端调用 Milvus search。
6. 后端二次校验返回结果的 metadata，确认没有越权 chunk。
7. 后端把检索结果拼入 RAG prompt。

示例 filter：

```text
tenant_id == "default" and kb_id == 1 and doc_id in [101, 102, 103]
```

如果用户没有权限，不应退化成无 filter 检索，而是直接返回空结果或 403。

## Collection 设计

两种设计都可行：

### 方案 A：每个知识库一个 collection

示例：

```text
coal_regulation_kb_1
coal_regulation_kb_2
```

优点：

- 和当前 ChromaDB 的 `kb_{kb_id}` 模型接近。
- 删除整个知识库比较直接。

缺点：

- collection 数量多时管理成本上升。
- 跨知识库检索需要多次 search 后合并。

### 方案 B：统一 collection，通过 metadata 过滤

示例：

```text
coal_regulation_chunks
```

优点：

- 统一建索引和管理。
- 跨知识库检索更方便。
- 更适合后续多租户、多文档过滤。

缺点：

- 删除知识库、重建局部数据时要依赖 metadata 删除。
- filter 不能漏，否则有越权风险。

建议：本项目后续如果要支持多用户、多知识库权限，优先选方案 B；如果只想最小改动替换 ChromaDB，先选方案 A。

## 从 ChromaDB 迁移到 Milvus 的步骤

第一阶段只做适配，不做大规模重构：

1. 在 `backend/requirements.txt` 增加：

```text
pymilvus>=2.5.0
milvus-lite>=2.5.0
```

2. 在配置中增加：

```text
VECTOR_BACKEND
MILVUS_URI
MILVUS_TOKEN
MILVUS_COLLECTION_PREFIX
```

3. 新增 Milvus 适配器，先实现当前 `VectorStoreService` 已使用的方法：

```text
process_file
search
get_stats
get_document_chunks
delete_document
delete_knowledge_base
```

4. 保持 RAG 调用层不变：

```text
backend/app/api/endpoints/rag.py
backend/app/services/chat_service.py
backend/app/services/document_review_service.py
```

5. 写一个一次性导入脚本：

```text
tools/migrate_chroma_to_milvus.py
```

脚本逻辑：

- 从当前 ChromaDB collection 读取 documents、metadatas、ids。
- 用同一套 embedding 模型重新生成向量，或读取已有向量。
- 写入 Milvus。
- 对比迁移前后 chunk 数量。
- 用固定问题做 TopK 检索对比。

## 从 Lite 切到 Standalone 的步骤

当开发期已经使用 Milvus Lite 后，切换 Standalone 应按以下步骤：

1. 启动 Milvus Standalone。
2. 修改环境变量：

```env
MILVUS_URI=http://localhost:19530
MILVUS_TOKEN=rag_app:strong_password
```

3. 在 Standalone 中创建 collection 和索引。
4. 将 Lite 数据导出。
5. 将导出数据导入 Standalone。
6. 跑迁移校验脚本。
7. 切换后端服务到 Standalone。
8. 保留 Lite 文件作为回滚备份，确认稳定后再归档。

如果数据量不大，最稳妥的做法不是直接搬 Lite 文件，而是用原始文档重新切块、重新 embedding、重新写入 Milvus Standalone。这样可以顺便校验 chunk、metadata、索引配置是否一致。

## 校验清单

迁移完成后至少检查：

- collection 数量符合预期。
- 每个知识库的 chunk 数量符合预期。
- `doc_id`、`kb_id`、`tenant_id`、`filename` 没有丢失。
- 普通用户只能检索自己有权限的文档。
- 无权限用户返回空结果或 403。
- 同一批测试问题下，TopK 结果和 ChromaDB / Lite 版本差异可解释。
- RAG prompt 中引用的法规片段仍能正确显示来源。
- 删除文档后，相关 chunk 不再被检索出来。
- 删除知识库后，相关 collection 或 metadata 数据被清理。

## 不建议的做法

不要这样做：

- 前端直接连 Milvus。
- 后端 search 方法默认不带权限 filter。
- 把 `./xxx.db` 写死在业务代码里。
- 用 MongoDB 权限控制后，就认为 Milvus Lite 本地文件也安全。
- 把 Lite 当成正式多人生产库。
- 在多个模块里分别实现 Milvus 查询逻辑。

## 推荐实施顺序

1. 先把当前 `VectorStoreService` 抽象出统一接口。
2. 保留 ChromaDB 适配器，新增 Milvus 适配器。
3. 用 Milvus Lite 跑通本地导入、检索、删除、权限过滤。
4. 增加固定测试集，对比 ChromaDB 与 Milvus Lite 的 TopK。
5. 切换开发环境默认后端为 Milvus Lite。
6. 部署 Milvus Standalone Docker。
7. 修改配置切到 Standalone。
8. 跑全量迁移校验。
9. 再考虑是否启用 Milvus 自身认证、角色和生产运维配置。

## 对本项目的具体建议

当前项目已经有两类向量检索路径：

- 后端 Web 知识库路径：`backend/app/services/vector_store.py`，当前使用 ChromaDB。
- v9 法规审查路径：`hybrid_rag_review_v9.py`，当前主要使用本地向量和 BGE-M3 检索流程。

后续不要一次性把所有检索路径都重写。建议先迁移 Web 知识库路径，因为它有明确的 `kb_id`、`doc_id`、上传、删除和 RAG API。等 Web 路径稳定后，再评估是否把 v9 法规库检索也接入 Milvus。

