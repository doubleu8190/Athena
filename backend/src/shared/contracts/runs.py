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
        """将 JSON 数组转换为不可变附件序列。"""
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value

    def to_domain(self) -> RunStartPayload:
        """把经过边界校验的参数转换为 Run 启动领域类型。"""
        return RunStartPayload(
            message=self.message,
            message_id=self.message_id,
            attachment_ids=self.attachment_ids,
            sequence=self.sequence,
        )

    @classmethod
    def from_domain(cls, value: RunStartPayload) -> RunStartPayloadSchema:
        """把 Run 启动领域类型转换为可序列化 schema。"""
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
        """把已校验的恢复身份转换为领域类型。"""
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
        """把领域恢复身份转换为可序列化 schema。"""
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
        """把已校验的恢复参数转换为领域类型。"""
        return RunResumePayload(resume_value=self.resume_value.to_domain())

    @classmethod
    def from_domain(cls, value: RunResumePayload) -> RunResumePayloadSchema:
        """把领域恢复参数转换为可序列化 schema。"""
        return cls(
            resume_value=RoundCompletedResumeValueSchema.from_domain(
                value.resume_value
            )
        )


def parse_run_command_payload(
    command_type: CommandType, value: str | object
) -> RunCommandPayload:
    """在持久化边界校验 JSON，并返回对应的 Run 领域参数。"""
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
