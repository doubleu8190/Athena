# Athena v2 第一期企业级后端重构 API 契约与二期前端交接文档

> **适用分支 / 基线**：`feat/phase1-enterprise-foundation`（Alembic 唯一 head：`20261204_0037`）
> **面向读者**：第二期企业级前端重构开发与 QA 联调人员
> **全局通用约定**：
> - **认证方式**：服务端可撤销 HttpOnly 同站 Cookie（`athena_session`，`SameSite=Lax`、`Path=/`，HTTPS 生产环境 `Secure=true`）。
> - **同源保护**：全部写请求（`POST` / `PUT` / `PATCH` / `DELETE`）如携带 `Origin` 请求头，必须与当前请求源或 `ATHENA_PUBLIC_ORIGIN` 严格同源，否则前置中间件直接返回 `403 {"detail": "origin_not_allowed"}`。
> - **统一错误结构**：HTTP 错误统一返回 `{"detail": "<stable_error_code>"}`（极少数 Pydantic 校验错误返回脱敏后的字段错误列表，不回显请求入参明文）。
> - **未登录响应**：未携带有效会话 Cookie 访问非公开 `/api/v1/*` 路由统一由 `AuthPrecheckMiddleware` 前置返回 `401 {"detail": "not_authenticated"}`。
> - **角色枚举升级（Breaking Change）**：原 `admin` / `user` 已全面升级为企业三级角色：`super_admin`（超级管理员）、`dept_admin`（部门管理员）、`member`（普通员工）。

---

## 目录

