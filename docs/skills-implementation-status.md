# Skills 实施与验收状态

本轮复审修复的独立记录见
[Skills 精简实施报告](skills-simplification-implementation-report.md)；下文历史结果保持原样，
新报告只追加本轮证据，不覆盖旧记录。

复审口径更正：此前把共享桶测试包装器表述为“只枚举本测试 `created_keys`”并不准确。
底层列举 API 可以返回共享桶其他对象的元数据，但维护候选与允许删除的范围只包含本测试
`created_keys`；不读取其他对象正文，也不删除其他对象。该说明只更正边界描述，不改写
已有测试结果和历史时间线。

记录日期：2026-11-12。状态：Skills 精简已完成独立复审；后端 Reviewer 第 1 复审
PASS，前端、LLM 和 harness Reviewer 均 PASS，所有 finding 已关闭。Root 已接受本轮
精简代码与受控 E2E 范围。此前三轮复审、浏览器 fresh12 和两个 P2 记录保留在下方作为
历史证据，不能替代本轮最终状态。

## 已实现范围

管理员技能包导入/不可变版本/启停与依赖检查；Run 技能快照、自动及手动选择、渐进加载
和压缩协同；动态脚本审批及安全用户投影；Python 文件式 IPC、当前 asyncio
Process/PGID 清理、5 MiB 日志和非权威诊断；会话文件上传/读取/下载、多产物同事务
交付和聊天消息恢复；默认 dry-run 的停写窗口离线对象维护；管理和聊天前端。

本轮后端删除了跨重启进程身份、启动/周期遗留目录扫描、在线对象 GC、发布预留、
owner、持久 cursor、generation 和相关锁调用，并新增
`20261122_0023_remove_online_file_maintenance`。Office 校验改为标准库 `zipfile`，
不做解压；接受中央目录线性内存和 `ZipFile` 构造期间不可取消边界。

实施遵循管理员可信脚本边界，没有重新引入强制容器、禁网、脚本 CPU/内存/磁盘/时限/
次数限额，也没有增加产物数量限额。

## 本轮后端验证

以下命令均使用当前工作区；未对现有业务 dev 库升级 `0023`，未停止或迁移 `8000/5173`
旧服务，未 commit/push，也未打印连接密钥。

- 后端非集成，排除其他 agent 所有的 browser 与 LLM ChatCompletions：
  `781 passed, 3 skipped, 44 deselected`。定向文件/Office/Skill 测试另有一次
  `88 passed`。
- `test_run_tools_postgres.py`：`11 passed`。覆盖配额原子回滚、多产物同事务、
  幂等重放、UNKNOWN、session→quota 锁顺序、小连接池、离线维护三表引用保护，以及
  隔离 schema 中只执行 `0023 -> 0022 -> 0023`。
- `test_skills_files_e2e_postgres_minio.py`：`1 passed`。使用真实 PostgreSQL、MinIO、
  Python 子进程和可控 LLM；底层可列举共享桶元数据，但 `_RecordingStore` 只把本测试
  `created_keys` 交给维护器并只允许删除这些对象，不读取其他对象正文。
- 全量后端 integration：`44 passed`，无 skip。覆盖现有 PostgreSQL、Redis、知识库、
  Run/工具、Skills/MinIO 和隔离迁移链。
- `ruff check backend/src backend/tests`、严格 `mypy backend/src`（307 files）、
  `compileall`、`git diff --check` 均通过。
- 迁移链唯一 head 为 `20261122_0023`；隔离 schema 已验证完整升级、仅该 revision
  downgrade/upgrade。test 根 `public` 无 Alembic 表，验证没有在根 public 执行迁移。

后端 Reviewer 独立复跑维护/CLI `13 passed` 和 fake 分页回归 `1 passed` 后返回
PASS；前端、LLM 和 harness Reviewer 也均返回 PASS。Root 已接受本轮精简代码和受控
E2E 范围，所有本轮 finding 已关闭。

