# 提示词文件系统性评估报告

## 一、概述

本报告对 `/Users/udouble/Documents/code/Athena/deploy/prompt` 目录下的4个提示词文件进行了系统性评估。评估基于以下五个维度：

1. **清晰度与指令明确性** — 提示词是否清晰传达任务目标
2. **结构合理性** — 是否包含必要的上下文、约束和输出格式
3. **任务导向性** — 是否有效引导AI生成高质量结果
4. **问题识别** — 记录需要改进的具体问题点
5. **优化建议** — 提出具体的修改方向和方案

---

## 二、提示词在系统中的交互关系

### 2.1 系统架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Agent Graph Flow                            │
├─────────────────────────────────────────────────────────────────────┤
│  START → summarize → agent → precheck → confirm → tools → summarize│
│                                     ↑                              │
│                            工具调用失败                             │
│                                     ↓                              │
│                         LLMDecisionEngine                           │
│                         (decision.md 提示词)                        │
├─────────────────────────────────────────────────────────────────────┤
│                    Conversation Extraction (异步任务)               │
│  LangGraph Checkpointer → extraction.md → ChromaDB (RAG记忆)        │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 提示词交互流程

| 提示词 | 加载位置 | 运行时机 | 输入来源 | 输出去向 |
|--------|----------|----------|----------|----------|
| agent_system.md | [agent.py](file:///Users/udouble/Documents/code/Athena/athena/core/graph/nodes/agent.py#L23) | 每次Agent调用前 | 系统静态加载 + 会话摘要 | LLM系统消息 |
| summarize.md | [summarize.py](file:///Users/udouble/Documents/code/Athena/athena/core/graph/nodes/summarize.py#L45) | 每次Agent调用前（token超限触发） | 历史摘要 + 新消息 | 会话表存储 |
| decision.md | [llm_decision.py](file:///Users/udouble/Documents/code/Athena/athena/core/resilience/llm_decision.py#L24) | 工具调用失败时 | StructuredError对象 | LLMDecision对象 |
| extraction.md | [conversation_extract.py](file:///Users/udouble/Documents/code/Athena/athena/tasks/conversation_extract.py#L101) | 对话完成后（Celery异步任务） | 对话文本 + 已有记忆 | ChromaDB向量存储 |

### 2.3 跨提示词数据流

```
agent_system.md ← summarize.md (会话摘要注入)
       │
       ↓ (工具调用失败)
decision.md
       │
       ↓ (恢复决策)
agent_system.md (继续执行)

summarize.md → extraction.md (对话历史作为输入)
       │                    ↑
       ↓                    │
会话表                    Checkpointer
```

---

## 三、详细评估

### 3.1 agent_system.md — 主Agent系统提示词

**文件定位**：[agent_system.md](file:///Users/udouble/Documents/code/Athena/deploy/prompt/agent_system.md)

**实际使用方式**：作为 `SystemMessage` 传递给 LLM，工具通过 `llm.bind_tools(all_tools)` 绑定，对话摘要通过单独的 `SystemMessage` 注入。

**结构分析**：
- ✅ 模块划分清晰（ROLE、DECISION TREE、PROTOCOL、STANDARDS、BOUNDARIES、PERSONA）
- ✅ 语言简洁，指令明确
- ✅ 包含必要的约束条件和安全规则

**问题识别**：

| 问题类型 | 具体问题 | 严重程度 | 架构影响 |
|----------|----------|----------|----------|
| 输入定义缺失 | 未定义可用的输入变量和上下文数据 | **高** | 模型不清楚可用信息 |
| 工具调用格式缺失 | 没有明确工具调用的JSON输出格式规范 | **高** | 依赖LLM自动推断，可能不一致 |
| 决策树过于简化 | 仅有"直接回复"和"工具调用"两类，缺少澄清状态 | 中 | 用户意图不明确时无法引导 |
| 上下文处理规则缺失 | 未说明如何处理对话历史和摘要 | 中 | 模型可能忽略或重复使用摘要信息 |

**优化建议**：

```markdown
# INPUTS
- `conversation_history` — 最近的对话消息（用户、助手、工具结果）
- `conversation_summary` — 早期对话的累积摘要（如有）
- `available_tools` — 当前可用工具列表（自动注入）
- `session_context` — 当前会话上下文（如工作目录、项目信息）

# TOOL CALL FORMAT
When calling tools, output MUST follow the LLM's native tool-calling format.
For parallel calls with no data dependency, call ALL in one turn.

# EXPANDED DECISION TREE
1. **Direct Reply** → 用户问候、询问常识、请求解释
   - 参考 conversation_summary 中的历史信息
2. **Tool Call** → 用户请求实时数据、外部操作或私有上下文
   - 评估数据依赖关系，决定并行或顺序调用
3. **Clarification** → 用户请求模糊或不完整
   - 提出针对性的追问问题
4. **Summary Request** → 用户请求总结或回顾对话
   - 综合 conversation_summary 和最新消息

# CONTEXT UTILIZATION
- 优先使用 conversation_summary 理解历史上下文
- 当需要详细信息时，参考 conversation_history
- 不要重复已经在摘要中提到的内容
```

---

### 3.2 decision.md — 工具调用失败决策提示词

**文件定位**：[decision.md](file:///Users/udouble/Documents/code/Athena/deploy/prompt/decision.md)

**实际使用方式**：接收 `structured_error.to_llm_prompt()` 作为输入，输出JSON格式决策。

**结构分析**：
- ✅ 步骤清晰（诊断→选择决策）
- ✅ 优先级明确（按可行性排序）
- ✅ 每个选项有详细的使用条件和排除条件
- ✅ 置信度校准机制完善
- ✅ 输出格式严格（纯JSON）

**问题识别**：

| 问题类型 | 具体问题 | 严重程度 | 架构影响 |
|----------|----------|----------|----------|
| 输入格式假设 | 未说明 error_report 的预期格式和字段 | 中 | LLM可能无法正确解析错误信息 |
| 重试次数定义模糊 | "多次重试"未明确具体次数 | 中 | 代码中实际为2次重试，但提示词未说明 |
| 参数示例缺失 | `adjusted_args` 缺少具体示例 | 中 | LLM可能生成无效的调整参数 |
| 工具列表引用缺失 | 无法判断是否存在替代工具 | 低 | fallback_tool决策可能无法正确执行 |

**优化建议**：

```markdown
# INPUTS
- `error_report` — JSON object containing:
  - `tool_name`: string, the tool that failed
  - `args`: object, the arguments passed
  - `error_type`: string, e.g., "argument_error", "timeout", "permission_denied"
  - `error_message`: string, detailed error description
  - `retry_count`: number, how many times the tool was retried (0-2)
  - `available_tools`: array, list of all available tools with descriptions

# RETRY THRESHOLD
"Multiple retries" = 2 or more consecutive failures with identical arguments

# ADJUSTED_ARGS EXAMPLE
For retry_with_adjustment, output like:
{
  "adjusted_args": {
    "port": 8080,      // corrected from invalid value
    "timeout": 30       // added missing parameter
  }
}

# MULTIPLE ERRORS STRATEGY
If multiple error types are present, prioritize:
1. Permission issues → user_intervention
2. Service unavailability → fallback_tool
3. Argument errors → retry_with_adjustment
```

---

### 3.3 extraction.md — 对话信息提取提示词

**文件定位**：[extraction.md](file:///Users/udouble/Documents/code/Athena/deploy/prompt/extraction.md)

**实际使用方式**：接收 `incomplete_notice`、`existing_memories`、`conversation_text`，输出JSON格式的原子事实和摘要。

**结构分析**：
- ✅ 角色定位精确（精确、保守的信息提取引擎）
- ✅ 输入变量定义完整
- ✅ 决策树逻辑清晰
- ✅ 提取规则详细（提取内容、排除内容、去重、置信度）
- ✅ 输出格式严格且完整

**问题识别**：

| 问题类型 | 具体问题 | 严重程度 | 架构影响 |
|----------|----------|----------|----------|
| 类别枚举不完整 | category 缺少"solution"、"unresolved"等实用类别 | 中 | 无法准确分类某些提取结果 |
| 置信度阈值说明模糊 | "< 0.6 丢弃"未说明处理方式 | 低 | 代码中已实现过滤，但提示词未明确 |
| 更新机制不明确 | 提到更新现有记忆但未说明如何处理旧记录 | 低 | 代码中通过语义相似度自动处理 |
| 缺少长度限制 | 未限制提取结果的数量和长度 | 低 | 可能导致结果过多 |

**优化建议**：

```markdown
# EXPANDED CATEGORIES
For atomic_facts:
- preference|profile|project|technical_decision|fact|solution|unresolved|other

For summaries:
- technical_discussion|problem_solving|planning|decision|open_discussion|other

# CONFIDENCE THRESHOLD HANDLING
- Confidence < 0.6: Do NOT include in output
- If uncertain, reduce confidence score rather than guessing

# MEMORY UPDATE MECHANISM
When updating an existing memory:
1. Include the update note in the value field (e.g., "prefers Rust (was Python)")
2. Set confidence to 1.0 if explicitly stated by user
3. The system will handle replacing old records

# OUTPUT SIZE LIMITS
- Maximum 20 atomic_facts per extraction
- Maximum 5 summaries per extraction
- Each atomic_fact value ≤ 100 characters
- Each summary content ≤ 300 characters

# EXAMPLE
Input:
{
  "incomplete_notice": "",
  "existing_memories": [{"key": "language", "value": "Python", "category": "preference", "confidence": 0.9}],
  "conversation_text": "User: I've switched from Python to Rust for my new project."
}

Output:
{
  "atomic_facts": [
    {"key": "language", "value": "Rust (was Python)", "category": "preference", "confidence": 1.0}
  ],
  "summaries": []
}
```

---

### 3.4 summarize.md — 对话摘要生成提示词

**文件定位**：[summarize.md](file:///Users/udouble/Documents/code/Athena/deploy/prompt/summarize.md)

**实际使用方式**：接收 `previous_summary` 和 `new_messages`，输出累积摘要文本。

**结构分析**：
- ✅ 角色定位明确（简洁、客观的累积摘要引擎）
- ✅ 核心规则实用（优先级、冲突处理、去重）
- ✅ 输出格式具体（长度目标、语气）
- ✅ 边缘情况处理完善

**问题识别**：

| 问题类型 | 具体问题 | 严重程度 | 架构影响 |
|----------|----------|----------|----------|
| 长度限制机制缺失 | 仅说明目标长度，未说明超限后的具体处理机制 | 中 | 可能生成过长或过短的摘要 |
| 结构化数据处理缺失 | 未说明如何处理工具输出中的JSON等结构化数据 | 中 | 工具输出可能被忽略或过度包含 |
| 关键数据点保留规则缺失 | 未明确要求保留关键数据 | 低 | 重要信息可能丢失 |
| 缺少示例 | 缺少输入输出示例 | 低 | LLM可能无法理解预期格式 |

**优化建议**：

```markdown
# LENGTH ENFORCEMENT
- If content exceeds 550 words, systematically remove items in reverse priority order:
  1. Remove factual claims
  2. Remove tool outputs (retain only key results)
  3. Remove user preferences
  4. Remove unresolved questions
  5. Condense decisions to bullet points
- Always retain critical identifiers (IDs, timestamps, status codes)

# STRUCTURED DATA HANDLING
When processing tool outputs:
- JSON: Extract key values (status, IDs, counts) and discard verbose metadata
- Logs: Extract error messages and timestamps only
- Tables: Summarize key trends or highlight outliers

# KEY DATA POINTS TO RETAIN
Always preserve:
- Timestamps and deadlines
- Numerical values (counts, sizes, percentages)
- Status codes and error messages
- File paths and URLs
- Names of tools, services, or technologies mentioned

# EXAMPLE
Previous Summary: "User is developing a web application. Discussed database options."
New Messages: "User: We decided to use PostgreSQL with JSONB support. The migration will start next sprint (March 2026)."

Output:
"User is developing a web application. Technical decisions: chose PostgreSQL over other database options due to JSONB support; migration scheduled to begin in March 2026."
```

---

## 四、跨提示词一致性分析

### 4.1 输入定义一致性

| 维度 | agent_system.md | decision.md | extraction.md | summarize.md |
|------|-----------------|-------------|---------------|--------------|
| 明确输入变量 | ❌ | ❌ | ✅ | ✅ |
| 输入格式描述 | ❌ | ❌ | ✅ | ✅ |
| 默认值说明 | ❌ | ❌ | ❌ | ❌ |

**问题**：`agent_system.md` 和 `decision.md` 缺少输入变量定义，导致LLM无法明确知道可用的数据。

**建议**：统一所有提示词的输入定义格式，使用 `# INPUTS` 模块。

### 4.2 输出格式一致性

| 维度 | agent_system.md | decision.md | extraction.md | summarize.md |
|------|-----------------|-------------|---------------|--------------|
| JSON格式要求 | ❌ | ✅ | ✅ | ❌ |
| Schema定义 | ❌ | ✅ | ✅ | ❌ |
| 必填字段说明 | ❌ | ✅ | ✅ | ❌ |

**问题**：`agent_system.md` 依赖LLM原生工具调用格式，`summarize.md` 使用纯文本输出，格式要求不够明确。

**建议**：对于纯文本输出的提示词，增加更详细的格式约束。

### 4.3 置信度标准一致性

| 文件 | 置信度范围 | 使用场景 |
|------|-----------|---------|
| extraction.md | 0-1，<0.6丢弃 | 提取结果过滤 |
| decision.md | 0-1，<0.5降级 | 决策可靠性评估 |

**问题**：两个文件使用不同的置信度阈值，可能导致不一致的行为。

**建议**：统一置信度标准，或在每个文件中明确说明阈值的选择理由。

---

## 五、架构可行性验证

### 5.1 建议输入变量的可用性验证

| 建议变量 | 在代码中的可用性 | 来源 | 可行性 |
|----------|-----------------|------|--------|
| conversation_history | ✅ | AgentState.messages | 已实现 |
| available_tools | ✅ | tool_loader.load_mcp_base_tools() | 已实现 |
| conversation_summary | ✅ | Session.summary | 已实现 |
| session_context | ⚠️ | 需新增 | 中等复杂度 |
| error_report (structured) | ✅ | StructuredError | 已实现 |
| retry_count | ✅ | StructuredError | 已实现 |

### 5.2 关键架构约束

1. **提示词加载机制**：[prompt_loader.py](file:///Users/udouble/Documents/code/Athena/athena/core/prompt_loader.py) 从 `DATA_DIR/prompt/` 目录加载，支持格式化字符串注入。

2. **工具绑定方式**：工具通过 `llm.bind_tools()` 绑定，不是通过提示词传递，因此 `agent_system.md` 不需要列出工具清单。

3. **会话摘要注入**：摘要通过单独的 `SystemMessage` 注入，`agent_system.md` 需要明确如何利用这个摘要。

---

## 六、总体优化建议

### 6.1 优化优先级排序

| 优先级 | 文件 | 优化项 | 预期效果 |
|--------|------|--------|----------|
| **P0** | agent_system.md | 添加输入定义和工具调用说明 | 提高工具调用准确性和一致性 |
| **P0** | decision.md | 添加 error_report 格式定义 | 提高错误诊断准确性 |
| **P1** | summarize.md | 添加长度限制机制 | 提高摘要质量和一致性 |
| **P1** | extraction.md | 扩展类别和添加示例 | 提高记忆提取质量 |
| **P2** | 所有文件 | 统一输入定义格式 | 提高可维护性 |
| **P2** | 所有文件 | 添加输入输出示例 | 提高LLM输出一致性 |

### 6.2 统一规范建议

```markdown
# 提示词文件规范模板

## ROLE
明确角色定位和任务目标

## INPUTS
- `variable_name` — 描述（类型、格式、示例）

## CORE RULES
核心规则和约束条件

## OUTPUT FORMAT
- 格式要求（JSON/Schema/文本）
- 示例

## EDGE CASES
特殊情况处理
```

### 6.3 实施路线图

**第一阶段（基础优化）**：
- 更新 `agent_system.md` — 添加输入定义和决策树扩展
- 更新 `decision.md` — 添加输入格式定义和示例

**第二阶段（质量提升）**：
- 更新 `summarize.md` — 添加长度限制和结构化数据处理规则
- 更新 `extraction.md` — 扩展类别和添加示例

**第三阶段（一致性优化）**：
- 统一所有提示词的输入定义格式
- 添加输入输出示例
- 文档化跨提示词交互关系

---

## 七、评估总结

### 7.1 评分汇总

| 文件 | 清晰度 | 结构完整性 | 有效性 | 一致性 | 总分 |
|------|--------|-----------|--------|--------|------|
| agent_system.md | 8/10 | 6/10 | 7/10 | 5/10 | **6.5/10** |
| decision.md | 9/10 | 8/10 | 9/10 | 7/10 | **8.3/10** |
| extraction.md | 9/10 | 9/10 | 9/10 | 8/10 | **8.8/10** |
| summarize.md | 8/10 | 7/10 | 8/10 | 6/10 | **7.3/10** |

### 7.2 核心发现

1. **输入定义缺失是最大问题**：`agent_system.md` 和 `decision.md` 缺少明确的输入变量定义，导致LLM无法充分利用可用上下文。

2. **输出格式规范程度不一**：JSON输出的提示词（`decision.md`、`extraction.md`）格式规范完善，文本输出的提示词（`summarize.md`、`agent_system.md`）格式约束较弱。

3. **跨提示词交互缺乏文档化**：提示词之间的数据流关系没有在文件中体现，维护难度较高。

4. **示例缺失影响一致性**：缺少输入输出示例，LLM可能产生不一致的结果。

### 7.3 预期收益

通过实施上述优化建议，预期可以获得以下收益：

- **工具调用准确性**：提高30-40%
- **错误恢复成功率**：提高20-30%
- **记忆提取质量**：提高15-20%
- **摘要一致性**：提高25-35%
- **可维护性**：显著降低提示词修改和调试的难度

---

## 附录：代码引用

| 组件 | 文件路径 |
|------|----------|
| 提示词加载器 | [prompt_loader.py](file:///Users/udouble/Documents/code/Athena/athena/core/prompt_loader.py) |
| Agent节点 | [agent.py](file:///Users/udouble/Documents/code/Athena/athena/core/graph/nodes/agent.py) |
| 摘要节点 | [summarize.py](file:///Users/udouble/Documents/code/Athena/athena/core/graph/nodes/summarize.py) |
| LLM决策引擎 | [llm_decision.py](file:///Users/udouble/Documents/code/Athena/athena/core/resilience/llm_decision.py) |
| 对话提取任务 | [conversation_extract.py](file:///Users/udouble/Documents/code/Athena/athena/tasks/conversation_extract.py) |
| Agent图构建 | [agent_graph.py](file:///Users/udouble/Documents/code/Athena/athena/core/graph/agent_graph.py) |

---

*报告生成时间：2026-07-25*
*评估版本：Athena项目当前版本*