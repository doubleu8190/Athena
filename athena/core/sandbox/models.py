"""Platform-neutral sandbox request and result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ExecutionStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OOM = "oom"
    RUNNER_UNAVAILABLE = "runner_unavailable"


@dataclass(frozen=True)
class SandboxRunRequest:
    run_id: str
    session_id: str
    workspace: Path
    image: str
    network_policy: str = "none"


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    run_id: str
    image: str
    argv: list[str]
    workspace: Path
    cwd: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60.0
    max_output_bytes: int = 1_000_000
    network_policy: str = "none"


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    output_truncated: bool = False
    error_code: str | None = None


@dataclass
class SandboxRunHandle:
    run_id: str
    container_id: str
    workspace: Path
    image: str
    network_policy: str


@dataclass(frozen=True)
class MCPProcessSpec:
    command: str
    args: list[str]
    env: dict[str, str]
    container_name: str
