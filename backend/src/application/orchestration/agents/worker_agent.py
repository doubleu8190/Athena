"""配置化 Worker Agent：执行单个已持久化任务。"""

from __future__ import annotations

from collections.abc import Mapping
import json
import re
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from application.orchestration.agents.llm_activity import (
    invoke_llm_with_activity,
)
from application.orchestration.worker_tools import worker_tool_manifest
from application.orchestration.lease_context import lease_grant_from_context
from application.tools.service import ToolService

from domain.events.ports import EventPort
from domain.integrations import LLMPort
from domain.orchestration import (
    WorkerExecutionEntryType,
    WorkerExecutionMessageAppend,
    WorkerExecutionMessageContent,
    WorkerExecutionMessageRepository,
    WorkerLLMRole,
    WorkerToolCallContent,
)
from shared.logging import get_logger

logger = get_logger(__name__)


class ConfiguredWorkerAgent:
    """使用已配置模型执行一个持久化 Worker 任务。
    """

    def __init__(
        self,
        llm: LLMPort,
        event_publisher: EventPort | None = None,
        tool_service: ToolService | None = None,
        execution_messages: WorkerExecutionMessageRepository | None = None,
    ) -> None:
        """保存对象依赖并初始化运行所需状态。

        参数：
        - `llm`（LLMPort）：执行模型请求的语言模型依赖；约束：无额外运行时限制。
        - `event_publisher`（EventPort | None）：发布运行进度和领域事件的依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `tool_service`（ToolService | None）：查询工具能力并执行工具调用的应用服务；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `execution_messages`（WorkerExecutionMessageRepository | None）：读取和追加 Worker 执行对话记录的仓储依赖；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        self._llm = llm
        self._events = event_publisher
        self._tool_service = tool_service
        self._execution_messages = execution_messages

    async def __call__(
        self,
        *,
        execution: Mapping[str, Any],
        state: Mapping[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """读取执行上下文并调用模型，返回 Worker 图状态更新。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `state`（Mapping[str, Any] | None）：当前可恢复的工作流状态，包含本阶段所需数据；约束：允许为 None，按未提供值的分支处理；可省略，默认值为 `None`。
        - `_kwargs`（Any）：调用适配器额外注入的关键字参数；当前处理不读取这些值；约束：无额外运行时限制。

        返回：
        - dict[str, Any]：提供给 Worker 图的回答正文或工具调用状态补丁。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        task = execution.get("task", {})
        task = task if isinstance(task, Mapping) else {}
        allowed_tools = tuple(
            str(name) for name in task.get("allowed_tools", ()) if str(name)
        )
        manifest = await worker_tool_manifest(self._tool_service)
        allowed_tool_set = set(allowed_tools)
        tool_specs = [
            {
                "name": item.name,
                "description": item.description,
                "parameters": item.parameters,
                "risk_level": item.risk_level,
                "requires_approval": item.requires_approval,
            }
            for item in manifest
            if item.name in allowed_tool_set
        ]
        logger.debug(
            "worker_tool_capability_snapshot",
            run_id=execution.get("run_id", ""),
            task_id=task.get("task_id", ""),
            allowed_tools=allowed_tools,
            available_tools=tuple(item.name for item in manifest),
            selected_tools=tuple(item["name"] for item in tool_specs),
        )
        if execution.get("_worker_result_refs"):
            tool_specs.append(
                {
                    "name": "read_worker_result",
                    "description": (
                        "Read a bounded slice from a large result produced earlier "
                        "in this Worker execution."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "result_ref": {"type": "string"},
                            "offset": {"type": "integer", "minimum": 0},
                            "max_chars": {"type": "integer", "minimum": 1},
                        },
                        "required": ["result_ref"],
                    },
                }
            )
        messages = _worker_messages(
            execution=execution,
            task=task,
            allowed_tools=allowed_tools,
            tool_specs=tool_specs,
        )
        tool_round = int(state.get("tool_round", 0)) if isinstance(state, Mapping) else 0
        invocation_id = _invocation_id(execution, tool_round)
        await self._persist_llm_messages(
            execution=execution,
            invocation_id=invocation_id,
            messages=messages,
        )
        value = await self._invoke(messages, execution)
        result = _worker_result(value)
        task_id = _task_id(execution)
        generation = int(execution.get("execution_generation", 0))
        tool_calls = result.get("tool_calls", [])
        for index, call in enumerate(tool_calls):
            call["id"] = f"worker:{task_id}:{generation}:{tool_round}:{index}"
        await self._persist_assistant_message(
            execution=execution,
            invocation_id=invocation_id,
            tool_calls=tool_calls,
            value=value,
        )
        return result

    async def _persist_llm_messages(
        self,
        *,
        execution: Mapping[str, Any],
        invocation_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """记录发送给 Provider 的系统提示和用户消息原文。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `invocation_id`（str）：本次图或模型调用的关联标识；约束：无额外运行时限制。
        - `messages`（list[dict[str, Any]]）：按顺序排列的模型对话消息；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._execution_messages is None:
            return
        for index, message in enumerate(messages):
            role = WorkerLLMRole(str(message["role"]))
            await self._execution_messages.append(
                _transcript_message(
                    execution=execution,
                    message_id=f"{invocation_id}:input:{index}",
                    invocation_id=invocation_id,
                    entry_type=WorkerExecutionEntryType.LLM_MESSAGE,
                    role=role,
                    content=WorkerExecutionMessageContent(
                        content=message.get("content")
                    ),
                )
            )

    async def _persist_assistant_message(
        self,
        *,
        execution: Mapping[str, Any],
        invocation_id: str,
        tool_calls: list[dict[str, Any]],
        value: Any,
    ) -> None:
        """记录助手实际返回内容及规范化后的工具调用。

        参数：
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
        - `invocation_id`（str）：本次图或模型调用的关联标识；约束：无额外运行时限制。
        - `tool_calls`（list[dict[str, Any]]）：模型本轮请求执行的工具调用集合；约束：无额外运行时限制。
        - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - None：操作完成后不提供返回数据。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if self._execution_messages is None:
            return
        normalized_calls = tuple(
            WorkerToolCallContent(
                id=str(call.get("id", "")),
                name=str(call.get("name", "")),
                arguments=(
                    dict(call["arguments"])
                    if isinstance(call.get("arguments"), Mapping)
                    else {}
                ),
            )
            for call in tool_calls
        )
        await self._execution_messages.append(
            _transcript_message(
                execution=execution,
                message_id=f"{invocation_id}:assistant",
                invocation_id=invocation_id,
                entry_type=WorkerExecutionEntryType.LLM_MESSAGE,
                role=WorkerLLMRole.ASSISTANT,
                content=WorkerExecutionMessageContent(
                    content=_assistant_content(value),
                    tool_calls=normalized_calls,
                ),
            )
        )

    async def _invoke(self, messages: list[Any], execution: Mapping[str, Any]) -> Any:
        """通过 LLM 活动封装执行一次模型调用。

        参数：
        - `messages`（list[Any]）：按顺序排列的模型对话消息；约束：无额外运行时限制。
        - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

        返回：
        - Any：Provider 返回的助手消息，供 Worker 解析正文或工具调用。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        task = execution.get("task", {})
        task_id = str(task.get("task_id", "")) if isinstance(task, Mapping) else ""
        generation = execution.get("execution_generation", 0)
        parent_id = f"task:{task_id}:exec:{generation}"
        return await invoke_llm_with_activity(
            self._llm,
            self._events,
            messages,
            session_id=str(execution.get("session_id", "")),
            run_id=str(execution.get("run_id", "")),
            parent_activity_id=parent_id,
        )


def _task_id(execution: Mapping[str, Any]) -> str:
    """从执行快照读取稳定 Task 身份。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。

    返回：
    - str：执行快照中经过规范化的 Task 标识。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    task = execution.get("task", {})
    return str(task.get("task_id", "")) if isinstance(task, Mapping) else ""


def _invocation_id(execution: Mapping[str, Any], tool_round: int) -> str:
    """根据执行代次生成幂等的模型调用身份。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
    - `tool_round`（int）：Worker 当前工具调用轮次，用于关联本轮调用；约束：须通过方法内校验；失败条件见下方异常说明。

    返回：
    - str：由执行代次和调用位置稳定生成的幂等调用标识。

    异常：
    - ValueError：校验条件 `not task_id or generation < 1 or tool_round < 0` 成立时（异常说明：worker_model_invocation_identity_missing）。
    - 未列出的依赖异常按当前实现传播。
    """
    task_id = _task_id(execution)
    generation = int(execution.get("execution_generation", 0))
    if not task_id or generation < 1 or tool_round < 0:
        raise ValueError("worker_model_invocation_identity_missing")
    return f"worker:{task_id}:{generation}:llm:{tool_round}"


def _transcript_message(
    *,
    execution: Mapping[str, Any],
    message_id: str,
    invocation_id: str,
    entry_type: WorkerExecutionEntryType,
    role: WorkerLLMRole,
    content: WorkerExecutionMessageContent,
) -> WorkerExecutionMessageAppend:
    """构造经过校验且绑定执行范围的对话追加记录。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
    - `message_id`（str）：关联消息的稳定标识；None 表示未绑定消息；约束：无额外运行时限制。
    - `invocation_id`（str）：本次图或模型调用的关联标识；约束：无额外运行时限制。
    - `entry_type`（WorkerExecutionEntryType）：Worker 执行记录的条目类型；约束：无额外运行时限制。
    - `role`（WorkerLLMRole）：消息在对话中的角色；约束：无额外运行时限制。
    - `content`（WorkerExecutionMessageContent）：待处理或返回的正文内容；约束：无额外运行时限制。

    返回：
    - WorkerExecutionMessageAppend：绑定 Worker 执行代次、租约和调用身份的持久化消息记录。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    return WorkerExecutionMessageAppend(
        message_id=message_id,
        session_id=str(execution.get("session_id", "")),
        run_id=str(execution.get("run_id", "")),
        plan_id=str(execution.get("plan_id", "")),
        task_id=_task_id(execution),
        execution_id=str(execution.get("execution_id", "")),
        execution_generation=int(execution.get("execution_generation", 0)),
        lease=lease_grant_from_context(execution),
        invocation_id=invocation_id,
        entry_type=entry_type,
        role=role,
        content=content,
    )


def _assistant_content(value: Any) -> Any:
    """提取 Provider 返回且不含 LangChain 包装的助手正文。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - Any：从 Provider 响应中提取的助手正文；无法识别的内容按输入值返回。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if isinstance(value, BaseMessage):
        return value.content
    if isinstance(value, Mapping) and "content" in value:
        return value.get("content")
    return value


def _worker_messages(
    *,
    execution: Mapping[str, Any],
    task: Mapping[str, Any],
    allowed_tools: tuple[str, ...],
    tool_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """构造包含任务输入、上游结果与可用工具的模型消息。

    参数：
    - `execution`（Mapping[str, Any]）：当前执行上下文，用于核对任务、代次和租约身份；约束：无额外运行时限制。
    - `task`（Mapping[str, Any]）：当前 Worker 任务定义；约束：无额外运行时限制。
    - `allowed_tools`（tuple[str, ...]）：当前任务明确授权使用的工具名集合；约束：无额外运行时限制。
    - `tool_specs`（list[dict[str, Any]]）：工具目录中的名称、参数和风险定义；约束：无额外运行时限制。

    返回：
    - list[dict[str, Any]]：包含系统指令、任务内容、上游结果及工具定义的模型消息列表。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    system_message = (
        "You are an independent worker. Complete the assigned objective. "
        "You may request only the tools in the supplied tool manifest. "
        "Return one JSON object with output, evidence, uncertainties, "
        "and optionally tool_calls. Each tool call has name and arguments. "
        "When tool results are supplied, use them before requesting more. "
        f"Tool manifest: {json.dumps(tool_specs, ensure_ascii=False)}"
    )
    user_message = json.dumps(
        {
            "objective": task.get("objective", ""),
            "input": task.get("input", {}),
            "expected_output": task.get("expected_output", {}),
            "upstream": execution.get("upstream_results", {}),
            "allowed_tools": list(allowed_tools),
            "tool_results": execution.get("tool_results", []),
        },
        ensure_ascii=False,
    )
    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": user_message},
    ]


def _worker_result(value: Any) -> dict[str, Any]:
    """把 Provider 回复统一为 Worker 的结果与工具调用契约。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：符合 Worker 状态契约的正文、工具调用或失败结果映射。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    # 兼容 LangChain 消息和普通响应映射，统一从各自边界提取正文及原始工具调用。
    content = value.get("content", value) if isinstance(value, Mapping) else value
    if isinstance(value, BaseMessage):
        content = value.content
    parsed = _decode_worker_reply(content)
    raw_calls = value.get("tool_calls", []) if isinstance(value, Mapping) else []
    if isinstance(value, AIMessage):
        raw_calls = value.tool_calls
    tool_calls = _normalize_tool_calls(parsed.get("tool_calls", raw_calls))
    output = parsed.get("output")
    if output is None:
        # 无显式 output 时，将非控制字段视为任务结果；工具调用单独保留在 tool_calls。
        output = {} if tool_calls else {
            key: item
            for key, item in parsed.items()
            if key not in {
                "evidence",
                "uncertainties",
                "tool_calls",
                "status",
                "error",
                "retryable",
            }
        }
    if not isinstance(output, Mapping):
        output = {"value": output}
    evidence = parsed.get("evidence", [])
    if not isinstance(evidence, (list, tuple)):
        evidence = [evidence] if evidence else []
    uncertainties = parsed.get("uncertainties", [])
    if isinstance(uncertainties, str):
        uncertainties = [uncertainties] if uncertainties else []
    elif not isinstance(uncertainties, (list, tuple)):
        uncertainties = []
    result = {
        "status": str(parsed.get("status", "completed")),
        "output": output,
        "evidence": evidence,
        "uncertainties": uncertainties,
        "tool_calls": tool_calls,
    }
    if parsed.get("error") is not None:
        error = parsed["error"]
        result["error"] = (
            dict(error) if isinstance(error, Mapping) else {"message": str(error)}
        )
    if parsed.get("retryable") is not None:
        result["retryable"] = bool(parsed["retryable"])
    return result


def _decode_worker_reply(content: Any) -> dict[str, Any]:
    """解析 Worker JSON 封装，同时保留普通文本回答。

    参数：
    - `content`（Any）：待处理或返回的正文内容；约束：无额外运行时限制。

    返回：
    - dict[str, Any]：解析后的 Worker 正文或工具调用状态字段。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if isinstance(content, Mapping):
        return dict(content)
    text = str(content or "").strip()
    provider_tool_calls = _decode_provider_tool_calls(text)
    if provider_tool_calls is not None:
        return {"tool_calls": provider_tool_calls}
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {"output": text}
    return dict(value) if isinstance(value, Mapping) else {"output": value}


def _decode_provider_tool_calls(text: str) -> list[dict[str, Any]] | None:
    """将 Provider 的文本函数调用解析为 Worker 工具契约。

    参数：
    - `text`（str）：待规范化、解析或展示的文本；约束：无额外运行时限制。

    返回：
    - list[dict[str, Any]] | None：解析后的工具调用列表；文本格式缺失、重复参数或结构不完整时返回 None。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if "<tool_call>" not in text:
        return None
    # 按 Provider 文本协议逐段解码；任一段结构不完整时整体拒绝，避免部分执行。
    blocks = re.findall(r"<tool_call>(.*?)</tool_call>", text, flags=re.DOTALL)
    if not blocks:
        return None
    calls: list[dict[str, Any]] = []
    for block in blocks:
        function_match = re.search(
            r"<function=([A-Za-z0-9_.:-]+)>", block, flags=re.DOTALL
        )
        if function_match is None:
            return None
        arguments: dict[str, Any] = {}
        for name, raw_value in re.findall(
            r"<parameter=([A-Za-z0-9_.:-]+)>(.*?)</parameter>",
            block,
            flags=re.DOTALL,
        ):
            if name in arguments:
                return None
            value = raw_value.strip()
            try:
                arguments[name] = json.loads(value)
            except (TypeError, ValueError):
                arguments[name] = value
        if not arguments:
            return None
        calls.append(
            {"name": function_match.group(1), "arguments": arguments}
        )
    return calls


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    """将 JSON 或 LangChain 工具调用记录规范为 Worker 图输入。

    参数：
    - `value`（Any）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - list[dict[str, Any]]：字段统一且参数为映射的 Worker 工具调用列表。

    异常：
    - 无预期业务异常；依赖调用产生的异常按当前实现传播。
    """
    if not isinstance(value, (list, tuple)):
        return []
    calls: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name", "")).strip()
        arguments = item.get("arguments", item.get("args", {}))
        if name:
            calls.append(
                {
                    "id": str(item.get("id", "")),
                    "name": name,
                    "arguments": dict(arguments)
                    if isinstance(arguments, Mapping)
                    else arguments,
                }
            )
    return calls
__all__ = ["ConfiguredWorkerAgent"]
