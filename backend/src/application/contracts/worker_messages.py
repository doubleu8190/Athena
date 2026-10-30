"""Worker 执行消息 JSON schema 与领域类型映射。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from domain.orchestration.worker_execution_messages import (
    WorkerExecutionMessageContent,
    WorkerToolCallContent,
    WorkerToolResultContent,
)


class WorkerToolCallContentSchema(BaseModel):
    """模型输出的工具调用 JSON schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]

    def to_domain(self) -> WorkerToolCallContent:
        """转换为工具调用领域类型。

        参数：
        - 无参数。

        返回：
        - WorkerToolCallContent：包含当前消息内容及工具调用信息的领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return WorkerToolCallContent(
            id=self.id,
            name=self.name,
            arguments=dict(self.arguments),
        )

    @classmethod
    def from_domain(cls, value: WorkerToolCallContent) -> WorkerToolCallContentSchema:
        """将工具调用领域类型转换为 JSON schema。

        参数：
        - `value`（WorkerToolCallContent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - WorkerToolCallContentSchema：由领域消息内容及其工具调用构造的 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(id=value.id, name=value.name, arguments=value.arguments)


class WorkerToolResultContentSchema(BaseModel):
    """模型可见工具结果的 JSON schema。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    status: str
    output: Any = None
    error: dict[str, Any] | None = None
    duration_ms: int | None = None
    result_ref: str | None = None
    truncated: bool | None = None

    def to_domain(self) -> WorkerToolResultContent:
        """转换为工具结果领域类型，并保留提供方扩展字段。

        参数：
        - 无参数。

        返回：
        - WorkerToolResultContent：包含当前消息内容及工具调用信息的领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return WorkerToolResultContent(
            status=self.status,
            output=self.output,
            error=self.error,
            duration_ms=self.duration_ms,
            result_ref=self.result_ref,
            truncated=self.truncated,
            extra_fields=dict(self.model_extra or {}),
        )

    @classmethod
    def from_domain(
        cls, value: WorkerToolResultContent
    ) -> WorkerToolResultContentSchema:
        """将工具结果领域类型转换为 JSON schema，展开扩展字段。

        参数：
        - `value`（WorkerToolResultContent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - WorkerToolResultContentSchema：由领域消息内容及其工具调用构造的 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls.model_validate(
            {
                "status": value.status,
                "output": value.output,
                "error": value.error,
                "duration_ms": value.duration_ms,
                "result_ref": value.result_ref,
                "truncated": value.truncated,
                **value.extra_fields,
            }
        )


class WorkerExecutionMessageContentSchema(BaseModel):
    """Worker 消息 JSON schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: Any = None
    tool_calls: tuple[WorkerToolCallContentSchema, ...] = ()
    tool_result: WorkerToolResultContentSchema | None = None

    def to_domain(self) -> WorkerExecutionMessageContent:
        """转换为 Worker 消息领域类型。

        参数：
        - 无参数。

        返回：
        - WorkerExecutionMessageContent：包含当前消息内容及工具调用信息的领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return WorkerExecutionMessageContent(
            content=self.content,
            tool_calls=tuple(call.to_domain() for call in self.tool_calls),
            tool_result=(
                self.tool_result.to_domain() if self.tool_result is not None else None
            ),
        )

    @classmethod
    def from_domain(
        cls, value: WorkerExecutionMessageContent
    ) -> WorkerExecutionMessageContentSchema:
        """将 Worker 消息领域类型转换为 JSON schema。

        参数：
        - `value`（WorkerExecutionMessageContent）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - WorkerExecutionMessageContentSchema：由领域消息内容及其工具调用构造的 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            content=value.content,
            tool_calls=tuple(
                WorkerToolCallContentSchema.from_domain(call)
                for call in value.tool_calls
            ),
            tool_result=(
                WorkerToolResultContentSchema.from_domain(value.tool_result)
                if value.tool_result is not None
                else None
            ),
        )


__all__ = [
    "WorkerExecutionMessageContentSchema",
    "WorkerToolCallContentSchema",
    "WorkerToolResultContentSchema",
]
