# 统一租约中心设计

## 目标

以 PostgreSQL 的 `execution_leases` 表作为执行权的唯一来源，统一 Worker Task、Root Run、Root Resume、Orchestration Outbox、知识文档和记忆作业的租约与 fencing token。业务仓储保留队列选择、业务状态、重试次数、退避时间、审批和取消规则。

首版以全新开发数据库结构为目标，不迁移历史租约字段。新增 Worker 只需定义稳定的资源类型和资源 ID，并通过同一租约中心领取、续租、校验、撤销和结束执行权。

## 租约身份和状态

资源身份是 `(resource_type, resource_id)`。当前资源类型为：

| 使用方 | resource_type | resource_id |
| --- | --- | --- |
| Worker Task | `worker_execution` | `execution_id` |
| Root start/resume | `root_execution` | `run_id` |
| Root Resume 队列 | `root_resume` | `job_id` |
| Orchestration Outbox | `orchestration_outbox` | `message_id` |
| 知识文档处理 | `document_ingestion` | `attachment_id` |
| 记忆处理 | `memory_processing` | `turn_id` |

业务作业入队时，PostgreSQL 在同一事务为对应资源预建租约记录。新记录状态为 `pending`，`owner_id`、`fencing_token` 和 `lease_until` 均为 `NULL`。这些入队触发器由 `PostgresLeaseCenter` 安装和维护，业务仓储不读写租约 ORM。

租约记录保存唯一 owner、单调递增的 fencing token、状态及 UTC 到期时间。首次成功授予时 token 从 `NULL` 变成 `1`，之后每次新授予都递增，包括同一 Worker 的重新领取。释放保留 token；业务工作最终完成时状态改为 `completed`，以便候选扫描跳过终态资源。数据库时钟是租约时间的唯一来源。

状态为 `pending`、`active`、`revoked`、`released`、`completed`。租约中心先枚举 `pending`、`released` 和已过期资源作为候选，业务仓储按现有锁顺序检查业务状态，再由中心用条件更新和 `RETURNING` 原子授予；只有成功修改行的 Worker 获得 grant。失败重试会释放租约，终态处理会标记完成，业务重排队时中心保留 token 并重新打开资源。撤销立即阻断续租和正常提交，但保留当前 owner、token 与到期时间，以便退出确认和取消收敛。退出确认必须匹配当前代次；新的授权产生后，旧执行不能确认或释放新租约。

## 接口与事务

领域定义不依赖 SQLAlchemy 的 `LeaseKey`、`LeaseGrant`、`ClaimedWork[T]` 和 `LeaseLostError`。`LeaseCenter` 提供 `acquire`、`renew`、`assert_current`、`finish`、`revoke` 和 `acknowledge_stopped`。PostgreSQL 实现支持在调用方现有事务内执行；单独事务只用于心跳等不涉及业务状态的续租。

队列仓储只接收租约中心分页返回的候选资源 ID，再在同一事务中锁定业务记录、请求中心 CAS 授权、更新业务状态并返回 `ClaimedWork`。分页会继续越过延迟重试、审批等待和 Root 挂起等当前不可执行的业务记录，避免较早的租约候选挡住后续任务。结果提交也在同一事务中验证 grant、更新业务结果并结束租约。租约校验成功后不再获取其它业务锁；多资源租约按 key 排序获取。除 `PostgresLeaseCenter` 外，应用仓储不得导入或查询 `ExecutionLeaseModel`。

公共 `LeaseGuard` 以 TTL 三分之一的间隔续租。续租失败、异常或超时会取消本地调用并阻止其正常结果写入。Root Resume 同时维护 Resume 作业和 Root Run 两个 grant。Root/Worker 继续使用 PostgreSQL Graph thread 独占锁，取得锁后再次校验租约；租约到期不表示旧进程已退出。

Root TTL 默认 60 秒，知识文档和记忆 TTL 默认 60 秒，其它调用默认 30 秒，均可配置。解析和分块等阻塞工作不得阻塞心跳。

## 六类使用方

- **Worker Task**：Job 与 Execution 共用 `worker_execution` 租约；接管和审批恢复获得新 token，但不改变业务 attempt。审批状态提交时结束当前租约。终态事件、工具操作及执行消息写入校验 grant；工具操作账本继续提供幂等性。
- **Root**：start 与 resume 使用同一 `root_execution` 租约。纯 fencing 用途的 Root `execution_generation` 改由 grant token 承担；attempt、消息执行代际和 `root_wait_generation` 保留。正常挂起释放租约；异常过期沿用失败收敛，不自动重放 Root。取消等待退出确认或到期后收敛。
- **Root Resume**：继续保留检查点身份和版本校验；租约归中心管理。检查点未就绪时原子安排重试并释放当前 grant。
- **Outbox**：继续保留待投递、已发布、attempt、退避时间和稳定 transition ID，移除租约状态。持久事件写入校验 grant；发布确认和租约结束在一个事务内完成，崩溃重放仍按 transition ID 去重。
- **记忆**：移除启动时把所有 `processing` 作业重新排队的逻辑，仅接管到期 grant。提取快照和 episode 写入校验 grant；事实、关系和向量在同一事务内校验并提交，通过稳定 operation key 保证幂等，不再持久化逐候选决策回执。人工管理使用独立的未绑定接口。
- **知识文档**：移除无租约恢复不了的 `running` 队列状态；过期可接管，达到既有最大尝试数后失败。删除、取消或失去 grant 后，旧执行不能发布解析结果或覆盖附件终态。

## 文档索引版本

每次文档执行创建 UUID `document_index_revision`，它是业务输出版本，与 fencing token 分开。分块、向量和 Neo4j 节点都属于该 revision；不同执行写入不同版本。

解析与各索引准备完成后，PostgreSQL 发布事务校验当前 grant、附件未删除及作业仍可完成，再原子更新附件当前 revision、作业结果并结束租约。关键词、向量和图检索只接纳当前有效 revision；图查询以批次校验候选后再计入结果上限，避免旧节点占满 top-k。发布前崩溃或过期的 revision 不可见，旧执行的迟到图写入只能落在自己的未发布 revision。Neo4j 保持可选；已启用的图索引失败时不发布本次 revision。

## 验收

使用隔离 PostgreSQL schema 覆盖并发领取、到期接管及旧 token 拒绝、跨表事务回滚、撤销和退出确认、Root/Worker thread 串行、审批恢复、Outbox 崩溃重放、记忆多实例恢复、文档旧 revision 迟到和删除竞争。索引发布验收覆盖 pgvector 与启用时的 Neo4j。Worker 执行详情仍显示 fencing token 和租约到期时间，但字段从租约中心关联读取。
