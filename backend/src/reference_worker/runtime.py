"""远程 Worker 注册、HTTPS 收件、确定性执行和结果 outbox。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from uuid import uuid4, uuid5

import httpx
import rfc8785
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from pydantic import JsonValue

from application.orchestration.worker_protocol_validation import (
    validate_worker_request,
)
from interfaces.http.worker_protocol import (
    WorkerProtocolError,
    WorkerProtocolHttp,
    install_worker_protocol_handlers,
)

from .config import (
    ReferenceWorkerConfig,
    WorkerCapabilityConfig,
    WorkerContractSnapshot,
)
from .http_client import create_center_client
from .store import StoredReceipt, WorkerStore

_LOGGER = logging.getLogger(__name__)


class _WorkerExecutionFailure(Exception):
    """用于区分 Worker 可报告的协议级执行失败。"""

    def __init__(self, code: str, *, retryable: bool) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


def _resource_http_failure(status_code: int) -> _WorkerExecutionFailure:
    """将资源服务 HTTP 状态映射到 S1 稳定的 WorkerError 分类。"""
    if status_code in {401, 403}:
        return _WorkerExecutionFailure("WORKER_PERMISSION_DENIED", retryable=False)
    if status_code in {408, 425, 429} or status_code >= 500:
        return _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
    if 400 <= status_code < 500:
        return _WorkerExecutionFailure("WORKER_INVALID_INPUT", retryable=False)
    return _WorkerExecutionFailure("WORKER_FATAL", retryable=False)


def _utc_now() -> str:
    """返回协议需要的 RFC 3339 UTC 时间。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _hash_json(body: dict[str, JsonValue]) -> str:
    """计算原始 JSON 对象的 SHA-256(JCS)。"""
    return hashlib.sha256(rfc8785.dumps(body)).hexdigest()


def _request_error(code: str, status: int, directive: str) -> WorkerProtocolError:
    """创建字段固定、不会泄露请求内容的协议错误。"""
    return WorkerProtocolError(
        code=code,
        status=status,
        directive=directive,
        message=code.replace("_", " ").lower(),
    )


