# Skills 精简方案（已实施，Reviewer PASS，Root 受控范围验收）

日期：2026-11-12。状态：用户已确认；后端 Implementer 已完成本文范围内的进程、对象
维护、Office 校验、迁移、测试和文档修改。后端 Reviewer 第 1 复审 PASS，前端、LLM
和 harness Reviewer 均 PASS，所有 finding 已关闭；Root 已接受本轮精简代码与受控
E2E 范围。

实施结果：

- 删除跨重启进程身份、Worker 启动/周期遗留目录扫描、在线 GC、发布预留、owner、
  cursor、generation 及相关锁调用；保留当前 `asyncio.Process`、PGID/`killpg`、
  Linux `/proc` 僵尸判断、5 MiB 日志和 `finally` 清理。
- 新增 `backend/src/cli/file_maintenance.py`：默认 dry-run，显式
  `--execute --confirm-all-writers-stopped` 才删除；每轮从桶起点分页，三表引用复核，
  无持久扫描状态。
- Office 改用 `zipfile`；先检查 `ZipInfo.orig_filename`，再检查
  `[Content_Types].xml` 和格式 root marker，不解压。
- 新增 `20261122_0023_remove_online_file_maintenance`，只在隔离 schema 验证完整 head
  和仅该 revision 的 downgrade/up；没有迁移现有业务 dev 库。
- 必要后端单元/集成、静态检查已通过；具体命令和结果见
  [Skills 实施与验收状态](skills-implementation-status.md)。

## 1. 决策摘要

执行顺序固定为：

1. 修复 failed/cancelled 终态对账导致 transient 文本丢失的问题，并关闭浏览器 harness 的 pending signal 问题。
2. 为真实 LLM 的工具调用流增加脱敏结构诊断，完成稳定的 scripted 与真实模型端到端验收。
3. 基础简化：删除跨重启进程身份推断、在线周期对象 GC 与持久游标/发布预留，简化 Office ZIP 校验。
4. 更新测试、运行手册和状态文档，再由 Reviewer 独立审查实际 Diff。

本方案不改变 Skills 的产品形态，不扩大正式兼容范围，不新增检查容器、脚本资源限额、文件删除 API、软删除、在线 GC 锁或代次机制。

## 2. 已核实事实

| 项目 | 当前事实 | 提案含义 |
| --- | --- | --- |
| failed/cancelled 终态 | `frontend/src/features/chat/SessionChatPage.tsx` 仍接受同一 Run 的任意 assistant 消息作为 settle 依据。交付文件消息本身也是 completed assistant，可能被误当最终回答并清除 transient。 | 这是优先修复项；不能把“存在 assistant”视为最终回答证据。 |
| 真实 LLM 工具调用 | `llm_parallel_tool_calls_unsupported` 抛于 `backend/src/app/run/executor.py`，发生在 Adapter 重组完成之后、工具批次落库之前。Adapter 已按配置传入 `parallel_tool_calls=false`。 | 根因仍未知，不直接开启并行、不静默串行、不靠重试掩盖。先保留足够区分上游多调用与重组重复的结构信息。 |
| 脚本子进程 | `backend/src/adapter/skill/execution.py` 使用 `asyncio.create_subprocess_exec`、`start_new_session=True`，通过当前 `asyncio.subprocess.Process`、PGID、`killpg` 和直接进程回收清理。 | 复用现有执行器，不重写为同步 `Popen`，不改当前进程组治理。 |
| 跨重启身份 | 当前另有进程身份文件、Linux procfs boot/PID namespace、macOS libproc 和跨进程目录扫描；该身份逻辑也供对象发布 owner 使用。 | 删除强身份、自动推断及关联发布 owner，只保留明确非权威的本地诊断记录；保留当前进程组内的 `/proc` 僵尸判断。 |
| 对象回收 | 当前 Worker 启动及周期性运行在线 GC，并有 `file_object_publications`、持久游标和 generation。 | 退为停写窗口中的离线维护命令；删除在线任务、发布预留、游标和 generation。 |
| Office 校验 | `backend/src/app/file/service.py` 自行解析 EOCD、ZIP64 和中央目录，并测试了低于 2 MiB 的峰值内存。 | 改用标准库 `zipfile` 检查结构 marker 和成员路径。`ZipFile` 会载入中央目录，不能宣称恒定内存。 |
| 浏览器证据 | `docs/skills-browser-e2e-report.md` 记录 scripted 4/4 通过，但前后端 Review 仍为 `CHANGES_REQUIRED`；`_wait_ready` 和 failed/cancelled settle 是两个未关闭 P2。real01 的失败依赖非业务固定措辞，real02 的真实工具调用根因未定。 | 现有证据只作为基线，不构成最终 PASS，也不能据此宣称真实模型链路稳定。 |

