# 全局 MCP 与 Tool 管理设计

**状态：** 本次范围与架构已确认，本文细化实现要求，代码仍未实施。

## 1. 目标与范围

管理员配置全局 MCP Server。所有有效用户都可通过 ToolManager 获得这些 Server 最近一次成功发现的工具定义，供助手后续使用。普通用户不能查看 MCP 配置、地址、命令或凭据。

| 本次包含 | 后续再设计 |
| --- | --- |
| admin/user 角色及受控管理员授权 | Redis 和缓存实现 |
| admin 对全局 MCP 注册、查看、修改、注销 | 定时刷新工具目录 |
| Streamable HTTP 与 stdio MCP | `tools/call` 和运行时执行生命周期 |
| 注册／修改时发现并持久化 Tool | LLM 厂商接入、Run、审批与调用记录 |
| ToolManager 面向所有有效用户提供安全工具目录 | 用户自有 MCP、跨租户配置、OAuth |

本次不实现定时任务或刷新接口。用户查询的工具目录来自数据库中最后一次成功发现的结果，不代表远端此刻在线。ToolManager 不发送 `tools/call`。

## 2. 当前基础与认证变化

当前仓库已有 Cookie 登录，`CurrentAuth` 包含由数据库查询得到的 `User`；`User`/ORM 尚无角色。开放注册 DTO 使用 `extra="forbid"`。部署前需要将下面的角色规则和字段迁移纳入认证模块。

- `users.role` 取值 `admin`／`user`，新用户默认 `user`；迁移把已有用户显式设为 `user`。
- 普通注册请求不接收 role，拒绝额外字段，不能由用户自助提权。
- 首个及后续管理员由部署方用受控本地运维命令，将已有且 active 的真实账号设为 admin。不给新账号默认密码，不在应用启动时自动选管理员，不新增 HTTP 提权或通用角色管理后台。本轮不执行该运维命令。
- `User`、`UserModel`、认证 converter/service 与 `AuthContext` 传递 role；当前用户自己的安全响应可包含 role。
- 每个请求按数据库当前角色授权，不能只信 Cookie 中缓存的角色。已禁用或删除用户不能通过 `CurrentAuth`。

管理员身份不是 MCP Server 的 owner。所有 Server 是全局配置，不按创建者过滤；任一 admin 可以管理和查看全部配置。Server 可记录 `created_by` 供审计，但首版不因它增加复杂依赖。

## 3. 四层职责与 ToolManager

| 层 | 职责 |
| --- | --- |
| ToolManager | 接收可信 `AuthContext`；组织 admin 注册、查看、更新、注销；向有效用户返回安全 ToolDefinition；服务层重新校验管理员权限 |
| MCP 通信层 | 按单次请求接收 endpoint／stdio 配置、认证 Header 或环境变量及 timeout；运行 SDK 协商和完整工具发现 |
| 全局 Server 配置 | 由 admin 维护 transport、连接配置和 Server 专属加密凭据；不保存用户级策略 |
| 数据库 | 持久化角色、全局 Server 和其 Tool，是目录的唯一依据 |
| Redis（后续） | 本次不加 Redis 依赖、不建空缓存接口；定时刷新或有查询压力后再评估 |

ToolManager 位于 `backend/src/app/tool/`。管理路由与普通工具目录路由都通过 ToolManager，不能绕过它直接读仓储。建议操作为：

```text
register_mcp(actor: AuthContext, config) -> McpServer
list_mcp_servers(actor: AuthContext) -> list[McpServer]
get_mcp_server(actor: AuthContext, server_id) -> McpServerDetail
update_mcp(actor: AuthContext, server_id, full_config) -> McpServer
unregister_mcp(actor: AuthContext, server_id) -> None
list_tools(actor: AuthContext) -> list[ToolDefinition]
list_public_tools(actor: AuthContext) -> list[PublicToolDefinition]
list_admin_tools(actor: AuthContext) -> list[AdminToolDefinition]
get_public_tool(actor: AuthContext, tool_id) -> PublicToolDefinition
get_admin_tool(actor: AuthContext, tool_id) -> AdminToolDefinition
```

