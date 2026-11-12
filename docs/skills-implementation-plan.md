# Skills 实施计划

日期：2026-11-12。状态：用户已确认
[Skills 精简方案](skills-simplification-plan.md)；后端相应用途已在当前工作树实施并通过
隔离 PostgreSQL/MinIO 验证，独立 Reviewer 与 Root 最终验收尚未完成。

依据：[Skills 功能设计](skills-design.md)。设计中的产品规则和用户后续修订优先；本文将其拆成可交给 Implementer 的任务，新增字段、方法名和模块拆分均为实施建议。本文不以“开始整理实施文档”推定已经授权编码。

## 1. 实施目标与边界

完成管理员 ZIP 安装与版本管理、Agent 自动/用户指定技能、Run 创建时版本固定、渐进加载、可信 Python 脚本执行、动态审批、会话文件和多文件产物交付的完整闭环。

必须沿用以下边界：

- 仅管理员安装与维护技能；默认不要求脚本审批，按技能配置后每次执行单独审批。
- 不建设强制容器/隔离服务，不增加脚本 CPU、内存、进程、临时磁盘、单次运行时间和执行次数限额。脚本以平台账号权限执行；独立工作目录不是沙箱。
- Agent 只执行包内登记脚本，不修改脚本、不生成临时 Python、不通过接口安装依赖。技能内容版本不可变，执行目录副本修改不回写版本。
- 保留既有 Run 截止时间、取消、租约及模型请求预算。5 MiB 日志收集上限、上传大小、会话文件数和包校验规则不等同于脚本资源限额。
- 运行中技能不因管理员更新/停用改变；用户权限、会话和文件授权仍实时复核。
- 多文件发布、调用完成和结果消息同事务；结果未知不静默重跑。

不增加用户私人技能、在线脚本编辑、其他执行语言、技能互调机制、市场/Git 同步或兼容框架。禁止修改只读参考仓库 `/Users/udouble/Documents/code/Athena`。

## 2. 代码基线与现状

下表保留 2026-11-12 实施前的基线核对，用于说明接入位置。Skills、会话文件、脚本执行
和对象维护代码已在用户确认后加入当前未提交工作树；本节不再表示这些能力缺失。

| 现有位置 | 实际接入与差距 |
| --- | --- |
| `backend/src/adapter/run/repository/run.py` | submit/retry 已绑定提示词版本；initialize_tools 在执行时冻结工具。技能目录必须另在创建事务固定。finish_tool_call 当前只完成结果/状态，未原子发布文件。 |
| `backend/src/app/run/executor.py` | `_submit_and_execute_batch` 静态读取 definition.requires_approval；`_validate_business_call` 没有 Run 参数；`_execute_tool` 对所有工具应用通用 tool_timeout；结果草稿只有 output。 |
| `backend/src/app/tool/ports.py`、`runtime.py` | 应用层工具运行时端口与实现；validate_call 是无 Run 上下文的同步校验，execute 传入工具及 Run 剩余时间。 |
| `backend/src/app/run/context.py`、`compression.py` | 主系统提示词在历史前；压缩已有基础预算，但触发阈值基于总窗口。需加入不可压缩技能上下文并统一计算。 |
| `backend/src/domain/message/model/part.py` | FileRefPart 已存在；ToolResultPart 仅单个 artifact_id，没有多文件集合。 |
| `backend/src/app/message/service.py` | 校验引用格式，不代表已完成文件归属授权。 |
| `backend/src/adapter/message/converter/message.py`、`api/dto/message.py`、`api/converter/message.py` | 消息持久化和 API 序列化边界，需贯通 artifacts。 |
| `backend/src/domain/tool/model/execution.py`、`api/dto/run.py`、`api/converter/run.py` | 审批与调用详情数据结构；增加用户态投影，不能直接透传技能裸参数。 |
| `backend/src/adapter/knowledge/storage.py` | 可参考私有 MinIO 存储实现，不复用知识库归属或强制依赖知识检索开关。 |
| `backend/src/adapter/session/model/session.py` | Session 是文件归属入口；新增配额事务需审查与既有 Session/Run 锁顺序的关系。 |
| `backend/src/bootstrap/tool.py`、`api.py`、`run_worker.py`、`main.py` | 统一装配、生命周期与路由注册。 |
| `frontend/src/features/chat` | 现有审批组件从详情展开 arguments；文件主要显示引用。需替换技能展示、补文件闭环。 |
| `frontend/src/features/admin`、`frontend/src/app/AppRoutes.tsx`、`AppShell.tsx` | 复用管理请求、缓存、权限、导航模式。 |

