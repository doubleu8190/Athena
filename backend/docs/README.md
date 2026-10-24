# Athena 当前系统文档索引

以下文档以当前工作树中实际注册的运行时和路由为准。推荐从总说明开始阅读；每份文档顶部都有依赖关系。

| 顺序 | 文档 | 用途 |
| --- | --- | --- |
| 1 | [系统实现总说明](athena-system-overview.md) | 定义复刻目标、组件、生命周期、不变量和验收标准 |
| 2 | [运行时与数据底座](athena-runtime.md) | 定义启动、命令、Root/Worker、DAG、事件、表和恢复 |
| 3 | [HTTP API 与事件协议](athena-api.md) | 定义全部当前 REST、SSE、请求字段、响应和错误 |
| 4 | [工具、审批与 MCP 实现](athena-tools-approval-mcp.md) | 定义内置工具、文件工具、审批、沙箱、MCP 和治理 |
| 5 | [文件智能与知识库实现](athena-files-knowledge.md) | 定义上传、适配器、解析、分块、索引、检索和分析 |
| 6 | [记忆、上下文检索与检索轨迹](athena-memory-retrieval.md) | 定义长期记忆、revision、融合召回、图上下文和轨迹 |
| 7 | [前端复刻说明](athena-frontend.md) | 定义页面、状态、SSE 投影和用户流程 |
| 8 | [评测系统设计](evaluation-system-design.md) | 定义评测层次、数据模型、指标、实验和回归门禁 |
| 9 | [评测系统实施](evaluation-system-implementation.md) | 定义实施阶段、代码落点、表结构、接口、测试和发布顺序 |
| 10 | [任务运行链路查看方案](tracing-implementation.md) | 定义基于现有运行事件和 SSE 的历史链路 API、前端时间线/执行树、事件协议和落地阶段 |

`athena-target-implementation.md` 是之前留下的目标架构设计稿，描述范围大于当前已接入实现；它不是本套“当前系统功能”文档的依据。`optional-none-audit.md` 是代码审计记录，也不是产品功能定义。

## 专项设计与实施记录

- [Root 统一决策设计与实施方案（首期收敛版）](root-unified-decision-implementation-plan.md)：记录取消独立前置分类、统一 Root 决策、按需检索和复用既有执行协议的实施范围与验收结果。历史附件扩展、访问统计修正和初始命令重投后置。
- [Root 首次决策前预检索与两阶段检索方案](root-context-prefetch-design.md)：已接通原始 query 预检索、LLM 定向补充检索、阶段摘要和超时；实际验证结果与边界见 [验收记录](verification/root-context-prefetch-20261024.md)。

## 阅读依赖图

```mermaid
flowchart TD
  O[总说明] --> R[运行时与数据底座]
  R --> A[HTTP API 与事件]
  R --> T[工具/审批/MCP]
  R --> F[文件智能/知识库]
  R --> M[记忆/检索]
  A --> U[前端复刻]
  T --> U
  F --> U
  M --> U
  R --> X[任务运行链路查看方案]
  X --> A
  X --> U
```

## 启动和验证

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cd frontend && npm install && cd ..
cp .env.example .env
./start.sh start
```

服务地址：后端 `http://127.0.0.1:8000`，Swagger `http://127.0.0.1:8000/docs`，前端 `http://127.0.0.1:5173`，SSE `/api/sessions/{id}/events`。后端测试使用 `./start.sh test`，前端生产构建使用 `cd frontend && npm run build`。