class ReferenceWorker:
    """在独立进程运行的最小、可重启 Worker v1 实例。"""

    def __init__(self, config: ReferenceWorkerConfig) -> None:
        """初始化配置和尚未打开的本地持久存储。

        参数：
        - `config`（ReferenceWorkerConfig）：服务身份、TLS、能力和运行参数。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self.config = config
        self.store = WorkerStore(config.database_path)
        self.capability = WorkerCapabilityConfig.model_validate_json(
            config.capability_path.read_text(encoding="utf-8")
        )
        self._client: httpx.AsyncClient | None = None
        self._registered = False
        self._accepting_tasks = False
        self._stop = asyncio.Event()
        self._background: list[asyncio.Task[None]] = []
        self._execution_tasks: set[asyncio.Task[None]] = set()
        self._execution_tasks_by_key: dict[tuple[str, int], asyncio.Task[None]] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    @property
    def registered(self) -> bool:
        """返回本进程是否完成注册并收到有效中心身份。"""
        return self._registered

    @property
    def accepting_tasks(self) -> bool:
        """返回运行器是否可以接收新任务。"""
        return self._registered and self._accepting_tasks

    async def start(self) -> None:
        """建立证书校验客户端并恢复注册、执行和 outbox 循环。

        参数：无。

        返回：所有后台循环均已启动。

        异常：TLS 客户端构造失败时传播其配置错误。
        """
        self.config.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._client = create_center_client(self.config)
        self._stop.clear()
        self._background = [
            asyncio.create_task(self._registration_loop(), name="worker-register"),
            asyncio.create_task(self._execution_loop(), name="worker-execute"),
            asyncio.create_task(self._outbox_loop(), name="worker-result-outbox"),
        ]

    async def stop(self) -> None:
        """停止新收件、等待后台循环结束并关闭 HTTPS 连接池。

        参数：无。

        返回：所有本进程后台任务和连接均已关闭。

        异常：无预期业务异常；底层连接关闭异常向调用方传播。
        """
        self._accepting_tasks = False
        self._stop.set()
        for task in self._background:
            task.cancel()
        for task in tuple(self._execution_tasks):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        if self._execution_tasks:
            await asyncio.gather(*self._execution_tasks, return_exceptions=True)
        self._background.clear()
        self._execution_tasks.clear()
        self._execution_tasks_by_key.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _registration_loop(self) -> None:
        """在成功登记前持续重试，并原子保存中心分配的契约身份。"""
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._register_once()
                self._registered = True
                self._accepting_tasks = True
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning("worker_registration_retry error=%s", type(exc).__name__)
                await self._sleep(delay)
                delay = min(30.0, delay * 2)

    async def _register_once(self) -> None:
        """使用持久 registration_request_id 完成一次注册或重放。

        参数：无。

        返回：成功响应已写入本地设置。

        异常：网络/HTTP/Schema 错误在注册循环内按指数退避重试。
        """
        if self._client is None:
            raise RuntimeError("worker_http_client_not_started")
        capability = self.capability.model_dump(mode="json")
        base_body: dict[str, JsonValue] = {
            "protocol_version": "1.0",
            "worker_instance_key": str(self.config.instance_key),
            "base_url": self.config.public_base_url,
            "execution_timeout_seconds": self.config.execution_timeout_seconds,
            "max_concurrency": self.config.max_concurrency,
            "capability": capability,
        }
        fingerprint = _hash_json(base_body)
        saved = self.store.get_setting("registration")
        previous_response: dict[str, object] | None = None
        if saved is not None and saved.get("request_fingerprint") == fingerprint:
            request_id = str(saved["registration_request_id"])
            response_value = saved.get("response")
            previous_response = (
                response_value if isinstance(response_value, dict) and response_value else None
            )
        else:
            request_id = str(uuid4())
            self.store.set_setting(
                "registration",
                {
                    "request_fingerprint": fingerprint,
                    "registration_request_id": request_id,
                    "response": {},
                },
            )
        body = {**base_body, "registration_request_id": request_id}
        status, _, response_body = await self._post_json(
            f"{self.config.orchestrator_url}/api/worker-instances",
            body=body,
            headers={"Idempotency-Key": request_id},
        )
        if status not in {200, 201}:
            raise RuntimeError(f"worker_registration_http_{status}")
        validate_worker_request("RegistrationResponse", response_body)
        if (
            response_body["worker_instance_key"] != str(self.config.instance_key)
            or response_body["contract_id"] is None
            or int(response_body["contract_revision"]) < 1
        ):
            raise ValueError("worker_registration_identity_mismatch")
        if previous_response is not None and response_body != previous_response:
            raise ValueError("worker_registration_replay_response_changed")
        self.store.set_setting(
            "registration",
            {
                "request_fingerprint": fingerprint,
                "registration_request_id": request_id,
                "response": response_body,
            },
        )

    async def health_body(self) -> dict[str, object]:
        """生成带回本机注册键的健康状态。

        参数：无。

        返回：满足 HealthResponse schema 的健康信息。

        异常：无预期业务异常。
        """
        body: dict[str, object] = {
            "protocol_version": "1.0",
            "worker_instance_key": str(self.config.instance_key),
            "status": "ready" if self._registered else "not_ready",
            "accepting_tasks": self.accepting_tasks,
            "checked_at": _utc_now(),
        }
        validate_worker_request("HealthResponse", body)
        return body

    def _read_registration_schema(self, key: str) -> dict[str, Any]:
        """读取成功登记后被冻结的本地能力 Schema。"""
        value = self.capability.model_dump(mode="json")
        schema = value[key]
        if not isinstance(schema, dict):
            raise RuntimeError("worker_capability_schema_invalid")
        return schema

    async def _accept_task(
        self, request: dict[str, JsonValue], raw_hash: str
    ) -> tuple[StoredReceipt, int]:
        """校验契约输入并持久接受一个 TaskCommand。"""
        try:
            replay = self.store.replay_command(str(request["command_id"]), raw_hash)
        except ValueError as exc:
            raise _request_error("COMMAND_ID_CONFLICT", 409, "stop") from exc
        if replay is not None:
            if replay.ignored:
                raise _request_error("TASK_CANCELLED", 409, "stop")
            return replay, 200
        if not self.accepting_tasks:
            raise _request_error("INSTANCE_NOT_READY", 503, "retry_same_command")
        registration = self.store.get_setting("registration")
        response = None if registration is None else registration.get("response")
        if not isinstance(response, dict):
            raise _request_error("INSTANCE_NOT_READY", 503, "retry_same_command")
        if (
            request.get("contract_id") != response.get("contract_id")
            or request.get("contract_revision") != response.get("contract_revision")
        ):
            raise _request_error("UNKNOWN_CONTRACT", 422, "root_replan")
        contract_snapshot = WorkerContractSnapshot(
            contract_id=str(response["contract_id"]),
            contract_revision=int(response["contract_revision"]),
            input_schema=self.capability.model_dump(mode="json")["input_schema"],
            output_schema=self.capability.model_dump(mode="json")["output_schema"],
        )
        try:
            Draft202012Validator(
                contract_snapshot.input_schema,
                format_checker=FormatChecker(),
            ).validate(request.get("input"))
        except ValidationError as exc:
            raise _request_error("TASK_INPUT_INVALID", 422, "root_replan") from exc
        try:
            receipt = self.store.accept_command(
                command_id=str(request["command_id"]),
                request_hash=raw_hash,
                command_type="task",
                request=request,
                execution_id=str(request["execution_id"]),
                task_id=str(request["task_id"]),
                generation=int(request["generation"]),
                operation_id=str(request["operation_id"]),
                cancellation_epoch=int(request["cancellation_epoch"]),
                receipt_id=str(uuid4()),
                contract_snapshot=contract_snapshot,
            )
        except ValueError as exc:
            code = (
                "COMMAND_ID_CONFLICT"
                if str(exc) == "command_id_conflict"
                else "EXECUTION_SUPERSEDED"
            )
            raise _request_error(code, 409, "stop") from exc
        if receipt.ignored:
            raise _request_error("TASK_CANCELLED", 409, "stop")
        return receipt, 202 if not receipt.replayed else 200

    async def _accept_resume(
        self, request: dict[str, JsonValue], raw_hash: str
    ) -> tuple[StoredReceipt, int]:
        """仅接受和已持久审批暂停点完全匹配的恢复命令。"""
        try:
            replay = self.store.replay_command(str(request["command_id"]), raw_hash)
        except ValueError as exc:
            raise _request_error("COMMAND_ID_CONFLICT", 409, "operator_action") from exc
        if replay is not None:
            return replay, 200
        pause = self.store.pause_snapshot(
            pause_id=str(request["pause_id"]), execution_id=str(request["execution_id"])
        )
        if pause is None:
            raise _request_error("APPROVAL_STALE", 409, "stop")
        if (
            int(pause["generation"]) != int(request["generation"])
            or int(pause["cancellation_epoch"]) != int(request["cancellation_epoch"])
            or pause["approval_request_id"] != request["approval_request_id"]
        ):
            raise _request_error("APPROVAL_STALE", 409, "stop")
        decisions = request.get("decisions")
        if not isinstance(decisions, list):
            raise _request_error("APPROVAL_REQUEST_INVALID", 422, "operator_action")
        decision_hash = hashlib.sha256(rfc8785.dumps(decisions)).hexdigest()
        if decision_hash != request.get("decision_set_sha256"):
            raise _request_error("APPROVAL_REQUEST_INVALID", 422, "operator_action")
        pause_item_ids = json.loads(str(pause["item_ids_json"]))
        pause_hashes = json.loads(str(pause["arguments_hashes_json"]))
        expected_items = list(zip(pause_item_ids, pause_hashes, strict=True))
        actual_items = [
            (item.get("item_id"), item.get("arguments_sha256"))
            for item in decisions
            if isinstance(item, dict)
        ]
        if len(actual_items) != len(decisions) or actual_items != expected_items:
            raise _request_error("APPROVAL_STALE", 409, "stop")
        try:
            receipt = self.store.validate_and_accept_resume(
                request=request,
                request_hash=raw_hash,
                receipt_id=str(uuid4()),
            )
        except LookupError as exc:
            raise _request_error("APPROVAL_STALE", 409, "stop") from exc
        except ValueError as exc:
            raise _request_error("APPROVAL_REQUEST_INVALID", 422, "operator_action") from exc
        return receipt, 202 if not receipt.replayed else 200

    async def _accept_cancel(
        self, request: dict[str, JsonValue], raw_hash: str
    ) -> tuple[StoredReceipt, int]:
        """持久接收 CancelCommand，并在任务级别建立取消栅栏。"""
        execution_id = str(request["execution_id"])
        generation = int(request["generation"])
        identity = self.store.execution_identity(execution_id, generation)
        receipt = self.store.accept_cancel(
            command_id=str(request["command_id"]),
            request_hash=raw_hash,
            request=request,
            task_id=None if identity is None else str(identity["task_id"]),
            execution_id=execution_id,
            generation=generation,
            cancellation_epoch=int(request["cancellation_epoch"]),
            reason=str(request["reason"]),
            receipt_id=str(uuid4()),
        )
        targets = self.store.cancel_targets(
            execution_id, generation, str(request["reason"])
        )
        running = [
            self._execution_tasks_by_key[key]
            for key in targets
            if key in self._execution_tasks_by_key
        ]
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
        self.store.finalize_cancel(str(request["command_id"]))
        return receipt, 202

    async def _execution_loop(self) -> None:
        """领取持久 echo 执行，并在完成事务中创建结果 outbox。"""
        active = self._execution_tasks
        while not self._stop.is_set():
            if len(active) >= self.config.max_concurrency:
                _, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            execution = self.store.claim_execution()
            if execution is None:
                await self._sleep(0.1)
                continue
            task = asyncio.create_task(self._execute_claimed(execution))
            active.add(task)
            key = (str(execution["execution_id"]), int(execution["generation"]))
            self._execution_tasks_by_key[key] = task
            task.add_done_callback(
                lambda finished, execution_key=key: self._execution_finished(
                    execution_key, finished
                )
            )
            self._execution_tasks.add(task)
            task.add_done_callback(self._execution_tasks.discard)
            task.add_done_callback(active.discard)
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def _execute_claimed(self, execution: dict[str, object]) -> None:
        """将并发数限制在本地容量内。"""
        async with self._semaphore:
            await self._execute(execution)

    def _execution_finished(
        self, key: tuple[str, int], task: asyncio.Task[None]
    ) -> None:
        """移除完成的本地执行句柄，供取消命令精确等待。"""
        if self._execution_tasks_by_key.get(key) is task:
            self._execution_tasks_by_key.pop(key, None)

    async def _execute(self, execution: dict[str, object]) -> None:
        """以可重复的纯 echo 处理输入或读取授权资源后持久保存结果。"""
        execution_id = str(execution["execution_id"])
        generation = int(execution["generation"])
        request = execution["request"]
        if not isinstance(request, dict):
            self.store.mark_execution_state(execution_id, generation, "failed")
            return
        try:
            snapshot_value = execution.get("contract_snapshot")
            if not isinstance(snapshot_value, WorkerContractSnapshot):
                raise ValueError("worker_contract_snapshot_missing")
            contract_snapshot = snapshot_value
            if (
                request.get("contract_id") != contract_snapshot.contract_id
                or request.get("contract_revision")
                != contract_snapshot.contract_revision
            ):
                raise ValueError("worker_contract_snapshot_identity_mismatch")
        except Exception as exc:
            _LOGGER.error("worker_contract_snapshot_invalid type=%s", type(exc).__name__)
            self._complete_execution_failure(
                execution_id, generation, str(uuid4()), "WORKER_FATAL", False
            )
            return
        if self.config.execution_delay_seconds:
            await self._sleep(self.config.execution_delay_seconds)
        result_id = str(uuid4())
        try:
            task_input = request["input"]
            if not isinstance(task_input, dict):
                raise _WorkerExecutionFailure("WORKER_INVALID_INPUT", retryable=False)
            output = await self._execute_resource_copy(task_input, request)
        except asyncio.CancelledError:
            # 关机时保留 running 供下次启动恢复；持久取消栅栏禁止复活执行。
            raise
        except _WorkerExecutionFailure as exc:
            self._complete_execution_failure(
                execution_id, generation, result_id, exc.code, exc.retryable
            )
            return
        except (httpx.TimeoutException, TimeoutError):
            self._complete_execution_failure(
                execution_id, generation, result_id,
                "WORKER_UPSTREAM_TIMEOUT", retryable=True,
            )
            return
        except httpx.TransportError:
            self._complete_execution_failure(
                execution_id, generation, result_id,
                "WORKER_RESOURCE_UNAVAILABLE", retryable=True,
            )
            return
        except Exception as exc:
            _LOGGER.exception("worker_execution_failed type=%s", type(exc).__name__)
            self._complete_execution_failure(
                execution_id, generation, result_id, "WORKER_FATAL", False
            )
            return
        try:
            Draft202012Validator(
                contract_snapshot.output_schema, format_checker=FormatChecker()
            ).validate(output)
        except ValidationError:
            self._complete_execution_failure(
                execution_id, generation, result_id, "WORKER_OUTPUT_INVALID", False
            )
            return
        except Exception as exc:
            _LOGGER.exception("worker_contract_snapshot_invalid type=%s", type(exc).__name__)
            self._complete_execution_failure(
                execution_id, generation, result_id, "WORKER_FATAL", False
            )
            return
        self.store.complete_execution(
            execution_id=execution_id,
            generation=generation,
            output=output,
            result_id=result_id,
        )

    def _complete_execution_failure(
        self,
        execution_id: str,
        generation: int,
        result_id: str,
        code: str,
        retryable: bool,
    ) -> None:
        """保存具有稳定协议分类的失败结果及其 outbox 记录。"""
        self.store.complete_failure(
            execution_id=execution_id,
            generation=generation,
            error={
                "code": code,
                "message": code.replace("_", " ").lower(),
                "retryable": retryable,
            },
            result_id=result_id,
        )

    async def _execute_resource_copy(
        self, task_input: dict[str, JsonValue], request: dict[str, JsonValue]
    ) -> dict[str, object]:
        """读取 Grant 授权资源并在同一授权下复制；普通任务回显输入。"""
        reference_value = task_input.get("resource_reference")
        if reference_value is None:
            return {"echo": task_input}
        if not isinstance(reference_value, dict) or self._client is None:
            raise _WorkerExecutionFailure("WORKER_INVALID_INPUT", retryable=False)
        grant = request.get("execution_grant")
        if not isinstance(grant, dict) or not isinstance(grant.get("token"), str):
            raise _WorkerExecutionFailure("WORKER_INVALID_INPUT", retryable=False)
        resource_id = str(reference_value.get("resource_id", ""))
        expected_size = reference_value.get("size_bytes")
        if not isinstance(expected_size, int) or not 0 <= expected_size <= self.config.resource_max_bytes:
            raise _WorkerExecutionFailure("WORKER_INVALID_INPUT", retryable=False)
        async with self._client.stream(
            "GET",
            f"{self.config.orchestrator_url}/api/worker-resources/{resource_id}",
            headers={"X-Athena-Execution-Grant": str(grant["token"])},
        ) as source:
            if source.status_code != 200:
                raise _resource_http_failure(source.status_code)
            content_length = source.headers.get("content-length")
            if content_length is not None and int(content_length) != expected_size:
                raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
            if content_length is None:
                raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
            if (
                source.headers.get("content-sha256") != reference_value.get("sha256")
                or source.headers.get("content-type") != reference_value.get("media_type")
            ):
                raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
            raw_bytes = bytearray()
            async for chunk in source.aiter_bytes():
                raw_bytes.extend(chunk)
                if len(raw_bytes) > self.config.resource_max_bytes:
                    raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
        raw = bytes(raw_bytes)
        if len(raw) != expected_size:
            raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
        actual_digest = hashlib.sha256(raw).hexdigest()
        if actual_digest != reference_value.get("sha256"):
            raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
        new_resource_id = str(
            uuid5(
                self.config.instance_key,
                f"{request['execution_id']}:{request['generation']}:{resource_id}:copy",
            )
        )
        media_type = str(reference_value.get("media_type", "application/octet-stream"))
        status, _, resource_body = await self._put_json_response(
            f"{self.config.orchestrator_url}/api/worker-resources/{new_resource_id}",
            content=raw,
            headers={
                "Content-Type": media_type,
                "Content-SHA256": actual_digest,
                "Idempotency-Key": new_resource_id,
                "X-Athena-Execution-Grant": str(grant["token"]),
            },
        )
        if status not in {200, 201}:
            raise _resource_http_failure(status)
        try:
            validate_worker_request("ResourceReference", resource_body)
        except ValidationError as exc:
            raise _WorkerExecutionFailure(
                "WORKER_RESOURCE_UNAVAILABLE", retryable=True
            ) from exc
        if (
            resource_body["resource_id"] != new_resource_id
            or resource_body["size_bytes"] != expected_size
            or resource_body["sha256"] != actual_digest
        ):
            raise _WorkerExecutionFailure("WORKER_RESOURCE_UNAVAILABLE", retryable=True)
        return {"resource": resource_body}

    async def _outbox_loop(self) -> None:
        """以持久指数退避持续提交结果或停止确认。"""
        while not self._stop.is_set():
            item = self.store.claim_outbox(time.time())
            if item is None:
                await self._sleep(0.2)
                continue
            await self._submit_outbox(item)

    async def _submit_outbox(self, item: dict[str, object]) -> None:
        """投递一个 outbox 项并持久确认、退避或隔离错误。"""
        if self._client is None:
            return
        kind = str(item["kind"])
        body = item["body"]
        if not isinstance(body, dict):
            self.store.reject_outbox(int(item["outbox_id"]), error="outbox_body_invalid")
            return
        if kind == "result":
            execution_id = str(self.store.execution_from_outbox(str(item["dedupe_key"])))
            path = f"/api/worker-executions/{execution_id}/result"
        elif kind == "stop_confirmation":
            execution_id = str(body["execution_id"])
            path = f"/api/worker-executions/{execution_id}/stop-confirmation"
        else:
            self.store.reject_outbox(int(item["outbox_id"]), error="outbox_kind_unknown")
            return
        retry_after: str | None = None
        try:
            status, response_headers, receipt = await self._post_json(
                f"{self.config.orchestrator_url}{path}",
                body=body,
                headers={"Idempotency-Key": str(body.get("result_id") or body["stop_confirmation_id"])},
            )
            if status == 200:
                schema_name = "ResultReceipt" if kind == "result" else "StopConfirmationReceipt"
                validate_worker_request(schema_name, receipt)
                if kind == "result" and (
                    receipt["result_id"] != body["result_id"]
                    or receipt["execution_id"] != self.store.execution_from_outbox(str(item["dedupe_key"]))
                    or receipt["generation"] != body["generation"]
                ):
                    raise ValueError("worker_result_receipt_identity_mismatch")
                if kind == "stop_confirmation" and (
                    receipt["stop_confirmation_id"] != body["stop_confirmation_id"]
                    or receipt["cancel_command_id"] != body["cancel_command_id"]
                    or receipt["execution_id"] != body["execution_id"]
                    or receipt["generation"] != body["generation"]
                    or receipt["cancellation_epoch"] != body["cancellation_epoch"]
                    or receipt["reason"] != body["reason"]
                ):
                    raise ValueError("worker_stop_receipt_identity_mismatch")
                self.store.acknowledge_outbox(int(item["outbox_id"]))
                return
            if status < 500 and status not in {408, 425, 429}:
                self.store.reject_outbox(
                    int(item["outbox_id"]), error=f"http_{status}"
                )
                return
            error = f"http_{status}"
            retry_after = response_headers.get("retry-after")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            error = type(exc).__name__
            retry_after = None
        attempt = int(item["attempt"])
        base = min(
            self.config.result_retry_max_seconds,
            self.config.result_retry_initial_seconds * (2 ** min(attempt - 1, 20)),
        )
        jitter = self.config.result_retry_jitter_ratio
        delay = random.uniform(
            base * (1 - jitter),
            min(self.config.result_retry_max_seconds, base * (1 + jitter)),
        )
        if retry_after is not None:
            try:
                requested_delay = float(retry_after)
                if math.isfinite(requested_delay) and requested_delay >= 0:
                    delay = min(
                        self.config.result_retry_max_seconds,
                        max(delay, requested_delay),
                    )
            except ValueError:
                pass
        self.store.retry_outbox(
            int(item["outbox_id"]), available_at=time.time() + delay, error=error
        )

    async def _post_json(
        self, url: str, *, body: dict[str, object], headers: dict[str, str]
    ) -> tuple[int, dict[str, str], dict[str, JsonValue]]:
        """有界读取中心 JSON POST 响应，避免未限长响应消耗内存。"""
        if self._client is None:
            raise RuntimeError("worker_http_client_not_started")
        request_headers = {
            **headers,
            "Content-Type": "application/json; charset=utf-8",
        }
        async with self._client.stream(
            "POST", url, json=body, headers=request_headers
        ) as response:
            data = await self._read_limited_response(response)
            return response.status_code, dict(response.headers), data

    async def _put_json_response(
        self, url: str, *, content: bytes, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], dict[str, JsonValue]]:
        """有界读取资源 PUT 的 ResourceReference 响应。"""
        if self._client is None:
            raise RuntimeError("worker_http_client_not_started")
        async with self._client.stream(
            "PUT", url, content=content, headers=headers
        ) as response:
            data = await self._read_limited_response(response)
            return response.status_code, dict(response.headers), data

    async def _read_limited_response(
        self, response: httpx.Response
    ) -> dict[str, JsonValue]:
        """限流式读取 UTF-8 JSON 响应，不接受非对象或超限内容。"""
        declared = response.headers.get("content-length")
        if declared is not None and int(declared) > self.config.max_json_body_bytes:
            raise ValueError("worker_center_response_too_large")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > self.config.max_json_body_bytes:
                raise ValueError("worker_center_response_too_large")
        decoded = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=self._unique_response_object,
            parse_constant=self._reject_non_finite_response_number,
        )
        if not isinstance(decoded, dict):
            raise ValueError("worker_center_json_response_not_object")
        rfc8785.dumps(decoded)
        return decoded

    @staticmethod
    def _unique_response_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        """拒绝响应 JSON 对象中的重复键，避免歧义回执被确认。"""
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("worker_center_response_duplicate_json_key")
            result[key] = value
        return result

    @staticmethod
    def _reject_non_finite_response_number(value: str) -> None:
        """拒绝 JSON 标准外的 NaN 与 Infinity 响应数字。"""
        raise ValueError(f"worker_center_response_non_finite_number:{value}")

    async def _sleep(self, delay: float) -> None:
        """结束关闭事件或运行设定时长，以便测试和关机及时退出。"""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return

    def create_app(self, protocol: WorkerProtocolHttp) -> FastAPI:
        """建立用于独立 HTTPS 服务的 FastAPI 接收端。

        参数：
        - `protocol`（WorkerProtocolHttp）：共享 S1 认证、路径绑定、限长和错误适配器。

        返回：含心跳、Task、Resume 和 Cancel 路由的 FastAPI app。

        异常：无预期业务异常。
        """
        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            await self.start()
            try:
                yield
            finally:
                await self.stop()

        app = FastAPI(lifespan=lifespan)
        install_worker_protocol_handlers(app)

        @app.get("/worker-api/v1/health")
        async def health(request: Request) -> dict[str, object]:
            protocol.authenticate(request)
            return await self.health_body()

        @app.post("/worker-api/v1/tasks")
        async def accept_task(request: Request) -> JSONResponse:
            protocol.authenticate(request)
            body = await protocol.read_json(
                request, schema_name="TaskCommand", id_field="command_id"
            )
            protocol.require_idempotency(request, str(body["command_id"]))
            accepted, status = await self._accept_task(body, _hash_json(body))
            receipt = dict(accepted.receipt)
            if accepted.replayed:
                receipt["disposition"] = "duplicate"
            return JSONResponse(receipt, status_code=status)

        @app.post("/worker-api/v1/executions/{execution_id}/resume")
        async def resume(execution_id: str, request: Request) -> JSONResponse:
            protocol.authenticate(request)
            body = await protocol.read_json(
                request,
                schema_name="ResumeCommand",
                id_field="command_id",
                execution_id=execution_id,
            )
            protocol.require_idempotency(request, str(body["command_id"]))
            accepted, status = await self._accept_resume(body, _hash_json(body))
            receipt = dict(accepted.receipt)
            if accepted.replayed:
                receipt["disposition"] = "duplicate"
            return JSONResponse(receipt, status_code=status)

        @app.post("/worker-api/v1/executions/{execution_id}/cancel")
        async def cancel(execution_id: str, request: Request) -> JSONResponse:
            protocol.authenticate(request)
            body = await protocol.read_json(
                request,
                schema_name="CancelCommand",
                id_field="command_id",
                execution_id=execution_id,
            )
            protocol.require_idempotency(request, str(body["command_id"]))
            try:
                accepted, _ = await self._accept_cancel(body, _hash_json(body))
            except ValueError as exc:
                code = (
                    "COMMAND_ID_CONFLICT"
                    if str(exc) == "command_id_conflict"
                    else "CANCEL_IDENTITY_CONFLICT"
                )
                raise _request_error(code, 409, "operator_action") from exc
            return JSONResponse(accepted.receipt, status_code=202)

        return app


__all__ = ["ReferenceWorker"]