## 3. 优先级 P0：终态与真实模型稳定性

### 3.1 failed/cancelled 的 transient 对账

目标行为：

- failed 或 cancelled 不得使用“同一 Run 存在 assistant 消息”来决定最终回答已完成。
- 如果界面已有未持久化 transient 文本，Run 进入 failed/cancelled 后将其标记为“未完成回答，仅保留在当前会话”，保留在页面中；刷新后不承诺恢复该文本。
- 已持久化的文件交付消息仍可展示，但不能触发 transient settle，也不能被描述为最终输出。
- 消息重新读取失败时继续保留草稿和重试入口，不用旧交付消息替换当前回答。
- 不新增 Run 事件类型，不把 transient 写入数据库。

completed 的对账建议：

- 直接使用现有 `RunCompletedPayload.assistant_message_id` 指向的权威消息作为最终 assistant；该字段已在当前事件协议中存在。
- 只为当前需要对账的 Run 获取消息页和事件页，不做 Session 全局历史扫描。
- 消息从最新页开始，通过 `before_sequence` 向更早页逐页查找；事件也使用现有分页接口补齐，直到找到完成事件或确认耗尽。
- 失败时保留草稿、提示并允许重试；消息或事件任一分页耗尽后明确显示“未找到完成事件所指的最终消息”，不得回退到 latest assistant。
- 只有在目标消息已从权威消息页读到后，才清除 transient 并标记 settle，不新增后端事件协议。

主要改动边界：

- `frontend/src/features/chat/SessionChatPage.tsx`：拆分 live/waiting/terminal 对账，区分 completed 与 failed/cancelled。
- `frontend/src/features/chat/api.ts`、`runStream.ts`：复用现有消息和事件读取能力，补充可验证的终态动作，不改变协议。
- `frontend/src/features/chat/sessionWorkspace.test.tsx`、`runStream.test.tsx`：覆盖“先交付文件、后失败/取消”、503、旧缓存和 completed 精确消息 ID。

### 3.2 真实 LLM 工具调用诊断

先在 `backend/src/adapter/llm/openai_chat_completions.py` 增加请求内、聚合后的结构诊断，不改调用语义：

- 记录 chunk 序号、choice index、finish reason、工具分片数组位置、每个分片的 tool call `index`、是否出现 id/name、id 的 SHA-256 短摘要、id 是否改变、name 的哈希、参数字节数和开始/完成状态。
- 不保存提示词、响应正文、完整 call id、完整工具参数、凭据、base URL 或原始 upstream payload。
- 每请求输出一条有界摘要或拒绝诊断，避免逐 chunk 写日志造成日志放大；用摘要区分“不同 index/id 的两个调用”与“同 index/id 被重复累计”。
- 诊断不能携带工具身份敏感字段，也不进入普通用户 API。可沿用现有应用日志边界，不新增事件协议或业务表。
- 合成流测试必须覆盖：同 index 多段拼接为一个 call、不同 index 形成两个 call、重复 id 被拒绝、超限早停，以及日志不含正文和完整 id。

根因确认前不改以下行为：

- 不把 `supports_parallel_tool_calls=false` 改成 true。
- 不静默只执行第一个调用，不把多个调用自动串行化。
- 不把 `llm_parallel_tool_calls_unsupported` 当作可重试错误。
- 若诊断证明供应商忽略请求参数，再由 Root 决定产品策略；若证明是重组器缺陷，再针对重组逻辑修复并重新 Review。

