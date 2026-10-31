"""Worker 协议通用 HTTP 请求边界。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue
from starlette.requests import Request

from application.orchestration.worker_protocol_models import (
    WorkerResultReceiptModel,
)
from interfaces.http.auth.middleware import AuthenticationMiddleware
from interfaces.http.auth.options import AuthOptions
from interfaces.http.controllers.worker_registry_controller import (
    build_worker_registry_router,
)
from domain.orchestration.worker_entities import WorkerIdempotencyConflict
from application.orchestration.worker_registration import (
    WorkerRegistrationService,
)
from domain.orchestration.worker_entities import WorkerRegistrationReceipt
from interfaces.http.worker_protocol import (
    WorkerProtocolError,
    WorkerProtocolHttp,
    install_worker_protocol_handlers,
)

_EXAMPLES = json.loads(
    (Path(__file__).parents[2] / "docs/worker-protocol-v1.examples.json").read_text(
        encoding="utf-8"
    )
)["examples"]


def _request(
    body: bytes = b"{}", headers: Iterable[tuple[bytes, bytes]] = ()
) -> Request:
    """为协议边界测试构造可重放的 ASGI 请求。

    参数：
    - `body`（bytes）：请求体原始字节。
    - `headers`（Iterable[tuple[bytes, bytes]]）：原样保留重复项的 ASGI 请求头。

    返回：向控制器提供指定请求头和请求体的 Request。

    异常：此测试辅助函数不主动抛出业务异常。
    """
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/worker-api/v1/tasks",
            "raw_path": b"/worker-api/v1/tasks",
            "query_string": b"",
            "headers": list(headers),
            "client": ("127.0.0.1", 1234),
            "server": ("worker.test", 443),
        },
        receive,
    )


def test_service_authentication_rejects_missing_invalid_and_duplicate_headers() -> None:
    """缺凭据返回 401，无效版本只在身份认证成功后报告。"""
    protocol = WorkerProtocolHttp({"worker-a": "secret-token"})

    with pytest.raises(WorkerProtocolError, match="authentication") as missing:
        protocol.authenticate(_request())
    assert missing.value.status == 401
    assert missing.value.code == "AUTHENTICATION_FAILED"

    invalid = _request(
        headers=[
            (b"authorization", b"Bearer wrong-token"),
            (b"x-athena-protocol-version", b"9.0"),
        ]
    )
    with pytest.raises(WorkerProtocolError) as unauthorized:
        protocol.authenticate(invalid)
    assert unauthorized.value.status == 401

    duplicated = _request(
        headers=[
            (b"authorization", b"Bearer secret-token"),
            (b"authorization", b"Bearer another-token"),
            (b"x-athena-protocol-version", b"1.0"),
        ]
    )
    with pytest.raises(WorkerProtocolError) as malformed:
        protocol.authenticate(duplicated)
    assert malformed.value.status == 400


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b'{"protocol_version":"1.0","status":"ready","status":"not_ready"}', "MALFORMED_REQUEST"),
        (b'{"protocol_version":"1.0","value":NaN}', "MALFORMED_REQUEST"),
        (b'{"protocol_version":"1.0","value":1e999}', "MALFORMED_REQUEST"),
        (b'{"protocol_version":"1.0","value":1208925819614629174706176}', "MALFORMED_REQUEST"),
        (b'{"protocol_version":"1.0","value":"\\ud800"}', "MALFORMED_REQUEST"),
    ],
)
def test_raw_json_rejects_non_jcs_or_duplicate_content(body: bytes, expected_code: str) -> None:
    """拒绝重键和 RFC 8785 不能规范化的 JSON 数值或文本。"""
    protocol = WorkerProtocolHttp({"worker-a": "secret-token"})
    request = _request(
        body,
        headers=[(b"content-type", b"application/json; charset=utf-8")],
    )

    with pytest.raises(WorkerProtocolError) as error:
        asyncio.run(protocol.read_json(request, schema_name="HealthResponse"))
    assert error.value.code == expected_code
    assert error.value.status == 400


def test_json_stream_body_limit_applies_without_content_length() -> None:
    """即使不提供 Content-Length，也按流式读取限制最大字节数。"""
    protocol = WorkerProtocolHttp({"worker-a": "secret-token"}, max_json_body_bytes=1024)
    body = b" " * 1025
    request = _request(
        body,
        headers=[(b"content-type", b"application/json; charset=utf-8")],
    )

    with pytest.raises(WorkerProtocolError) as error:
        asyncio.run(protocol.read_json(request, schema_name="HealthResponse"))
    assert error.value.code == "PAYLOAD_TOO_LARGE"
    assert error.value.status == 413


def test_result_route_binds_execution_from_path_and_database_not_result_body() -> None:
    """ResultSubmission 不含 execution_id；路由从路径及持久分配解析身份。"""
    identities: list[tuple[str, str]] = []
    results: list[tuple[str, str, dict[str, JsonValue]]] = []

    class RegistrationService:
        async def find_replay(self, raw_body, *, service_principal):
            return None

        async def register_with_replay(self, raw_body, *, service_principal):
            raise AssertionError("registration route was not called")

    class RegistryService:
        async def resolve_execution_instance(self, service_principal, execution_id):
            identities.append((service_principal, execution_id))
            return "instance-owned-by-worker-a"

        async def resolve_cancel_instance(
            self, service_principal, execution_id, cancel_command_id
        ):
            raise AssertionError("stop route was not called")

    class ResultService:
        async def submit(self, raw_body, *, instance_id, execution_id):
            results.append((instance_id, execution_id, raw_body))
            return WorkerResultReceiptModel.model_validate(
                {
                    "protocol_version": "1.0",
                    "result_id": raw_body["result_id"],
                    "execution_id": execution_id,
                    "generation": raw_body["generation"],
                    "disposition": "recorded",
                    "reason": "success_recorded",
                    "received_at": "2026-10-30T08:10:00Z",
                }
            )

    class StopService:
        async def confirm(self, raw_body, *, instance_id):
            raise AssertionError("stop route was not called")

    class OriginValidator:
        async def validate_registration_origin(self, base_url):
            return None

    protocol = WorkerProtocolHttp({"worker-a": "secret-token"})
    app = FastAPI()
    app.add_middleware(AuthenticationMiddleware, options=AuthOptions(enabled=True))
    app.include_router(
        build_worker_registry_router(
            RegistrationService,
            RegistryService,
            ResultService,
            StopService,
            OriginValidator(),
            protocol,
        ),
        prefix="/api",
    )
    client = TestClient(app)
    body = {
        "protocol_version": "1.0",
        "result_id": "11000000-0000-4000-8000-000000000001",
        "generation": 1,
        "status": "completed",
        "output": {"answer": "ok"},
    }
    response = client.post(
        "/api/worker-executions/execution-01/result",
        json=body,
        headers={
            "Authorization": "Bearer secret-token",
            "X-Athena-Protocol-Version": "1.0",
            "Idempotency-Key": body["result_id"],
            "Content-Type": "application/json; charset=utf-8",
        },
    )

    assert response.status_code == 200
    assert response.json()["execution_id"] == "execution-01"
    assert identities == [("worker-a", "execution-01")]
    assert results[0][:2] == ("instance-owned-by-worker-a", "execution-01")
    assert "execution_id" not in results[0][2]


@pytest.mark.parametrize(
    ("path", "example_name", "conflicting_service"),
    [
        ("/api/worker-instances", "registration_request", "registration"),
        (
            "/api/worker-executions/execution-01/result",
            "result_success",
            "result",
        ),
        (
            "/api/worker-executions/80000000-0000-4000-8000-000000000001/stop-confirmation",
            "stop_confirmation_timeout",
            "stop",
        ),
    ],
)
def test_idempotency_conflicts_map_to_protocol_409(
    path: str, example_name: str, conflicting_service: str
) -> None:
    """注册、结果和停止确认的领域幂等冲突均返回 Worker v1 409。"""
    class Service:
        async def find_replay(self, raw_body, *, service_principal):
            if conflicting_service == "registration":
                raise WorkerIdempotencyConflict("worker_registration_idempotency_conflict")
            return None

        async def register_with_replay(self, raw_body, *, service_principal):
            raise AssertionError("unexpected service call")

        async def submit(self, raw_body, *, instance_id, execution_id):
            if conflicting_service == "result":
                raise WorkerIdempotencyConflict("worker_result_idempotency_conflict")
            raise AssertionError("unexpected service call")

        async def confirm(self, raw_body, *, instance_id):
            if conflicting_service == "stop":
                raise WorkerIdempotencyConflict("worker_stop_confirmation_idempotency_conflict")
            raise AssertionError("unexpected service call")

    class Registry:
        async def resolve_execution_instance(self, service_principal, execution_id):
            return "worker-instance"

        async def resolve_cancel_instance(self, service_principal, execution_id, cancel_command_id):
            return "worker-instance"

    class OriginValidator:
        async def validate_registration_origin(self, base_url):
            return None

    body = _EXAMPLES[example_name]["payload"]
    identity_field = {
        "registration_request": "registration_request_id",
        "result_success": "result_id",
        "stop_confirmation_timeout": "stop_confirmation_id",
    }[example_name]
    app = FastAPI()
    install_worker_protocol_handlers(app)
    app.include_router(
        build_worker_registry_router(
            Service,
            Registry,
            Service,
            Service,
            OriginValidator(),
            WorkerProtocolHttp({"worker-a": "secret-token"}),
        ),
        prefix="/api",
    )
    response = TestClient(app).post(
        path,
        json=body,
        headers={
            "Authorization": "Bearer secret-token",
            "X-Athena-Protocol-Version": "1.0",
            "Idempotency-Key": body[identity_field],
            "Content-Type": "application/json; charset=utf-8",
        },
    )

    assert response.status_code == 409
    expected_code = {
        "registration": "IDEMPOTENCY_KEY_REUSED",
        "result": "RESULT_ID_CONFLICT",
        "stop": "STOP_CONFIRMATION_CONFLICT",
    }[conflicting_service]
    assert response.json()["code"] == expected_code


def test_registration_replay_precedes_origin_validation_and_hash_conflict_precedes_dns() -> None:
    """持久化精确重放跳过 DNS；同 ID 不同内容先返回 409。"""
    request_body = dict(_EXAMPLES["registration_request"]["payload"])

    class Repository:
        receipt: WorkerRegistrationReceipt | None = None
        request_hash: str | None = None

        async def find_registration_receipt(
            self, service_principal, registration_request_id, request_hash
        ):
            if self.receipt is None:
                return None
            if self.request_hash != request_hash:
                raise WorkerIdempotencyConflict("worker_registration_idempotency_conflict")
            return WorkerRegistrationReceipt(
                instance_id=self.receipt.instance_id,
                instance_key=self.receipt.instance_key,
                contract_id=self.receipt.contract_id,
                contract_revision=self.receipt.contract_revision,
                registered_at=self.receipt.registered_at,
                replayed=True,
            )

        async def register_worker(self, intent):
            self.request_hash = intent.request_hash
            self.receipt = WorkerRegistrationReceipt(
                instance_id="instance-id",
                instance_key=intent.instance_key,
                contract_id="contract-id",
                contract_revision=1,
                registered_at=datetime(2026, 10, 31, tzinfo=timezone.utc),
            )
            return self.receipt

    class Registry:
        async def resolve_execution_instance(self, service_principal, execution_id):
            return None

        async def resolve_cancel_instance(self, service_principal, execution_id, cancel_command_id):
            return None

    class ResultService:
        async def submit(self, *args, **kwargs):
            raise AssertionError("result route was not called")

    class StopService:
        async def confirm(self, *args, **kwargs):
            raise AssertionError("stop route was not called")

    class OriginValidator:
        calls = 0

        async def validate_registration_origin(self, base_url):
            self.calls += 1
            if self.calls > 1:
                raise ValueError("worker_origin_not_allowlisted")

    repository = Repository()
    origin_validator = OriginValidator()
    registration = WorkerRegistrationService(repository)
    app = FastAPI()
    install_worker_protocol_handlers(app)
    app.include_router(
        build_worker_registry_router(
            lambda: registration,
            Registry,
            ResultService,
            StopService,
            origin_validator,
            WorkerProtocolHttp({"worker-a": "secret-token"}),
        ),
        prefix="/api",
    )
    client = TestClient(app)

    def register(body):
        return client.post(
            "/api/worker-instances",
            json=body,
            headers={
                "Authorization": "Bearer secret-token",
                "X-Athena-Protocol-Version": "1.0",
                "Idempotency-Key": body["registration_request_id"],
                "Content-Type": "application/json; charset=utf-8",
            },
        )

    first = register(request_body)
    replay = register(request_body)
    changed = dict(request_body, base_url="https://not-allowlisted.example")
    conflict = register(changed)

    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert origin_validator.calls == 1
