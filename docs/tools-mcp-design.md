# 工具与 MCP 管理设计

**状态：** 管理范围已经用户确认；本文是按该范围重新设计的方案，尚未批准代码实施。

**本次目标：** 管理用户私有 MCP Server 和工具，探测工具发现是否可用，并向后续 LLM 层提供经过筛选的工具定义。不实现 `tools/call`、模型调用和工具调用生命周期。

## 1. 当前基础与范围

### 1.1 已有基础

根据 `docs/architecture.md`、`backend/src/main.py`、`api/security.py` 和项目模块：

- 已有 FastAPI、Cookie 认证、用户、项目、Session 和消息存储。
- API 通过 `CurrentAuth` 取得用户身份，写请求复用现有 Origin 校验。
- 数据库存储采用 async SQLAlchemy、PostgreSQL 和 Alembic。
- 模块依赖为 `api -> app -> domain`，适配器实现领域端口。
- 尚无 MCP 模块、模型调用、Run 编排和前端实现。

本次不依赖尚未实现的执行系统，也不改动消息存储。

### 1.2 已确认范围

| 包含 | 不包含 |
| --- | --- |
| 用户私有 Server 的创建、查询、修改、删除 | 管理员、全局共享和跨用户连接共享 |
| 静态 Header 凭据的安全保存与更新 | OAuth、任意进程环境变量读取 |
| Streamable HTTP 连接测试 | `stdio`、旧 HTTP+SSE 传输 |
| 手动发现、刷新和保存工具目录 | Resources、Prompts、Sampling、Elicitation |
| 查看工具定义，启用／禁用 Server 与工具 | 内置工具和内置工具注册接口 |
| 用户隔离、网络安全和资源限制 | 模型接入、工具调用和调用参数校验 |
| 健康探测、管理操作的错误与最近结果展示 | Run 快照、审批、信任、执行策略 |
| 向后续 LLM 层提供经过筛选的统一工具定义 | 模型供应商适配与 LLM 请求发送 |
| | 调用账本、恢复、幂等、取消和结果产物 |

连接测试和发现可以发送 MCP 初始化和 `tools/list` 等管理协议消息，但绝不发送 `tools/call`。

### 1.3 本文提出的交付边界

本次建议交付后端 HTTP 管理接口、持久化、MCP 发现客户端、面向后续 LLM 层的内部工具目录接口和必要测试，不交付前端页面。现有仓库没有前端，因此页面与交互设计另行确认。

本次“工具管理”只管理 MCP 发现得到的工具。不允许手动创建工具、编辑远端名称／描述／Schema，也不预建通用能力网关。

## 2. 用户操作与规则

### 2.1 Server 管理

- 创建时填写稳定键 `name`、展示名 `display_name`、MCP endpoint URL 和可选 Header。
- `name` 在同一用户的未删除 Server 中唯一，采用小写 ASCII 字母、数字、下划线、连字符，长度 1–64；创建后不可修改。展示名可修改，长度 1–120，不含控制字符。
- 创建只保存配置，不访问远端；初始 `enabled=true`，尚无工具目录。
- URL 必须是完整 endpoint，不自动拼接 `/mcp`。
- 修改配置不在保存时隐式连接；之后可手动测试／刷新，或由内部可用目录查询按预算刷新。
- Server 采用软删除，清空保存的加密凭据；其工具不再出现在任何公开查询中。重复删除只对当前 owner 幂等，其他 owner 始终返回 404。
- 删除后可以使用相同 `name` 新建 Server，但使用新 ID，不继承旧目录。

### 2.2 连接测试与健康探测

连接测试建立短生命周期 MCP 会话，完成 `initialize`、协议版本与能力协商，发送 `notifications/initialized`，确认远端声明 `tools` capability，随后关闭连接。

连接测试不获取或更新工具目录，也不改变 Server／工具的启用状态。成功只表示该配置在测试时通过握手，不代表持续在线，也不能将 Server 标记为工具发现健康。

面向工具的健康探测采用“初始化 + 完整分页 `tools/list` + 工具定义检查”，直接复用第 2.3 节的刷新流程。成功时同时更新目录与健康观测；失败时保留旧目录，但将当前配置的发现健康观测记为失败。空工具列表可以健康，只是没有工具可提供。

MCP `ping` 可探测已建立会话是否仍能响应，但不能证明工具发现或业务工具可用。本次使用短连接，不增加周期 ping 或独立心跳服务；也不通过执行工具探测健康。

