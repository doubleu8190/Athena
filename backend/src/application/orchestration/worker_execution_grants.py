"""签发和验证绑定执行身份的 Worker 资源授权。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import rfc8785
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domain.orchestration.worker_resource_entities import WorkerExecutionResourceScope


class WorkerExecutionGrantClaims(BaseModel):
    """签名授权令牌内稳定保存的执行范围。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str = "1.0"
    service_principal: str = Field(min_length=1, max_length=255)
    worker_instance_id: str = Field(min_length=1, max_length=255)
    worker_instance_key: str = Field(min_length=1, max_length=255)
    run_id: str = Field(min_length=1, max_length=255)
    plan_id: str = Field(min_length=1, max_length=255)
    task_id: str = Field(min_length=1, max_length=255)
    execution_id: str = Field(min_length=1, max_length=255)
    generation: int = Field(ge=1)
    cancellation_epoch: int = Field(ge=0)
    expires_at: datetime
    read_resource_ids: tuple[str, ...] = ()


class WorkerExecutionGrantSigner:
    """使用仅 Athena 持有的密钥签发不可伪造的执行授权。"""

    def __init__(self, secret: SecretStr) -> None:
        """初始化授权签名器。

        参数：
        - `secret`（SecretStr）：独立于 Worker 服务令牌的签名密钥；解码后的 UTF-8 字节至少 32 个。

        返回：无。

        异常：ValueError 表示密钥强度不足。
        """
        key = secret.get_secret_value().encode("utf-8")
        if len(key) < 32:
            raise ValueError("worker_execution_grant_secret_too_short")
        self._key = key

    def issue(
        self,
        scope: WorkerExecutionResourceScope,
        *,
        now: datetime | None = None,
    ) -> str:
        """签发序列化的 HMAC-SHA256 执行授权。

        参数：
        - `scope`（WorkerExecutionResourceScope）：数据库验证后的实例、执行、代次和读资源范围，必须包含授权过期时间。
        - `now`（datetime | None）：可选的权威 UTC 当前时间；首次发送事务使用数据库时钟，其它调用默认使用本机 UTC 时钟。

        返回：由规范 JSON 和签名组成的不透明 Base64URL 字符串。

        异常：ValueError 表示授权有效期不是未来 UTC 时间。
        """
        current_time = _as_utc(now or datetime.now(timezone.utc))
        if scope.expires_at is None:
            raise ValueError("worker_execution_grant_expiry_required")
        expires_at = _as_utc(scope.expires_at)
        if expires_at <= current_time:
            raise ValueError("worker_execution_grant_expiry_not_future")
        claims = WorkerExecutionGrantClaims(
            service_principal=scope.service_principal,
            worker_instance_id=scope.worker_instance_id,
            worker_instance_key=scope.worker_instance_key,
            run_id=scope.run_id,
            plan_id=scope.plan_id,
            task_id=scope.task_id,
            execution_id=scope.execution_id,
            generation=scope.generation,
            cancellation_epoch=scope.cancellation_epoch,
            expires_at=expires_at,
            read_resource_ids=scope.read_resource_ids,
        )
        payload = rfc8785.dumps(claims.model_dump(mode="json"))
        signature = hmac.digest(self._key, payload, "sha256")
        token = f"{_encode(payload)}.{_encode(signature)}"
        if len(token) > 8192:
            raise ValueError("worker_execution_grant_too_large")
        return token

    def verify(self, token: str, *, now: datetime | None = None) -> WorkerExecutionGrantClaims:
        """验证授权签名并拒绝过期或格式错误的令牌。

        参数：
        - `token`（str）：资源请求的 opaque execution grant；长度上限 8192 字符。
        - `now`（datetime | None）：测试用的带时区当前时间；生产使用 UTC 系统时钟。

        返回：可信的 WorkerExecutionGrantClaims。

        异常：ValueError 表示签名错误、内容损坏或授权已过期。
        """
        if len(token) > 8192:
            raise ValueError("worker_execution_grant_too_large")
        try:
            encoded_payload, encoded_signature = token.split(".", maxsplit=1)
            payload = _decode(encoded_payload)
            signature = _decode(encoded_signature)
        except (ValueError, TypeError) as exc:
            raise ValueError("worker_execution_grant_malformed") from exc
        expected = hmac.digest(self._key, payload, "sha256")
        if not hmac.compare_digest(signature, expected):
            raise ValueError("worker_execution_grant_invalid_signature")
        try:
            claims = WorkerExecutionGrantClaims.model_validate_json(payload)
        except ValueError as exc:
            raise ValueError("worker_execution_grant_invalid_claims") from exc
        current_time = _as_utc(now or datetime.now(timezone.utc))
        if _as_utc(claims.expires_at) <= current_time:
            raise ValueError("worker_execution_grant_expired")
        return claims


def _encode(value: bytes) -> str:
    """编码不带填充字符的 URL 安全 Base64。"""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    """严格解码 URL 安全 Base64，拒绝非规范字符。"""
    raw = value.encode("ascii")
    decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    if _encode(decoded) != value:
        raise ValueError("base64url_not_canonical")
    return decoded


def _as_utc(value: datetime) -> datetime:
    """确保签发、校验的时间都表示带时区 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("worker_execution_grant_time_must_be_aware")
    return value.astimezone(timezone.utc)


__all__ = ["WorkerExecutionGrantClaims", "WorkerExecutionGrantSigner"]
