# Worker S8 验收报告

## 范围

本轮验证展示所依赖的后端快照、空库初始化，以及真实 HTTPS Worker 的并发基线。测试使用随机 `worker_s8_<uuid>` PostgreSQL schema；结束时删除该 schema，不连接或改写项目正式数据。Worker 由独立 `reference_worker` 进程提供，中心和 Worker 均使用临时 CA/证书。

## 可复现命令

```bash
cd backend
ATHENA_TEST_DATABASE_URL='(本地测试配置)' \
  PYTHONPATH=src:.. ../.venv/bin/python scripts/worker_acceptance_s8.py \
  --tasks 100 --concurrency 8
```

脚本不会打印数据库 URL、凭据、执行 Grant 或 token。`--schema` 参数不存在，测试自行生成并清理隔离 namespace。成功后脚本从临时 JUnit 文件读取固定白名单并直接打印指标，例如：

```text
S8 measured metrics:
s8.concurrency=8
s8.elapsed_seconds=8.438
s8.max_checked_out_connections=10
s8.result_http_requests=100
s8.result_receipts=100
s8.sql_statements=12237
s8.task_http_requests=100
s8.tasks=100
s8.tasks_per_second=11.851
s8.terminal_backlog=0
s8.transactions=1803
s8.connection_checkouts=1803
s8.scanner_batch_size=4
s8.scanner_initial_due=12
s8.scanner_final_due=0
s8.scanner_handled=12
s8.scanner_batches=3
s8.scanner_elapsed_seconds=0.224670
s8.scanner_tasks_per_second=53.412
s8.scanner_repeat_handled=0
```

## 主场景

默认 100 个任务、调度并发 8。每个任务通过真实 HTTPS TaskCommand 到独立 Worker，再由 Worker 通过真实 HTTPS result 接口写回 PostgreSQL。测试注入了首个 Task `202 Accepted` 回执丢失。此次实际运行中，Worker 的结果先到达中心并完成幂等结算，因此没有产生额外的 Task HTTP 重投：Task 请求 100 次、Result 请求 100 次；这证明了“回执丢失但业务结果先到”路径，而不是证明每次丢回执都会重投。所有任务各有一个逻辑终态，结果回执数为 100，Root resume job 为 1，队列积压为 0。

最近一次独立运行（2026-10-31，本机 PostgreSQL、临时 TLS 证书、`100/8`）的实测值如下：测试用例自身计时 8.438 秒，吞吐 11.851 tasks/s；pytest 包络耗时 9.722 秒。Task HTTP 请求 100，Result HTTP 请求 100，结果回执 100，终态积压 0。限定在本测试 SQLAlchemy engine 的观测为 SQL statements 12,237、transactions 1,803、connection checkouts 1,803、最大同时 checkout 10。测试进程只使用随机 `worker_s8_<uuid>` schema，运行结束后已清理。

Root 可用 CLI `--tasks 100 --concurrency 8` 复验另一组独立样本；最近一组样本为 9.023 秒、11.083 tasks/s、Task/Result HTTP 100/100、SQL 12,632、transactions 1,858、最大同时 checkout 10。两组数值均保留，未用后一组替换前一组。

`terminal_backlog=0` 只表示本次 Plan 的 execute command 终态队列余额，不是 timeout scanner 的扫描积压。另一个独立 scanner 场景在随机 schema 中预置 12 个已到期 running Execution，以批量大小 4 调用生产 `WorkerTimeoutService`：扫描前 due backlog 为 12，三批处理 12 个 Execution 后降为 0；扫描耗时 0.224670 秒，吞吐 53.412 executions/s，重复扫描处理数为 0。该场景只验证到期候选的批处理收敛和幂等，不代表生产容量目标。CPU、数据库全局 `pg_stat`、网络带宽和生产 SLO 均未测量，不用其它业务流量推算。上述吞吐是当前开发机和测试配置的可复现基线，不是容量承诺。

## 既有证据与限制

S4-S7 已分别覆盖超时、重试、取消竞态、重复结果、Root 重启恢复和真实 LLM 逐轮闭环；本轮只复用这些证据，不重复实现完整故障矩阵。S8 负载是单机可复现基线；硬件、PostgreSQL 配置、证书生成和 Worker 延迟都会影响结果。浏览器页面验收使用 `scripts/worker_acceptance_s8_ui_server.py` 的确定性 fixture：它现在返回经生产 `RunOrchestrationSnapshotResponse` `model_validate` 的两轮 canonical 快照，展示重试 Execution、批量审批和有状态 decision，并提供 sessions/runs/timeline/approval 路由；但它不连接生产 PostgreSQL、不会启动真实 Root/Orchestrator/Worker，也不调用真实 LLM，因此不能作为生产全链路或真实前端数据一致性的证据。