健康状态按当前配置最近一次已提交的发现观测派生，不额外保存一份状态：

| 状态 | 判定 |
| --- | --- |
| `unknown` | 当前配置尚无发现观测，或观测超过有效期 |
| `healthy` | 当前配置最近发现成功，且观测仍在有效期内 |
| `unhealthy` | 当前配置最近发现失败，且观测仍在有效期内 |

配置变更后立即为 unknown。最近失败后的较早成功不能继续支撑 healthy；握手测试成功也不能清除发现失败。健康表示最近验证了协议和目录，不保证远端业务依赖或未来调用成功。

### 2.3 发现和刷新

用户主动刷新，客户端初始化后读取完整的 `tools/list`，包括所有分页，成功后一次性更新本地目录。

- 首次刷新和后续刷新使用相同流程；允许空工具列表。
- 同一个 `(server_id, remote_name)` 对应稳定工具 ID。
- 远端名称原样保存；供 LLM 使用的名称另外生成，并保存明确映射，不反向解析远端名称。
- 刷新只更新远端字段和可用状态，不能覆盖用户设置的 `enabled`。
- 工具从完整列表消失时标记 `available=false`，不删除；重新出现时恢复可用，保留 ID 和本地启用设置。
- 翻页失败、重复工具名、循环 cursor、格式不合法或超过上限，整次刷新失败，不提交部分结果，也不将未读到的工具标记为消失。
- 不订阅长期目录变化通知，不启动周期后台探测。用户可以手动刷新；内部工具目录查询也可以按第 2.5 节触发有界刷新，不做后台重试。

Server 被禁用后仍允许所有者显式测试和刷新，方便维护；这些操作不会重新启用 Server。

### 2.4 工具目录与启用状态

目录展示工具 ID、来源 Server、远端名称、可选标题／描述、输入 Schema、可选输出 Schema、annotations、本地启用状态和远端可用状态。

只允许修改工具的 `enabled`，不引入审批、风险等级或信任字段。远端 annotations 作为未经信任的元数据展示，不触发权限或策略变化。

区分三个事实：

- `enabled`：用户对工具的本地开关，首次发现默认 true。
- `available`：最近一次完整目录中是否存在该工具。
- `catalog_current`：目录对应的连接配置是否仍然是当前配置。

派生管理状态：

```text
catalog_current = discovery_config_version == server.config_version
effective_enabled = server.enabled AND tool.enabled AND available AND catalog_current
```

`effective_enabled` 仅表示当前管理配置允许使用，不代表已接入模型、获得执行授权或远端在线。无目录时 `catalog_current=false`。

URL 或 Header 发生变更时，旧目录保留用于查看，但立即变为过期；只有当前配置刷新成功才能恢复为当前目录。单纯修改展示名或启用开关不使目录过期。

刷新失败保留最后一次成功目录和时间；如果配置未变，目录仍是该配置最后一次成功发现的结果，同时明确展示最近刷新失败，不把失败推断为所有工具消失。

### 2.5 向后续 LLM 层提供工具定义

管理目录保留禁用、消失、过期和健康未知的条目。内部可用目录只返回当前 owner 的活动 Server 中满足下式的工具：

```text
eligible_for_llm = effective_enabled AND server.health_status == healthy
```

该标记只表示定义可提供给 LLM，不构成后续调用授权。Server 或工具禁用、目录过期、健康未知／失败时均排除；不能回退为未经筛选的管理目录。

应用层提供 `list_available_definitions(owner_id)`，owner 由可信调用方注入，不能来自模型参数。接口返回统一结构：

```text
AvailableToolDefinition
- tool_id
- name                  LLM 可见稳定名称
- description           经长度限制和控制字符清理的远端描述
- input_schema          原样保存的受限 JSON Schema
- server_id             仅内部路由使用
- remote_name           仅内部路由使用
- config_version        目录对应配置版本
- discovered_at         目录成功发现时间
```

模型侧只接收 name、description 和 input_schema。Header、凭据、URL、内部映射和 MCP 服务端指令不进入 LLM 工具定义。供应商格式适配、Schema 兼容性限制和实际 bind_tools 接入由后续 LLM 层负责；本次不声称所有远端 Schema 均兼容所有模型供应商。

