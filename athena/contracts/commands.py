"""跨越 Gateway 与 Runtime 边界的版本化命令信封。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any
from athena.contracts.statuses import AgentApprovalDecision
from pydantic import BaseModel, ConfigDict, Field


class CommandType(StrEnum):
    MESSAGE_SUBMIT = "message.submit"
    RUN_CANCEL = "run.cancel"
    RUN_PAUSE = "run.pause"
    RUN_RESUME = "run.resume"
    APPROVAL_RESOLVE = "approval.resolve"
    APPROVAL_CANCEL = "approval.cancel"
    MEMORY_CREATE = "memory.create"
    FILE_RETRY = "file.retry"
    FILE_CANCEL = "file.cancel"


class Command(BaseModel):
    """应用接收的 JSON 安全且可幂等处理的命令对象。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1)
    command_id: str = Field(min_length=1)
    command_type: CommandType
    session_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    def validate_payload(self) -> None:
        """校验不同命令类型所需的最小 payload 协议。

        参数:
            无；使用当前命令中的 ``command_type`` 和 ``payload``。
        返回值:
            None: payload 满足当前命令的必填字段和枚举约束。
        异常:
            ValueError: 缺少必填字段或审批决策不是合法枚举值。
        """
        required: dict[CommandType, tuple[str, ...]] = {
            CommandType.MESSAGE_SUBMIT: ("message",),
            CommandType.APPROVAL_RESOLVE: ("approval_id", "decision"),
            CommandType.APPROVAL_CANCEL: ("approval_id",),
            CommandType.MEMORY_CREATE: ("content",),
            CommandType.FILE_RETRY: ("task_id",),
            CommandType.FILE_CANCEL: ("task_id",),
        }
        missing = [
            key
            for key in required.get(self.command_type, ())
            if key not in self.payload
        ]
        if missing:
            raise ValueError(f"missing command payload: {', '.join(missing)}")
        if self.command_type == CommandType.APPROVAL_RESOLVE:
            try:
                AgentApprovalDecision(str(self.payload["decision"]))
            except ValueError as exc:
                raise ValueError("invalid approval decision") from exc

    def payload_fingerprint(self) -> str:
        """计算 payload 的稳定 SHA-256 指纹，用于幂等冲突检测。

        参数:
            无；使用当前信封中的 JSON payload。
        返回值:
            str: 64 位小写十六进制 SHA-256 摘要。
        异常:
            TypeError: payload 含不可 JSON 序列化的值时抛出。
        """
        raw = json.dumps(
            self.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(raw.encode()).hexdigest()
