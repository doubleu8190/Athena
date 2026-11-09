# LLM 调用模块设计

状态：原 Responses 模块方案保持有效；Run 另使用已确认的 Chat Completions Adapter 和持久化模型版本。

## 1. 已确认选择与首版范围

- 首个供应商为 OpenAI，使用官方异步 Python SDK。
- 支持服务端配置自定义 `base_url`，接入支持 Responses API 的兼容服务。
- 首版仅支持 Responses API，不探测协议、不自动回退。
- Run 首期另行增加 Chat Completions Adapter；既有 Responses Adapter 和部署 Profile 保持独立，不自动探测或回退。

首版提供普通非流式、流式和非流式结构化输出，以及请求级动态 tools 绑定。
本文件描述的 Responses 模块不新增 HTTP/SSE 接口、数据库表、模型管理后台；Run 设计单独增加 Chat Completions 模型版本管理和执行接口。
多模态输入、自动模型降级或 Agent 框架。

工具执行、权限复核、上下文组织和多轮循环属于后续 Run 模块。
兼容服务必须实际支持所需能力，不能仅凭“OpenAI 兼容”认定全部功能可用。

## 2. 分层与职责

沿用现有领域、应用和适配器边界，建议新增：

```text
backend/src/
├── domain/llm/
│   ├── model/
│   │   ├── message.py
│   │   ├── request.py
│   │   ├── response.py
│   │   ├── tool.py
│   │   ├── event.py
│   │   └── errors.py
│   └── ports.py
├── app/llm/
│   └── service.py
└── adapter/llm/
    ├── model/
    ├── converter/
    └── openai_responses.py
```

| 层 | 职责 |
| --- | --- |
| domain | 消息、请求、结果、事件、异常与模型调用端口 |
| app | 请求校验、模型配置选择、结构化结果校验 |
| adapter | SDK 调用、协议转换、流重组、上游错误转换 |
| main.py | 客户端创建、依赖注入与关闭 |

Adapter 显式实现领域端口。SDK 类型只存在于 Adapter 内，不进入 domain/app。
不创建无持久化需求的 repository。JSON 和日期处理复用现有公共工具。

## 3. 服务接口

以下为接口形状示意，具体类型在实施时落实：

```python
async def complete(request: LlmRequest) -> LlmResponse: ...

def stream(request: LlmRequest) -> AsyncContextManager[LlmStream]: ...

async def generate_structured(
    request: StructuredLlmRequest[T],
) -> StructuredLlmResponse[T]: ...
```

```python
response = await llm.complete(request)

async with llm.stream(request) as events:
    async for event in events:
        ...

result = await llm.generate_structured(structured_request)
value = result.value  # 已校验的 Pydantic 对象
```

三个方法均只执行一次模型调用。流接口使用异步上下文管理器，确保正常结束、
异常、取消或调用方提前退出时关闭上游连接。

## 4. 请求、消息和结果

| 请求字段 | 含义 |
| --- | --- |
| model_profile | 服务端模型配置标识 |
| messages | 本次完整输入上下文 |
| tools | 本次绑定的工具定义，默认空 |
| tool_choice | 自动、不调用、必须调用、指定工具 |
| max_output_tokens | 输出上限 |
| temperature | 可选，仅目标模型支持时发送 |

超时、并发上限和响应大小上限由部署配置控制，业务请求只能在允许范围内收紧。

LLM 使用独立消息类型，支持 system/user/assistant/tool，不直接接收数据库消息
实体。保留文本、工具调用和工具结果的顺序，通过调用 ID 关联工具结果。

响应包含有序输出内容、完成状态和未完成原因、实际模型、上游响应 ID、请求 ID、
token usage 及供应商续接数据。usage 缺失时保留未知，不填零。

Responses 输出可能包含 function call 和 reasoning item，不能压平成字符串。
手动传递上下文时，必须保留续接所需项。续接数据通过专门类型封装，由 Adapter
解释，不暴露 SDK 对象、不作为用户可见内容。首版不依赖供应商托管会话，使用
显式上下文并设置 `store=false`。

## 5. 动态工具绑定

工具列表是每次请求的不可变快照，不修改共享客户端状态，避免并发请求串用工具。

```python
request = LlmRequest(
    model_profile="assistant",
    messages=messages,
    tools=selected_tools,
    tool_choice="auto",
)
```

模型仅接收 name、description、input_schema。未来对接 ToolManager 时，仅转换
公开工具投影，不向模型发送 MCP 地址、凭据、server ID 或远端路由信息。

1. 请求前校验工具名唯一、Schema 合法、指定工具已绑定。
2. Adapter 转换为 Responses 原生 function tools。
3. 返回调用的名称必须属于本次工具快照。
4. 参数解析为 JSON 对象，并按对应 input schema 校验。
5. 保留 `call_id`，支持一次返回多个工具调用。

已有 MCP Schema 不一定符合供应商严格模式约束。首版显式使用非严格工具
Schema，并在本地验证参数，不静默改变必填字段或删除约束。
非法 JSON、未知工具和参数校验失败抛出明确异常，不能补成空参数继续执行。

LLM 只生成调用。Run 负责执行前权限复核、工具执行、结果回填和下一轮调用。
工具目录可见性不能替代执行时授权。

## 6. 流式输出