实施基线以实际工作区为准，不覆盖、回滚或提交他人的现有修改；发现架构需要改变时回到 Root，取得用户确认。

## 3. 核心契约与落点

### 3.1 模块与数据

新增建议路径：

- `domain/skill`：有类型包清单、版本、Run 技能快照、参数校验结果、脚本执行请求/结果与仓储/执行端口。
- `app/skill`：导入、启停、版本切换、依赖检查、加载资源与脚本用例。
- `adapter/skill/model`、`repository`、`converter`：技能持久化及显式接口实现；脚本执行适配器按同一领域归组，进程调用不进入 app/domain。
- `domain/file`、`app/file`、`adapter/file`：会话文件归属、授权、私有存储、列表/读取/下载与产物发布准备。对象存储客户端由组合根共享或独立注入，不绑定知识检索启用状态。
- `api/skill.py`、`api/files.py` 及 dto/converter；前端 admin/chat 对应功能。

持久化至少覆盖 skills、skill_versions、session_files、Run 技能快照和脚本调用附加信息。附加信息可用与 RunToolCall 一对一表，不能再复制整套调用状态机。依赖检查是随执行环境变化的检查记录，不修改不可变版本正文；已加载技能没有独立权威表。

Run 技能快照含全部可用技能的 ID、固定版本 ID、名称/简介、审批开关和用户选中项。以显式 Pydantic 类型读写 JSON，区分空目录与缺失快照。版本资源引用只读持久化对象，不向 Run 复制整包。

session_files 保存 owner/session、来源、调用 ID、文件 ID、名称/可选标签、MIME、大小、摘要、私有对象键和创建时间；产物使用来源调用及清单序号唯一约束辅助幂等。文件 ID 不是授权凭据。

### 3.2 技能包与依赖

ZIP 根 SKILL.md，元数据包含 slug/name/description/dependencies/scripts，正文为指南；脚本登记 path/用途/参数 JSON Schema。实施采用 Draft 2020-12：根省略 `type` 时按 `object` 处理，显式非 object 根拒绝；允许 `allOf`、`anyOf`、`$defs`、本地 `$ref`、`pattern` 和 boolean 子 schema。`format` 遵循库默认 annotation，不额外承诺强制校验；验证器使用空 `referencing.Registry()`，本地引用可解析而外部引用稳定失败，不执行隐式网络 I/O。实施依据为 `backend/src/app/skill/package.py` 当前标准 validator 边界，schema 和参数字节仍遵守现有工具协议边界。

包限额按设计：25 MiB ZIP、100 MiB 解压、500 文件、单文件 25 MiB、指南 100,000 字符；路径/链接/冲突/加密包/压缩比校验在实际解压流中执行。压缩比具体阈值需在实施配置中给出并覆盖测试，未配置时不能假装已校验。

依赖使用 PEP 508 Requirement 解析、规范化发行包名、实际解释器环境的 distributions 与版本约束比对。URL/非法格式/首期不支持的 extras 返回 422；marker 为 False 时跳过。合法但缺失或不符保存 dependency_unsatisfied，禁止启用；每次执行前复核。不自动安装依赖，不要求不可变镜像。

首期部署建议 API 和 Worker 使用同一平台解释器与依赖配置，并暴露非敏感环境指纹。不能在 API 环境检查后无条件断言异构 Worker 满足依赖；环境不同须由真实目标环境检查，无法检查则不启用。预装库清单是部署项，不通过技能导入添加。

### 3.3 工具与动态审批

新增 builtin 工具：skill_load、skill_read_resource、skill_execute、session_file_list、session_file_read，签名及分页/UTF-8 规则按设计第 7 节。

建议显式扩展应用层校验契约，输入 Run 固定技能上下文，输出有类型的准备结果（即时错误或验证后的技能目标、单次审批值及安全展示信息）。本地同步校验只用预先解析的固定元数据；文件归属/名称关联等需要数据库的步骤由提交前异步准备完成，不能偷偷塞入同步 validator。准备失败直接得到 immediate_outcome。

