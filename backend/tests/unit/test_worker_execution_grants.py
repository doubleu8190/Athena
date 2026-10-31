"""Worker 执行授权签名、身份绑定和过期边界测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import SecretStr

from domain.orchestration.worker_resource_entities import WorkerExecutionResourceScope
from application.orchestration.worker_execution_grants import WorkerExecutionGrantSigner


def _scope(*, expires_at: datetime | None = None) -> WorkerExecutionResourceScope:
    """构造仅供本文件使用的执行授权值对象。"""
    return WorkerExecutionResourceScope(
        service_principal="worker-service",
        worker_instance_id="instance-1",
        worker_instance_key="2f3adf90-1c74-4d41-8fdd-791adbcfc110",
        run_id="run-1",
        plan_id="plan-1",
        task_id="task-1",
        execution_id="execution-1",
        generation=1,
        cancellation_epoch=0,
        read_resource_ids=("a0000000-0000-4000-8000-000000000001",),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def test_worker_execution_grant_round_trip_binds_resource_scope() -> None:
    """有效 token 返回所有执行身份和明确读资源列表。"""
    signer = WorkerExecutionGrantSigner(SecretStr("k" * 32))

    claims = signer.verify(signer.issue(_scope()))

    assert claims.service_principal == "worker-service"
    assert claims.worker_instance_key == _scope().worker_instance_key
    assert claims.run_id == "run-1"
    assert claims.execution_id == "execution-1"
    assert claims.read_resource_ids == _scope().read_resource_ids


def test_worker_execution_grant_rejects_tampering_and_expiry() -> None:
    """签名修改、失效密钥、过期和朴素时间戳都被拒绝。"""
    signer = WorkerExecutionGrantSigner(SecretStr("k" * 32))
    token = signer.issue(_scope())
    payload, signature = token.split(".")
    tampered_signature = ("A" if signature[0] != "A" else "B") + signature[1:]

    with pytest.raises(ValueError, match="signature"):
        signer.verify(f"{payload}.{tampered_signature}")
    with pytest.raises(ValueError, match="signature"):
        WorkerExecutionGrantSigner(SecretStr("z" * 32)).verify(token)
    with pytest.raises(ValueError, match="expired"):
        signer.verify(token, now=datetime.now(timezone.utc) + timedelta(minutes=2))
    with pytest.raises(ValueError, match="aware"):
        signer.issue(_scope(expires_at=datetime.now() + timedelta(minutes=1)))


def test_worker_execution_grant_uses_authoritative_database_time() -> None:
    """首发事务使用数据库时钟签发期限，不受应用机时钟偏差影响。"""
    signer = WorkerExecutionGrantSigner(SecretStr("k" * 32))
    database_now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    deadline = database_now + timedelta(seconds=5)

    token = signer.issue(_scope(expires_at=deadline), now=database_now)
    claims = signer.verify(token, now=database_now)

    assert claims.expires_at == deadline


def test_worker_execution_grant_requires_strong_signing_secret() -> None:
    """少于 32 字节的签名密钥不创建签名器。"""
    with pytest.raises(ValueError, match="too_short"):
        WorkerExecutionGrantSigner(SecretStr("x" * 31))


def test_worker_execution_grant_rejects_claims_larger_than_s1_token_limit() -> None:
    """读资源范围超出 TaskCommand 令牌上限时拒绝签发。"""
    signer = WorkerExecutionGrantSigner(SecretStr("k" * 32))
    resource_ids = tuple(
        f"a0000000-0000-4000-8000-{number:012x}"
        for number in range(1000)
    )
    with pytest.raises(ValueError, match="too_large"):
        signer.issue(
            WorkerExecutionResourceScope(
                service_principal="worker-service",
                worker_instance_id="instance-1",
                worker_instance_key="2f3adf90-1c74-4d41-8fdd-791adbcfc110",
                run_id="run-1",
                plan_id="plan-1",
                task_id="task-1",
                execution_id="execution-1",
                generation=1,
                cancellation_epoch=0,
                read_resource_ids=resource_ids,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
