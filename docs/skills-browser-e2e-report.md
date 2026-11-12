# Skills 浏览器 E2E 实测报告

记录日期：2026-11-12。

历史记录状态：以下 Scripted、真实外部 LLM、三轮 Review 和停止修改结论，是
2026-11-12 当时测试与复审轮次的历史快照，不再代表当前工作区状态。历史证据与失败
结论继续保留；后续修复已经关闭“未关闭 Finding”中列出的两个 P2，但本报告没有重新
执行浏览器全栈或真实外部 LLM 套件，因此不能据此宣称这些套件当前全绿或整体 PASS。
当时 fresh12 scripted 套件 4/4 通过，耗时约 41.5 秒并完成清理；真实外部 LLM 两次
受控尝试未得到全绿套件。fresh12 只证明其覆盖路径通过，不代表全面验收。

后续状态补充：

- 后端 harness 的 `_wait_ready` 已在每轮读取 readiness 前、后以及全绿返回边界调用
  终止检查，并已有待处理 SIGINT/SIGTERM 与就绪竞态回归。
- frontend 的 failed/cancelled 终态已在旧交付对账前直接返回，不再把同 Run 的旧文件
  交付消息当作最终 assistant，并已有失败/取消保留临时回答的回归。
- 后续独立复审发现的 converter `tool_id=None` 或与 `modelName` 不一致时可能回退暴露
  Skill 原始详情，已另行按工具 ID 白名单和归一化 `modelName` 白名单/后缀修复；该项
  不属于本报告原有的两个历史 P2。
- 后续修复轮已实际运行 backend 定向 114 passed 和 frontend 262 passed；这些数字是
  阶段证据，不替代本报告未重跑的浏览器全栈和真实外部 LLM 测试。

## 环境边界

- 使用生产 main.create_app、正常 FastAPI lifespan、真实 PostgreSQL schema、Redis、
  MinIO、RunWorker、模型 Adapter 和 Python 子进程。
- scripted 模式使用本机真实 HTTP/SSE ChatCompletions provider。它覆盖真实协议和完整
  后端链路，但不是外部真实 LLM。
- configured-default 模式只读克隆现有默认模型连接配置到隔离 schema；不修改原
  public schema、dev 模型记录或凭据。
- 浏览器测试通过独立 Vite 服务访问后端，不使用 route.fulfill 伪造 API。
- 原有 8000/5173 服务及 public schema 保持不变。

## Scripted 真实全栈

最新完整执行证据为 var/skills-e2e/scripted-terminal-fix-12。Playwright 结果为 4/4
通过，耗时约 41.5 秒。该轮修复覆盖：

- 终态 refetch 检查 isError，不以失败查询替换已有终态。
- 按最新同一 Run 的 assistant sequence 确认后 settle，避免先断流导致 transient 残留。
- 刷新前断言持久 sequence 已存在且 transient 消失；刷新后从 API 持久消息恢复终态和
  文件引用。
- 旧交付缓存与 503 保留 transient 重试回归通过。

frontend 同时报告 251 项 unit tests、typecheck 和 build 通过。frontend 第三轮 Review
独立复跑 37 项相关测试通过，同时指出下述 SessionChatPage 终态识别 P2。因此 fresh12
是当前最新执行结果，但不替代独立 Review 结论。

| 场景 | 实测覆盖 | 结果 |
| --- | --- | --- |
| 主链路 | 管理员 ZIP 导入、启用、依赖检查、普通用户真实密码登录、Skill 选择、上传 CSV、动态审批、执行、xlsx/png/txt 三产物下载；终态 sequence settle 后刷新，并从 API 持久消息恢复，再继续后续 Run 读取选中摘要 | fresh12 通过 |
| 拒绝审批与取消 | 拒绝审批不发布产物；等待真实脚本 start marker 后 UI 取消，Run 终止、无产物、工作目录收敛 | 通过 |
| 版本与停用固定 | 待审批旧 Run 在导入 v2 或停用 Skill 后仍固定执行 v1 并完成 | 通过 |
| 失效选择 | 已选 Skill 失效后阻止新 Run，保留用户草稿 | 通过 |

后端 API 复核还确认：

- 主链路产生三个交付产物，后续 Run 的 session_file_read 返回 outcome=ok，内容包含
  2026-10 和 42。
- 取消 Run 为 cancelled，拒绝和取消均无交付产物。
- 版本固定与停用固定 Run 记录创建时的 v1 快照。
- skill_temp_entries=[]，真实执行临时目录已收敛。

证据索引：

- var/skills-e2e/scripted-terminal-fix-12/.last-run.json
- var/skills-e2e/scripted-terminal-fix-12 下的清理证据、场景截图和 trace
- 各场景截图和 trace 位于该目录对应的 Playwright 场景子目录。

## 真实外部 LLM

真实模型的两轮尝试均使用 configured-default 只读克隆，没有修改原 dev 配置。

### real01

- Worker 观察到 7 次上游 Chat Completions HTTP 200。
- 浏览器主链路已到达后续 Run，覆盖真实模型审批、三个产物下载、刷新恢复和
  session_file_read 的 outcome=ok、2026-10、42 前置断言。
- 套件未全绿：最后一次断言依赖 scripted provider 的固定措辞；真实模型实际已回答 42，
  该固定短语并不属于业务契约。
- 该次截图和 trace 副本已经丢失，当前不保有其文件。

### real02

- Worker 观察到 2 次上游 Chat Completions HTTP 200。
- Run run_4af523cdfb25440385274f22340dc3f5 最终为 failed，
  safe_error_code=llm_parallel_tool_calls_unsupported。
- 克隆配置中 supports_parallel_tool_calls=false 且未被修改；第一次 skill_load
  已完成。执行器在 Adapter 流式重组后检测到多个 completed tool calls，并在执行第二个
  调用前失败。