该范围验收不表示真实模型连续 3 次通过，也不表示已完成生产部署或外部模型全绿。
real live02 仍未满足三次门槛，`0023` 未应用到业务 dev；历史失败和未部署边界继续
按下方记录处理。

## 历史验收记录

以下章节保留各历史轮次的原始结论和时间线；其中 `CHANGES_REQUIRED`、未关闭 P2 或
“等待 Review”均为当时状态，不代表本轮最终结论。

### 此前基线与浏览器记录

以下是主体实现此前的完整测试记录。Reviewer 独立复跑当时 Office、文件服务、回收及存储扫描测试，63 项通过，并检查实际实现及 Diff；这不代表本轮新增浏览器 harness 已通过最终 Review。

- 后端非集成：841 passed，3 skipped，46 deselected。
- 真实 PostgreSQL 集成：46 passed，无 skip。
- 真实 PostgreSQL + MinIO + Python 子进程 + 可控测试 LLM 的 Skills 完整 HTTP 链路：1 passed（包含导入、启用、审批、双文件交付、消息分页、下载、下一 Run 引用及 GC）。
- 前端：251 tests passed；typecheck、build 通过；5 项管理导航/布局 Playwright 用例通过。
- Ruff、后端源代码严格 mypy、compileall 与 git diff --check 通过。
- 唯一迁移 head：`20261121_0022`。隔离 schema 已验证 `0022 → 0021 → 0022`。

测试通过不替代独立代码审查。当前未提交、未 push，未将本次迁移应用到现有业务数据库或部署新版服务。

上述“完整浏览器流程未验证”记录已被本轮执行结果更新：

- scripted 模式真实全栈浏览器套件已有 4/4 执行记录，最新为
  var/skills-e2e/scripted-terminal-fix-12，耗时约 41.5 秒并完成清理。使用真实 main.create_app、PostgreSQL、
  Redis、MinIO、RunWorker、Python 子进程和本机 HTTP/SSE ChatCompletions provider；
  不是外部真实 LLM。
- fresh12 覆盖终态 refetch、同 Run latest assistant sequence settle、刷新前 sequence
  与 transient 消失、刷新后持久消息恢复，以及旧交付缓存与 503 重试。
- frontend 报告 251 项 unit tests、typecheck 和 build 通过；Reviewer 独立复跑 37 项
  相关测试通过，但仍发现 failed/cancelled Run 的最终消息对账 P2。
- 最新 backend harness/provider 定向测试：9 passed；Ruff、mypy、compileall 和
  git diff --check 通过。
- 最新 harness 真实清理故障验证通过：SIGTERM、schema 创建后失败、Redis 启动后失败、
  provider/API 启动后失败均只清理本次 owned resources。
- 最终信号 handler 只记录 pending signal，创建函数完成 ownership 登记后再统一退出；
  长驻 Popen 不继承 signal mask。Popen 登记窗口、schema 提交后 dispose 故障、schema
  提交窗口和 Redis 创建窗口均通过真实 SIGTERM/故障注入验证。正常运行栈收到 SIGTERM
  后 provider/API 为 -15、Worker 正常退出。

脚本栈的精确覆盖、证据路径及与真实外部模型的区别见
[Skills 浏览器 E2E 实测报告](skills-browser-e2e-report.md)。

## 真实外部模型边界

真实 configured-default 只读克隆模型已有多次浏览器尝试，截至本轮仍未取得连续 3 次
全绿证据：

- 最新 live01 主链已通过，耗时约 45.5 秒，模型配置未修改；这仍不是连续 3 次通过，
  不能作为稳定的三次门槛证据。
- real01 完成到真实审批、三个产物下载、刷新、后续 Run 和 session_file_read 内容
  前置断言，但最后一项依赖 scripted provider 固定措辞，真实模型虽正确总结 42 仍导致
  断言失败。该尝试的截图和 trace 副本已丢失。