1. [认证、飞书 SSO、本地 Mock 快捷登录与角色升级](#一认证飞书-sso本地-mock-快捷登录与角色升级)
2. [组织通讯录管理 API（超管专属）](#二组织通讯录管理-api超管专属)
3. [轻量个人工作偏好与显式记忆 API](#三轻量个人工作偏好与显式记忆-api)
4. [统一三层可见性（知识库 / Skills / MCP / Subagent）](#四统一三层可见性知识库--skills--mcp--subagent)
5. [对话能力底座升级（无项目知识库 / 场景化模型 / 项目增强 / 会话松绑 / 自动标题）](#五对话能力底座升级)
6. [管理后台运维与业务化待审批卡片投影](#六管理后台运维与业务化待审批卡片投影)

---

## 一、认证、飞书 SSO、本地 Mock 快捷登录与角色升级

### 与旧版核心差异（Breaking Changes）
1. **开放自助注册彻底下线**：`POST /api/v1/auth/register` 已从路由表与公开白名单中永久移除（匿名调用被前置鉴权返回 `401`，已登录调用返回 `404`）。首次初始化应急超管须由运维执行 `python -m cli.bootstrap_super_admin`。
2. **角色枚举变更**：`UserResponse.role` 取值由旧版 `"admin" | "user"` 变更为 `"super_admin" | "dept_admin" | "member"`。
3. **身份投影扩充**：`GET /api/v1/auth/me` 与登录响应新增 `auth_provider`、`employee_no`、`job_title`、`avatar_url`、`departments`、`managed_department_ids`。

### 公共枚举
- `UserRole`: `"super_admin" | "dept_admin" | "member"`
- `AuthProvider`: `"local" | "feishu" | "mock"`
- `UserStatus`: `"active" | "disabled"`

### 1.1 查询登录能力声明
- **端点**：`GET /api/v1/auth/providers`
- **权限**：公开（无需登录），响应带 `Cache-Control: no-store`
- **响应 `200 OK`**：
```json
{
  "feishu_enabled": false,
  "local_login_enabled": true,
  "mock_enabled": true
}
```
> 前端登录页应先拉取此接口，动态决定展示「飞书一键登录」按钮、本地应急邮箱密码表单，以及非生产环境下的「Mock 快捷切换登录」面板。

### 1.2 飞书原生 OAuth 登录（发起与回调）
- **发起授权**：`GET /api/v1/auth/feishu/authorize?return_to=/chat`
  - **权限**：公开；`return_to` 仅允许站内安全相对路径（如 `/chat`），拒绝绝对 URL、协议相对路径（`//evil.com`）与控制字符。
  - **响应**：`302 Found`，`Location` 指向飞书授权页，并写入一次性防重放 `state`。
  - **错误码**：`409 feishu_not_enabled`、`400 invalid_return_to`、`503 oauth_state_unavailable`。
- **OAuth 回调**：`GET /api/v1/auth/feishu/callback?code=<oauth_code>&state=<state>`
  - **权限**：公开；后端原子消费一次性 `state`、换取飞书用户身份、同步本地记录并签发 `athena_session` HttpOnly Cookie 后 `302 Found` 跳转回 `return_to`。
  - **错误码**：`409 feishu_not_enabled`、`400 invalid_oauth_callback`、`400 invalid_oauth_state`、`403 user_disabled`（离职或被停用员工禁止登录）、`502 feishu_auth_failed`、`503 oauth_state_unavailable`。

### 1.3 本地应急账号登录、登出、改密与个人资料
- **`POST /api/v1/auth/login`**（公开）：请求 `{"email": "...", "password": "...", "remember_me": false}`，返回 `200 UserResponse` 并写入 `athena_session` Cookie。仅允许 `status=active` 且带本地密码哈希的账号登录；错误码：`401 invalid_credentials`、`429 rate_limited`、`503 rate_limit_unavailable`。
- **`POST /api/v1/auth/logout`**（公开）：撤销当前会话并清除 Cookie，返回 `204 No Content`。
- **`GET /api/v1/auth/me`**（需登录）：返回当前用户的实时组织身份投影 `200 UserResponse`：
```json
{
  "id": "mock_fin_admin",
  "email": "mock_fin_admin@mock.athena.local",
  "display_name": "财务主管",
  "created_at": "2026-12-05T09:00:00Z",
  "role": "dept_admin",
  "auth_provider": "mock",
  "employee_no": "MOCK002",
  "job_title": "财务部负责人",
  "avatar_url": null,
  "departments": [
    {
      "id": "dept_mock_fin",
      "name": "财务部",
      "path": "/dept_mock_root/dept_mock_fin/",
      "is_primary": true
    }
  ],
  "managed_department_ids": ["dept_mock_fin"]
}
```
  > 前端应使用 `role` 与 `managed_department_ids` 控制导航菜单显隐（如仅 `super_admin` 展示组织管理/模型/MCP/子智能体管理；`dept_admin` 可见本部门知识库与 `/api/v1/admin/skills` 部门技能管理入口）。
- **`PATCH /api/v1/auth/me`**（需登录）：请求 `{"display_name": "新名称"}`，返回 `200 UserResponse`（校验失败 `422 display_name_invalid`）。
- **`POST /api/v1/auth/password`**（需登录）：请求 `{"current_password": "...", "new_password": "..."}`，成功返回 `204` 并吊销其他设备会话。飞书与 Mock 账号调用返回 `400 password_change_not_supported`；当前密码错误返回 `400 current_password_invalid`；新密码不合规返回 `422 password_invalid`。

### 1.4 非生产本地 Mock 快捷登录（仅 `ATHENA_AUTH_MOCK_ENABLED=true` 且非生产挂载）
> 未开启 Mock 模式时以下端点不注册，因仍位于公开认证白名单内，匿名与已登录调用均返回 `404`。生产环境开启 `ATHENA_AUTH_MOCK_ENABLED=true` 会直接阻断 API 启动。

- **获取预置 Mock 账号清单**：`GET /api/v1/auth/mock/users`（公开）
  - **响应 `200 OK`**：返回 `{"items": [MockUserResponse, ...]}`（预置 6 个典型角色账号：`mock_super_admin`、`mock_fin_admin`、`mock_rd_admin`、`mock_fin_member`、`mock_rd_member`、`mock_hr_member`）。
  - `MockUserResponse` 字段：`id`, `display_name`, `email`, `role`, `employee_no`, `job_title`, `avatar_url`, `departments`, `managed_department_ids`。
- **一键签发真实 Cookie 会话**：`POST /api/v1/auth/mock/login`（公开，受同源校验）
  - **请求体**：`{"user_id": "mock_fin_admin"}`（`1..128` 字符，`extra="forbid"`）
  - **响应 `200 OK`**：`UserResponse` 并写入真实服务端 `athena_session` HttpOnly Cookie。
  - **错误码**：`404 mock_user_not_found`、`401 invalid_credentials`（账号已停用）、`409 auth_mock_not_enabled`（服务层检测到 Mock 未启用）。

---

## 二、组织通讯录管理 API（超管专属）

**路由前缀**：`/api/v1/admin/org`
**统一权限**：仅 `super_admin` 可访问；`dept_admin` 与 `member` 访问均返回 `403 {"detail": "admin_required"}`。全部响应附带 `Cache-Control: no-store`。

### 2.1 查询部门树列表
- **端点**：`GET /api/v1/admin/org/departments?include_archived=false`
- **说明**：按物化路径 `path` 与 `order_index` 有序返回扁平部门节点列表，前端可根据 `parent_id` 或 `path` 直接渲染组织树。
- **响应 `200 OK`**：
```json
{
  "items": [
    {
      "id": "dept_mock_root",
      "external_id": "mock_dept_root",
      "parent_id": null,
      "name": "集团总部",
      "path": "/dept_mock_root/",
      "order_index": 1,
      "status": "active",
      "synced_at": "2026-12-05T09:00:00Z",
      "created_at": "2026-12-05T09:00:00Z",
      "updated_at": "2026-12-05T09:00:00Z"
    }
  ]
}
```
  > `status` 枚举：`"active" | "archived"`。

### 2.2 分页检索企业用户
- **端点**：`GET /api/v1/admin/org/users`
- **Query 参数**：
  | 参数 | 类型 | 默认值 | 说明 |
  | --- | --- | --- | --- |
  | `department_id` | `string \| null` | `null` | 按指定部门筛选 |
  | `include_subdepartments` | `bool` | `true` | 为 `true` 时使用 `path` 前缀匹配涵盖全部子部门成员 |
  | `role` | `UserRole \| null` | `null` | `super_admin` / `dept_admin` / `member` |
  | `status` | `UserStatus \| null` | `null` | `active` / `disabled` |
  | `q` | `string \| null` | `null` | 模糊搜索显示名、邮箱或工号 |
  | `page` | `int (>=1)` | `1` | 页码 |
  | `page_size` | `int (1..100)` | `20` | 每页条数 |
- **响应 `200 OK`**：`{"items": [OrgUserResponse, ...], "total": 6, "page": 1, "page_size": 20}`
  - `OrgUserResponse` 字段：`id`, `email`, `display_name`, `role`, `status`, `auth_provider`, `employee_no`, `job_title`, `avatar_url`, `last_synced_at`, `created_at`, `updated_at`, `departments`, `managed_departments`（`[{department_id, department_name, department_path, include_subdepartments}]`）, `managed_department_ids`。

### 2.3 设置用户角色及部门管辖范围
- **端点**：`PUT /api/v1/admin/org/users/{user_id}/role`
- **请求体**（`extra="forbid"`）：
```json
{
  "role": "dept_admin",
  "managed_departments": [
    {
      "department_id": "dept_mock_fin",
      "include_subdepartments": true
    }
  ]
}
```
  - 当 `role="dept_admin"` 时，`managed_departments` 必须非空且指向未归档的合法部门；
  - 当 `role="super_admin"` 或 `"member"` 时，`managed_departments` 应为空数组（旧管辖范围会被自动清空）。
- **响应 `200 OK`**：`OrgUserResponse`。
- **错误码**：
  - `404 user_not_found`
  - `422 department_not_found` / `dept_admin_departments_required` / `duplicate_department_scope`（部门不存在、为空或重复配置管辖范围）
  - `409 last_super_admin_protected`（禁止降权系统内最后一位活跃超级管理员）

### 2.4 启用或停用用户
- **端点**：`PUT /api/v1/admin/org/users/{user_id}/status`
- **请求体**：`{"status": "disabled"}`（`"active" | "disabled"`）
- **事务与安全语义**：切换为 `disabled` 时在**同一数据库事务内吊销该用户全部未过期会话**，其历史 Cookie 立即失效（后续请求返回 `401`）。
- **响应 `200 OK`**：`OrgUserResponse`。
- **错误码**：`404 user_not_found`、`409 last_super_admin_protected`。

### 2.5 手动触发飞书通讯录同步
- **端点**：`POST /api/v1/admin/org/sync`
- **响应 `200 OK`**：
```json
{
  "departments_upserted": 12,
  "departments_archived": 1,
  "users_upserted": 85,
  "users_disabled": 2,
  "sessions_revoked": 3,
  "synced_at": "2026-12-05T09:05:00Z"
}
```
- **错误码**：`409 feishu_not_enabled`、`422 department_cycle_detected`、`422` 通讯录同步校验细分错误码（`department_external_id_empty` / `duplicate_department_external_id` / `department_depth_exceeded` / `empty_remote_departments` / `empty_remote_employees` / `department_path_too_long` / `org_sync_validation_failed`）、`502 feishu_sync_failed`。

---

## 三、轻量个人工作偏好与显式记忆 API

### 与旧版核心差异（Breaking Changes）
- **旧版重型自动记忆接口全部下线**：原 `/api/v1/memories*`、自动提取状态查询与 `backend-memory-worker` 已彻底移除。
- **新版定位**：极简、可控、面向最终用户透明的个人工作背景指令 + 显式备忘记忆（单用户上限 **50 条**）。对话中模型亦可调用免审批内置工具 `save_user_memory` 写入（`source="chat_explicit"`）。

### 3.1 读取个人工作偏好
- **端点**：`GET /api/v1/me/preferences`（需登录）
- **响应 `200 OK`**（从未设置过时返回空字符串 `custom_instructions=""`、`memory_enabled=true` 且 `updated_at=null`）：
```json
{
  "custom_instructions": "我是财务专员，请先给结论再列制度依据。",
  "memory_enabled": true,
  "updated_at": "2026-12-05T09:10:00Z"
}
```

### 3.2 更新个人工作偏好与显式记忆开关
- **端点**：`PUT /api/v1/me/preferences`（需登录）
- **请求体**（`extra="forbid"`）：
```json
{
  "custom_instructions": "我是财务专员，请先给结论再列制度依据。",
  "memory_enabled": true
}
```
  - `custom_instructions`：最大 `4000` 字符（拒绝除换行/制表符外的控制字符，允许空字符串清空）。
  - `memory_enabled`：设为 `false` 后，Run 上下文不再注入显式备忘记忆，且不再向模型暴露 `save_user_memory` 工具。
- **响应 `200 OK`**：`UserWorkProfileResponse`。
- **错误码**：`422 custom_instructions_invalid`。

### 3.3 显式记忆 CRUD
- **列出全部显式记忆**：`GET /api/v1/me/memories`（需登录）
  - **响应 `200 OK`**（按 `updated_at` 倒序）：
  ```json
  {
    "items": [
      {
        "id": "umem_01...",
        "content": "周报金额统一使用万元为单位",
        "source": "chat_explicit",
        "created_at": "2026-12-05T09:12:00Z",
        "updated_at": "2026-12-05T09:12:00Z"
      }
    ],
    "total": 1,
    "limit": 50
  }
  ```
  - `source` 枚举：`"manual"`（前端手动新增）| `"chat_explicit"`（对话中通过 `save_user_memory` 保存）。
- **手动新增**：`POST /api/v1/me/memories`
  - 请求体：`{"content": "常用报表按月度汇总"}`（去首尾空白后 `1..500` 字符）。
  - 响应：`201 Created`（带 `Location: /api/v1/me/memories/{id}`）返回 `UserMemoryResponse`。
  - 错误码：`422 user_memory_content_invalid`、`409 user_memory_limit_reached`（已达 50 条上限）。
- **修改正文**：`PATCH /api/v1/me/memories/{memory_id}`
  - 请求体：`{"content": "更新后的备忘"}`，响应 `200 UserMemoryResponse`。
  - 错误码：`422 user_memory_content_invalid`、`404 user_memory_not_found`（不存在或非本人记忆）。
- **删除**：`DELETE /api/v1/me/memories/{memory_id}`
  - 响应 `204 No Content`；不存在或跨用户返回 `404 user_memory_not_found`。

---

## 四、统一三层可见性（知识库 / Skills / MCP / Subagent）

### 4.1 共享数据模型与权限矩阵

四条业务线共享统一的三层可见性字段与授权表 `resource_visibility_grants`：

- **`visibility_scope`**（Skills 接口中命名为 **`scope`**）：
  - `"company"`：全公司活跃员工可读；仅 `super_admin` 可创建/修改/删除。
  - `"department"`：归属 `owner_department_id`，默认自动为归属部门（含子部门）生成一条授权记录，并支持额外配置多条 `visibility_grants` 跨部门或按角色共享；`super_admin` 与管辖该归属部门的 `dept_admin` 可管理（MCP 与 Subagent 仅限 `super_admin` 管理）。
  - `"personal"`：仅归属者本人可读可管理（**仅知识库与个人 Skills 支持**；MCP 与 Subagent 仅支持 `"company" | "department"`）。
- **授权结构（`VisibilityGrantRequest` / `VisibilityGrantResponse`）**：
```json
{
  "target_type": "department",
  "target_id": "dept_mock_fin",
  "include_subdepartments": true
}
```
  - `target_type`：`"department" | "role"`（`role` 时 `target_id` 取 `"super_admin" | "dept_admin" | "member"`）。
  - 响应中每条记录额外包含 `id` 与 `created_at`。

### 4.2 统一的 403 / 404 安全边界约定（前端必须正确处理）
- **无读权限资源一律返回 `404`**：当用户直调自己无权查看的跨部门知识库、技能或工具 ID（如研发员工访问财务部知识库 `GET/PATCH/DELETE/POST search`，或财务部管尝试修改研发部知识库），后端统一返回 **`404`**，不泄漏资源存在性。
- **可读但无写权限返回 `403`**：当用户对资源拥有读权限但无管理权限（例如 `dept_admin` 或 `member` 尝试 `PATCH` / `DELETE` / 上传文档至 `company` 级公司公共知识库，或非管理角色访问管理端点），后端返回 **`403`**。
- **调岗实时生效**：所有读/写 API 与 Run 工具运行时均实时查库校验最新组织归属，员工调岗后同一 Cookie 立即丧失原部门资源访问权。

### 4.3 四条业务线的接口字段对齐

| 业务线 | 可见范围字段 | 响应新增字段 | 创建/更新接口变更 | 管理角色要求 |
| --- | --- | --- | --- | --- |
| **知识库** (`/api/v1/knowledge-bases`) | `visibility_scope`: `"company" \| "department" \| "personal"`（创建默认 `"personal"`） | `visibility_scope`, `owner_department_id`, `visibility_grants` | `GET /api/v1/knowledge-bases` 新增筛选 query：`visibility_scope`、`owner_department_id`；`POST` / `PATCH /{base_id}` 支持设置三字段 | `personal`: 本人；`department`: 超管或管辖该部门的 `dept_admin`；`company`: 超管 |
| **Skills** (`/api/v1/admin/skills`, `/api/v1/skills`) | `scope`: `"company" \| "department"`（个人技能通过 `/api/v1/me/skills` 固定 `"personal"`） | `scope`（原 `"public"` 已迁移为 `"company"`）, `owner_department_id`, `visibility_grants` | `POST /api/v1/admin/skills/import` multipart 表单新增可选字段 `scope`（默认 `"company"`）、`owner_department_id`、`visibility_grants`（JSON 字符串）；`PATCH /api/v1/admin/skills/{id}` 新增可选 `visibility_grants`；新增 **`DELETE /api/v1/admin/skills/{id}?expected_revision=N`**（204 软删部门级 Skill） | `GET/POST/PATCH/DELETE /api/v1/admin/skills*` 面向 **`super_admin` 与 `dept_admin`** 开放（`dept_admin` 仅限管辖部门级 Skill；执行日志排查仍限 `super_admin`） |
| **MCP Server** (`/api/v1/admin/mcp/servers`, `/api/v1/tools`) | `visibility_scope`: `"company" \| "department"`（默认 `"company"`） | `visibility_scope`, `owner_department_id`, `visibility_grants`, `enabled` | `POST` / `PUT` / `PATCH /api/v1/admin/mcp/servers*` 均支持设置可见性三字段；`GET /api/v1/tools` 自动按当前用户部门权限与 `enabled=true` 过滤 | 仅 `super_admin` |
| **Subagent** (`/api/v1/admin/subagents`) | `visibility_scope`: `"company" \| "department"`（默认 `"company"`） | `visibility_scope`, `owner_department_id`, `visibility_grants` | `POST` / `PATCH /api/v1/admin/subagents*` 均支持设置可见性三字段；Run 运行期 `dispatch_subagents` 自动按当前用户权限过滤可调度列表 | 仅 `super_admin` |

---

## 五、对话能力底座升级

### 5.1 普通用户场景化模型目录（新增）
- **端点**：`GET /api/v1/models`
- **权限**：任意已登录用户（`super_admin` / `dept_admin` / `member`）
- **筛选规则**：仅返回 `enabled=true`、`visible_to_users=true`、`deleted_at IS NULL` 且满足可对话能力（`supports_stream=true` 且 `supports_tools=true`）的模型；严格脱敏，**绝不返回 `base_url`、`api_key`、`has_api_key` 及超时/并发运维参数**。
- **响应 `200 OK`**：
```json
{
  "items": [
    {
      "id": "llmprof_01...",
      "name": "通用对话模型",
      "scenario_label": "日常办公",
      "scenario_description": "适合常规文档撰写与日常问答",
      "is_default": true,
      "supports_vision": false
    },
    {
      "id": "llmprof_02...",
      "name": "深度思考模型",
      "scenario_label": "深度思考",
      "scenario_description": "适合复杂财务分析与方案推演",
      "is_default": false,
      "supports_vision": true
    }
  ],
  "default_model_id": "llmprof_01..."
}
```

### 5.2 Run 提交增强：无项目知识库检索、显式范围与场景模型选择
- **端点**：`POST /api/v1/sessions/{session_id}/runs`
- **请求体 `RunSubmitRequest`**（`extra="forbid"`）：
```json
{
  "request_id": "req-uuid-123",
  "text": "请结合公司和财务制度说明一线城市差旅报销标准",
  "skill_ids": [],
  "input_file_ids": [],
  "knowledge_base_ids": null,
  "model_profile_id": "llmprof_02..."
}
```
- **新增字段语义与边界**：
  - `knowledge_base_ids`（`list[str] | null`，默认 `null`，上限 20 个，纳入 `request_id` 幂等指纹）：
    - **`null` / 不传（企业默认模式）**：无需项目绑定即可自动暴露 `knowledge_search`；未绑定项目会话默认检索当前用户可读的**公司公共库 + 本部门共享库**（上限 20 个，按优先级确定性截断）；已绑定项目会话自动合并项目关联库与企业共享库；
    - **`[]`（空数组）**：明确关闭本轮知识库检索（不暴露 `knowledge_search`）；
    - **非空 ID 列表**：仅检索指定的知识库；任一 ID 不存在或当前用户无读权限时，返回 **`404 {"detail": "knowledge_base_not_found"}`**。
  - `model_profile_id`（`string | null`，默认 `null`，`1..64` 字符，纳入 `request_id` 幂等指纹）：
    - `null` / 不传时使用系统默认模型；
    - 传入指定模型时须满足已启用、未删除、支持对话且 `visible_to_users=true`（对普通用户隐藏的内部压缩模型不允许直接作为对话模型提交）。
- **响应 `RunResponse` 新增字段**：
  - `knowledge_base_ids`: `list[str] | null`（回显本轮提交固化的知识库范围语义）
  - `model_snapshot`: `RunModelSnapshotResponse | null`（Run 创建时固化的只读安全模型快照，**即使后续管理员编辑或软删除原模型，历史 Run 仍保持准确展示且不含 `base_url`/凭据**）：
  ```json
  {
    "id": "llmprof_02...",
    "name": "深度思考模型",
    "model": "o3-mini",
    "scenario_label": "深度思考",
    "supports_vision": false,
    "version": 1
  }
  ```

### 5.3 项目能力增强与温和解绑删项
- **创建项目**：`POST /api/v1/projects`
  - 新增可选字段：`description`（`string | null`，最大 500 字符）、`instructions`（`string | null`，最大 8000 字符，主 Run 执行时注入项目背景指令，子智能体 Run 不重复注入）、`default_skill_ids`（`list[str]`，最多 20 个，每项 `<= 128` 字符）。
- **局部更新项目**：`PATCH /api/v1/projects/{project_id}`
  - 兼容仅传 `name`，支持按需传入 `description`、`instructions`、`default_skill_ids`；**显式传 `null` 可清空可选字段**（不传字段则保留原值）。
  - 校验错误码：`422 project_name_invalid`、`422 project_description_invalid`、`422 project_instructions_invalid`、`422 project_default_skill_ids_invalid`。
- **响应 `ProjectResponse` 新增字段**：`description`, `instructions`, `default_skill_ids`。
- **温和删除项目**：`DELETE /api/v1/projects/{project_id}?detach_sessions=false`
  - `detach_sessions=false`（默认）：若项目下仍有未删除会话，返回 `409 {"detail": "project_has_sessions"}`。
  - `detach_sessions=true`：若所有关联会话均无活跃 Run，则在同事务内将关联会话的 `project_id` 置为 `null`（转为无项目会话保留历史）后软删除项目，返回 `204`；若任一关联会话存在活跃 Run，拒绝删除并返回 `409 {"detail": "session_has_active_run"}`。

### 5.4 会话归属松绑、标题搜索与异步自动标题
- **创建会话**：`POST /api/v1/sessions`，`title` 与 `project_id` 均可省略或传 `null`（日常办公默认无项目会话）。
- **会话列表与搜索**：`GET /api/v1/sessions`
  - 新增 Query 参数：`q`（`string | null`，最大 `120` 字符，支持按会话标题安全转义子串模糊检索）；保留既有 `page`、`page_size`、`project_id`、`unbound`（`unbound=true` 仅查无项目会话，不可与 `project_id` 同时使用，冲突返回 `422 invalid_session_filter`）。
- **修改会话**：`PATCH /api/v1/sessions/{session_id}`
  - 支持修改 `title`（传 `null` 清空）及绑定/改绑/解绑 `project_id`（允许已有历史消息的会话自由变更归属；若存在活跃 Run 则拒绝变更并返回 `409 session_has_active_run`）。
- **异步自动会话标题**：
  - 首轮主 Run 完成后（且会话当前 `title IS NULL`），Worker 后台异步生成 `<= 20` 字的干净中文标题（优先使用当前压缩策略模型，无则回退 Run 自身模型，超时/失败降级截取首条消息前 24 字）；
  - 采用 `WHERE title IS NULL` 条件更新，**绝不覆盖用户预先设置或途中手工修改的标题**；子智能体 Run 绝不触发自动标题；
  - 前端在首轮 Run 完成后可刷新会话详情或列表拉取生成的 `title`。

---

## 六、管理后台运维与业务化待审批卡片投影

### 6.1 模型配置运维（超管专属：`/api/v1/admin/llm/model-profiles`）

- **`ModelProfileResponse` 新增字段**：
  - `visible_to_users: bool`
  - `scenario_label: str | None`（最大 64 字符）
  - `scenario_description: str | None`（最大 255 字符）
  - `supports_vision: bool`
  - `version: int`（乐观锁版本号，起始为 `1`）
  - `updated_at: datetime`
- **创建模型**：`POST /api/v1/admin/llm/model-profiles` 新增可选字段 `visible_to_users`（默认 `true`）、`scenario_label`、`scenario_description`、`supports_vision`（默认 `false`）。
- **局部保密编辑模型（新增）**：`PATCH /api/v1/admin/llm/model-profiles/{profile_id}`
  - 请求体 `ModelProfileUpdateRequest`（`extra="forbid", strict=True`）：支持传入可选乐观锁 `expected_version` 及任意待修改字段；
  - **密钥保护铁律**：不传 `api_key` 或显式传 `api_key: null` 时，**严格保留库中原有加密凭据**；仅当传入非空字符串时才加密轮换新密钥；
  - 不可空字段（如 `name`、`base_url`、`model`、`enabled`、`visible_to_users` 等）严禁显式传 `null`（返回 `422 llm_model_profile_invalid`）；
  - **强引用保护与错误码**：若模型正在被默认指向或当前压缩策略引用，禁止通过 `PATCH` 破坏其必需能力或停用：
    - `404 model_profile_not_found`（目标模型不存在或已软删除；注：既有旧端点 `PUT /{profile_id}/enabled`、`PUT /default` 仍沿用 `404 llm_model_profile_not_found`）
    - `409 model_profile_version_conflict`（乐观锁版本不匹配）
    - `409 model_profile_in_use_as_default`
    - `409 model_profile_in_use_by_compression_policy`
  - 响应 `200 ModelProfileResponse`（`version` 递增，`updated_at` 刷新）。
- **软删除闲置模型（新增）**：`DELETE /api/v1/admin/llm/model-profiles/{profile_id}`
  - 成功返回 `204 No Content`（历史 Run 因已固化 `model_snapshot_json` 不受影响）。
  - 强引用保护拦截错误码（`409 Conflict`）：
    - `model_profile_in_use_as_default`（当前默认模型）
    - `model_profile_in_use_by_compression_policy`（当前上下文压缩策略模型）
    - `model_profile_in_use_by_subagent`（仍被未软删除的子智能体显式绑定）
  - 不存在返回 `404 model_profile_not_found`（注：既有旧端点 `PUT /{profile_id}/enabled`、`PUT /default` 仍返回 `404 llm_model_profile_not_found`）。

### 6.2 MCP Server 保密局部更新、启停、独立重发现与软删除（超管专属：`/api/v1/admin/mcp/servers`）

- **`McpServerResponse` 新增字段**：`enabled: bool`、`visibility_scope`、`owner_department_id`、`visibility_grants`。
- **保密局部更新（新增）**：`PATCH /api/v1/admin/mcp/servers/{server_id}`
  - 请求体 `McpServerPatchRequest`（`extra="forbid", strict=True`）：
  ```json
  {
    "expected_config_version": 1,
    "name": "ERP 报销服务-V2",
    "config": {"url": "https://erp.example.com/mcp"},
    "secrets_upsert": {"AUDIT_KEY": "new-secret-value"},
    "secrets_remove": ["OLD_SIGNING_KEY"],
    "visibility_scope": "department",
    "owner_department_id": "dept_mock_fin",
    "visibility_grants": [
      {"target_type": "department", "target_id": "dept_mock_fin", "include_subdepartments": true}
    ],
    "enabled": true,
    "rediscover": false
  }
  ```
  - **保密语义**：仅更新 `secrets_upsert` 指定的键值、移除 `secrets_remove` 指定的键名；**未提及的既有加密凭据 100% 原样保留**，前端编辑表单无需、也不能要求管理员重新输入旧密钥。
  - 可选 `rediscover: true` 在同操作后触发远端重发现（默认 `false`）。
  - 错误码：`409 mcp_config_version_conflict`、`409 mcp_conflict`（重名冲突）、`404 resource_not_found`、`422 mcp_request_invalid`、`502 mcp_discovery_failed`、`504 mcp_discovery_timeout`。
- **独立启停开关（新增）**：`PUT /api/v1/admin/mcp/servers/{server_id}/enabled`
  - 请求体：`{"enabled": false, "expected_config_version": 2}`（`expected_config_version` 可选）。
  - 行为：停用后该 Server 下所有工具**立即从 `GET /api/v1/tools`、新 Run 快照及运行期调用中下线**；启用后恢复可见；配置版本号递增。
  - 错误码：`409 mcp_config_version_conflict`、`404 resource_not_found`。
- **独立重发现工具（新增）**：`POST /api/v1/admin/mcp/servers/{server_id}/rediscover`
  - 行为：使用库中已存加密凭据重新拉取远端 MCP 工具清单，按 `remote_name` 执行增量对齐：
    - **存活工具**（`remote_name` 仍在清单中）：**严格保留既有 `tool_id`、`requires_approval`、`display_name`、`approval_summary_template` 与 `parameter_labels`**，仅更新描述与 `input_schema`；
    - **新增工具**：自动入库；
    - **消失工具**：自动清理下线。
  - 响应 `200 McpServerResponse`（更新 `tools_discovered_at`）。
- **软删除与重名复用**：`DELETE /api/v1/admin/mcp/servers/{server_id}` 返回 `204`，改为软删除（数据库采用部分唯一索引 `uq_mcp_servers_name_active WHERE deleted_at IS NULL`），删除后支持立即以相同名称重新注册新 Server。

### 6.3 工具业务化展示配置与原子批量审批（超管专属：`/api/v1/admin/tools`）

- **`AdminToolResponse` 新增字段**：
  - `display_name: str | None`（业务中文名称，最大 `120` 字符）
  - `approval_summary_template: str | None`（审批一句话摘要模板，最大 `500` 字符，支持 `{arg_key}` 占位符，如 `"为 {applicant} 提交 {amount} 元报销单"`）
  - `parameter_labels: dict[str, str]`（参数英→中展示映射，最多 `50` 项，key 最大 `128` 字符，label 最大 `120` 字符）
- **单工具局部更新**：`PATCH /api/v1/admin/tools/{tool_id}`
  - 请求体 `ToolApprovalPolicyRequest`（`extra="forbid", strict=True`，向后兼容仅传 `requires_approval`）：
  ```json
  {
    "requires_approval": true,
    "display_name": "提交费用报销单",
    "approval_summary_template": "为 {applicant} 提交 {amount} 元报销单（科目：{category}）",
    "parameter_labels": {
      "applicant": "申请人",
      "amount": "报销金额",
      "category": "费用科目"
    }
  }
  ```
  - 显式传 `null` 可清空 `display_name` 或 `approval_summary_template`；`requires_approval` 不可显式置 `null`（`422 mcp_request_invalid`）。
- **原子批量切换审批开关（新增）**：`PATCH /api/v1/admin/tools/batch-approval`
  - 请求体 `ToolBatchApprovalRequest`：`{"tool_ids": ["tool_1", "tool_2"], "requires_approval": true}`（`1..100` 个工具 ID）。
  - 事务语义：**单事务全成功或全回滚**，任一 `tool_id` 不存在时整批回滚并返回 `404 {"detail": "mcp_tool_not_found"}`。
  - 响应 `200 AdminToolBatchApprovalResponse`：`{"updated_count": 2, "items": [AdminToolResponse, ...]}`。

### 6.4 Run 待审批业务化卡片投影（主 Run 与子智能体统一支持）

当 Run 进入 `waiting_for_approval` 时，`GET /api/v1/sessions/{session_id}/runs/{run_id}` 返回的 `pending_approvals`（以及主 Run 下的 `pending_subagent_approvals`）每一项均包含结构化中文业务展示字段：

```json
{
  "call_id": "rtc_01...",
  "batch_message_id": "exec_01...",
  "ordinal": 1,
  "model_name": "erp_expense_mcp__submit_expense",
  "tool_kind": "mcp",
  "requested_at": "2026-12-05T09:20:00Z",
  "safe_skill_projection": null,
  "display_name": "提交费用报销单",
  "approval_summary": "为 财务员工 提交 1280 元报销单（科目：差旅费）",
  "formatted_arguments": [
    {"key": "applicant", "label": "申请人", "value": "财务员工"},
    {"key": "amount", "label": "报销金额", "value": "1280"},
    {"key": "category", "label": "费用科目", "value": "差旅费"},
    {"key": "cost_center", "label": "cost_center", "value": "CC-2026"}
  ]
}
```

#### 前端渲染约定
1. **卡片标题**：优先展示 `display_name`，为空时回退展示 `model_name`（Skill 脚本工具同时保留 `safe_skill_projection` 供专属卡片使用）。
2. **摘要说明**：若 `approval_summary` 非空直接展示（后端已执行防属性注入的安全占位符替换并截断至 `1000` 字符）。
3. **参数清单**：直接遍历 `formatted_arguments` 渲染 `label: value`（后端已按 `parameter_labels` 优先映射中文标签，未映射参数自动回退原参数名 `key` 作为 `label`，并对超长值安全截断至 `500` 字符，严禁前端自行解析原始危险模板）。
4. **审批提交接口保持不变**：`POST /api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}/approval`，请求体 `{"request_id": "...", "decision": "approve" | "reject"}`。