`AuthContext` 只能来自认证端口对当前有效 session 的校验；ToolManager 每次操作都用可信 user/session ID 从 auth 端口或数据库重新确认账号 active 和当前 role，不把调用者传入的 `actor.user.role` 快照当授权依据，不接受请求体、query、模型或调用方自报 role。admin 操作在网络／进程启动前确认当前 admin；发现完成、资源清理后，在同一短提交事务中先锁 user 行并再次验证 session、active 状态和 admin role，再锁 Server 行并提交。角色变更和此检查遵循 user 行锁顺序。若长发现期间被撤权，可让已启动操作完成清理，但不允许提交配置或目录。MCP 管理资源的存在性检查只能发生在授权之后，普通用户不能用错误差异探测配置。

建议模块职责沿用现有 DDD 边界：`domain/auth` 增角色；`domain/tool` 定义 Tool 与端口；`domain/mcp` 定义 Server、配置联合类型和发现端口；`app/tool` 实现 ToolManager；`adapter` 持久化；`infrastructure/mcp` 实现 HTTP／stdio SDK client 和资源回收；`api` 提供路由与 DTO。`main.py` 组装用例。领域层不依赖 FastAPI、SQLAlchemy、HTTP 或 MCP SDK。

## 4. 用户可见行为与权限

| 操作 | admin | 普通 user |
| --- | --- | --- |
| 注册／查看／完整修改／注销 MCP 配置 | 允许 | 403，不能枚举 Server 是否存在 |
| 查看 MCP 地址、stdio command/args、Header／env 名称掩码 | 允许 | 禁止 |
| 获取 Tool 的名称、描述、输入 Schema | 允许 | 允许 |
| 查看 Tool 的来源映射、Server 配置或管理原始元数据 | 允许 | 禁止 |
| 执行 MCP Tool | 本次不实现 | 本次不实现 |

受信应用组件调用内部 `list_tools` 获得全局完整目录与来源映射。所有用户可见 HTTP、model 和 session 响应必须使用 `PublicToolDefinition`：opaque tool id、model name、description、input schema。API 不得透传内部 ToolDefinition。admin 配置查看使用独立的 Admin DTO 和管理方法。用户可见 DTO 不含 `server_id`、`remote_name`、endpoint URL、command、args、cwd、env、Header、凭据或 SDK 原始对象。普通用户无任何 `/api/v1/admin/mcp/*` 或 admin Tool 管理路由访问权；这不妨碍助手后续使用安全 Tool 定义。

admin 管理响应可显示 endpoint、command、args 与凭据名称及 `has_value`，但不显示凭据明文或密文。权限失败、异常和错误体不得回显秘密字段、原始请求体、stdio stdout/stderr 或 SDK 异常。

## 5. Transport 与 MCP 注册

Server 配置是有判别字段的联合类型，`transport` 只能是 `streamable_http` 或 `stdio`。请求必须且只能包含所选 transport 的字段，拒绝混合配置。

| transport | 必填配置 | 秘密字段 |
| --- | --- | --- |
| `streamable_http` | HTTPS MCP endpoint URL | 静态 Header |
| `stdio` | 绝对 executable 路径、参数数组；可选绝对 cwd | 显式环境变量 |

协议自动协商采用已讨论的 SDK 默认 `mode="auto"`，并要求锁定 SDK 同时支持配置的 HTTP 与 stdio transport。现代与 legacy 请求格式、协议版本和相应 session 生命周期由 SDK 协商适配；应用不复制协商过程。认证、网络、TLS 错误不得被当作任意协议回退理由。协议日期不作为配置字段或目录版本。

注册和修改都在存储前执行完整发现：

1. admin 请求提供名称、transport 配置及完整凭据。创建时生成 server ID；修改时读取当前 `config_version`。
2. 为本次操作创建独立 SDK Client。HTTP 凭据或 stdio 环境变量只注入本次 client，不跨用户请求共享。
3. 由 SDK auto 协商，确认 `tools` capability，读取所有 `tools/list` 分页。
4. 无交互地验证发现和工具 Schema；适用的新版 `x-mcp-header` 无效工具从目录排除并记录安全告警。其余列表结构、分页或资源限制失败则整次操作失败。
5. 退出 SDK 上下文，完成 HTTP client／stdio process 清理后，才在短数据库事务中保存配置和全部发现 Tool。

新注册返回空工具列表可成功。修改只有在发现与资源清理均成功后才原子替换配置与目录；发现失败保留旧配置、旧凭据与旧目录。数据库提交结果未知时管理员可查列表确认，不引入幂等子系统。

### 修改和注销