### 3.3 浏览器 harness 与端到端门槛

`backend/tests/browser/harness.py` 的 `_wait_ready` 只做小修：

- 等待循环每轮检查 pending termination；HTTP readiness 返回后、函数返回前再次检查。
- readiness 与终止信号同时出现时终止优先，不得继续登录、写 manifest 或执行场景。
- 子进程 HTTP 请求已有 1 秒超时，不引入新生命周期框架。
- 测试覆盖 wait 前收到 SIGINT/SIGTERM、wait 中收到信号、信号与 readiness 竞态以及 owned resource 清理。

端到端验收：

- scripted 全栈必须增加 failed/cancelled 交错场景，并继续覆盖审批、取消、版本固定、文件交付、刷新、后续 Run 读取和资源清理。
- 真实模型断言只检查业务契约：Run 终态、持久消息存在性、文件数量/下载/内容读取和错误码，不依赖某供应商固定措辞。
- 首期建议以当前默认模型配置的主链路连续 3 次通过为验收门槛。当前已尝试但尚未取得连续 3 次通过；该结果只用于当前版本回归，不是统计意义上的长期稳定性证明。
- 一次成功不能证明稳定，一次失败也不能替代根因证据；扩大 provider/model 矩阵需另行决定。

## 4. 优先级 P1：进程与工作目录简化

保留：

- 当前 Worker 持有的 `asyncio.subprocess.Process`、独立工作目录、`start_new_session=True` 和 PGID 清理。
- 当前执行会话内通过 `killpg` 收敛组内进程，直接子进程仍在时用现有 Process 回退终止并等待回收。
- `_process_group_has_live_members` 枚举当前 Linux 进程组并排除僵尸；该 `/proc` 用法属于当前执行治理，不能与跨重启身份推断一起删除。
- stdout/stderr 合计 5 MiB 保留、超限后继续排空、独立目录 `finally` 清理和清理错误码。
- Worker 清理失败时保留目录并收敛为 UNKNOWN/失败审计；文件可能已产生但无法确认时不得自动重跑。
- 保留一个简单、非权威的本地诊断记录，使用现有结构化日志，并可在本次独立工作目录写入小型 JSON。建议字段仅为：Run ID、call ID、Worker 启动 UUID、记录时的 PID/PGID、应用记录的启动墙钟时间和工作目录。

删除：

- 删除跨重启的 PID/PGID owner、OS 精确 starttime、boot ID、PID namespace、libproc/procfs 身份核验。
- 删除 `SKILL_TEMP_STALE_SECONDS` 驱动的启动/周期遗留目录扫描，不再按数据库租约、年龄和身份自动删除重启遗留目录。
- 上述日志或 JSON 只服务人工排查，不是进程身份或存活证明。不得仅凭其中 PID/PGID 发信号，也不得把缺失诊断解释为脚本已结束。
- 脚本可能在 `spawn` 成功、诊断记录写入前遭遇崩溃，此时诊断信息可能缺失。人工必须使用宿主系统工具复核；无法确认时保留目录，必要时由管理员在受控停写维护窗口清理和处置。不承诺跨宿主自动保证。
- 删除身份与生命周期锁只为遗留扫描服务的职责。当前执行仍在同一上下文内完成进程、管道、结果上传和目录清理。

主要联动文件：

- 删除 `backend/src/infrastructure/skill/process_identity.py`。
- 删除 `backend/src/adapter/file/publication_identity.py`。
- 精简 `backend/src/domain/skill/execution.py` 中强身份类型、精确身份核验端口和只为遗留扫描服务的锁常量；可保留诊断字段的类型边界。
- 精简 `backend/src/adapter/skill/execution.py` 的精确身份核验与生命周期锁；保留进程组、僵尸判断、日志、清理，并记录无权威性的本地诊断字段。
- 精简 `backend/src/app/skill/execution_service.py` 的身份 verifier、stale scan 参数和方法；保留脚本准备、上传、结果收敛。
- 删除 `SkillRepository.list_active_script_call_ids` 及对应 adapter 实现。
- 删除 `backend/src/bootstrap/run_worker.py`、`backend/src/run_worker.py` 中的启动扫描和周期维护装配。