skill_execute 静态 requires_approval=False，仅服务端 builtin 身份触发动态解析；有效调用读 Run 快照的技能策略，其他工具维持静态属性。缺失快照、非法 skill_id、脚本或参数不合法均不审批、不执行。合法待审批调用仍构成整批屏障，非法项不出现在待审批列表。

SafeSkillApprovalProjection 同调用事务持久化展示快照，包含固定名称/版本、脚本用途、安全输入文件名及白名单参数摘要。决策只接受 call ID/decision/request ID；服务端审批入口也要阻止必需元数据缺失的批准，不能仅禁用前端按钮；拒绝仍可用。查询重新验证权限，不回退原始 arguments/output/outcome.data。非技能工具响应维持原语义。

技能详情 DTO 使用判别分支显式返回 safe_skill_projection、artifacts 等用户态字段；原始排查数据独立 AdminAuth 接口。还需审查消息 API、Run 快照/SSE 和执行历史投影，防止通过其他入口获得相同裸参数或日志。

### 3.4 上下文与加载恢复

主提示词 → 简要技能目录 → 活跃完整指南构成不可压缩系统基准，之后才是历史摘要和正式消息。所有完整请求、预算估算、压缩重建及回退调用同一构建逻辑，不能重复注入或在回退漏注入。

活跃集合 = Run 显式选中项 ∪ 本 Run 全部成功 skill_load 调用的 skill_id；读取持久化调用身份、参数、成功终态及结果，不解析摘要或模型文本。固定排序、去重，查询不受 cutoff/分页裁剪影响。Worker 重启从同样证据重建，不额外建易失权威状态。

历史预算 = 模型窗口 − 输出预留 − 基础系统/工具/封装/重试占用。摘要和近期历史额度、触发阈值均基于剩余历史预算；最终完整请求复核。指南本身放不下明确失败，不截断、不拿摘要代替指南。

### 3.5 脚本进程与 IPC

运行登记脚本，无 shell；输入为工作目录 input.json，ATHENA_INPUT_PATH 仅传路径，字段为 parameters/input_files/output_dir。退出 0 后读取 outputs/manifest.json；空输出仍需有效清单。仅清单普通文件可发布，链接/越界/重复路径/清单损坏整组失败。标签和文件名分别保存，平台生成文件 ID、MIME/大小等元数据。

start_new_session=True，记录启动时 PGID。取消、失租、Run 截止时间、Worker 正常退出及入口正常结束后均清理组内残留、回收直接子进程，再读取发布文件；保护取消期间清理逻辑。同组回收不承诺处理主动脱离组后代或 Worker SIGKILL 后自动杀进程。

**现有通用工具超时需显式分流**：executor._execute_tool 当前对 builtin 也取 tool_timeout 与 Run 剩余时间的较小值。skill_execute 应只使用剩余 Run 时间作为外层执行截止，不额外套通用单工具时限；现有 MCP/其他工具保持原 deadline 语义。仍必须受最外层 Run 截止与取消控制，不把取消解释为脚本可以无限脱离 Run 运行。

stdout/stderr 并发有界读取，合计保留 5 MiB，溢出 log_truncated 后继续排空丢弃，不能停止读管道。用增量日志落盘/上传，不能 communicate 后再截断。输入/清单用结构化文件读写，超大清单需增量解析，不恢复输出数量限额；文件正文流式处理。

finally 先收敛进程/管道，再删除本次目录；产物和保留日志已独立落存储。失败清理记录
cleanup_error_code，由同一执行路径尽力收敛。Worker 启动及周期任务不扫描或自动删除
遗留目录；本地诊断 JSON 仅记录 Run/调用/Worker/PID/PGID/时间/目录，供人工排查，
不是跨重启身份或存活证明，不能据此自动杀 PID/PGID。

### 3.6 文件配额与原子多文件结果

用户上传单文件 50 MiB，会话上传+产物合计 100 文件；仅平台存储配额，生成文件不沿用上传单文件限额。按白名单与内容校验类型，下载 attachment/nosniff，文本读取严格 UTF-8 并返回续读字节偏移，二进制返回元数据与 file_not_text。