admin 使用完整替换的 `PUT` 修改名称与 transport 配置；不提供 PATCH 式隐含保留秘密语义。响应只给秘密名称及是否已设置。修改请求必须重新提交需要保留的全部秘密，空 map 表示清除。改 transport 时整套配置一并替换。

修改流程在网络阶段不持数据库锁。完整发现和 client 清理成功后，短事务先锁 user 并确认 admin/session 仍有效，再锁 Server，比较开始时的 `config_version`：仍一致才替换配置和工具、版本加一；若版本已变、角色已撤销或 Server 已删除，返回 409／403，旧操作不能覆盖新配置或复活 Server。发现失败不改版本。

同一 Server 与相同 `remote_name` 的 Tool 在完整替换后保留 tool id 与 model name；新增工具生成新 ID/name，不再存在的工具随目录原子删除。无历史目录表。admin 注销时物理删除 Server，级联删除工具，并删除关联加密凭据。

## 6. stdio 启动与回收

stdio MCP 程序由 Athena 后端所在主机／容器启动，不是用户设备。它只能由 admin 注册的全局配置创建。admin 是受信任部署操作员；无 shell 执行并不构成沙箱。

- `command` 必须是绝对可执行路径；`args` 是数组，使用 subprocess 参数形式执行，绝不通过 shell、`sh -c` 或命令字符串拼接。
- 可选 `cwd` 必须是绝对路径。命令不自动调用 npx、pip 或下载依赖。
- 进程不继承 Athena 完整环境。只传部署明确允许的基础变量和该 Server 明确配置的环境变量。
- stdout 只作 MCP 协议数据；stderr 只用于受限诊断，不原样进入 API 或日志。错误不能回显 command 参数、环境值或原始 stdout/stderr。
- 使用部署服务账号尽可能降低权限，并选择不会泄露工作区／业务临时内容的工作目录。后端配置的 stdio 程序执行权限是平台级高信任能力，不面向普通用户开放，也不声称由应用实现隔离沙箱。
- 发现完成前进程非零退出、异常退出、协议错误或超时均判发现失败。发现完整成功后关闭 stdin 并按有界宽限期等待；未退出则 TERM，再过有限等待后 KILL 并回收被管理的进程树。由本次受控 TERM／KILL 导致的负 signal exit code 是正常资源清理结果，不回头改判发现失败。总操作 deadline 覆盖协商和分页，回收另有有界清理预算。
- 发现失败或无法在清理预算内确认进程已回收，都不写新配置；若更新则保留旧配置。只有完整发现成功且资源回收成功才提交。

## 7. ToolManager 数据与可见投影

内部 ToolDefinition：

```text
tool_id       数据库工具 ID
model_name    稳定、唯一的 mcp__<tool UUID hex> 名称
description   清理控制字符并限制长度后的描述
input_schema  有界 JSON Schema
server_id     内部来源映射
remote_name   MCP 远端名称，仅服务端使用
```

model name 不依赖 Server 名称或远端 name，不靠拆解名称推测路由。受信应用组件使用 ToolManager `list_tools(actor)` 只查询数据库，不触发网络，不读取 Redis；返回全局所有最新已提交目录及内部来源映射。对所有普通用户输出、LLM 工具列表和会话响应都必须先转换为 `PublicToolDefinition`；admin 配置和来源映射用独立方法及 `AdminToolDefinition`。

Athena 自身不把配置或凭据字段拼入公开 DTO、错误或日志。admin 选择并注册 MCP Server，即同意把其发现到的 description 和 input schema 提供给所有有效用户；这些远端元数据是不可信内容，必须作 JSON 转义和长度限制，不能当指令执行。恶意 Server 可以在描述或 Schema 中自行嵌入看似敏感的文本；本设计不承诺语义脱敏、不增加自动 secret 扫描或审批，也不把工具元数据当作沙箱。admin 应只选择可信 Server 并确保其公开目录不含敏感内容。

后续 LLM 格式转换只使用公开的 model_name、description 和 input_schema，不接触 Server 凭据；本次不验证所有 JSON Schema 都兼容所有供应商。

## 8. 凭据、隔离与 HTTP 安全

### 管理员授权

- 普通用户注册不能设置 role。初始 admin 由部署运维受控地提升已有 active 账号；新增用户默认 user。
- 每个 admin 路由和 ToolManager 管理用例都验证当前数据库角色。未登录 401；已登录但非 admin 返回 403。授权先于读取资源，防止 Server 枚举。
- Server 全局共享，因此普通用户只拿安全工具定义；管理配置不会因为 ToolDefinition 输出而泄漏。