## 5. 优先级 P2：对象 GC 退为离线维护

### 5.1 保留的数据一致性

- 仍先写对象，再在业务事务中提交 `session_files`、`skill_versions`、`skill_executions.log_object_key` 等引用。
- 多产物仍与工具调用终态、结果消息和普通文件交付消息同事务发布。
- Run 租约、epoch、取消、授权和失败补偿保持现有原子边界。
- DB commit 回包丢失必须按幂等键或来源调用重查。无法确认是否已发布时保留对象，不能因捕获到异常就直接删除。
- 明确未发布的失败补偿可以尝试删除已知对象；删除失败或状态不明留待离线维护。

### 5.2 删除在线机制

- 删除 Worker 启动和周期执行的 `OrphanObjectReclaimer`。
- 删除 `file_object_publications` 发布预留、owner identity 查询、引用 advisory lock 的在线 GC 配对逻辑。
- 删除 `file_object_reclamation_cursors`、`generation`、跨 Worker CAS 和持久扫描位置。
- 删除 `ATHENA_FILE_MAINTENANCE_INTERVAL_SECONDS` 和 Worker 在线 GC 配置；保留或迁移对象保留期配置到离线命令参数。
- 不增加新的维护状态表、分布式租约、扫描代次或在线锁。

### 5.3 离线维护命令

已新增独立维护命令 `backend/src/cli/file_maintenance.py`。行为如下：

1. 默认 dry-run，只列举候选、引用命中、预计删除数和失败数，绝不删除对象。
2. 显式传入 `--execute` 和“全部写入者已停止”确认参数后才允许删除；确认只是操作者承诺，不能伪装成系统已验证前提。
3. 操作者必须先停止所有 API、Run Worker、管理写入脚本、迁移写入者及其他普通业务写入者，并确认没有遗留脚本继续写入；维护窗口内只允许当前一个离线维护进程读写目标对象存储。自动检查无法证明该前提，因此不新增“维护状态”来制造虚假保证。
4. 单次命令内分页列举对象，从该次命令的起点开始，不使用数据库持久游标；中断后下次从头 dry-run，可接受重复检查。
5. 只扫描平台合法对象前缀，先按对象时间宽限期过滤，再逐对象复核全部数据库对象引用；引用查询失败时该对象保留并报告。
6. 删除按小批执行，每批失败独立计数。dry-run 和 execute 都输出稳定汇总，不输出凭据、对象 URL 或文件正文。

当前默认宽限期为 86,400 秒、每批删除 100 个对象；两者可通过命令参数覆盖。

### 5.4 引用面

离线命令在停写窗口内必须检查：

- `session_files.object_key` 的全部行，包括文件不可见会话中的历史行；只要数据库行存在即保护对象。
- `skill_versions.object_key` 的全部行，包括旧版本、非当前版本、停用 Skill 下版本和可能被称为“已删除”的历史版本记录；现有代码没有文件删除 API，本提案不新增软删除。
- `skill_executions.log_object_key` 的全部非空行。

当前实现位于 `backend/src/adapter/file/repository.py` 的
`SqlAlchemySessionFileRepository.is_object_key_referenced`，查询三类数据库事实，
不保留在线 advisory lock、Cursor 或 owner 存活判断，不能只查 `session_files`。

### 5.5 生命周期限制

- 不得对 `skills/`、正式 `session-files/`、正式 `session-artifacts/` 或 `skill-logs/` 配置仅按前缀年龄删除的对象存储 lifecycle。
- 只有未来明确引入专用临时上传前缀或不含正式引用的临时分片时，lifecycle 才可以作用于该临时范围；本轮不新增这类上传路径。
- 在线先年龄后查引用的删除存在 TOCTOU，不能作为“安全 GC”保留。离线安全只以停写为前提。
- 没有在线 GC 后，崩溃/失败遗留对象会累积到下一次停写维护；这是接受的运维成本，不承诺自动容量收敛。