文件发布不能在普通 FileService 内另开事务后才 finish_tool_call。扩展 FinishToolCallCommand 或新增同属 Run repository 的完成命令，携带已上传待发布文件的有类型元数据；repository 复核租约/epoch/取消/会话/文件配额，在同一 AsyncSession 中写文件行、ToolResultPart.artifacts、调用完成与事件。文件 adapter 提供共享事务内部方法，不另起事务。

上传和脚本发布共享同一会话配额锁。建议使用以 session ID 派生稳定键的事务级 advisory lock，上传仅取此锁，Run 完成先沿既有顺序取 Run 锁再取配额锁；持有配额锁的路径不得反向取 Run 锁。不要在 finish_tool_call 中临时加入 Session 行锁，形成既有 Session→Run 的反向顺序。实施时审查所有新写路径后固定顺序，若需更改既有锁架构交回 Root。

对象先写私有存储，DB 事务只发布引用，不执行对象上传。任一失败整组不发布，孤立对象
保留到停写窗口中的离线维护；已提交结果重放原文件 ID，不重复启动脚本。日志失败与
业务发布分开，不能成功交付后为补日志重跑。

离线维护默认 dry-run。只有 `--execute --confirm-all-writers-stopped` 才能删除宽限期外
的合法前缀对象，并必须逐项确认 `session_files.object_key`、
`skill_versions.object_key` 和 `skill_executions.log_object_key` 均无引用。引用查询失败
时保留对象并计入失败；每次命令从桶起点分页扫描，不读写持久游标、代次或发布 owner。

ToolResultPart 增加 ArtifactFileRef 集合，贯通 ORM JSON converter、API DTO、前端、执行结果/完成命令、模型 TOOL 转换和压缩。原 artifact_id 不承担技能交付；一个调用仍只有一个 TOOL 结果。非空 output 也要渲染文件引用，文本截断不能丢失权威集合；模型预算不足时通过文件列表发现全集。

## 4. 分阶段实施与退出条件

各阶段由 Implementer 按依赖顺序实施，不以创建文件或单元测试通过代替用户闭环。

| 阶段 | 主要交付及具体改动 | 阶段退出条件 |
| --- | --- | --- |
| S0 基线与契约 | 重查 AGENTS、Diff、migration head；核对实际解释器/MinIO/测试环境；确定包元数据和脚本 schema、DTO、共享事务完成命令。保留现有系统提示词改动。 | 接口与设计一致；未引入已排除隔离/限额；明确环境和测试前提。 |
| S1 持久化与技能管理 | skill/file 领域类型与端口；技能表、文件表、脚本附加记录、Run 字段及唯一约束；包存储/校验/依赖；管理 API 与基础装配。 | 新建导入、更新、依赖不符、启停/切换并发通过 PostgreSQL 验证；版本不可变。 |
| S2 会话文件与消息集合 | 上传/列表/读取/下载、配额锁、文件引用授权；artifacts 全链路序列化；前端可先使用 API fixture。 | 上传和产物计数一致；跨会话拒绝；文本边界正确；多文件往返不丢失。 |
| S3 Run 与上下文 | submit/retry 在创建事务绑定目录；选中项/文件 ID 纳入指纹；answer 补文件；新增加载/读取工具；成功调用恢复；基础上下文和压缩预算。 | 发布/停用不影响旧 Run，重放不重绑；重启/压缩后指南一致。 |
| S4 批次审批与投影 | 显式 Run 校验上下文、动态单次审批、展示快照原子写入；安全详情/待审批 DTO；拒绝非法批准。 | 合法开关组合、非法项混批、审批恢复/篡改请求和直接 API 泄露测试通过。 |
| S5 脚本与产物发布 | Python adapter、文件 IPC、PGID/日志/finally 清理、deadline 分流；共享事务完成接口、幂等/未知结果处理。 | 真实进程取消及日志溢出测试通过；多产物发布/回滚/失租原子性通过真实 DB/存储验证。 |
| S6 完整前端 | 管理员导入/检查/版本/启停/审批；对话技能选择、上传、审批投影、进展和多文件下载；刷新恢复、错误保留。 | 普通用户无管理入口且后端拒绝；真实文件上传到下载及下一 Run 复用闭环完成。 |
| S7 集成与审查 | 回归、真实环境演示、部署说明与验收记录；Implementer 报告，terra-reviewer 独立实际 Diff 审查，Root 验收。 | 必要测试通过、无阻断 Finding，所有限制和未验证项真实列出。 |

