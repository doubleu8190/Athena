# 知识库运行环境

知识库与文件/技能能力已收敛为后端默认无条件装配的核心能力。API、`run_worker` 与
`knowledge_worker` 必须使用同一个 PostgreSQL 数据库、统一单桶私有对象存储
（`ATHENA_STORAGE_*`）以及输出维度一致（1024 维）的无状态远程 OpenAI 兼容
Embedding 服务。启用前先应用 Alembic 迁移、预建私有存储桶，若启用了本地扫描件/图片
OCR 则准备可写的 `ATHENA_KNOWLEDGE_OCR_CACHE_ROOT`。本地仅保留上传暂存、解析临时文件
与可选 OCR 缓存，不再作为文档原件或本地向量权重存储后端。

## 统一对象存储配置（必填）

API、`run_worker` 与 `knowledge_worker` 均在启动阶段前置校验统一对象存储配置；
知识库原件统一落入同桶 `knowledge/` 前缀，与文件/技能前缀（`session-files/`、
`session-files-extracted/`、`session-artifacts/`、`skill-logs/`、`skills/`）严格隔离。
各进程的 endpoint、凭据、桶名及 TLS 必须一致：

```text
ATHENA_STORAGE_ENDPOINT=host[:port]
ATHENA_STORAGE_ACCESS_KEY=<由 secret manager 注入>
ATHENA_STORAGE_SECRET_KEY=<由 secret manager 注入>
ATHENA_STORAGE_BUCKET=<预建私有桶>
ATHENA_STORAGE_SECURE=true
```

应用不创建桶、不修改桶策略，也不提供本地存储回退。桶由部署方预建并禁止匿名访问；
服务凭据至少需要桶检查、对象列举、读取、上传和删除权限。`ATHENA_STORAGE_SECURE`
默认 `true`，只接受明确的布尔值。启动阶段仅通过 `bucket_exists` 验证桶存在，实际
读写删权限仍需部署后冒烟确认。TLS 部署应使用系统信任库，或通过 `SSL_CERT_FILE`
指向可信 CA；关闭 TLS 只限受控本地环境。

## 无状态远程 Embedding 配置（必填）

API、`run_worker` 与 `knowledge_worker` 不再在进程内加载本地 `BAAI/bge-m3` 权重，
而是统一通过 OpenAI 兼容 `/embeddings` 接口（如 Ollama、vLLM、Xinference、OpenAI、
SiliconFlow 等）完成向量化；输出维度须与 PostgreSQL `knowledge_chunks.embedding`
`Vector(1024)` 列严格一致：

```text
ATHENA_EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
ATHENA_EMBEDDING_API_KEY=<可选，有鉴权时注入>
ATHENA_EMBEDDING_MODEL=BAAI/bge-m3
ATHENA_EMBEDDING_DIMENSIONS=1024
ATHENA_EMBEDDING_SEND_DIMENSIONS=true
ATHENA_EMBEDDING_TIMEOUT_SECONDS=30.0
ATHENA_EMBEDDING_BATCH_SIZE=32
ATHENA_KNOWLEDGE_SEMANTIC_MIN_SCORE=0.35
```

## 依赖分层与启动方式

轻量化文本解析、中文分词与多模态图像处理依赖（`jieba==0.42.1`、`pypdfium2==5.15.0`、
`python-docx==1.2.0`、`openpyxl==3.1.5`、`python-pptx==1.0.2`、`Pillow==13.0.0`）已移入
核心 `[project].dependencies`，随默认安装直接就绪。

可选依赖分组 `knowledge` 仅包含重量级本地扫描件/图片 OCR 引擎
（`paddlepaddle==3.2.2; python_version < '3.14'`、`paddleocr==3.7.0; python_version < '3.14'`）：

```sh
# 仅在需要本地扫描件/图片 OCR 时安装重量级可选依赖（Python < 3.14）
.venv/bin/python -m pip install -e '.[knowledge]'
```

Python 3.14 当前没有兼容的 PaddleOCR 引擎组合；在未安装 `knowledge` 可选依赖或镜像
`INSTALL_KNOWLEDGE=false` 时，普通 PDF、Office、纯文本解析与远程向量检索仍完整可用，
仅本地扫描件/图片 OCR 不可用。

容器编排（`compose.yml`）中 `backend-knowledge-worker` 已移除可选 profile，随
`docker compose up` 默认启动；宿主机本地开发可直接启动常驻 worker：

```sh
PYTHONPATH=backend/src python -m knowledge_worker
```

## 运行时约束与运维边界

- **对象存储客户端**：MinIO Python SDK 固定为 `minio==7.2.20`。API 与 `run_worker`
  内由组合根统一创建单实例共享 `MinioObjectClient` 供知识库原件与文件/技能存储共用；
  默认连接超时 5 秒、读取超时 30 秒、最多 2 次重试，只允许 HEAD、GET、PUT、DELETE
  等幂等方法重试。响应体和凭据不写入日志，失败只记录操作和稳定错误码。应用不管理未完成
  分段上传，部署方应在存储桶配置未完成分段上传的生命周期回收。
- **OCR 与隔离解析池**：启用本地 OCR 时，模型由 PaddleOCR 首次加载写入
  `PADDLE_PDX_CACHE_HOME`，worker 会将 `ATHENA_KNOWLEDGE_OCR_CACHE_ROOT` 映射到该变量。
  隔离文档解析通过标准库 `ProcessPoolExecutor`（`spawn` 模式，`max_tasks_per_child=50`）
  复用解析子进程，单文档解析超时或子进程异常崩溃后会自动强杀并重建子进程池；内存边界由
  容器部署层统一配额控制。关闭 worker 会让未完成任务在租约到期后重新排队。
- **上传准入与孤儿回收**：文件上传入口使用进程内并发槽位（默认 4 个）限制同时上传数；
  每个请求的 ASGI body 上限为 22 MiB，spool 阈值为 1 MiB，整个上传请求超时上限
  600 秒。写入对象存储前先执行无锁只读预检（核对幂等重放/冲突、目标知识库与文档进行中
  状态及用户配额），拦截重放、冲突与超配额请求避免写入无谓孤儿；因网络中断或极端并发
  遗留的 `knowledge/` 未引用对象，由 worker 降频执行超过 24 小时宽限期的孤儿对象扫描
  兜底回收。
- **数据迁移注意**：当前不提供用于清理知识库测试数据的专用脚本；切换或修改存储配置前
  须确保各进程配置一致并暂停写入。
