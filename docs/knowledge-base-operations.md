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
`ATHENA_KNOWLEDGE_OCR_CACHE_ROOT` 映射到该变量。解析子进程默认限制为
`ATHENA_KNOWLEDGE_PARSE_MEMORY_MB=4096` MiB。Linux 使用 `RLIMIT_AS`；macOS
由父进程每 250 ms 通过 psutil 采样子进程 RSS 并在超限时终止，无法读取 RSS 时
返回明确错误。其他平台须提供 `resource.RLIMIT_AS`，否则解析明确失败。关闭
worker 会让未完成任务在租约到期后重新排队。

文件上传入口在 PostgreSQL 中共享 4 个并发 slot；每个请求的 ASGI body 上限为
22 MiB，spool 阈值为 1 MiB，整个 slot 持有期限最长 600 秒。按同时占用估算，
ASGI body 与 API 文件缓冲总量不超过约 168 MiB。API 会在系统临时目录可用空间
低于 `ATHENA_KNOWLEDGE_UPLOAD_MIN_FREE_BYTES` 时返回 503，默认水位为 384 MiB。
上传 reservation 在幂等检查和 owner 配额预留之后才写 MinIO；写入、
对象哈希复核和数据库提交完成前不会登记可处理文档。结果不确定时 reservation
继续占用配额，worker 先确认清理 fencing 和来源/预留引用，再删除对象并释放预留。

当前不提供用于清理知识库测试数据的专用脚本。写入过新对象后不能直接退回旧本地
存储版本；切换失败时应暂停写入并修复 MinIO 配置。
