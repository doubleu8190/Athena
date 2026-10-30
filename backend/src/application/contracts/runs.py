"""Run 参数 JSON schema 及其到领域类型的转换。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.runs import (
    CommandType,
    RoundCompletedResumeValue,
    RunCommandPayload,
    RunResumePayload,
    RunStartPayload,
)


class RunStartPayloadSchema(BaseModel):
    """消息提交及持久化 JSON 使用的启动参数 schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    message: str
    message_id: str
    attachment_ids: tuple[str, ...] = ()
    sequence: int = Field(default=1, ge=1)

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def _freeze_attachment_ids(cls, value: object) -> object:
        """将 JSON 数组转换为不可变附件序列。

        参数：
        - `value`（object）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - object：列表输入转换为元组，其余输入原样返回，供后续字段校验使用。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    def to_domain(self) -> RunStartPayload:
        """把经过边界校验的参数转换为 Run 启动领域类型。

        参数：
        - 无参数。

        返回：
        - RunStartPayload：与当前请求契约对应的运行时领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RunStartPayload(
            message=self.message,
            message_id=self.message_id,
            attachment_ids=self.attachment_ids,
            sequence=self.sequence,
        )

    @classmethod
    def from_domain(cls, value: RunStartPayload) -> RunStartPayloadSchema:
        """把 Run 启动领域类型转换为可序列化 schema。

        参数：
        - `value`（RunStartPayload）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RunStartPayloadSchema：由领域对象字段构造的不可变请求 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            message=value.message,
            message_id=value.message_id,
            attachment_ids=value.attachment_ids,
            sequence=value.sequence,
        )


class RoundCompletedResumeValueSchema(BaseModel):
    """Worker 完成事件的 Root 恢复身份 schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["round_completed"]
    run_id: str
    plan_id: str
    round_id: str
    plan_version: int = Field(gt=0)
    root_wait_generation: int = Field(gt=0)
    interrupt_id: str

    def to_domain(self) -> RoundCompletedResumeValue:
        """把已校验的恢复身份转换为领域类型。

        参数：
        - 无参数。

        返回：
        - RoundCompletedResumeValue：与当前请求契约对应的运行时领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RoundCompletedResumeValue(
            type=self.type,
            run_id=self.run_id,
            plan_id=self.plan_id,
            round_id=self.round_id,
            plan_version=self.plan_version,
            root_wait_generation=self.root_wait_generation,
            interrupt_id=self.interrupt_id,
        )

    @classmethod
    def from_domain(
        cls, value: RoundCompletedResumeValue
    ) -> RoundCompletedResumeValueSchema:
        """把领域恢复身份转换为可序列化 schema。

        参数：
        - `value`（RoundCompletedResumeValue）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RoundCompletedResumeValueSchema：由领域对象字段构造的不可变请求 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            type=value.type,
            run_id=value.run_id,
            plan_id=value.plan_id,
            round_id=value.round_id,
            plan_version=value.plan_version,
            root_wait_generation=value.root_wait_generation,
            interrupt_id=value.interrupt_id,
        )


class RunResumePayloadSchema(BaseModel):
    """Run 恢复参数在工作流和持久化边界上的 schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resume_value: RoundCompletedResumeValueSchema

    def to_domain(self) -> RunResumePayload:
        """把已校验的恢复参数转换为领域类型。

        参数：
        - 无参数。

        返回：
        - RunResumePayload：与当前请求契约对应的运行时领域对象。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return RunResumePayload(resume_value=self.resume_value.to_domain())

    @classmethod
    def from_domain(cls, value: RunResumePayload) -> RunResumePayloadSchema:
        """把领域恢复参数转换为可序列化 schema。

        参数：
        - `value`（RunResumePayload）：待解析、校验或转换的输入值；约束：无额外运行时限制。

        返回：
        - RunResumePayloadSchema：由领域对象字段构造的不可变请求 schema。

        异常：
        - 无预期业务异常；依赖调用产生的异常按当前实现传播。
        """
        return cls(
            resume_value=RoundCompletedResumeValueSchema.from_domain(
                value.resume_value
            )
        )


def parse_run_command_payload(
    command_type: CommandType, value: str | object
) -> RunCommandPayload:
    """在持久化边界校验 JSON，并返回对应的 Run 领域参数。

    参数：
    - `command_type`（CommandType）：待解析命令载荷对应的领域命令类型；约束：无额外运行时限制。
    - `value`（str | object）：待解析、校验或转换的输入值；约束：无额外运行时限制。

    返回：
    - RunCommandPayload：按命令类型解析并转换后的 Run 启动或恢复命令载荷。

    异常：
    - ValueError：校验条件 `捕获 ValueError 后` 成立时。
    - ValueError：执行到该异常分支时。
    - 未列出的依赖异常按当前实现传播。
    """
    try:
        if command_type is CommandType.RUN_START:
            schema = (
                RunStartPayloadSchema.model_validate_json(value)
                if isinstance(value, str)
                else RunStartPayloadSchema.model_validate(value)
            )
            return schema.to_domain()
        if command_type is CommandType.RUN_RESUME:
            schema = (
                RunResumePayloadSchema.model_validate_json(value)
                if isinstance(value, str)
                else RunResumePayloadSchema.model_validate(value)
            )
            return schema.to_domain()
    except ValueError as error:
        raise ValueError(f"invalid_payload_for_{command_type.value}") from error
    raise ValueError(f"unsupported_run_command_type:{command_type}")


__all__ = [
    "RoundCompletedResumeValueSchema",
    "RunResumePayloadSchema",
    "RunStartPayloadSchema",
    "parse_run_command_payload",
]