LLM 名称采用 `mcp__<tool_id 的 UUID hex>`，长度固定且仅含 ASCII 字母、数字和下划线，不受 Server 展示名、远端特殊字符或不同 Server 重名影响。持久化 model_name 并加唯一约束；刷新保持不变。内部明确保存 name 到 tool_id／server_id／remote_name 的对应关系，后续不得靠拆解名称推断远端工具。

建议健康观测有效期为 5 分钟，可部署配置。内部查询只对已启用且健康 unknown 的 Server 触发一次有界发现；仍在有效期的 unhealthy Server 暂时排除，可由用户手动刷新提前恢复。过期失败也回到 unknown，下一次查询可重新检查。

查询刷新复用相同安全、并发与提交规则，设置整次查询截止时间和最多尝试 Server 数；超过预算的 Server 暂时排除，不返回旧工具冒充可用。返回 `definitions` 及内部 `excluded_servers`（ID 和安全原因），明确空目录或部分目录的原因，不将诊断内容送入 LLM。数据库不可用整体失败，不伪装为空目录。

刷新完成后以同一次数据库读取重新筛选当前启用状态、配置版本和健康有效期；并发配置更新或撤销不能仅凭刷新开始时的状态进入结果。目录是读取时的观测，不承诺未来执行成功，也不替代后续执行前检查。

## 3. 模块边界

沿用现有分包模式，实际实施时新增以下职责：

| 位置 | 职责 |
| --- | --- |
| `backend/src/domain/mcp/` | Server、发现结果、业务异常和仓储／发现客户端端口 |
| `backend/src/domain/tool/` | 工具目录实体、查询与启用管理端口 |
| `backend/src/app/mcp/` | Server 管理、测试、刷新用例 |
| `backend/src/app/tool/` | 管理目录查询、工具启用管理、供 LLM 层使用的可用定义查询 |
| `backend/src/adapter/mcp/` | Server 与工具目录的事务持久化 |
| `backend/src/adapter/tool/` | 工具目录查询与启用持久化 |
| `backend/src/infrastructure/mcp/` | MCP SDK 适配、安全 HTTP 传输与短生命周期连接 |
| `backend/src/infrastructure/security/` | 本次所需的 Header 加解密实现 |
| `backend/src/api/mcp.py`、`backend/src/api/tools.py` | 管理 API，配套 DTO 与 converter |

领域层不得依赖 FastAPI、数据库、HTTP 或 MCP SDK。API 不直接暴露 ORM 和 SDK 对象。工具表只有一份；两个适配器按职责使用同一映射，不建立重复目录或额外策略表。刷新事务由 MCP 仓储完成，不通过逐条调用工具管理服务拼接事务。

`adapter/models.py` 聚合新增 ORM，`main.py` 组装服务和客户端；沿用现有一套数据库 engine。迁移使用 Alembic，不在启动时自动建表。

本次不创建 `ToolInvocation`、`ToolResult`、`run_tool_snapshots`、`tool_policies`、调用执行网关或空 builtin 包。

## 4. 数据设计

新实体 ID 采用带前缀的 UUID，不沿用项目的进程内毫秒 ID。时间采用 UTC。下述 JSON 字段应有明确的边界类型和转换，不能将任意 SDK 对象直接入库。

### 4.1 mcp_servers

```text
id                          主键
owner_id                    非空，外键 users.id
name, display_name
transport                   首版仅 streamable_http
url
header_credentials_json     私有加密封装；无 Header 时为空
                            包含 ciphertext、nonce、key_version
enabled                    用户开关
config_version              初始 1，URL／Header 变化时递增
management_revision         测试／刷新顺序与旧结果失效控制
last_operation              test / refresh；尚未操作为空
last_operation_status       success / error；尚未操作为空
last_error_code             安全机器码，不保存远端正文
last_operation_at           最近有效操作完成时间
last_test_at                最近有效测试完成时间
last_test_success           可空布尔
last_refresh_at             最近有效刷新完成时间
last_refresh_success        可空布尔
last_discovered_at          最近一次成功提交目录时间
discovery_config_version   最近成功目录所对应配置版本；初始为空
created_at, updated_at, deleted_time
```

- 活动行 `(owner_id, name)` 唯一，列表按 owner 与未删除条件过滤。
- 不保存“connected”或“online”持续状态。时间和结果都是历史观测。
- 配置变更清空旧配置的测试／刷新结果字段；保留 `last_discovered_at` 和 `discovery_config_version` 以明确旧目录过期。
- 无需通用 `secret_refs` 表：凭据仅服务于所属 Server，由该 Server 原子管理。