| 领域事件 | 含义 |
| --- | --- |
| TextDelta | 新增文本，包含内容块索引 |
| ToolCallStarted | 工具调用开始 |
| ToolArgumentsDelta | 原始参数 JSON 片段 |
| ToolCallCompleted | 参数完整且校验通过 |
| ResponseCompleted | 整次响应正常完成，包含完整结果和 usage |

Adapter 处理名称分片、参数跨 chunk 和多个调用交错，累积续接所需的非展示内容。

- 参数片段不能触发工具执行。
- ToolCallCompleted 不代表整次响应成功；首版等待 ResponseCompleted 后再交给 Run。
- 失败、截断、断连不能发送成功完成事件；异常通过类型化异常传播。
- 取消信号继续向上传播，并进入资源清理。
- 直接异步迭代形成背压，不使用无界队列。
- 限制累积文本、参数大小和工具调用数量。

未来 HTTP 层负责映射 SSE，不将供应商事件名直接作为前端协议。

## 7. 非流式结构化输出

业务方传入 Pydantic 模型，返回 `StructuredLlmResponse[T]`，其中 value 是已验证对象。

```python
class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Literal["question", "task"]
    confidence: float
```

流程：Pydantic 类型 → JSON Schema 生成与检查 → Responses 原生结构化输出 →
检查响应状态和拒答 → JSON 解析 → Pydantic 校验 → 类型化结果。

- 使用 Responses `text.format` 严格结构化输出。
- JSON Schema 是受限子集；本地预检已知限制，上游仍可能拒绝。
- 不兼容 Schema 明确报错，不静默修改业务语义。
- 拒答、截断、非法 JSON、类型校验失败分别处理。
- 不退化为提示词要求 JSON，不自动修复或追加模型请求。
- 首版结构化调用不绑定 tools。

结果依赖工具时，由 Run 先完成工具循环，再发起无 tools 的结构化调用。

## 8. 配置与客户端生命周期

部署配置提供命名 Profile：名称、base_url、API Key 环境变量引用、model、能力声明、
超时与并发限制。默认模型不写死在代码中。

能力声明包含 tools、流式、结构化输出和可用生成参数。不支持的参数不发送；
请求要求未声明支持的能力时提前失败。声明不能替代联调，Adapter 仍校验上游结果。

仅部署方管理配置，不接受普通用户或模型动态指定地址、密钥。
相同连接配置复用异步客户端与连接池，随应用生命周期创建和关闭。

## 9. 异常、重试与日志

统一区分配置错误、能力不支持、认证失败、限流、超时、连接失败、上游服务错误、
协议错误、工具参数错误、结构化输出错误，以及拒答和未完成。

首版应用层与 SDK 层均关闭自动重试。流已输出后不能透明重放请求。

日志默认只记录 Profile、模型、耗时、usage、请求 ID 和安全错误码，不记录完整
上下文、工具参数、原始上游错误体或密钥。

## 10. 与现有模块的连接

```text
Run
  → 获取并裁剪上下文
  → 从 ToolManager 选择公开工具定义
  → 调用 LlmService
  → 收集完整响应
  → 复核工具执行权限
  → 执行工具并记录结果
  → 继续下一轮调用
```

LLM 不读取 Session、不查询工具目录、不执行工具、不写消息表。
现有消息内容块由 Run 转换为 LLM 输入；FileRefPart 首版不直接发送给模型。
供应商续接数据的持久化位置在 Run 设计中落实，本轮不为其修改消息表。
LLM 模块自身可独立实施，不以尚未实施的 ToolManager 或 Run 为前置依赖。

## 11. 验收标准

- 不同并发请求的 tools、连接配置和凭据不串用。
- 非流式和流式具有一致的完整输出语义。
- 多调用交错、参数分片、空输出正确处理。
- 未绑定工具、非法参数和不兼容 Schema 明确失败。
- 工具结果通过正确 call_id 回填，续接所需项完整保留。
- 流中断、截断、取消和提前退出正确清理资源，不产生伪成功事件。
- 结构化成功返回类型化对象，拒答和校验失败可区分。
- 兼容服务异常响应不被当作成功。
- SDK 无隐藏重试；日志和错误不泄露凭据。

先用模拟 transport 和协议样本进行确定性测试，再对实际服务和模型联调。
缺少凭据时明确标记联调未完成，不能以模拟测试替代兼容性验证。

## 12. 实施流程与待确认事项

本文整体方案经用户确认后，按 Implementer → Reviewer → Root 流程实施、独立
审查实际 Diff 并验收。修复必须重新审查，最多三轮，不跳过 Review。

默认模型名、实际 base_url 和凭据可在真实联调前提供；秘密值不写入本文。
SDK 版本在实施时锁定，并以该版本验证所需接口与资源关闭行为。

## Run 配置说明

Run 模型版本通过管理员接口持久化；首期显式选择 Chat Completions 协议、流式与工具能力声明、`cl100k_base` 或 `o200k_base` tokenizer 映射；凭据仅引用以 `ATHENA_LLM_CREDENTIAL_` 开头且已注入的环境变量名。
Responses 的 `ATHENA_LLM_PROFILES` 部署配置仍服务现有 LLM 调用，不转换为 Run 配置。

## 13. 官方参考

- [Responses 迁移与上下文项](https://developers.openai.com/api/docs/guides/migrate-to-responses)
- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

官方文档说明 OpenAI 行为；自定义兼容服务的实际支持范围须分别验证。
