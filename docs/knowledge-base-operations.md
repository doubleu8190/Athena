# 知识库运行环境

知识库 API 与 `python -m knowledge_worker` 使用同一个 PostgreSQL 数据库、
本地原件目录和 embedding 模型版本。启用前先应用 Alembic 迁移，并准备可写的
`ATHENA_KNOWLEDGE_STORAGE_ROOT` 和 `ATHENA_KNOWLEDGE_OCR_CACHE_ROOT`。

解析依赖在 Python 3.12 ARM64 环境验证：jieba 0.42.1、PaddlePaddle 3.2.2、
PaddleOCR 3.7.0、pypdfium2 5.15.0、python-docx 1.2.0、openpyxl 3.1.5、
python-pptx 1.0.2、Pillow 13.0.0。Python 3.14 当前没有兼容的 OCR 引擎组合，
不要在该环境运行知识库 worker。安装解析支持时使用 `.[knowledge]` extra，
并按目标平台单独确认 Paddle wheel。

设置 `ATHENA_KNOWLEDGE_ENABLED=true` 后启动 API；另启一个长驻 worker：

```sh
PYTHONPATH=backend/src python -m knowledge_worker
```

worker 使用本地缓存中的 `BAAI/bge-m3` revision；它不会在运行时联网下载模型。
OCR 模型由 PaddleOCR 首次加载时写入 `PADDLE_PDX_CACHE_HOME`，worker 会将
`ATHENA_KNOWLEDGE_OCR_CACHE_ROOT` 映射到该变量。API 和 worker 必须共享数据库、
模型 revision 和存储目录。解析子进程默认限制为 `ATHENA_KNOWLEDGE_PARSE_MEMORY_MB=4096`
MiB。Linux 使用 `RLIMIT_AS`；macOS 由父进程每 250 ms 通过 psutil 采样子进程 RSS 并在超限时终止，
无法读取 RSS 时会终止解析并返回明确错误。其他平台须提供 `resource.RLIMIT_AS`，否则解析
明确失败。超过 24 小时的
未引用原件和暂存文件由 worker 周期性孤儿扫描清理。关闭 worker 会让未完成任务在租约
到期后重新排队。

文件上传入口在 PostgreSQL 中共享 4 个并发 slot；每个请求的 ASGI body 上限为 22 MiB，
spool 阈值为 1 MiB，整个 slot 持有期限最长 600 秒。按同时占用估算，ASGI body 与 API
文件缓冲总量不超过约 168 MiB。API 会在系统临时目录和原件目录任一文件系统可用空间低于
`ATHENA_KNOWLEDGE_UPLOAD_MIN_FREE_BYTES` 时返回 503，默认水位为 384 MiB。上传 reservation
在幂等检查和 owner 配额预留之后才写原件；结果不确定时 reservation 继续占用配额，worker
先确认清理 fencing 和来源引用，再删除原件并释放预留。