### 4.2 mcp_tools

```text
id                          主键
server_id                   外键 mcp_servers.id
remote_name
model_name                  稳定 LLM 名称，唯一
title                      可空远端展示标题
description                可空
input_schema_json
output_schema_json          可空
annotations_json            可空
enabled                    用户开关；首次发现 true
available                   最近完整列表是否包含
first_discovered_at
last_seen_at                最近一次完整刷新包含该工具的时间
updated_at
```

- `(server_id, remote_name)` 唯一；远端名称按精确值匹配，不做大小写折叠。
- 不重复保存 owner；所有工具查询通过 JOIN 活动 Server 并筛选 owner。
- 保存稳定模型名称，不保存执行策略、信任哈希或定义历史。当前目录不需要执行快照。
- 输入 Schema 用于管理展示及统一工具定义输出，输出 Schema 仅作为发现元数据保存。验证发现结构、Schema 为 JSON object 及元数据限制；本次不实现调用参数校验，也不获取远程 `$ref`。

## 5. 凭据与网络安全

### 5.1 Header 管理

建议首版仅允许在配置应用主密钥后保存 Header 值；未配置时仍允许无认证 Server，但携带非空 Header 的创建／更新返回 `503 mcp_secret_storage_unavailable`。不提供任意进程环境变量引用，防止用户通过 MCP 配置读取宿主机秘密。

这是对旧草案凭据方案的收敛建议，需随本文一起确认。

- 使用成熟密码库的认证加密（AEAD），每次加密独立随机 nonce，绑定 owner 与 Server ID 为附加认证数据，记录 key_version。
- 主密钥不入数据库，不写日志；同一部署的应用进程必须使用相同有效密钥。配置错误或无法解密时失败关闭，不能以无认证请求降级。
- 本次不建设在线密钥轮换功能；部署更换主密钥前必须保留旧版本解密能力或另行完成受控重加密，不能直接丢弃旧密钥。
- Header 写入是 write-only。读取只返回名称与 `has_value`，不返回明文、末尾字符或密文。
- PATCH 不传 `headers` 保留；传入映射整体替换；`{}` 清空；`null` 拒绝。更新时需要提交全部要保留的 Header 值，不接受占位掩码回写。
- Header 名大小写不敏感，不允许重复规范化名称；拒绝 CR/LF、非法名称和协议控制字段，例如 Host、Content-Length、Connection、Transfer-Encoding、Accept、Content-Type、MCP-Session-Id、MCP-Protocol-Version。认证 Header 允许使用 Authorization、X-API-Key 等合法名称。
- 不透传用户浏览器 Cookie，不接受客户端指定其他 Server 的凭据 ID。

### 5.2 URL 与出站连接

- 默认仅允许 HTTPS，始终验证证书和主机名；URL 拒绝 userinfo、query 和 fragment，避免在 endpoint 中保存凭据。
- 禁止自动跟随重定向，不把认证 Header 发往重定向目标。
- 拒绝回环、私网、链路本地、非公网保留地址和云元数据地址，覆盖 IPv4、IPv6 及 IPv4-mapped IPv6。
- 连接前解析 DNS，所有解析结果都必须通过规则；将校验的地址绑定到实际出站连接，同时保留原主机的 TLS SNI／证书验证，不能校验后交由客户端重新解析。
- 禁用 HTTP 客户端从进程环境继承代理，防止绕过目标地址检查。
- 自托管内网 MCP 只能由部署配置允许指定主机／网段；HTTP 也只能在显式配置的目标范围内允许。普通用户不能修改例外。例外不关闭 TLS 验证，也不解除元数据地址等明确危险目标的禁止规则。
- 以上规则作用于初始化、分页等全部请求。URL 保存时做格式校验；实际出站前必须重新执行网络检查。

远端描述、Schema 和错误都是不可信数据。展示按文本或 JSON 转义，不渲染任意 HTML；描述去除控制字符并限制长度。日志只记录操作类型、内部 ID、安全错误码和耗时，不写 Header、远端响应正文或 SDK 原始异常。请求校验错误也不得回显 Header 值；需要针对敏感 DTO 定制安全错误输出。

## 6. MCP 管理客户端

每次 test／refresh 都创建独立客户端及 MCP 会话，操作结束或失败后释放 HTTP 连接。只在单次操作内部复用会话，不创建跨 Run、跨用户、跨操作或跨进程共享连接池／会话管理器；底层有界资源由应用生命周期清理。

