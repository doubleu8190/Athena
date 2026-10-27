"""Run 执行所需的不可变领域参数。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class RunStartPayload:
    """启动 Run 时冻结的用户消息和附件身份。"""

    message: str
    message_id: str
    attachment_ids: tuple[str, ...] = ()
    sequence: int = 1

    def __post_init__(self) -> None:
        """确保消息身份、附件类型和会话序号可用于本轮执行。"""
        if not isinstance(self.message, str) or not isinstance(self.message_id, str):
            raise ValueError("run_start_payload_message_invalid")
        if any(not isinstance(value, str) for value in self.attachment_ids):
            raise ValueError("run_start_payload_attachment_ids_invalid")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("run_start_payload_sequence_invalid")


@dataclass(frozen=True, slots=True)
class RoundCompletedResumeValue:
    """Worker 一轮完成后恢复 Root 所需的身份。"""

    type: Literal["round_completed"]
    run_id: str
    plan_id: str
    round_id: str
    plan_version: int
    root_wait_generation: int
    interrupt_id: str

    def __post_init__(self) -> None:
        """确保恢复身份完整，且版本与等待代次为正数。"""
        values = (
            self.run_id,
            self.plan_id,
            self.round_id,
            self.interrupt_id,
        )
        if self.type != "round_completed" or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError("run_resume_identity_invalid")
        if type(self.plan_version) is not int or self.plan_version < 1:
            raise ValueError("run_resume_plan_version_invalid")
        if type(self.root_wait_generation) is not int or self.root_wait_generation < 1:
            raise ValueError("run_resume_wait_generation_invalid")


@dataclass(frozen=True, slots=True)
class RunResumePayload:
    """从持久化 Root checkpoint 继续执行所需的领域参数。"""

    resume_value: RoundCompletedResumeValue

    def __post_init__(self) -> None:
        """确保恢复参数具有已验证的领域类型。"""
        if not isinstance(self.resume_value, RoundCompletedResumeValue):
            raise ValueError("run_resume_payload_identity_invalid")


RunCommandPayload: TypeAlias = RunStartPayload | RunResumePayload


__all__ = [
    "RoundCompletedResumeValue",
    "RunCommandPayload",
    "RunResumePayload",
    "RunStartPayload",
]
