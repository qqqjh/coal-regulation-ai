# MongoDB / Redis / Milvus 数据分层完备性验证

验证日期：2026-07-01  
验证路径：`D:\work\project\coal-regulation-ai`  
验证范围：在现有 Milvus Standalone 基础上，引入 MongoDB 长期业务记录层，并预留 Redis 短期状态层。

## 1. 结论

当前已达到：

- MongoDB 与 Redis 已加入本地 Docker Compose：`docker-compose.data.yml`。
- `langchain0.3` 环境已安装 `pymongo==4.17.0`。
- 后端已新增 MongoDB 配置项与服务层：`backend/app/services/mongo_service.py`。
- v9 多智能体审查任务已接入 MongoDB 长期镜像：
  - 创建审查任务时镜像 `review_jobs`。
  - 查询任务状态时同步 `review_jobs`。
  - 查询问题列表时同步 `review_issues`。
  - 提交人工反馈时同步 `review_issues`。
  - 历史待审文档接口优先读 MongoDB，不可用时自动退回 SQLite。
- MongoDB / Redis / Milvus 容器均已真实启动并显示 healthy。
- MongoDB 最小写入/读取闭环已通过。

当前尚未达到：

- Redis 尚未承接 v9 实时任务状态、SSE 状态、短期缓存或任务队列。
- SQLite 仍是 v9 worker 的进程间任务队列来源，MongoDB 目前是长期镜像层，不是唯一主库。
- 待审文档 chunks 的向量尚未写入 Milvus 的独立 `review_doc_chunks` collection。
- MongoDB 用户、角色、组织、权限过滤尚未完整接入所有 API。

## 2. 数据分层边界

| 组件 | 当前状态 | 职责 |
| --- | --- | --- |
| SQLite `review_queue_v9.db` | 仍在用 | v9 worker 当前任务队列、issues、feedback 通道 |
| MongoDB | 已引入并可用 | 长期保存审查任务、问题清单、人工反馈镜像 |
| Redis | 容器已引入，代码未使用 | 后续承接短期任务状态、SSE 状态、锁、限流、缓存 |
| Milvus Standalone | 已运行 | 法规/知识库向量；后续扩展待审文档向量 |

推荐后续目标：

```text
Redis  = 秒级状态 / 短期缓存 / 实时推送
MongoDB = 用户 / 权限 / 文档 / 任务 / 问题 / 人工反馈 / 审计
Milvus = 法规 chunks 向量 + 待审文档 chunks 向量
```

## 3. 新增和修改文件

| 文件 | 作用 |
| --- | --- |
| `docker-compose.data.yml` | 本地启动 MongoDB 与 Redis |
| `backend/app/core/config.py` | 新增 `ENABLE_MONGODB`、`MONGODB_URI`、`MONGODB_DB`、`REDIS_URL` |
| `backend/app/services/mongo_service.py` | MongoDB 长期业务记录镜像服务 |
| `backend/app/api/endpoints/v9_review.py` | v9 审查任务、问题、反馈同步 MongoDB；新增存储健康检查 |
| `review_queue.py` | `jobs` 表补 `user_id`，历史列表支持用户过滤 |
| `backend/requirements.txt` | 新增 `pymongo>=4.6.0,<5.0.0` |
| `如何运行.md` | 增加 MongoDB / Redis 启动说明 |

## 4. 运行配置

### 4.1 Docker Compose

新增：

```powershell
docker compose -f docker-compose.data.yml up -d
```

包含：

- `coal-regulation-mongodb`
  - image: `mongo:7.0`
  - port: `27017`
  - data: `backend/data/mongodb`
- `coal-regulation-redis`
  - image: `redis:7.2-alpine`
  - port: `6379`
  - data: `backend/data/redis`

### 4.2 后端配置

默认配置：

```text
ENABLE_MONGODB=true
MONGODB_URI=mongodb://coal_admin:coal_password@localhost:27017/coal_regulation_ai?authSource=admin
MONGODB_DB=coal_regulation_ai
REDIS_URL=redis://localhost:6379/0
```

## 5. 验证记录

### 5.1 Python 编译

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -m py_compile backend/app/core/config.py backend/app/services/mongo_service.py backend/app/api/endpoints/v9_review.py review_queue.py
```

结果：通过。

### 5.2 Compose 配置校验

命令：

```powershell
docker compose -f docker-compose.data.yml config
```

结果：通过。  
备注：Docker CLI 输出过 `C:\Users\Administrator\.docker\config.json` 访问警告，但 Compose 配置仍成功解析。

### 5.3 依赖安装

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -m pip install "pymongo>=4.6.0,<5.0.0"
```

结果：成功安装：

```text
pymongo-4.17.0
dnspython-2.8.0
```

### 5.4 容器运行状态

命令：

```powershell
docker compose -f docker-compose.data.yml ps
```

结果：