- 遵循 MCP 初始化和版本协商，基于支持 Streamable HTTP 的 SDK；具体依赖版本在实施前锁定并验证。
- 支持 Streamable HTTP 的 JSON 响应和其内部的 SSE 流式响应。这不等于支持旧 HTTP+SSE 传输，不提供自动旧传输回退。
- 无 tools capability 返回 `mcp_tools_not_supported`。
- 发现处理分页，检测重复 cursor、重复名称、非法响应和超额结果。
- 不接受需要用户交互的反向能力请求，不触发工具执行，也不下载 Schema 引用地址。
- 超时／网络失败不后台重试；用户可以再次主动操作。

## 7. 事务与并发

数据库事务不能覆盖远端网络等待，避免长时间占用业务连接和行锁。

### 7.1 启动操作

1. 短事务按 owner 查询活动 Server 并锁定该行。
2. 递增 `management_revision`，读取配置、config_version 和本次 revision，提交事务。
3. 在事务外解密并进行网络操作。

不持久化长期 `running` 状态，也不创建后台任务。请求中断后旧目录不变，用户可重新操作；下一次操作产生新 revision。

### 7.2 提交结果

网络完成后，开启短事务并锁定同一 Server 行。仅当 owner、未删除状态、config_version 和 management_revision 均匹配时提交成功或失败结果；否则返回 `409 mcp_operation_superseded`，不更新状态或目录。

- 较晚发起的测试／刷新使较早操作失效，即使较早操作更晚结束。
- URL／Header 更新同时递增 config_version 与 management_revision，使旧操作失效。
- 删除同样使旧操作不可提交，不能复活 Server 或写回凭据。
- 展示名／enabled 更新不改变连接配置，不必使管理操作失效；提交结果不得覆盖这些字段。
- test 成功只写测试观测，不使旧目录变新，也不覆盖发现健康观测。

刷新先在内存中完整读取和验证，再在一个事务中批量 upsert、标记消失工具并更新目录版本／观测字段。写入失败全部回滚，保留旧目录。

工具启用修改也锁定所属 Server 行；刷新只更新远端字段。无论启用修改发生在网络等待前后，都不能被刷新覆盖。不存在的工具不能通过 PATCH 创建。

Server 数量上限检查与创建通过锁定真实 `users` 行在同一事务中串行化，不能只做无锁计数。不同用户互不串行化。

## 8. HTTP 接口

