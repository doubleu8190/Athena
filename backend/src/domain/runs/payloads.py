"""Validated JSON contracts carried by run commands."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _RunCommandPayload(BaseModel):
    """Base configuration shared by persisted command payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RunStartPayload(_RunCommandPayload):
    """User input required to start a Root run."""

    message: str
    message_id: str
    attachment_ids: tuple[str, ...] = ()

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def _freeze_attachment_ids(cls, value: object) -> object:
        # PostgreSQL returns JSON arrays as lists; normalize them to the immutable domain form.
        if isinstance(value, (list, tuple)):
            return tuple(value)
        return value


class RoundCompletedResumeValue(_RunCommandPayload):
    """Identity required to resume Root after a completed Worker round."""

    type: Literal["round_completed"]
    run_id: str
    plan_id: str
    round_id: str
    plan_version: int = Field(gt=0)
    root_wait_generation: int = Field(gt=0)
    interrupt_id: str


class RunResumePayload(_RunCommandPayload):
    """Identity required to continue a suspended Root checkpoint."""

    resume_value: RoundCompletedResumeValue


class RunCancelPayload(_RunCommandPayload):
    """Reason associated with a run cancellation request."""

    reason: str = "USER_REQUEST"


RunCommandPayload = RunStartPayload | RunCancelPayload | RunResumePayload
