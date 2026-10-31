"""参考 Worker 的本地连接、TLS 和持久化配置。"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from pydantic import JsonValue
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class WorkerCapabilityConfig(BaseModel):
    """Worker 向注册中心声明的能力和 JSON Schema。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    can_do: tuple[str, ...] = Field(min_length=1)
    cannot_do: tuple[str, ...] = Field(min_length=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


class WorkerContractSnapshot(BaseModel):
    """绑定到已收件 Execution 的不可变契约 Schema 快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: str = Field(min_length=1, max_length=255)
    contract_revision: int = Field(ge=1)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ReferenceWorkerConfig:
    """独立 Worker 进程所需的网络、身份、TLS 和本地数据设置。"""

    orchestrator_url: str
    public_base_url: str
    service_token: str = field(repr=False)
    orchestrator_token: str = field(repr=False)
    instance_key: UUID
    database_path: Path
    capability_path: Path
    tls_certificate_path: Path
    tls_private_key_path: Path
    ca_certificate_path: Path
    host: str = "0.0.0.0"
    port: int = 9443
    max_concurrency: int = 4
    execution_timeout_seconds: int = 300
    execution_delay_seconds: float = 0.0
    result_retry_initial_seconds: float = 1.0
    result_retry_max_seconds: float = 60.0
    result_retry_jitter_ratio: float = 0.2
    max_json_body_bytes: int = 1_048_576
    resource_max_bytes: int = 104_857_600

    def __post_init__(self) -> None:
        """验证进程能安全建立 TLS 服务并按协议注册。

        参数：无。

        返回：无。

        异常：
        - ValueError：URL、并发、超时或重试范围不合法时抛出。
        """
        for value, name in (
            (self.orchestrator_url, "worker_orchestrator_url"),
            (self.public_base_url, "worker_public_url"),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError(f"{name}_must_be_https_origin")
        if not self.service_token.strip():
            raise ValueError("worker_service_token_required")
        if not self.orchestrator_token.strip():
            raise ValueError("worker_orchestrator_token_required")
        if not 1 <= self.port <= 65535:
            raise ValueError("worker_port_out_of_range")
        if not 1 <= self.max_concurrency <= 256:
            raise ValueError("worker_max_concurrency_out_of_range")
        if not 1 <= self.execution_timeout_seconds <= 604800:
            raise ValueError("worker_execution_timeout_out_of_range")
        if not math.isfinite(self.execution_delay_seconds) or self.execution_delay_seconds < 0:
            raise ValueError("worker_execution_delay_negative")
        if (
            not math.isfinite(self.result_retry_initial_seconds)
            or not math.isfinite(self.result_retry_max_seconds)
            or not 0.1 <= self.result_retry_initial_seconds <= 60
            or not self.result_retry_initial_seconds
            <= self.result_retry_max_seconds
            <= 3600
            or not math.isfinite(self.result_retry_jitter_ratio)
            or not 0 <= self.result_retry_jitter_ratio <= 1
        ):
            raise ValueError("worker_result_retry_range_invalid")
        if not 1024 <= self.max_json_body_bytes <= 104_857_600:
            raise ValueError("worker_json_body_limit_out_of_range")
        if not 1 <= self.resource_max_bytes <= 1_073_741_824:
            raise ValueError("worker_resource_max_bytes_out_of_range")
        for path in (
            self.tls_certificate_path,
            self.tls_private_key_path,
            self.ca_certificate_path,
        ):
            if not path.is_file():
                raise ValueError(f"worker_tls_file_missing:{path.name}")

    @classmethod
    def from_environment(cls) -> ReferenceWorkerConfig:
        """从 ATHENA_REFERENCE_WORKER_* 环境变量读取严格配置。

        参数：无。

        返回：TLS 文件存在且配置范围有效的 Worker 运行设置。

        异常：
        - ValueError：必填变量缺失、能力文件无效或配置不满足协议时抛出。
        """
        def required(name: str) -> str:
            value = os.environ.get(f"ATHENA_REFERENCE_WORKER_{name}", "")
            if not value.strip():
                raise ValueError(f"worker_configuration_missing:{name}")
            return value

        data_root = Path(required("DATA_DIR")).expanduser().resolve()
        capability_path = Path(required("CAPABILITY_FILE")).expanduser().resolve()
        try:
            capability = WorkerCapabilityConfig.model_validate_json(
                capability_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as exc:
            raise ValueError("worker_capability_file_invalid") from exc
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key_path = data_root / "worker-instance-key.json"
        if key_path.exists():
            try:
                instance_key = UUID(
                    json.loads(key_path.read_text(encoding="utf-8"))[
                        "worker_instance_key"
                    ]
                )
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError("worker_instance_key_store_invalid") from exc
        else:
            configured_key = os.getenv("ATHENA_REFERENCE_WORKER_INSTANCE_KEY")
            instance_key = UUID(configured_key) if configured_key else uuid4()
            temp_path = key_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps({"worker_instance_key": str(instance_key)}), encoding="utf-8"
            )
            temp_path.replace(key_path)
        return cls(
            orchestrator_url=required("ORCHESTRATOR_URL").rstrip("/"),
            public_base_url=required("PUBLIC_BASE_URL").rstrip("/"),
            service_token=required("SERVICE_TOKEN"),
            orchestrator_token=required("ORCHESTRATOR_TOKEN"),
            instance_key=instance_key,
            database_path=data_root / "worker.sqlite3",
            capability_path=capability_path,
            tls_certificate_path=Path(required("TLS_CERTIFICATE")).expanduser().resolve(),
            tls_private_key_path=Path(required("TLS_PRIVATE_KEY")).expanduser().resolve(),
            ca_certificate_path=Path(required("CA_CERTIFICATE")).expanduser().resolve(),
            host=os.getenv("ATHENA_REFERENCE_WORKER_HOST", "0.0.0.0"),
            port=int(os.getenv("ATHENA_REFERENCE_WORKER_PORT", "9443")),
            max_concurrency=int(os.getenv("ATHENA_REFERENCE_WORKER_MAX_CONCURRENCY", "4")),
            execution_timeout_seconds=int(
                os.getenv("ATHENA_REFERENCE_WORKER_TIMEOUT_SECONDS", "300")
            ),
            execution_delay_seconds=float(
                os.getenv("ATHENA_REFERENCE_WORKER_EXECUTION_DELAY_SECONDS", "0")
            ),
            result_retry_initial_seconds=float(
                os.getenv("ATHENA_REFERENCE_WORKER_RESULT_RETRY_INITIAL_SECONDS", "1")
            ),
            result_retry_max_seconds=float(
                os.getenv("ATHENA_REFERENCE_WORKER_RESULT_RETRY_MAX_SECONDS", "60")
            ),
            result_retry_jitter_ratio=float(
                os.getenv("ATHENA_REFERENCE_WORKER_RESULT_RETRY_JITTER_RATIO", "0.2")
            ),
            resource_max_bytes=int(
                os.getenv("ATHENA_REFERENCE_WORKER_RESOURCE_MAX_BYTES", "104857600")
            ),
        )


__all__ = ["ReferenceWorkerConfig", "WorkerCapabilityConfig"]
