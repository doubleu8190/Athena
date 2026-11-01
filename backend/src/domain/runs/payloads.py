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
class RootPlanWait:
    """Root 等待一个独立 Plan 全部任务结束时的 checkpoint 身份。"""

    run_id: str
    plan_id: str
    plan_sequence: int
    root_wait_generation: int
    interrupt_id: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        """拒绝缺失标识或无效的 Plan 序号、等待代次。"""
        if not all(value.strip() for value in (self.run_id, self.plan_id, self.interrupt_id, self.checkpoint_id)):
            raise ValueError("root_plan_wait_identity_invalid")
        if type(self.plan_sequence) is not int or self.plan_sequence < 1:
            raise ValueError("root_plan_sequence_invalid")
        if type(self.root_wait_generation) is not int or self.root_wait_generation < 1:
            raise ValueError("root_plan_wait_generation_invalid")


@dataclass(frozen=True, slots=True)
class RootPlanCompletedResumeValue:
    """Worker Plan 完成后恢复 Root 所需的稳定等待身份。"""

    type: Literal["plan_completed"]
    run_id: str
    plan_id: str
    plan_sequence: int
    root_wait_generation: int
    interrupt_id: str

    def __post_init__(self) -> None:
        """确保恢复身份绑定到一个有效 Plan 和等待代次。"""
        if self.type != "plan_completed" or not all(value.strip() for value in (self.run_id, self.plan_id, self.interrupt_id)):
            raise ValueError("run_resume_identity_invalid")
        if type(self.plan_sequence) is not int or self.plan_sequence < 1:
            raise ValueError("run_resume_plan_sequence_invalid")
        if type(self.root_wait_generation) is not int or self.root_wait_generation < 1:
            raise ValueError("run_resume_wait_generation_invalid")


@dataclass(frozen=True, slots=True)
class RootPlanCheckpointRecovery:
    """恢复已消费 Plan 中断后仍停留在持久 checkpoint 的 Root cursor。"""

    type: Literal["plan_checkpoint_recovery"]
    run_id: str
    plan_id: str
    plan_sequence: int
    root_wait_generation: int

    def __post_init__(self) -> None:
        """校验恢复 cursor 的 Run、Plan 和等待代次身份。"""
        if self.type != "plan_checkpoint_recovery" or not all(
            isinstance(value, str) and value.strip()
            for value in (self.run_id, self.plan_id)
        ):
            raise ValueError("run_checkpoint_recovery_identity_invalid")
        if type(self.plan_sequence) is not int or self.plan_sequence < 1:
            raise ValueError("run_checkpoint_recovery_sequence_invalid")
        if type(self.root_wait_generation) is not int or self.root_wait_generation < 1:
            raise ValueError("run_checkpoint_recovery_generation_invalid")


@dataclass(frozen=True, slots=True)
class RunResumePayload:
    """从持久化 Root checkpoint 继续执行所需的领域参数。"""

    resume_value: (
        RootPlanCompletedResumeValue
        | RootPlanCheckpointRecovery
    )

    def __post_init__(self) -> None:
        """确保恢复参数具有已验证的领域类型。"""
        if not isinstance(
            self.resume_value,
            (
                RootPlanCompletedResumeValue,
                RootPlanCheckpointRecovery,
            ),
        ):
            raise ValueError("run_resume_payload_identity_invalid")


RunCommandPayload: TypeAlias = RunStartPayload | RunResumePayload


__all__ = [
    "RootPlanCompletedResumeValue",
    "RootPlanCheckpointRecovery",
    "RootPlanWait",
    "RunCommandPayload",
    "RunResumePayload",
    "RunStartPayload",
]