## 6. 优先级 P3：Office ZIP 校验简化

`backend/src/app/file/service.py` 保留 `validate_file_type` 的扩展名、声明 MIME 和内容交叉校验，但将 Office 分支改为：

- 使用标准库 `zipfile.ZipFile` 打开文件。
- 先读取 `ZipInfo.orig_filename`，拒绝 NUL、控制字符、反斜杠、绝对路径、空段和目录穿越；路径检查完成后才检查必要 marker：`[Content_Types].xml` 和 xlsx/docx/pptx 各自的 root marker。
- 不把项解压到磁盘，不读取正文，不手写二进制 EOCD/ZIP64 扫描，不自行解析中央目录，不调用 `testzip` 验证所有 CRC。
- 删除 `_ZIP_*` 手写结构、`_OfficeZipFormatError`、EOCD/ZIP64 定位及中央目录手工扫描实现。

接受的边界：

- `ZipFile()` 会载入 ZIP 中央目录，其内存随成员数量和元数据线性增长，不是恒定内存。删除旧测试中的“低于 2 MiB 峰值”承诺，改为验证真实 Office 文件和坏结构行为。
- 用户上传仍单文件最多 50 MiB；脚本生成产物不沿用该上限，不能借简化新增生成文件限额。
- `ZipFile()` 构造期间没有取消注入点。后台线程收到取消后要等中央目录构造完成，再在 marker/成员检查边界退出；之前不得发布对象。
- API 上传的外层异步取消会设置线程停止标记并等待检查线程退出，随后 `staged_upload` 删除临时文件。脚本产物取消时先等待检查线程退出、停止发布，再由 runner 上下文退出延后清理工作目录。
- 大量中央目录导致的内存和取消延迟被接受为本轮风险，不通过容器、正文解压、新限额或手写 EOCD 解决。

测试调整在 `backend/tests/test_office_zip_validation.py`：保留真实 xlsx/docx/pptx、坏 ZIP、缺 marker、危险成员路径和取消不发布；删除 ZIP64 前置数据、locator offset 和恒定内存测试；增加“构造完成后取消很快退出”和标准库兼容性验证。

## 7. 文件与代码依赖清单

下表列出已确认的改动范围；后端 Implementer 已完成本行范围内的文件，前端、LLM
诊断和 harness 由对应 agent 负责。

| 范围 | 文件/模块 | 动作 |
| --- | --- | --- |
| 终态对账 | `frontend/src/features/chat/SessionChatPage.tsx`、`api.ts`、`runStream.ts` 及测试 | 精确 completed 对账；failed/cancelled 保留未完成草稿；旧交付不 settle |
| LLM 诊断 | `backend/src/adapter/llm/openai_chat_completions.py`、`backend/tests/test_llm_chat_completions.py` 等 | 增加脱敏结构摘要与合成流测试；不改变并行语义 |
| Harness | `backend/tests/browser/harness.py`、`backend/tests/browser/test_contract.py` | 小修 pending signal 轮询与竞态测试 |
| 端到端 | `frontend/e2e/skills.live.spec.ts`、fixtures/provider、报告 | 增加 failed/cancelled 交错；真实模型只断言业务契约 |
| 进程身份 | `backend/src/infrastructure/skill/process_identity.py`、`backend/src/adapter/file/publication_identity.py` | 删除跨重启身份模块 |
| 进程执行 | `backend/src/domain/skill/execution.py`、`backend/src/adapter/skill/execution.py` | 删身份/遗留锁；保留 `create_subprocess_exec`、PGID、当前组 `/proc` 僵尸判断、5 MiB 日志、finally |
| 进程编排 | `backend/src/app/skill/execution_service.py`、`backend/src/domain/skill/ports.py`、`backend/src/adapter/skill/repository.py` | 删 stale scan、verifier、活动调用列举 |
| Worker 生命周期 | `backend/src/bootstrap/run_worker.py`、`backend/src/run_worker.py` | 删除启动遗留扫描、在线 GC、周期维护 |
| GC 用例 | `backend/src/app/file/reclamation.py`、新维护入口 | 改为 dry-run 默认、显式 execute 的离线命令，无持久游标 |
| 文件领域 | `backend/src/domain/file/model.py`、`ports.py`、`backend/src/domain/file/__init__.py` | 删除 owner、cursor、generation、在线 guard；保留文件授权/配额/存储契约 |
| 文件适配器 | `backend/src/adapter/file/repository.py`、`storage.py`、`locking.py`、`model/publication.py`、`model/reclamation_cursor.py` | 删除发布预留、在线锁和游标；保留分页列举、输出白名单和三表引用查询能力 |
| 写路径 | `backend/src/app/file/service.py`、`backend/src/app/skill/service.py`、`backend/src/app/skill/execution_service.py`、`backend/src/adapter/run/repository/run.py` | 去除预留/consume 调用；保留 DB 引用原子提交、幂等重查和 UNKNOWN 保留 |
| ORM 注册 | `backend/src/adapter/models.py`、`backend/src/adapter/file/model/__init__.py`、`backend/src/adapter/file/__init__.py` | 删除两张废弃表模型及其导出 |
| Office | `backend/src/app/file/service.py`、`backend/tests/test_office_zip_validation.py` | 标准库 `ZipFile`；只查 marker/路径；声明非恒定内存 |
| 回归测试 | `test_skill_process_identity.py`、`test_file_reclamation.py`、`test_skill_files.py`、`test_run_worker.py`、集成测试 | 删除已撤销机制测试；增加离线命令、重启不清理、当前 PGID 清理和三引用保护 |