- real02 在第二次模型请求后以 llm_parallel_tool_calls_unsupported 失败；Worker 有
  2 次 HTTP 200，克隆配置 supports_parallel_tool_calls=false。现有证据不能区分
  upstream 多 call 与流式重组重复，根因保持 undetermined。
- live02 的脱敏结构诊断为两个独立 tool-call index、两个不同 ID，没有重复 index/ID；
  当前仍不满足连续 3 次通过门槛。Root 已停止重复尝试，并保持
  `parallel_tool_calls=false` 及现有并行策略不变。
- 两次尝试均未修改原 dev 模型配置，也未保留 API key、Cookie、模型 base URL、提示词
  或响应正文。

脱敏诊断位于 var/skills-e2e/real-model-diagnostics。

## 历史修复记录（本轮已替代相关机制）

### 历史 P1：Office ZIP 手工解析边界

旧实现曾从文件尾部手工解析 EOCD/ZIP64 和中央目录，并对成员数量、前置数据和峰值
内存做专项兼容。用户确认精简后，该机制已由标准库 `zipfile` 代替；旧测试与当时
Review 记录仅保留为历史，不再描述当前取消或内存保证。

### 历史 P2：回收游标 ABA 竞争

旧实现曾用持久 `after_key` 和单调 generation 做跨 Worker / CAS 扫描。用户确认精简后，
在线回收、owner、发布预留、cursor、generation 及相关锁均已删除；当前实现每次从桶
起点分页，并以停写确认和三表引用复核作为安全边界。

两项原 Findings 及继续修复中发现的 Office 兼容性回归在旧机制下均已关闭；本轮随后
按用户确认的简化方案删除回收游标，并把 Office 校验改为标准库 `zipfile`。上方“本轮
后端验证”和精简方案是实现现状，本节仅保留历史决策与测试证据。

## 流程与后续边界

以下内容均为历史轮次记录，用于保留问题演进；本轮最终复审状态以上方 PASS、Root
受控范围验收和真实模型未达三次门槛的边界为准。

此前三轮全量修复后按仓库规则停止并报告。用户明确授权继续修复后，复用了原 Implementer
与 Reviewer；本窗口经过两轮实施与复审，主体实现获得通过。本轮浏览器 harness 的新修改
frontend 已测试 fresh12 4/4，但第三轮 Review 仍为 CHANGES_REQUIRED 并已停止修改。

Backend 第三轮复审的未关闭 P2 位于
backend/tests/browser/harness.py 的 _wait_ready：启动等待期间没有检查 pending
termination，收到 SIGTERM 后可能等待至 readiness 截止时间（最多约 90 秒），并可能在
等待恢复后继续登录、写 manifest 或发布运行状态。该项未修复；backend 已按 AGENTS
三轮上限停止修改，最终处理由 Root 决策。

Frontend 第三轮 Review 的未关闭 P2 位于
frontend/src/features/chat/SessionChatPage.tsx 约第 400 行：failed/cancelled Run 仍接受
同一 Run 的任意 assistant 消息。若先有文件交付、随后最终流式回答失败或被取消，旧交付
可能被误当最终消息并清除 transient。Run 的失败/取消状态仍可见，但界面不会提示最终回答
尚未完成对账；backend 对 failed/cancelled Run 不保证存在最终 assistant 持久消息。
frontend 已按三轮上限停止修改。

代码目前保留在工作区。部署前需按[运行手册](skills-files-operations.md)配置文件存储与目标解释器、执行迁移并重启新版 API/Worker；本次验收不表示已部署或完成生产环境联调。

Linux 实机进程收敛、真实外部 LLM 全绿浏览器套件及多机硬崩恢复仍未完成验证。真实外部
模型已有两次受控实测，但结果不是全绿。当前不再保留跨重启身份或自动目录扫描；Worker
崩溃遗留临时目录由人工复核。孤立对象只在已确认全部写入者停止的维护窗口内，用默认
dry-run 的离线命令回收；共享测试桶底层可列举其他对象元数据，但维护候选和删除范围
只能包含本测试记录的 keys。