```text
coal-regulation-mongodb   mongo:7.0          Up ... (healthy)   0.0.0.0:27017->27017/tcp
coal-regulation-redis     redis:7.2-alpine   Up ... (healthy)   0.0.0.0:6379->6379/tcp
```

同时当前 Milvus 仍在运行：

```text
coal-regulation-milvus-etcd         healthy
coal-regulation-milvus-minio        healthy
coal-regulation-milvus-standalone   healthy
```

### 5.5 MongoDB Python 连接

命令：

```powershell
D:/Anaconda/envs/langchain0.3/python.exe -c "import sys; sys.path.insert(0, 'backend'); from app.services.mongo_service import mongo_service; print(mongo_service.health())"
```

结果：

```text
{'enabled': True, 'available': True, 'database': 'coal_regulation_ai', 'error': ''}
```

### 5.6 MongoDB 最小写入/读取闭环

命令摘要：

```powershell
# 写入 review_jobs
mongo_service.upsert_review_job({...})

# 查询 review_jobs
mongo_service.list_review_jobs('smoke', 5)

# 写入 review_issues
mongo_service.replace_review_issues('mongo_smoke_review', [...])
```

结果：

```text
upsert True
rows 1
replace_issues True
issue_count 1
```

说明：已确认 `review_jobs` 和 `review_issues` 两个集合可写、可读。

### 5.7 Diff 空白检查

命令：

```powershell
git diff --check -- backend/app/core/config.py backend/app/services/mongo_service.py backend/app/api/endpoints/v9_review.py backend/requirements.txt review_queue.py docker-compose.data.yml 如何运行.md
```

结果：通过。  
备注：Git 输出 LF/CRLF 提示，不是语法或空白错误。

## 6. API 变化

### 6.1 历史待审文档

接口：

```text
GET /api/v9/jobs?user_id=guest&limit=30
```

行为：

1. 先从 SQLite 取当前用户任务。
2. 尝试同步到 MongoDB。
3. MongoDB 可用时返回 MongoDB 长期镜像。
4. MongoDB 不可用时返回 SQLite 结果。

### 6.2 存储健康检查

接口：

```text
GET /api/v9/storage-health
```

返回内容包括：

- SQLite 队列路径和可用性。
- MongoDB enabled / available / database / error。
- Redis URL 与当前是否被 v9 使用。
- Milvus URI。

## 7. 风险与边界

### 7.1 MongoDB 当前不是唯一主库

当前 v9 worker 仍从 SQLite 领取任务。MongoDB 是长期镜像层。这样做的原因是迁移风险低：

- 不破坏现有 worker。
- 不影响前端审查。
- MongoDB 未启动时仍能正常使用旧流程。

后续若要把 MongoDB 变为主任务库，需要重写 worker 的任务领取、锁、重试、状态更新和反馈领取逻辑。

### 7.2 Redis 当前只是基础设施

Redis 容器已经可以启动，但 v9 代码还没有写入 Redis。后续建议把以下内容迁入 Redis：

- `job:{job_id}:status`
- `job:{job_id}:progress`
- `job:{job_id}:sse:last_event`
- 上传/解析过程中的短期状态
- worker 分布式锁
- 限流和防重复提交

### 7.3 待审文档向量尚未长期入库

当前重复性审查已复用 `q_dense` 做整篇内部余弦相似度，但这些向量还没有写入 Milvus。

后续建议新增：

```text
Milvus collection: review_doc_chunks
MongoDB collection: review_chunks
```

写入字段：

- `job_id`
- `doc_id`
- `user_id`
- `org_id`
- `chunk_index`
- `content`
- `source_blocks`
- `embedding`
- `created_at`

用途：

- 历史待审文档语义检索。
- 跨文档重复性审查。
- 用户/角色权限范围内的相似规程追溯。

## 8. 后续优先级

### P0

1. 将待审文档 chunks 的 `q_dense` 写入 Milvus `review_doc_chunks`。
2. 在 MongoDB 保存 `review_chunks` 元数据，并与 Milvus primary key 对齐。
3. 将前端历史待审文档的详情页扩展为 MongoDB 主查询。

### P1

1. Redis 接入实时进度状态和 SSE 状态缓存。
2. MongoDB 接入用户、角色、组织、知识库权限。
3. Milvus 检索统一加权限 filter。

### P2

1. SQLite 队列迁移到 MongoDB + Redis。
2. worker 支持多实例抢占锁和失败重试。
3. 管理员视角增加跨用户、跨部门待审文档检索。

## 9. 当前判定

当前版本已经完成 MongoDB 基础引入和真实容器验证；Redis 基础设施已就绪但还未承接业务；Milvus 已保持原有 Standalone 可用状态。

本阶段可以作为“SQLite 原型队列 + MongoDB 长期镜像 + Redis 预留 + Milvus 向量库”的过渡版本。
