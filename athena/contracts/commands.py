"""跨越 Gateway 与 Runtime 边界的版本化命令信封。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from athena.contracts.statuses import AgentApprovalDecision
from athena.models.json_models import CommandPayload
from pydantic import BaseModel, ConfigDict, Field


class CommandType(StrEnum):
    MESSAGE_SUBMIT = "message.submit"
    RUN_CANCEL = "run.cancel"
    RUN_PAUSE = "run.pause"
    RUN_RESUME = "run.resume"
    APPROVAL_RESOLVE = "approval.resolve"
    APPROVAL_CANCEL = "approval.cancel"
    MEMORY_CREATE = "memory.create"


class Command(BaseModel):
    """应用接收的 JSON 安全且可幂等处理的命令对象。"""

    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1)
    command_id: str = Field(min_length=1)
    command_type: CommandType
    session_id: str = Field(min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: CommandPayload = Field(default_factory=CommandPayload)

    def validate_payload(self) -> None:
        """检查当前命令是否包含所需的基本内容。

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
        }
        missing = [
            key
            for key in required.get(self.command_type, ())
            if getattr(self.payload, key, None) is None
        ]
        if missing:
            raise ValueError(f"missing command payload: {', '.join(missing)}")
        if self.command_type == CommandType.APPROVAL_RESOLVE:
            try:
                AgentApprovalDecision(str(self.payload.decision))
            except ValueError as exc:
                raise ValueError("invalid approval decision") from exc

    def payload_fingerprint(self) -> str:
        """计算 payload 的稳定 SHA-256 指纹，用于幂等冲突检测。

        参数:
            无；使用当前信封中的 JSON payload。
        返回值:
            str: 64 位小写十六进制 SHA-256 摘要。
        异常:
            TypeError: 命令内容中包含无法转换为 JSON 的值时抛出。
        """
        raw = json.dumps(
            self.payload.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(raw.encode()).hexdigest()
