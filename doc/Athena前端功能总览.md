# Athena 前端功能总览

## 一、页面与功能

### 1. Chat（聊天主界面）

- 多会话管理（创建、切换、自动命名），会话列表持久化到 localStorage
- SSE 实时流式接收 AI 响应，支持随时中断
- 任务执行全流程可视化：计划生成 → 子任务开始/完成/失败/跳过/降级 → 任务完成/失败
- 子任务需要用户确认时弹出审批对话框（Approve / Deny）
- Markdown 消息渲染（代码高亮、表格、引用、图片等），含 GitHub 风格语法着色
- 用户消息橙气泡右对齐，助手消息灰卡片左对齐，自动滚到底部

### 2. Dashboard（仪表盘）

- 四张指标卡片：已完成/运行中/待处理/失败 任务数量
- SVG 环形成功率仪表盘（颜色根据百分比变化），含 24h 统计
- 安全规则触发次数（Harness Blocks 24h）
- 快捷入口链接卡片（6 个管理页面）
- 每 30 秒自动刷新指标

### 3. MCP Servers（MCP 服务器管理）

- 表格列出所有服务器（Server ID、名称、传输协议、启用状态、连接状态、来源）
- 注册服务器（表单：ID、名称、传输协议 stdio/http/sse、来源、连接配置 JSON）
- 启用/禁用服务器
- 删除服务器（带确认弹窗）

### 4. Skills（技能管理）

- 响应式卡片网格展示已安装技能（名称、版本、镜像 URI、允许域名、状态标签）
- 安装技能（表单：名称、版本、镜像 URI、允许域名）
- 卸载技能（带确认弹窗）

### 5. Devices（设备管理）

- 表格列出已注册设备（ID、类型、状态、连接信息、心跳时间）
- 注册设备（表单：ID、类型 host/android、连接信息 JSON）
- 注销设备（带确认弹窗）

### 6. Harness Rules（Harness 规则管理）

- 表格列出安全规则（按优先级降序），显示优先级、类型、名称、描述、启用开关、版本
- 编辑规则配置（JSON 编辑器）
- 行内 Toggle 开关启用/禁用规则
- 一键重载规则缓存

### 7. Audit Logs（审计日志）

- 表格展示系统事件（时间戳、事件类型、操作者），支持展开查看 JSON 详情
- 事件类型下拉过滤（harness_block、task_completed 等 7 种）
- 游标分页加载更多

---

## 二、使用的后端接口

所有接口基础路径为 `/api/v1`，开发时 Vite 代理到 `http://localhost:8000`。

### 聊天

| 方法 | 路径 | 说明 |
|------|------|------|
| POST (SSE) | `/im/web/message` | 发送消息，流式返回任务执行事件 |
| POST | `/im/web/message/confirm` | 确认/拒绝子任务审批 |

### 仪表盘

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/dashboard` | 获取系统指标 |

### MCP 服务器

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/mcp-servers` | 获取服务器列表 |
| POST | `/admin/mcp-servers` | 注册服务器 |
| DELETE | `/admin/mcp-servers/:id` | 删除服务器 |
| PUT | `/admin/mcp-servers/:id/status` | 启用/禁用服务器 |

### 技能

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/skills` | 获取技能列表 |
| POST | `/admin/skills` | 安装技能 |
| DELETE | `/admin/skills/:id` | 卸载技能 |

### 设备

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/devices` | 获取设备列表 |
| POST | `/admin/devices` | 注册设备 |
| DELETE | `/admin/devices/:id` | 注销设备 |

### Harness 规则

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/harness/rules` | 获取规则列表 |
| PUT | `/admin/harness/rules/:id` | 更新规则配置 |
| POST | `/admin/harness/reload` | 重载规则缓存 |

### 审计日志

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/audit-logs` | 获取审计日志（支持 `event_type`、`cursor`、`limit` 参数） |

### IM 状态

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/im/status` | 获取 IM 通道状态（已定义，暂未在页面中使用） |

---

## 三、待实现

- 用户认证（`authStore` 为占位实现）
- 会话消息历史从服务端加载（`selectSession` 注释标记 TODO）
- 消息附件上传（`MessageRequest.attachments` 字段已预留）
