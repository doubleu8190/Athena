# 知识库运行环境

知识库 API、`knowledge_worker` 与启用知识查询的 `run_worker` 必须使用同一个
PostgreSQL 数据库、embedding 模型 revision 和部署级私有 MinIO 桶。启用前先应用
Alembic 迁移，预建桶，并准备可写的 `ATHENA_KNOWLEDGE_OCR_CACHE_ROOT`。本地只保留
上传暂存、解析临时文件、OCR 与模型缓存；不再是文档原件后端。

MinIO 配置仅在 `ATHENA_KNOWLEDGE_ENABLED=true` 时读取。API 和相关 Worker 的
endpoint、凭据、桶名及 TLS 必须一致：

```text
ATHENA_KNOWLEDGE_MINIO_ENDPOINT=host[:port]
ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY=<由 secret manager 注入>
ATHENA_KNOWLEDGE_MINIO_SECRET_KEY=<由 secret manager 注入>
ATHENA_KNOWLEDGE_MINIO_BUCKET=<预建私有桶>
ATHENA_KNOWLEDGE_MINIO_SECURE=true
```

应用不创建桶、不修改桶策略，也不提供本地存储回退。桶由部署方预建并禁止匿名
访问；服务凭据至少需要桶检查、对象列举、读取、上传和删除权限。`SECURE` 默认
`true`，只接受明确的布尔值。启动只通过 `bucket_exists` 验证桶存在，实际读写删
权限仍需部署后冒烟确认。TLS 部署应使用系统信任库，或通过 `SSL_CERT_FILE`
指向可信 CA；关闭 TLS 只限受控本地环境。

安装解析和存储支持：

```sh
.venv/bin/python -m pip install -e '.[knowledge]'
```

MinIO Python SDK 固定为 `minio==7.2.20`。适配器默认连接超时 5 秒、读取超时
30 秒、最多 2 次重试，只允许 HEAD、GET、PUT、DELETE 等幂等方法重试；每个进程
最多 4 个并发同步存储调用。响应体和凭据不写入日志，失败只记录操作和稳定错误码。
应用不管理未完成分段上传；部署方应在专用桶配置未完成分段上传的生命周期回收，
应用只清理完成后超过 24 小时的合法孤儿对象。

设置 `ATHENA_KNOWLEDGE_ENABLED=true` 后启动 API，并另启长驻 worker：

```sh
PYTHONPATH=backend/src python -m knowledge_worker
```

解析依赖在 Python 3.12 ARM64 环境验证：jieba 0.42.1、PaddlePaddle 3.2.2、
PaddleOCR 3.7.0、pypdfium2 5.15.0、python-docx 1.2.0、openpyxl 3.1.5、
python-pptx 1.0.2、Pillow 13.0.0。Python 3.14 当前没有兼容的 OCR 引擎组合，
不要在该环境运行知识库 worker；缺少 Paddle 条件依赖时仍可运行 MinIO 存储相关
单元测试。按目标平台单独确认 Paddle wheel。

worker 使用本地缓存中的 `BAAI/bge-m3` revision，不会在运行时联网下载模型。
OCR 模型由 PaddleOCR 首次加载时写入 `PADDLE_PDX_CACHE_HOME`，worker 会将
`ATHENA_KNOWLEDGE_OCR_CACHE_ROOT` 映射到该变量。隔离文档解析通过标准库
`ProcessPoolExecutor`（`spawn` 模式，`max_tasks_per_child=50`）复用解析子进程，
单文档解析超时或子进程异常崩溃后会自动强杀并重建子进程池；内存边界由容器部署层统一
配额控制。关闭 worker 会让未完成任务在租约到期后重新排队。

文件上传入口使用进程内并发槽位（默认 4 个）限制同时上传数；每个请求的 ASGI body 上限为
22 MiB，spool 阈值为 1 MiB，整个上传请求超时上限 600 秒。写入 MinIO 前先执行无锁只读
预检（核对幂等重放/冲突、目标知识库与文档进行中状态及用户配额），拦截重放、冲突与超配额
请求避免写入无谓孤儿；因网络中断或极端并发遗留的未引用对象，由 worker 降频执行超过
24 小时宽限期的孤儿对象扫描兜底回收。

当前不提供用于清理知识库测试数据的专用脚本。写入过新对象后不能直接退回旧本地
存储版本；切换失败时应暂停写入并修复 MinIO 配置。