S1–S5 有交叉依赖，以类型/接口先行；S2 没有脚本时可用受控测试命令验证原子发布，S5 必须再以实际脚本验证。S4 不能在 S5 未完成时对外宣称技能执行可用。

## 5. API 与前端任务清单

管理接口按设计第 9 节：import 仅首发，重复 slug 409；versions 更新校验目标 slug，串包 422；修订号控制启停/版本/审批并发。补齐版本详情、包资源检查以及管理员日志查询的明确路由，不能只提供列表无法查看指南。写接口复用来源校验，上传网络结果不确定通过 request ID/摘要查询核实。

普通用户目录、会话文件上传/列表/下载及工具读取共用权限。submit/answer/retry 的 DTO、converter、service、domain command、repository 指纹一起扩展，避免前端传入后服务端丢字段。新会话先创建再上传，失败保留用户文字/技能选择，不重复生成 Run。

前端 query keys 区分管理目录、公开目录、版本、会话文件和调用详情。管理员更改刷新当前目录；运行中界面继续按 Run 固定版本显示。技能未生效冲突不自动换成其他版本。下载通过鉴权 API；文件标签不做 HTML 渲染。

ToolApprovalPanel 与 RunExecutionTrace 不再对技能调用 JSON.stringify 原始参数；使用 safe projection，缺字段时禁止批准但可拒绝。ChatMessageList 渲染 artifacts 集合，与文件列表共享下载能力；不把同文件多界面展示误当作重复存储。

## 6. 验证矩阵

| 测试类别 | 必须覆盖的触发条件与可观察结果 |
| --- | --- |
| 包和依赖单元测试 | 路径穿越、链接、重复/规范化冲突、压缩超量、错 schema；非法 PEP 508/URL/extras 422；缺包/版本不符不可启用；markers 与发行包名正确。 |
| 管理 PostgreSQL 集成 | 并发同 slug 首发仅一个成功；串包不写版本；并发修订冲突；事务回滚不切指针；导入不自动启用。 |
| Run PostgreSQL 集成 | 创建/重试/重放/澄清/审批恢复的版本固定；管理员并发更新；旧 Run 空快照不补绑。 |
| 上下文回归 | 完整请求、压缩和回退的基础内容/Token 一致；大指南触发历史压缩而非压缩指南；重启查全量成功加载，失败加载无效。 |
| 审批集成 | 两技能开关不同；非法 ID/脚本/参数即时失败；混批只合法项待审批；展示冻结与请求一致；后端拒绝元数据缺失的批准。 |
| 文件与消息 | 多文件各字段/顺序持久化往返，output 非空仍可见；UTF-8 边界和二进制拒绝；跨会话/删除后不可用；类型伪装及下载头；50 MiB/100 文件并发边界。 |
| 实际子进程 | 文件 IPC、大参数不在 argv/env；非零退出/坏清单零发布；父先退/同组后代、取消/失租/Run 到期、双路日志溢出、finally 清理；不得仅 mock runner。 |
| 离线对象维护 | 默认 dry-run 不删除；未确认停写时拒绝 execute；三张引用表任一命中即保留；引用/单删失败计数并保留；每次从桶起点开始；共享桶测试只处理本次创建的键。 |
| Office 类型 | 标准库 `zipfile` 读取真实 xlsx/docx/pptx；先检查每个 `ZipInfo.orig_filename` 的安全路径，再核对 `[Content_Types].xml` 与格式 root marker；不解压、不读取正文。 |
| 迁移 | 隔离 schema 完整升级到唯一 head；只执行 `0023 -> 0022 -> 0023`，确认旧维护表仅在该往返窗口内存在且业务 schema 不被根 public 影响。 |
| 产物发布竞争 | 第 N 文件上传失败、DB 完成失败、取消/失租胜出整组不可见；配额与上传竞争无超额/死锁；提交后重放同文件 ID；UNKNOWN 不自动启动。 |
| 安全投影 | 直接访问待审批/详情/消息/流接口不能得到技能裸参数、日志、对象键、宿主路径或凭据；普通用户不能调用管理员排查接口。 |
| 前端和真实闭环 | 管理导入/启停/冲突、技能选择、上传失败保留、审批拒绝/批准、两产物下载、刷新恢复、下一 Run 引用。 |

