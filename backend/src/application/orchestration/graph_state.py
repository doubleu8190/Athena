"""Root checkpoint 字典对应的具体内容类型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from application.runs.request_contracts import RootRequestState


class RootAnswerInputState(BaseModel):
    """回答生成节点使用的具体回答意图。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["decision", "plan_summary"]
    kind: Literal["final", "clarification"]
    instructions: str = Field(min_length=1)


class RootPlanIdentity(BaseModel):
    """Root 等待节点读取的已提交计划身份。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    plan_id: str


class RootWaitIdentity(BaseModel):
    """Root checkpoint 中等待 Worker 轮次完成的身份。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: Literal["round_wait"]
    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    plan_version: int = Field(strict=True, gt=0)


class RootResumeIdentity(BaseModel):
    """checkpoint 中恢复已完成轮次时使用的身份字段。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: Literal["round_completed"]
    run_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    plan_version: int = Field(strict=True, gt=0)
    interrupt_id: str | None = None
    root_wait_generation: int | None = Field(default=None, gt=0)


__all__ = [
    "RootAnswerInputState",
    "RootPlanIdentity",
    "RootRequestState",
    "RootResumeIdentity",
    "RootWaitIdentity",
]