### Secret

Header 和 stdio 环境变量使用应用主密钥 AEAD 加密，独立随机 nonce，并用 server id 与用途作为附加认证数据。秘密值仅保存在该 Server 的加密封装中，不建立通用 secret_refs 表。密钥缺失或解密失败即拒绝启动，不能降级为不带凭据。

admin 读回仅显示 Header／env 名称及 `has_value`，不回明文或密文。秘密写入使用完整映射，空映射清除。禁止把凭据放在 URL、command 或 args；Athena 不把配置秘密拼入 admin DTO、普通用户响应、错误、日志、审计或 LLM 工具定义。远端工具元数据可能包含任意文本，按第 7 节处理。

### HTTP 网络

- 只允许 HTTPS，校验证书与主机名。拒绝 URL userinfo、query、fragment。
- 禁止重定向和进程环境代理；SDK 和 transport wrapper 均不得在同源或跨源重定向后发目标请求。
- DNS 检查所有解析地址，并将经检查的 IP 绑定实际连接，同时保留原主机 SNI／TLS 校验。拒绝 IPv4／IPv6 回环、私网、链路本地、保留与云元数据地址。部署级内网例外只能由部署配置设置。
- 静态 Header 名大小写不敏感、规范化后唯一；拒绝 CR/LF、非法名称与协议保留 Header，包括 Host、Content-Length、Connection、Transfer-Encoding、Accept、Content-Type、MCP-Protocol-Version、Mcp-Method、Mcp-Name、Mcp-Param-* 和协议会话 Header。协议专用 Header 只由 SDK 控制。
- SDK auto 的现代／legacy discovery 与传输错误不能混为一谈。安全 transport 必须确保上游 HTTP 401／403、网络与 TLS 错误不会触发不适当的协议回退。认证、网络错误不可用作降级条件。

## 9. 数据持久化与迁移

只新增必要字段和两张 MCP 表；不保存 owner、user policy、协议历史、缓存或调用状态。

### `users`

增加 `role`，取值 `admin`／`user`，默认 `user`，数据库约束限定枚举值。迁移将现有用户设为 `user`。注册请求不能写此字段。角色在用户 ORM、领域 User、AuthContext 和认证转换中传递。

### `mcp_servers`

```text
id                  UUID 主键
name                全局唯一名称
transport           streamable_http / stdio
config_json         对应 transport 的非秘密类型配置
secrets_ciphertext  可空的加密 Header／env 映射
secrets_nonce       可空随机 nonce
key_version         应用密钥版本
config_version      初始 1，完整配置变更后递增
created_by          可空审计信息（可选）
created_at
updated_at
tools_discovered_at
```

不含 owner。`config_json` 使用明确的 transport 联合结构：HTTP 保存 url；stdio 保存 command、args、cwd。秘密映射整体加密，不需要单独凭据表。协议版本协商结果不持久化。

### `mcp_tools`

沿用当前设计的 `id`、`server_id`、`remote_name`、`model_name`、`description`、`input_schema_json`、`created_at`。`(server_id, remote_name)` 和 `model_name` 唯一，server 外键级联删除。Tool 不重复记录 owner。

迁移只对 `users` 增加角色并新增两张 MCP 表、约束和索引；当前仓库尚无 MCP 表，不涉及迁移既有 MCP owner 字段。部署回滚需备份配置，恢复秘密需同时持有应用主密钥。

## 10. HTTP API

管理接口均要求当前数据库角色为 admin；写请求复用 `verify_request_origin`。所有管理资源先授权后查询。

| 方法与路径 | 行为 |
| --- | --- |
| `POST /api/v1/admin/mcp/servers` | 注册全局 MCP，完成发现后返回 201 |
| `GET /api/v1/admin/mcp/servers` | 查看全局配置列表 |
| `GET /api/v1/admin/mcp/servers/{server_id}` | 查看完整配置与已脱敏凭据状态 |
| `PUT /api/v1/admin/mcp/servers/{server_id}` | 完整替换配置，发现成功后原子更新 |
| `DELETE /api/v1/admin/mcp/servers/{server_id}` | 注销配置与 Tool，返回 204 |
| `GET /api/v1/admin/mcp/servers/{server_id}/tools` | admin 查看 Tool 与来源映射 |
| `GET /api/v1/admin/tools` | admin 查看全局工具管理目录 |
| `GET /api/v1/admin/tools/{tool_id}` | admin 查看 Tool 和 MCP 来源 |
| `GET /api/v1/tools` | 任一有效用户查询安全 ToolDefinition 列表 |
| `GET /api/v1/tools/{tool_id}` | 任一有效用户查询安全 ToolDefinition 投影 |