新增测试建议放在 backend/tests 的 skill/file 单元与 integration 目录；实际文件名由实施者按现有组织选定。相关既有回归包括 test_run_executor_tools、test_run_compression、test_runs_api、test_run_worker、test_run_stream 与 Run/压缩/工具 PostgreSQL 集成，不只运行新增测试。

已核对仓库使用根 pyproject.toml 和根 `.venv`，前端 package.json 提供
test/typecheck/build/test:e2e。后端验证命令如下；集成测试只会创建并删除隔离 schema，
文件存储验证必须使用独占 bucket，或让测试包装层允许列举共享桶元数据但只把本测试
创建的键交给维护器。不得把共享桶中的其他对象当作候选、读取其正文或删除：

```sh
# 仓库根目录；环境脚本按既有部署提供，不输出密钥。
source scripts/load-test-env.sh
.venv/bin/python -m pytest backend/tests/test_run_executor_tools.py backend/tests/test_run_compression.py backend/tests/test_runs_api.py backend/tests/test_run_worker.py backend/tests/test_run_stream.py
.venv/bin/python -m pytest -m integration backend/tests/integration
.venv/bin/python -m pytest -q backend/tests/integration/test_skills_files_e2e_postgres_minio.py
```

```sh
# frontend 目录
npm run test
npm run typecheck
npm run build
npm run test:e2e
```

新增 skill/file 测试须追加到实际命令；先做定向验证，再完成必要回归。集成测试需 PostgreSQL/Redis/MinIO，缺依赖跳过必须报告，不能算通过。E2E 若环境不能运行，给出明确阻断与实际手工替代证据，不声称自动 E2E 通过。必要时以完整 backend suite 验证跨消息协议变化。

## 7. 数据变更与交付

新增迁移按实际 head 顺序追加；本轮新增
`20261122_0023_remove_online_file_maintenance`，只删除
`file_object_publications` 和 `file_object_reclamation_cursors`。不修改已有迁移链，
不提供旧预留/游标数据回填。downgrade 只恢复两张空结构及游标初值，不恢复历史对象引用
或业务数据。

本期部署交付包括：执行解释器/依赖检查方式、MinIO 私有对象前缀、临时根目录和权限、
包/上传/日志参数、离线维护命令及错误排查。`ATHENA_SKILL_TEMP_STALE_SECONDS`、
`ATHENA_FILE_ORPHAN_RETENTION_SECONDS` 和
`ATHENA_FILE_MAINTENANCE_INTERVAL_SECONDS` 不再由应用读取；维护宽限期通过命令参数
指定。上传与技能功能不能因为知识库默认关闭而失效；不要为了技能强制启动知识嵌入模型。

对象写入与数据库引用仍按“先对象、后引用”进行；提交结果不确定时先按幂等键或来源
重查，确认未发布后再交给离线维护，不能把空引用查询当成仍在写入的证明。离线执行前
必须停止 API、Worker、迁移脚本及全部其他写入者；命令默认 dry-run，删除按小批执行。

Implementer 返回：实际修改文件、分阶段完成情况、准确测试命令/结果、migration 与环境步骤、未处理问题。Reviewer 独立检查实际 Diff，返回 PASS 或 CHANGES_REQUIRED，Finding 包含位置、触发条件、影响和建议。有效问题优先交回原 Implementer，修复后必须重新 Review，最多三轮。Root 核验目标、测试和阻断问题后验收；未经 Review 不宣称完成。

## 8. 当前准备程度

设计规则、进程执行、原子发布、离线维护、Office 校验和迁移已映射到当前实现。后端
Implementer 已完成必要的单元与隔离 PostgreSQL/MinIO 集成验证；本文仍不代表独立
Reviewer PASS、真实外部模型全绿或生产部署完成。

仍需由 Root 统一验收：前端终态对账、浏览器 harness、真实外部模型工具流、完整部署
编排和服务级 E2E。离线对象维护没有在线自动容量收敛；崩溃遗留临时目录由人工复核。
这些边界不得被改写为“自动清理已完成”。

当前仍处于 Implementer → Reviewer → Root 流程；Reviewer 通过并完成最终验收前，
不能宣称整体完成。
