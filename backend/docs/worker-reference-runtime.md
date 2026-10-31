# Worker v1 参考运行器

参考运行器位于 backend/src/reference_worker，仅处理确定性 JSON 回显和获授权资源复制。它不调用语言模型，也不接收可执行代码。

## 启动配置

运行 PYTHONPATH=backend/src python -m reference_worker，并设置以下环境变量：

| 变量 | 说明 |
| --- | --- |
| ATHENA_REFERENCE_WORKER_ORCHESTRATOR_URL | Athena HTTPS origin，例如 https://athena.example:8000 |
| ATHENA_REFERENCE_WORKER_PUBLIC_BASE_URL | Worker HTTPS origin，必须与注册中心允许的 Worker 地址一致 |
| ATHENA_REFERENCE_WORKER_SERVICE_TOKEN | Worker 调用注册、结果和资源接口的服务 Bearer token |
| ATHENA_REFERENCE_WORKER_ORCHESTRATOR_TOKEN | Worker 接收端验证 Athena Orchestrator 的独立 Bearer token |
| ATHENA_REFERENCE_WORKER_DATA_DIR | SQLite 和持久实例键目录；需要 Worker 重启后保留 |
| ATHENA_REFERENCE_WORKER_CAPABILITY_FILE | can_do、cannot_do、input_schema 和 output_schema JSON 文件 |
| ATHENA_REFERENCE_WORKER_TLS_CERTIFICATE | HTTPS 服务端证书 |
| ATHENA_REFERENCE_WORKER_TLS_PRIVATE_KEY | HTTPS 服务端私钥 |
| ATHENA_REFERENCE_WORKER_CA_CERTIFICATE | 校验 Athena 和资源服务端证书的 CA 文件 |

可选项为 HOST、PORT、MAX_CONCURRENCY、TIMEOUT_SECONDS、EXECUTION_DELAY_SECONDS、RESULT_RETRY_INITIAL_SECONDS 和 RESULT_RETRY_MAX_SECONDS，均使用 ATHENA_REFERENCE_WORKER_ 前缀。并行实例使用独立数据目录和端口。认证 token 和授权 token 不写入 SQLite、日志或错误消息。

服务启动时先用稳定的 worker_instance_key 和持久的 registration_request_id 登记。能力或地址配置变化会生成新的注册请求 ID；请求响应丢失时继续重放相同 ID 和 body。完成注册前 health 返回 not_ready 和 accepting_tasks=false。

SQLite 使用 WAL、FULL 同步以及事务内唯一键保存完整请求、首次收件回执、取消栅栏、执行状态、按 Execution 固定的契约 Schema 快照、结果和停止确认 outbox。结果和停止确认收到符合 v1 的成功回执后才标记已投递。HTTP 4xx 将项目隔离供排查；网络错误和 5xx 以有抖动的指数间隔永久重投。

可选输入字段 resource_reference 是标准 ResourceReference。运行器将使用 TaskCommand 的执行 Grant 读取该资源、核对 SHA-256，再用同一 Grant 上传新的内容寻址资源，并以 ResourceReference 作为输出。超时、暂态资源服务错误、权限拒绝、非法输入和实现内部错误分别使用 S1 的稳定 WorkerError 分类。这个动作只读取和复制字节，不共享本地路径。

审批暂停快照在 Worker SQLite 内校验恢复命令的 pause、代次、取消代次、审批请求、完整 item 集合和 arguments 摘要。S3 不提供额外的暂停调试路由；生产审批申请和用户决定流程在 S6 接通。

## 本地双进程验收

双进程验收由 `tests/integration/test_reference_worker_https.py` 提供。它在临时目录生成短期测试 CA 和 SAN 证书，启动真实中心 HTTPS 子进程和 `python -m reference_worker` Worker 子进程，并以 PostgreSQL 随机 schema 装配实际注册、资源、结果路由和健康监视器。测试覆盖注册重放、错误身份/输入/资源授权拒绝、重复收件、结果已提交但回执丢失后的本地 outbox 重启重投、健康下线摘除，以及通过授权资源 API 读取和复制字节。运行集成测试的 pytest 进程需设置专用 `ATHENA_TEST_DATABASE_URL`；这不是 Worker 运行配置。Worker 自身只需上方列出的 Athena HTTPS URL、Bearer token、TLS/CA 文件、能力文件和本地数据目录。