列表接口分页，默认 20、最大 100。普通用户 tools API 不接受 `include_source`、config/detail 扩展参数，不提供绕过投影的替代路由。

错误码简要约定：未登录 401、非 admin 403、不可见资源 404、全局名称冲突／版本并发冲突 409、配置无效 422、MCP/stdio 协议或连接错误 502、超时 504、数据库／凭据服务不可用 503。不得将进程命令、stdout、stderr、Header、env、URL 敏感信息放入错误详情。

## 11. 资源限制与协议依赖

建议首版设置可部署调整的上限：单操作 30 秒总时限、2 MiB 响应、最多 100 页／200 个 Tool、Header 或 env 总计最多 16 KiB。stdio 总 deadline 覆盖 discovery 和分页，进程回收有独立有界预算。限制必须在流读取和进程运行期间执行。

仓库采用 Python 3.12；SDK 版本锁定后须核实它同时支持 `mode="auto"`、Streamable HTTP、stdio、modern／legacy 协商、分页、资源关闭和本设计所需的进程退出控制。还须验证 discovery 401／403 不触发不合适的 initialize 回退，以及 SDK 内部不会跟随同源／跨源重定向。主分支文档或源码不能替代锁定 release 的文档、源码和测试证据。SDK 不支持任一目标 transport 或安全约束时属于依赖阻断，不在应用层另造协议协商器。

## 12. 验收标准

- 迁移后既有用户为 user，新注册用户为 user，RegisterRequest 无法写 role；受控运维命令只允许将已有 active 真实账号设为 admin，无默认 admin。
- 未登录访问 admin API 为 401，普通用户为 403；普通用户不能通过资源差异枚举全局配置。
- admin 可注册、查看、完整修改、注销 HTTP 与 stdio 全局 Server；第二个 admin 能管理第一个 admin 创建的配置。
- HTTP 与 stdio 使用 SDK auto，完成 modern 及锁定 SDK 支持的 legacy 发现与完整分页；不发送 `tools/call`。
- admin 在长时间发现期间被撤权时，发现仍能清理资源，但最终写事务复核 active／role 后拒绝提交；按 user 行再 Server 行顺序与角色变更串行化。
- 注册和修改在 Client／进程资源清理后才写库；发现失败保留旧配置；已知事务回滚不留半条 Server/Tool。修改冲突或已删除时旧操作不覆盖／复活。
- 错误的 transport 字段组合被拒绝；stdio 不走 shell、不下载包、不继承完整宿主环境；发现阶段非零退出会失败，发现成功后的预期 TERM／KILL 负 signal status 作为清理成功处理；无法回收进程则不提交。
- 普通用户能通过 ToolManager／`GET /api/v1/tools` 获得全局工具名、描述和 input schema；不能获得 URL、command、args、cwd、env、Header、server_id 或 remote_name。
- 两个 admin 并发操作不同 MCP 时，各自独立 Client 中的 Header／env 不串用；普通用户 `list_tools` 只查数据库，不启动 Client 或进程。
- 加密秘密不会出现在管理响应、普通响应、日志或错误中；密钥缺失／解密失败不会无认证降级。
- HTTP 目标的 DNS/IP 防护、TLS 校验和同源／跨源重定向阻断生效；协议、响应、页数和总时限受限。
- 普通工具目录读取只查数据库，不连接 MCP；首版无 Redis、定时刷新、健康探测或用户策略。

本设计覆盖已确认的全局配置和双 transport 范围，尚需锁定具体 MCP SDK release 并验证。文档不等于代码已实施；按仓库流程审阅确认后再交 Implementer 编码。

## 13. 协议参考

截至 2026-11-06，官方 2026-07-28 规范作为当前版本参考；实际协议范围由所锁定 SDK release 的 `auto` 能力决定。

- [官方协议版本说明](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning)
- [Server Discovery](https://modelcontextprotocol.io/specification/2026-07-28/server/discover)
- [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [stdio](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)
- [Python SDK 协议版本与 auto 模式](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/protocol-versions.md)
