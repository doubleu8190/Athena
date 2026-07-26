# Athena 日志系统与 ELK 集成架构文档

> **版本**: 1.0  
> **最后更新**: 2026-06-18  
> **适用版本**: Athena v0.1.0 / ELK Stack 9.4.2

---

## 目录

1. [架构概览](#1-架构概览)
2. [应用层：结构化日志生成](#2-应用层结构化日志生成)
3. [采集层：Filebeat 容器日志收集](#3-采集层filebeat-容器日志收集)
4. [处理层：Logstash 管道](#4-处理层logstash-管道)
5. [存储层：Elasticsearch](#5-存储层elasticsearch)
6. [可视化层：Kibana](#6-可视化层kibana)
7. [数据流全景图](#7-数据流全景图)
8. [日志字段规范](#8-日志字段规范)
9. [索引策略](#9-索引策略)
10. [部署与运维](#10-部署与运维)
11. [关键文件索引](#11-关键文件索引)

---

## 1. 架构概览

Athena 采用 **ELK Stack（Elasticsearch + Logstash + Kibana）+ Filebeat** 作为日志系统的核心架构。整体分为五层：

```
┌─────────────────────────────────────────────────────────────────┐
│                        应用层 (Python)                          │
│  structlog → JSON → stdout → Docker json-file log driver        │
│  标签: app=athena, component=core|worker                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │ 读取容器日志文件
┌──────────────────────────▼──────────────────────────────────────┐
│                    采集层 (Filebeat 9.4.2)                      │
│  filestream input → 解析容器JSON → 解析structlog JSON           │
│  → 注入Docker元数据 → 按app=athena标签过滤 → 转发至Logstash     │
└──────────────────────────┬──────────────────────────────────────┘
                           │ TCP 5044 (Beats protocol)
┌──────────────────────────▼──────────────────────────────────────┐
│                    处理层 (Logstash 9.4.2)                      │
│  ┌─ Pipeline 1: app_logs ─┐  ┌─ Pipeline 2: sqlite_sync ──────┐│
│  │ Beats input            │  │ JDBC input (30s 轮询)            ││
│  │ → 字段提取/清洗        │  │ → audit_logs 表                 ││
│  │ → 时间戳标准化         │  │ → token_usage_log 表            ││
│  │ → 字段裁剪             │  │ → 时间戳转换                    ││
│  │ → 级别映射             │  │ → 按类型路由                    ││
│  └─────────┬──────────────┘  └──────────┬──────────────────────┘│
└────────────┼────────────────────────────┼───────────────────────┘
             │                            │
┌────────────▼────────────────────────────▼───────────────────────┐
│                  存储层 (Elasticsearch 9.4.2)                    │
│  单节点 | 512MB Heap | 安全功能关闭 | CORS 开启                 │
│  ┌─────────────────┬──────────────────┬──────────────────────┐  │
│  │ athena-logs-*   │ athena-audit-*   │ athena-token-*       │  │
│  │ (应用日志)      │ (审计日志)       │ (Token 用量)         │  │
│  └─────────────────┴──────────────────┴──────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                   可视化层 (Kibana 9.4.2)                        │
│  Dashboard / Discover / Visualize | 端口 5601                   │
└─────────────────────────────────────────────────────────────────┘
```

### 关键设计决策

| 决策 | 说明 |
|------|------|
| **structlog + JSON stdout** | 应用不直接对接 ELK，只输出结构化 JSON 到 stdout，实现应用与日志基础设施的解耦 |
| **双通道采集** | 应用日志走 Filebeat（实时流式），数据库审计/Token 数据走 Logstash JDBC（定时轮询） |
| **Docker 标签过滤** | 通过 `app=athena` 容器标签精准过滤，避免收集 Redis、ES 等基础设施日志 |
| **按日索引** | 三套索引均按 `YYYY.MM.dd` 分片，便于按时间范围检索和过期清理 |

---

## 2. 应用层：结构化日志生成

### 2.1 核心配置

**文件**: [athena/logging_config.py](../athena/logging_config.py)

```python
# 处理链（按顺序执行）
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,          # 1. 添加日志级别
        structlog.stdlib.add_logger_name,        # 2. 添加 logger 名称
        structlog.processors.TimeStamper(        # 3. 添加 ISO 8601 UTC 时间戳
            fmt="iso", utc=True
        ),
        structlog.processors.JSONRenderer(       # 4. 渲染为 JSON，保留 Unicode 字符
            ensure_ascii=False
        ),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
```

### 2.2 日志输出格式

每条日志为一整行 JSON，输出到 stdout：

```json
{
  "event": "llm_generate_success",
  "level": "info",
  "logger": "athena.core.llm_provider.manager",
  "timestamp": "2026-06-18T10:30:00.123456Z",
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "total_tokens": 1520,
  "session_id": "sess_abc123",
  "task_id": "task_xyz789"
}
```

### 2.3 上下文绑定

通过 `bind_context()` 函数，在整个请求链路中绑定跨域上下文：

```python
from athena.logging_config import bind_context

logger = bind_context(
    session_id="sess_abc123",
    task_id="task_xyz789",
    step=3,
)
logger.info("subtask_executed", status="success", duration_ms=1200)
```

**常用上下文字段**:

| 字段 | 类型 | 说明 | 来源 |
|------|------|------|------|
| `session_id` | string | 会话 ID | 中间件注入 |
| `task_id` | string | 任务 ID | 任务执行时绑定 |
| `step` | int | 当前步骤编号 | 规划器/执行器 |
| `tool_name` | string | 工具名称 | 工具调用时 |
| `mcp_server_id` | string | MCP 服务器 ID | MCP 客户端 |

### 2.4 Token 用量日志规范

所有 LLM 调用的日志事件 **必须** 包含 `token_usage` 子对象：

```json
{
  "event": "llm_generate_success",
  "provider": "deepseek",
  "token_usage": {
    "prompt_tokens": 800,
    "completion_tokens": 720,
    "total_tokens": 1520
  }
}
```

> **注意**: 实时应用日志中的 token_usage 是即时记录；更完整的 Token 用量数据通过 SQLite → Logstash JDBC 通道独立采集（见 [4.2 节](#42-pipeline-2-sqlite_sync---数据库同步)）。

### 2.5 日志使用范围

日志覆盖以下模块（共 30+ 文件）:

| 模块 | 文件示例 | 典型日志事件 |
|------|---------|-------------|
| 核心引擎 | `core/planner.py`, `core/executor.py`, `core/harness.py` | `plan_generated`, `step_executed`, `harness_blocked` |
| LLM 调用 | `core/llm_provider/*.py` | `llm_generate_success`, `llm_provider_failed` |
| 任务系统 | `tasks/execution.py`, `tasks/recovery.py` | `task_started`, `task_completed`, `task_recovered` |
| 工具调用 | `tools/filesystem.py`, `tools/web_search.py` | `tool_executed`, `fallback_used` |
| MCP 客户端 | `mcp_client/client.py`, `mcp_client/registry.py` | `mcp_connected`, `tool_registered` |
| API 层 | `api/admin.py`, `api/device.py` | `user_action`, `device_registered` |
| 网关 | `gateway/web.py`, `gateway/wechat.py` | `im_message_received` |
| Skills | `skills/proxy.py`, `skills/manager.py` | `skill_installed`, `acl_updated` |

### 2.6 Docker 日志驱动配置

```yaml
# athena-core 和 celery-worker 均使用 json-file 驱动 + 标签
logging:
  driver: json-file
  options:
    max-size: "10m"       # 单个日志文件最大 10MB
    max-file: "3"         # 最多保留 3 个轮转文件
    labels: "app=athena,component=core"   # 标签用于 Filebeat 过滤
```

| 组件 | labels |
|------|--------|
| athena-core | `app=athena,component=core` |
| celery-worker | `app=athena,component=worker` |

---

## 3. 采集层：Filebeat 容器日志收集

**文件**: [deploy/filebeat/filebeat.yml](../deploy/filebeat/filebeat.yml)

### 3.1 输入配置

```yaml
filebeat.inputs:
  - type: filestream                    # Filebeat 9.x 推荐：使用 filestream 替代已弃用的 container input
    id: athena-container-logs
    enabled: true
    paths:
      - /var/lib/docker/containers/*/*.log   # 读取所有 Docker 容器的日志文件
    parsers:
      - container:
          stream: all                   # stdout + stderr
          format: auto                  # 自动检测 Docker 日志格式
```

### 3.2 处理器链

Filebeat 按顺序执行以下处理器：

```
步骤1: decode_json_fields
  ┌─────────────────────────────────────────────┐
  │ 解析 Docker json-file 日志中的 message 字段  │
  │ Docker 输出: {"log":"{...structlog JSON...}","stream":"stdout","time":"..."} │
  │ 解析后 message 字段展开为 structlog JSON 对象 │
  │ 写入根级别 (target: "")，覆盖同名键          │
  └─────────────────────────────────────────────┘
                      │
步骤2: add_docker_metadata
  ┌─────────────────────────────────────────────┐
  │ 通过 /var/run/docker.sock 注入 Docker 元数据 │
  │ 添加字段:                                    │
  │   docker.container.id                        │
  │   docker.container.name                      │
  │   docker.container.image                     │
  │   docker.container.labels                    │
  └─────────────────────────────────────────────┘
                      │
步骤3: drop_event (过滤器)
  ┌─────────────────────────────────────────────┐
  │ 仅保留 docker.container.labels.app == "athena" 的日志 │
  │ 丢弃 Redis、ES、Logstash、Kibana 等基础设施日志      │
  └─────────────────────────────────────────────┘
```

### 3.3 输出配置

```yaml
output.logstash:
  hosts: ["logstash:5044"]     # 发送到 Logstash 的 Beats 输入端口
  loadbalance: true            # 在多个 Logstash 实例间负载均衡（当前单实例）
```

### 3.4 关键挂载

| 宿主机路径 | 容器路径 | 用途 | 权限 |
|-----------|---------|------|------|
| `./deploy/filebeat/filebeat.yml` | `/usr/share/filebeat/filebeat.yml` | 配置文件 | ro |
| `/var/lib/docker/containers` | `/var/lib/docker/containers` | 读取容器日志文件 | ro |
| `/var/run/docker.sock` | `/var/run/docker.sock` | 查询容器元数据 | ro |

> **注意**: Filebeat 以 `user: root` 运行，因为需要读取 `/var/lib/docker/containers` 和 `/var/run/docker.sock`。

---

## 4. 处理层：Logstash 管道

**主配置**: [deploy/logstash/config/logstash.yml](../deploy/logstash/config/logstash.yml)  
**管道定义**: [deploy/logstash/config/pipelines.yml](../deploy/logstash/config/pipelines.yml)

Logstash 运行两个独立的管道，各自有独立的 input/filter/output：

```yaml
- pipeline.id: athena-app-logs       # 管道 1: 实时应用日志
  path.config: "/usr/share/logstash/pipeline/app_logs.conf"
  pipeline.workers: 1

- pipeline.id: athena-sqlite-sync    # 管道 2: 数据库同步
  path.config: "/usr/share/logstash/pipeline/sqlite_sync.conf"
  pipeline.workers: 1
```

### 4.1 Pipeline 1: app_logs — 应用日志处理

**文件**: [deploy/logstash/pipelines/app_logs.conf](../deploy/logstash/pipelines/app_logs.conf)

#### Input

```ruby
input {
  beats {
    port => 5044       # 接收 Filebeat 数据
  }
}
```

#### Filter 处理流程

```
┌──────────────────────────────────────────────────────────┐
│ Step 1: mutate — 提取容器名称                            │
│ [docker][container_name] ← [docker][container][name]     │
│ 简化字段路径，方便在 Kibana 中查询                        │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│ Step 2: date — 时间戳标准化                              │
│ 将 structlog 的 timestamp 字段 (ISO 8601)                │
│ 解析为 Elasticsearch 的 @timestamp 字段                  │
│ 条件: 仅当 timestamp 字段存在时执行                       │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│ Step 3: prune — 字段白名单裁剪                            │
│ 仅保留以下字段以减小索引大小:                             │
│ @timestamp, event, level, logger, session_id, task_id,    │
│ step, tool_name, mcp_server_id, status, fallback_used,    │
│ fallback_from, duration_ms, input_args, output_preview,   │
│ token_usage, error, traceback, docker, host, message      │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│ Step 4: translate — 级别映射                              │
│ 映射 structlog level → Kibana 显示级别:                   │
│ debug → DEBUG | info → INFO | warning → WARN             │
│ error → ERROR | critical → CRITICAL                      │
│ 结果写入 [log][level] 字段供 Kibana 着色                  │
└──────────────────────────────────────────────────────────┘
```

#### Output

```ruby
output {
  elasticsearch {
    hosts => ["http://elasticsearch:9200"]
    index => "athena-logs-%{+YYYY.MM.dd}"   # 按日分索引
  }
}
```

### 4.2 Pipeline 2: sqlite_sync — 数据库同步

**文件**: [deploy/logstash/pipelines/sqlite_sync.conf](../deploy/logstash/pipelines/sqlite_sync.conf)

此管道通过 JDBC 驱动直连 SQLite 数据库，每 30 秒轮询一次增量数据，实现数据库到 Elasticsearch 的同步。

#### 数据源 1: audit_logs 表

```ruby
jdbc {
  jdbc_driver_library => "/usr/share/logstash/drivers/sqlite-jdbc.jar"
  jdbc_driver_class => "org.sqlite.JDBC"
  jdbc_connection_string => "jdbc:sqlite:/data/athena.db"
  statement => "SELECT *, strftime('%Y-%m-%dT%H:%M:%SZ', timestamp) AS timestamp_iso
                FROM audit_logs WHERE timestamp > :sql_last_start ORDER BY timestamp ASC"
  schedule => "*/30 * * * * *"              # 每 30 秒
  tracking_column => "timestamp"             # 增量追踪列
  last_run_metadata_path => "/usr/share/logstash/data/.audit_last_run"
  type => "audit_log"
}
```

**audit_logs 表结构**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_id` | TEXT (PK) | 事件唯一 ID |
| `event_type` | TEXT | 事件类型：`harness_block`, `subtask_executed`, `confirm_timeout` 等 |
| `actor_user_id` | TEXT | 操作者用户 ID |
| `details_json` | TEXT | 事件详情（JSON） |
| `timestamp` | DATETIME | 事件时间戳 |

#### 数据源 2: token_usage_log 表

```ruby
jdbc {
  jdbc_driver_library => "/usr/share/logstash/drivers/sqlite-jdbc.jar"
  jdbc_driver_class => "org.sqlite.JDBC"
  jdbc_connection_string => "jdbc:sqlite:/data/athena.db"
  statement => "SELECT *, strftime('%Y-%m-%dT%H:%M:%SZ', recorded_at) AS recorded_at_iso
                FROM token_usage_log WHERE recorded_at > :sql_last_start ORDER BY recorded_at ASC"
  schedule => "*/30 * * * * *"              # 每 30 秒
  tracking_column => "recorded_at"           # 增量追踪列
  last_run_metadata_path => "/usr/share/logstash/data/.token_last_run"
  type => "token_usage"
}
```

**token_usage_log 表结构**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `log_id` | TEXT (PK) | 日志唯一 ID |
| `session_id` | TEXT | 会话 ID |
| `task_id` | TEXT | 任务 ID |
| `step` | INTEGER | 步骤编号 |
| `source` | TEXT | 来源：`planner`, `executor`, `summarizer`, `context_compressor` |
| `model` | TEXT | 模型名称 |
| `prompt_tokens` | INTEGER | 提示 Token 数 |
| `completion_tokens` | INTEGER | 生成 Token 数 |
| `total_tokens` | INTEGER | 总 Token 数 |
| `recorded_at` | DATETIME | 记录时间 |

#### 路由输出

```ruby
output {
  if [type] == "audit_log" {
    elasticsearch {
      hosts => ["http://elasticsearch:9200"]
      index => "athena-audit-%{+YYYY.MM.dd}"
    }
  }
  if [type] == "token_usage" {
    elasticsearch {
      hosts => ["http://elasticsearch:9200"]
      index => "athena-token-%{+YYYY.MM.dd}"
    }
  }
}
```

### 4.3 关键挂载

| 宿主机路径 | 容器路径 | 用途 |
|-----------|---------|------|
| `./deploy/logstash/pipelines/` | `/usr/share/logstash/pipeline/` | 管道配置文件 |
| `./deploy/logstash/config/logstash.yml` | `/usr/share/logstash/config/logstash.yml` | Logstash 主配置 |
| `./deploy/logstash/config/pipelines.yml` | `/usr/share/logstash/config/pipelines.yml` | 管道定义 |
| `./data` | `/data` (只读) | SQLite 数据库访问 |
| `./deploy/logstash/drivers/` | `/usr/share/logstash/drivers/` | JDBC 驱动 (sqlite-jdbc.jar) |

---

## 5. 存储层：Elasticsearch

```yaml
elasticsearch:
  image: docker.elastic.co/elasticsearch/elasticsearch:9.4.2
  environment:
    - discovery.type=single-node
    - ES_JAVA_OPTS=-Xms512m -Xmx512m
    - xpack.security.enabled=false
    - xpack.monitoring.collection.enabled=true
    - http.cors.enabled=true
    - http.cors.allow-origin="*"
  ports:
    - "9200:9200"
  volumes:
    - ./data/elasticsearch:/usr/share/elasticsearch/data
```

### 配置说明

| 配置项 | 值 | 说明 |
|--------|-----|------|
| `discovery.type` | single-node | 单节点模式（开发环境） |
| `ES_JAVA_OPTS` | `-Xms512m -Xmx512m` | JVM 堆内存 512MB |
| `xpack.security.enabled` | false | 关闭安全功能（开发环境） |
| `http.cors.enabled` | true | 允许跨域（方便前端直接查询） |
| 数据持久化 | `./data/elasticsearch` | 宿主机目录挂载 |

### 健康检查

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -s http://localhost:9200/_cluster/health | grep -qE 'green|yellow'"]
  interval: 10s
  timeout: 5s
  retries: 30    # 首次启动较慢，最多等待 5 分钟
```

---

## 6. 可视化层：Kibana

```yaml
kibana:
  image: docker.elastic.co/kibana/kibana:9.4.2
  environment:
    - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    - SERVER_NAME=kibana
    - XPACK_SECURITY_ENABLED=false
  ports:
    - "5601:5601"
```

- **访问地址**: `http://localhost:5601`
- **索引模式** (需在 Kibana 中手动创建):
  - `athena-logs-*` — 应用日志
  - `athena-audit-*` — 审计日志
  - `athena-token-*` — Token 用量

---

## 7. 数据流全景图

```
时间线视角的完整数据流：

┌──────────────────┐
│  athena-core /   │  structlog JSON → stdout
│  celery-worker   │  Docker json-file driver 写入 .log 文件
└────────┬─────────┘
         │ /var/lib/docker/containers/<id>/<id>-json.log
         │ Docker 格式: {"log":"{...structlog JSON...}","stream":"stdout","time":"..."}
         │
┌────────▼─────────┐
│    Filebeat      │  1. filestream 读取 *.log
│    (root)        │  2. container parser 解析 Docker 外层 JSON
│                  │  3. decode_json_fields 展开 structlog JSON
│                  │  4. add_docker_metadata 注入容器元数据
│                  │  5. drop_event 过滤: 仅保留 app=athena
│                  │  6. 转发至 logstash:5044
└────────┬─────────┘
         │ Beats protocol over TCP
         │
┌────────▼──────────────────────────────────────┐
│              Logstash                         │
│                                               │
│  ┌─ Pipeline: app_logs ────────────────────┐  │
│  │ beats input :5044                       │  │
│  │ → 提取 docker.container_name            │  │
│  │ → timestamp → @timestamp (ISO8601)      │  │
│  │ → prune 白名单裁剪                      │  │
│  │ → level → [log][level] 映射             │  │
│  │ → es output: athena-logs-YYYY.MM.dd     │  │
│  └─────────────────────────────────────────┘  │
│                                               │
│  ┌─ Pipeline: sqlite_sync ────────────────┐  │
│  │ jdbc input (每30秒轮询 SQLite)          │  │
│  │  ├─ audit_logs 表                       │  │
│  │  │  → es output: athena-audit-YYYY.MM.dd│  │
│  │  └─ token_usage_log 表                  │  │
│  │     → es output: athena-token-YYYY.MM.dd│  │
│  └─────────────────────────────────────────┘  │
└────────┬──────────────────────────────────────┘
         │ HTTP REST API
         │
┌────────▼─────────┐
│  Elasticsearch   │  三个按日索引：
│  (single-node)   │  athena-logs-YYYY.MM.dd
│  :9200           │  athena-audit-YYYY.MM.dd
│                  │  athena-token-YYYY.MM.dd
└────────┬─────────┘
         │ HTTP REST API
         │
┌────────▼─────────┐
│     Kibana       │  Discover / Dashboard / Visualize
│     :5601        │  索引模式: athena-logs-*, athena-audit-*, athena-token-*
└──────────────────┘
```

### 服务启动依赖链

```
redis (health check: redis-cli ping)
  ├─→ athena-core (依赖 redis healthy + alembic migrate)
  ├─→ celery-worker (依赖 redis healthy)
  └─→ elasticsearch (health check: cluster health green/yellow)
        ├─→ logstash (依赖 es healthy)
        │     └─→ filebeat (依赖 logstash)
        └─→ kibana (依赖 es healthy)
```

---

## 8. 日志字段规范

### 8.1 应用日志标准字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `event` | string | ✅ | 事件名称（snake_case），如 `task_started`、`llm_generate_success` |
| `level` | string | ✅ | 日志级别：debug / info / warning / error / critical |
| `timestamp` | string | ✅ | ISO 8601 UTC 时间戳 |
| `logger` | string | ✅ | Logger 名称，如 `athena.core.planner` |
| `session_id` | string | - | 会话 ID |
| `task_id` | string | - | 任务 ID |
| `step` | int | - | 步骤编号 |
| `tool_name` | string | - | 工具名称 |
| `mcp_server_id` | string | - | MCP 服务器 ID |
| `status` | string | - | 操作状态：success / failed / timeout |
| `duration_ms` | int | - | 操作耗时（毫秒） |
| `error` | string | - | 错误信息 |
| `traceback` | string | - | 错误堆栈 |
| `token_usage` | object | 条件必填* | Token 用量详情（LLM 调用时必须） |
| `fallback_used` | bool | - | 是否使用了降级方案 |
| `fallback_from` | string | - | 从哪个工具降级 |
| `input_args` | object | - | 输入参数（脱敏后） |
| `output_preview` | string | - | 输出预览 |

> \* **条件必填**: 所有 LLM 调用相关的日志事件必须包含 `token_usage` 子对象。

### 8.2 token_usage 子对象

```json
{
  "token_usage": {
    "prompt_tokens": 800,
    "completion_tokens": 720,
    "total_tokens": 1520
  }
}
```

### 8.3 事件命名规范

| 类别 | 命名模式 | 示例 |
|------|---------|------|
| 生命周期 | `<component>_<action>` | `athena_starting`, `athena_started` |
| LLM 调用 | `llm_<action>` | `llm_generate_success`, `llm_provider_failed` |
| 任务执行 | `task_<action>` | `task_started`, `task_completed` |
| 工具调用 | `tool_<action>` | `tool_executed`, `fallback_used` |
| 错误/降级 | `<problem_description>` | `database_connection_failed`, `circuit_breaker_triggered` |

---

## 9. 索引策略

| 索引模式 | 数据来源 | 采集方式 | 轮询间隔 | 用途 |
|---------|---------|---------|---------|------|
| `athena-logs-YYYY.MM.dd` | structlog stdout | Filebeat 实时 | 实时 | 应用运行日志、调试、错误排查 |
| `athena-audit-YYYY.MM.dd` | SQLite audit_logs 表 | Logstash JDBC | 30 秒 | 安全审计、操作追溯 |
| `athena-token-YYYY.MM.dd` | SQLite token_usage_log 表 | Logstash JDBC | 30 秒 | Token 用量分析、成本统计 |

### 索引生命周期

- **按日分片**: 每天自动创建新索引，便于按时间范围检索
- **数据保留**: 当前未配置 ILM (Index Lifecycle Management) 策略，数据会持续累积
- **建议**: 生产环境应配置 ILM 策略，例如：
  - 热阶段（Hot）: 最近 7 天
  - 温阶段（Warm）: 8-30 天
  - 冷阶段（Cold）: 31-90 天
  - 删除阶段（Delete）: 90 天后

---

## 10. 部署与运维

### 10.1 启动顺序

```bash
# 1. 启动基础设施
docker compose up -d redis elasticsearch

# 2. 等待 ES 健康（green/yellow）
curl -s http://localhost:9200/_cluster/health | jq .status

# 3. 启动日志管道
docker compose up -d logstash kibana

# 4. 启动日志采集
docker compose up -d filebeat

# 5. 启动应用
docker compose up -d athena-core celery-worker
```

或一键启动全部：

```bash
docker compose up -d
```

### 10.2 验证日志链路

```bash
# 1. 检查 Filebeat 是否采集到日志
docker compose exec filebeat filebeat test output

# 2. 查看 Logstash 管道状态
curl -s http://localhost:9600/_node/pipelines | jq .

# 3. 确认索引已创建
curl -s http://localhost:9200/_cat/indices/athena-* | sort

# 4. 通过 Kibana Discover 验证
open http://localhost:5601
# → Stack Management → Index Patterns → 创建 athena-logs-*
# → Discover → 选择 athena-logs-* → 查看实时日志
```

### 10.3 常见问题排查

| 问题 | 排查方法 |
|------|---------|
| Filebeat 未发送日志 | `docker compose logs filebeat` — 检查文件路径权限 |
| Logstash 管道异常 | `docker compose logs logstash \| grep -i error` |
| ES 索引未创建 | 检查 Logstash output 配置和 ES 连接 |
| 磁盘空间不足 | `curl localhost:9200/_cat/allocation?v` 查看磁盘使用 |
| SQLite 同步失败 | 检查 `/data/athena.db` 是否挂载为只读，JDBC 驱动是否存在 |

### 10.4 资源需求

| 组件 | 内存 | 说明 |
|------|------|------|
| Elasticsearch | 512 MB (固定) | 单节点开发配置 |
| Logstash | 256 MB (固定) | 两个管道各 1 worker |
| Filebeat | ~50 MB (默认) | 轻量级采集器 |
| Kibana | ~300 MB (默认) | 可视化界面 |

---

## 11. 关键文件索引

### 应用层

| 文件 | 说明 |
|------|------|
| [athena/logging_config.py](../athena/logging_config.py) | structlog 配置：JSON 输出、时间戳格式化、日志级别 |
| [athena/main.py](../athena/main.py) | 应用入口：调用 `setup_logging()` 初始化日志系统 |
| [athena/middleware.py](../athena/middleware.py) | 请求上下文中间件：注入 request_id |
| [athena/models/audit_log.py](../athena/models/audit_log.py) | 审计日志 ORM 模型 |
| [athena/models/token_usage.py](../athena/models/token_usage.py) | Token 用量 ORM 模型 |

### ELK 部署配置

| 文件 | 说明 |
|------|------|
| [docker-compose.yml](../docker-compose.yml) | 全部服务编排：ES、Logstash、Kibana、Filebeat |
| [deploy/filebeat/filebeat.yml](../deploy/filebeat/filebeat.yml) | Filebeat 配置：输入、处理器链、输出 |
| [deploy/logstash/config/logstash.yml](../deploy/logstash/config/logstash.yml) | Logstash 主配置 |
| [deploy/logstash/config/pipelines.yml](../deploy/logstash/config/pipelines.yml) | 管道定义：app_logs + sqlite_sync |
| [deploy/logstash/pipelines/app_logs.conf](../deploy/logstash/pipelines/app_logs.conf) | 应用日志管道：Beats → 过滤 → ES |
| [deploy/logstash/pipelines/sqlite_sync.conf](../deploy/logstash/pipelines/sqlite_sync.conf) | 数据库同步管道：JDBC → ES |
| [deploy/logstash/drivers/sqlite-jdbc.jar](../deploy/logstash/drivers/sqlite-jdbc.jar) | SQLite JDBC 驱动（14MB） |
| [deploy/kibana/kibana.yml](../deploy/kibana/kibana.yml) | Kibana 配置 |

---

> **文档维护**: 当日志系统架构或配置发生变更时，请同步更新本文档。