- 已有持久化日志没有保存原始 upstream fragment 序列，因此无法区分“上游返回多个独立
  call”与“流式重组重复”。根因记为 undetermined，不能据此判定供应商故障。
- 本轮没有为诊断重放真实请求。

脱敏诊断：

- var/skills-e2e/real-model-diagnostics/README.md
- var/skills-e2e/real-model-diagnostics/real01.json
- var/skills-e2e/real-model-diagnostics/real02.json

这些文件不包含 API key、Cookie、模型 base URL、提示词、响应正文、截图或 trace，也不
声称已丢失的证据仍然存在。

## 后端测试栈

本轮 harness/provider 定向回归结果：

    .venv/bin/python -m pytest -q backend/tests/browser/test_contract.py
    9 passed

    .venv/bin/ruff check backend/tests/browser
    All checks passed

    .venv/bin/mypy backend/tests/browser --ignore-missing-imports
    Success: no issues found in 8 source files

    .venv/bin/python -m compileall -q backend/tests/browser
    通过

    git diff --check
    通过

真实清理故障验证覆盖：

- harness 收到 SIGTERM 后进入受控清理，3/3 进程逐项停止，schema、bucket、Redis 均不
  存在。
- Popen 已创建、ownership 尚未登记时收到 SIGTERM：父进程先完成登记再退出，1/1 子进程
  以 -15 停止，全部 owned resources 清理。
- schema 提交成功后连接 dispose 故障：ownership 已在 dispose 前登记，schema 最终删除
  并独立确认不存在。
- schema 创建后故障、Redis 启动后故障、provider 启动后故障和 API 启动后故障均只清理
  本次 owned resources。
- 0/1/2/3 个实际进程路径的 processes_expected 与逐项 process_cleanup 一致。

## 独立清理核查

Root 独立读取 18 份 state manifest 的 resources 字段，并分别查询真实 PostgreSQL
to_regnamespace、MinIO bucket_exists 和 Docker 容器状态，结果：

- owned_schema_remaining=[]
- owned_buckets_remaining=[]
- owned_redis_remaining=[]
- 8011、4183、18001 均已关闭。
- 原有 8000、5173 保持 open，且此前 health 返回 HTTP 200。

核查只输出资源标识和存在性结果，没有打印 manifest 中的账号、密码或凭据。

## 历史停止状态

- frontend/backend 均已达到 AGENTS 三轮修复上限并停止代码修改。
- 两个未关闭 P2 尚待 Root 决定后续处理。
- Backend 第三轮 Review 仍为 CHANGES_REQUIRED；_wait_ready 的 pending TERM 延迟 P2
  未关闭，backend 已按三轮上限停止修改。
- 本报告没有将 scripted 4/4 等同于真实外部 LLM 全绿；真实模型套件仍为非全绿状态。

## 历史 Finding（后续已关闭）

以下条目保留当时的触发条件、影响和未关闭结论。它们描述的是历史复审快照，不代表
当前代码仍有这些问题。

### P2：_wait_ready 未检查 pending termination

位置：backend/tests/browser/harness.py 的 _wait_ready（约第 586 行）及其 main 调用点
（约第 985 行附近）。

触发条件：

- API、provider 或 Worker 启动等待尚未完成时，harness 收到 SIGINT/SIGTERM。
- 此时 handler 只记录 pending signal，但 _wait_ready 的内部循环没有检查该状态。

影响：

- 清理最多要等 readiness 截止时间约 90 秒后才发生。
- readiness 若在等待期间恢复，主流程仍可能继续登录、写 manifest 和发布运行状态，
  即使已经收到终止信号。

当时处理：

- 该项是 backend 第三轮复审发现的未关闭 P2。
- 按 AGENTS，backend 已达到三轮修复上限，本轮停止修改，不再启动测试栈。
- 最终处理由 Root 决定；本报告不宣称 backend harness PASS。

后续状态：已关闭。`_wait_ready` 在 readiness 探测前、每轮探测后及全绿返回前检查
pending termination，使终止信号优先于登录、manifest 发布和继续运行；对应回归覆盖
预先存在信号、探测期间信号和就绪竞态。本报告没有据此重新宣称浏览器全栈 PASS。

### P2：failed/cancelled Run 可能把旧交付消息误当最终 assistant

位置：frontend/src/features/chat/SessionChatPage.tsx 约第 400 行。

触发条件与影响：

- failed 或 cancelled Run 的 settle 逻辑仍接受同一 Run 的任意 assistant 消息。
- 如果该 Run 已经先持久化文件交付消息，随后流式最终输出失败或 Run 被取消，旧文件交付
  可能被误判为最终 assistant。
- 误判会清除 transient 状态，并且不提示最终回答尚未完成对账；Run 的 failed/cancelled
  状态仍可见，本条限制针对丢失的最终回答提示。
- backend 对 failed/cancelled Run 不保证必定存在最终 assistant 消息，因此不能只依赖
  “同 Run assistant 存在”判断安全 settle。

当时处理：

- 该项是 frontend 第三轮 Review 发现的未关闭 P2。
- frontend 已达到三轮修复上限，已停止代码修改。
- fresh12 的 4/4 覆盖的是已执行场景，没有覆盖该失败/取消交错，不能据此宣称全面 PASS。

后续状态：已关闭。`SessionChatPage` 对 failed/cancelled 终态只关闭流、刷新文件查询并
保留临时输出，不再使用同一 Run 的旧交付消息执行 settle；对应回归覆盖 failed 与
cancelled 两种终态。上述 frontend 262 passed 是后续阶段证据，本轮没有重跑浏览器
全栈套件。