保留且不复用简化名义改动的关键链路包括：管理员权限、Run 固定 Skill 版本、安全审批快照、工具结果安全投影、文件 owner/session 授权、会话 100 文件配额、上传 50 MiB、MIME 与内容校验、attachment/nosniff 下载、脚本 manifest、多产物同事务发布、5 MiB 日志和失败/UNKNOWN 语义。

## 8. 迁移与部署边界

- 不修改已存在的 `0018`、`0019`、`0020`、`0021`、`0022` 迁移链。
- 已追加 `20261122_0023_remove_online_file_maintenance`，移除
  `file_object_publications` 和 `file_object_reclamation_cursors`，即一并移除 owner
  身份字段、持久游标和 generation。其他业务表没有需要为本次简化新增的兼容字段。
- 不提供面向生产旧数据的状态转换、owner 补齐或 cursor 迁移。旧预留/游标数据不属于新兼容目标。
- 应用启动、本维护命令和普通测试不得自动 downgrade、drop 表或删除数据。清理迁移只能由后续授权的维护窗口显式执行。
- 强身份、发布 owner、在线 GC、相关调用点、测试与清理迁移必须作为同一个可运行变更交付和验证；不得先部署删除模块或协议而保留悬空调用，也不得分阶段制造不可运行状态。
- 建议顺序：停止所有写入者并确认脚本不再写入 → 备份数据库与对象存储 → dry-run → 显式执行离线回收 → 执行清理迁移 → dry-run/验证引用 → 重启新版 API/Worker。
- 本轮没有迁移现有业务 dev 库、没有停止或重启 `8000/5173`、没有删除业务对象；
  `0023` 只在隔离 schema 往返验证。部署时仍须由 Root 按维护窗口协调。

`docs/skills-design.md`、`docs/skills-implementation-plan.md`、
`docs/skills-files-operations.md` 和本文件已同步当前精简实现；状态文档记录实际验证
结果。Root 已接受本轮实现及受控 E2E 范围，但这不等于生产部署或外部模型全绿。

## 9. 可观测验收与失败场景