所有接口需要有效登录；owner 仅来自 `CurrentAuth`。跨 owner 或软删除资源返回 404。所有写接口复用 `verify_request_origin`，请求 DTO 拒绝额外字段。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /api/v1/mcp/servers` | 分页查看自己的活动 Server |
| `POST /api/v1/mcp/servers` | 保存 Server；201，不自动发现 |
| `GET /api/v1/mcp/servers/{server_id}` | 查看配置摘要与最近观测 |
| `PATCH /api/v1/mcp/servers/{server_id}` | 更新展示名、URL、Header、enabled；稳定 name 不可改 |
| `DELETE /api/v1/mcp/servers/{server_id}` | 软删除且清除凭据；204 |
| `POST /api/v1/mcp/servers/{server_id}/test` | 同步测试；不更新目录 |
| `POST /api/v1/mcp/servers/{server_id}/refresh` | 同步完整刷新，返回新增／更新／消失数量及目录时间 |
| `GET /api/v1/mcp/servers/{server_id}/tools` | 分页查询该 Server 工具 |
| `GET /api/v1/tools` | 分页查询自己的 MCP 工具，可按 server_id 筛选 |
| `GET /api/v1/tools/{tool_id}` | 工具详情，包含完整的受限元数据 |
| `PATCH /api/v1/tools/{tool_id}` | 只接受非空布尔字段 enabled |

分页沿用 `items / total / page / page_size`，默认 20、最大 100。Server 按 updated_at、id 降序；工具按 remote_name、id 升序。目录列表省略 Schema 正文，详情返回 Schema。各工具接口均返回 server_enabled、catalog_current 和 effective_enabled，避免隐藏父级开关影响。消失和过期工具仍可查询，状态明确展示。

Server 响应另返回派生的 health_status、health_checked_at（last_refresh_at）、health_expires_at；工具响应返回 eligible_for_llm。GET 管理接口只读取，不隐式访问远端。LLM 可用目录通过第 2.5 节内部应用接口提供，本次不新增对外 LLM 接口。

Server 响应不返回 owner_id 和凭据封装；Header 摘要为名称及是否有值。URL 按已验证的不含敏感参数的形式返回。

### 8.1 错误约定

| HTTP 状态 | 示例安全错误码 |
| --- | --- |
| 401 | `not_authenticated` |
| 403 | `origin_not_allowed` |
| 404 | `mcp_server_not_found`、`tool_not_found` |
| 409 | `mcp_server_name_conflict`、`mcp_server_limit_exceeded`、`mcp_operation_superseded` |
| 422 | `mcp_config_invalid`、`mcp_target_not_allowed` |
| 429 | `mcp_management_busy`，当前进程并发已满 |
| 502 | `mcp_connection_failed`、`mcp_auth_failed`、`mcp_protocol_error`、`mcp_tools_not_supported`、`mcp_discovery_limit_exceeded` |
| 504 | `mcp_operation_timeout` |
| 503 | `database_unavailable`、`mcp_secret_storage_unavailable`、`mcp_credentials_unavailable` |

MCP 请求的 OSError 在客户端边界转换为 MCP 错误，不能扩大现有数据库 OSError 路由捕获范围而将外部故障误报为数据库失败。远端 401／403 映射为上游认证失败，不等于当前用户登录失效。

有效 test／refresh 失败保存安全错误码和该操作观测，返回对应失败状态；目录保持不变。数据库本身失败时不承诺错误状态能落库。客户端断开也不承诺记录完成状态，且不自动重跑。

## 9. 资源上限建议

以下是本次建议默认值，不是用户已确认的配额，可通过部署配置调整；不新增用户配额管理接口。

| 项目 | 建议默认上限 |
| --- | --- |
| 每用户活动 Server | 20 |
| 单 Server 工具数 | 200 |
| 单次发现页数 | 100 |
| 单次管理操作总接收数据 | 2 MiB，包含全部分页与 SSE 流开销 |
| 单工具元数据 | 128 KiB |
| 描述长度 | 8,000 字符 |
| JSON 嵌套深度 | 32 |
| 单次连接超时 | 5 秒 |
| 整次操作截止时间 | 30 秒，包含 DNS、初始化与全部分页 |
| 进程内并发 test／refresh | 8，满额直接拒绝 |
| 发现健康观测有效期 | 5 分钟 |
| 单次内部可用目录查询总时限 | 30 秒，包含查询触发的全部刷新 |
| 单次内部查询最多探测 Server | 5，按最近探测时间升序、从未探测优先，再按 ID 排序 |
| 单 Server Header | 20 项，合计 16 KiB |
| endpoint URL | 2,048 字符 |

字节限制在流式读取时执行，不能先无限读取再检查；若启用解压，对解压后数据也必须设限。名称和结构错误导致整次发现失败；描述允许安全清理并截断，不能截断 Schema 后当作合法定义保存。

并发上限是每进程资源保护，不宣称全部署统一限流。跨进程重复操作由数据库 revision 保证提交顺序，不保证只产生一次网络请求。分布式管理任务和公平配额不在本次范围。

## 10. 实施顺序与迁移

本节仅为后续实施建议；用户确认本文后才能进入代码阶段。

1. 新增领域与应用模型、Server／目录仓储及 Alembic 迁移，完成 owner 隔离、CRUD 与启用管理。
2. 接入 Header 加密和安全出站客户端，完成 test。
3. 接入完整发现、原子刷新和版本并发控制。
4. 提供健康状态派生、稳定工具命名和面向 LLM 层的内部可用目录查询。
5. 接入管理 API、安全错误转换及验收测试。

迁移只新增本次表、外键、唯一约束和查询索引，不重建现有用户、项目、Session 或消息表，不回填虚构数据。新表初始为空；回滚删除本次表会丢失管理配置和凭据，需要按部署环境明确备份后执行。主密钥不随数据库备份存放，但恢复凭据时必须具备对应密钥。

引入 MCP SDK、HTTP 安全传输和加密库前，需验证依赖兼容 Python 3.12、支持 JSON／SSE 响应以及目标 IP 绑定。SDK 默认传输若无法满足网络防护，不能以仅做 URL 预检查代替，必须实现受控传输适配。现有仓库尚未具备这些依赖。

## 11. 验收标准

| 场景 | 可观察结果 |
| --- | --- |
| 未登录／跨用户访问 | 未登录 401；跨用户 Server、工具和管理操作 404，无网络请求 |
| 创建和保存配置 | 不连接远端；同 owner 名称冲突 409，不同 owner 可重名 |
| CRUD 与删除 | name 不可变；软删除后目录不可见；凭据清空；同 owner 重复删除幂等 |
| Header 操作 | 省略保留、映射替换、空映射清空；响应／日志／校验错误无值与密文泄露 |
| 未配置主密钥 | 无 Header Server 可保存；非空 Header 写入拒绝；已有凭据无法解密则不连接 |
| 测试成功／失败 | 正确保存当前配置的观测，不更新目录、不改变 enabled |
| 健康探测 | 握手成功不等于 healthy；完整发现成功才 healthy；空列表健康但无工具 |
| 健康时效 | 当前发现失败立即 unhealthy；超期／配置变更 unknown；测试成功不覆盖发现失败 |
| LLM 可用目录 | 仅返回当前 owner 启用、存在、当前配置且健康的定义；禁用／失败／过期条目仍在管理目录中 |
| 名称和输出边界 | 跨 Server 同名与特殊远端名称不冲突；刷新后模型名称不变；定义中无凭据或服务端指令 |
| 按需探测 | unknown 按预算刷新；有效期内 unhealthy 不自动重试；单 Server 失败不混入旧定义；超预算明确排除原因 |
| 查询并发与故障 | 查询触发刷新后配置修改／禁用，最终筛选排除；数据库失败不伪装空工具列表 |
| 正常发现 | JSON 与 Streamable HTTP SSE 响应均可发现，分页完整、空列表合法 |
| 刷新失败 | 翻页失败、循环 cursor、重复名、超限或数据库失败，不出现半份新目录 |
| 工具消失再出现 | available 切换，ID 和本地 enabled 保持 |
| URL／Header 更新 | config_version 递增，旧目录过期；测试成功仍过期，刷新成功后才变为当前 |
| 并发操作 | 旧配置或旧 revision 的成功／失败均不能覆盖较新状态；删除后不能写回 |
| 刷新与开关并发 | 本地开关不会被发现覆盖，派生状态正确 |
| 并发创建 | 同用户不能绕过 Server 数量上限；名称唯一由数据库保证 |
| 网络安全 | 私网／IPv6／混合 DNS／rebind／重定向／环境代理不能绕过规则；SNI 和证书验证正常 |
| 资源安全 | 超时、分页、字节及解压限制生效，失败后连接释放、数据库不长期持锁 |
| 管理边界 | 所有管理测试不发送 tools/call，不写消息，不创建 Run／审批／调用账本 |

测试应覆盖应用规则、API 隔离与安全输出、受控 MCP 测试服务的协议响应，以及真实 PostgreSQL 的锁／唯一约束／原子提交。不得依赖公网 MCP 或真实业务凭据完成自动化测试。

## 12. 待确认与就绪状态

管理、发现健康探测以及向后续 LLM 层提供筛选后的工具定义范围已经确认；本设计没有将调用阶段功能作为实施前置条件。代码实施前仍需用户确认本文提出的具体选择：

1. 本次交付仅后端 HTTP 管理能力，不含前端页面。
2. Header 采用应用主密钥加密保存，取消用户指定任意进程环境变量的入口；无主密钥仍可管理无认证 Server。
3. 保存不自动发现，test 和 refresh 分离，采用每操作独立短连接。
4. 采用本文资源上限作为可部署调整的初始默认值。
5. 健康有效期 5 分钟、无周期后台探测、内部查询按预算刷新 unknown Server，以及基于工具 UUID 的稳定模型名称。

方案在需求层可评审；SDK／安全传输兼容性和加密依赖版本仍需在实施准备时验证。本文未授权代码实施，尚未经过独立 Reviewer 审查。

## 13. 协议参考

下列指定版本规范用于核对工具分页与传输行为，不表示首版承诺支持所有 MCP 扩展，也不声称它是最新规范：

- [MCP Tools（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)：tools capability、tools/list 分页和工具元数据。
- [MCP Transports（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)：Streamable HTTP 的 JSON／SSE 响应，以及与旧传输的区别。
- [MCP Lifecycle（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)：初始化、能力协商与 initialized 通知。
- [MCP Ping（2025-06-18）](https://modelcontextprotocol.io/specification/2025-06-18/basic/utilities/ping)：已建立连接的响应性探测。