| 场景 | 必须观察到的结果 |
| --- | --- |
| 文件交付后 Run failed | 文件仍可见；transient 不丢失，标记为未完成且不称最终回答；刷新后不承诺 transient |
| 文件交付后 Run cancelled | 同上；不把交付消息用于 settle |
| completed 且最终消息迟到 | 等待与 `assistant_message_id` 匹配的权威消息后 settle；refetch 失败时保留草稿和重试 |
| completed 只有旧文件交付消息 | 不 settle，不清 transient，继续对账 |
| completed 消息/事件分页耗尽 | 明确提示完成事件所指的最终消息未找到；保留草稿和重试，不回退 latest assistant |
| 真实模型多个工具调用 | 诊断能区分相同 index/id 的续片与不同 index/id 的独立调用，且不含正文/完整 id/参数 |
| 并行能力关闭但观察到独立调用 | Run 保持稳定失败，不自动重试、不串行执行；等待 Root 依据诊断决定修复 |
| harness readiness 等待中收到 TERM | 不登录、不写 manifest、不发布运行状态；owned 资源完成清理 |
| 当前 Worker 取消脚本 | 终止当前 PGID、回收直接子进程、排空日志、finally 清理目录并记录稳定错误码 |
| 清理失败或 Worker 崩溃后重启 | 不使用诊断 JSON 中的 PID/PGID 杀进程；目录保留，状态为 UNKNOWN/人工确认 |
| 离线 GC dry-run | 删除数为 0；列出年龄合格、数据库未引用且格式合法的预计删除对象 |
| 离线 GC execute | 仅删除宽限期外且三处 DB 引用均缺失的对象；遇到引用查询失败不删除该项 |
| 离线命令中断 | 无持久 cursor；下次从头 dry-run，不依赖 generation/CAS |
| Office 标准文件 | 先按 `ZipInfo.orig_filename` 拒绝危险路径，再检查 marker；坏 ZIP、缺 marker、穿越路径失败 |
| Office 取消 | 构造完成后停止 marker/成员扫描；不发布对象，线程退出后临时文件/工作目录收敛 |
| 疑似已提交但回包丢失 | 先按幂等键/调用来源重查；确认未发布前不删潜在引用对象 |

实施测试至少包括相关单元、PostgreSQL/MinIO 集成、前端单元/typecheck/build、scripted 全栈和真实模型全栈。测试必须报告实际命令与结果；未运行的测试、环境缺失或真实模型非全绿均不能写成通过。

## 10. 已确认取舍与验收状态

已确认并落实的核心取舍：

1. 对象回收只在全部写入者停止的维护窗口离线执行，没有在线自动回收、持久 cursor、
   generation 或分布式维护锁。
2. Worker 崩溃后的进程与目录保守保留，由管理员结合宿主工具人工复核；不建设跨重启/
   跨宿主自动强清理，诊断 JSON 不用于自动发信号。
3. Office 使用标准库 `zipfile`，接受中央目录线性内存和 `ZipFile` 构造期间不可取消
   边界；不解压，不增加产物数量或文件大小限额。

后端已完成：

- 删除跨重启身份、残留扫描、在线 GC、发布预留、owner、cursor、generation 及全部
  相关调用；保留当前 asyncio Process/PGID、Linux `/proc` 僵尸判断、5 MiB 日志和
  `finally` 清理。
- 新增默认 dry-run 的离线维护命令和三表引用复核；execute 必须显式确认停写，每次从
  桶起点开始。
- 新增 `0023` 清理迁移，并在隔离 schema 验证完整 head 与仅该 revision 的
  downgrade/up。
- 完成后端单元、相关 PostgreSQL/MinIO 集成、Ruff、mypy、compileall 与 diff 检查。

复审和 Root 决策已完成：

- 后端 Reviewer 第 1 复审 PASS；独立维护/CLI `13 passed`、fake 分页回归 `1 passed`，
  所有 finding 已关闭。
- 前端终态对账、LLM 流诊断和 backend harness Reviewer 均 PASS。Root 接受本轮精简
  代码与受控 E2E 范围；历史 `CHANGES_REQUIRED` 和失败记录保留作为演进证据。
- 真实模型连续 3 次通过门槛仍未达到，因此不得宣称生产环境或外部模型全绿。
- 部署时仍需停写、备份、dry-run、显式 execute、迁移和重启协调；当前没有迁移业务
  dev 库，也没有停止现有 `8000/5173`，本轮验收不等于已部署。
