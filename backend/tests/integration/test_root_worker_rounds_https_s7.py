"""S7：经真实 Root 图、持久恢复和独立 HTTPS Worker 完成逐轮执行。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import ssl
import sqlite3
import subprocess
import sys
import time
import traceback
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from pydantic import SecretStr
from pydantic import BaseModel, ValidationError

from application.orchestration.root_decision import (
    AnswerIntent,
    PlanDraft,
    PlanIntent,
    PlanTaskDraft,
    RootAgentInput,
    RootDecision,
    RootPlanInput,
    RootPlanResult,
)
from application.orchestration.protocols import RootAgent
from application.orchestration.agents import ConfiguredRootAgent
from configuration.settings import Settings
from domain.integrations import LLMStreamEnd, LLMTextDelta
from infrastructure.persistence.postgres.models import (
    AgentRunModel,
    OrchestratorExecutionModel,
    OrchestratorPlanModel,
    OrchestratorRootResumeJobModel,
    OrchestratorTaskModel,
    OrchestratorWorkerCommandModel,
    WorkerInstanceModel,
    WorkerResultReceiptModel,
)
from tests.integration.test_reference_worker_https import (
    _ORCHESTRATOR_TOKEN,
    _SERVICE_TOKEN,
    _create_test_certificates,
    _free_port,
    _terminate,
    _wait_until,
    _worker_process,
)
from interfaces.http.worker_protocol import install_worker_protocol_handlers
from bootstrap.postgres import build_postgres_dependencies
from infrastructure.integrations.llm.llm_provider import ConfiguredLLMProvider
from infrastructure.integrations.llm.options import coerce_llm_options


_DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_LLM_OPT_IN = "ATHENA_S7_REAL_LLM"
_SERVICE_PRINCIPAL = "worker-service"
_GRANT_SECRET = "s7-test-only-grant-secret-long-enough"
_CAPABILITY = {
    "can_do": ["Echo one JSON object and return the exact input."],
    "cannot_do": ["Run arbitrary code or access data outside the request."],
    "input_schema": {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
        "additionalProperties": False,
    },
    "output_schema": {
        "type": "object",
        "properties": {"echo": {"type": "object"}},
        "required": ["echo"],
        "additionalProperties": False,
    },
}


class _DeterministicRoundAgent(RootAgent):
    """S7 确定性 Root Agent：第二轮输入只能来自首轮真实输出。"""

    def __init__(self) -> None:
        self.plan_inputs: list[RootPlanInput] = []
        self.decide_calls = 0

    async def decide(self, inputs: RootAgentInput) -> RootDecision:
        """根据已完成计划轮数选择下一轮计划或最终回答。"""
        self.decide_calls += 1
        if len(inputs.completed_plans) >= 2:
            return AnswerIntent(
                action="answer",
                kind="final",
                instructions="返回 Worker 两轮结果",
            )
        if len(inputs.completed_plans) == 1:
            return PlanIntent(
                action="plan",
                goal="根据第一轮实际结果完成复核",
                instructions="使用上一轮真实输出",
            )
        return PlanIntent(action="plan", goal="先读取并回显，再基于结果复核", instructions="执行一轮 Worker 任务")

    async def plan(self, inputs: RootPlanInput) -> RootPlanResult:
        """把首轮随机值或首轮输出 nonce 放入后续任务输入。"""
        self.plan_inputs.append(inputs)
        if not inputs.worker_capabilities:
            raise AssertionError("Root must receive the registered Worker capability")
        capability = inputs.worker_capabilities[0]
        if not inputs.completed_plans:
            value = f"s7-first-{uuid4().hex}"
        else:
            first = inputs.completed_plans[-1]
            output = first.tasks[0].output if first.tasks else None
            value = json.dumps(output, ensure_ascii=False, sort_keys=True)
        return PlanDraft(
            action="plan",
            goal=inputs.goal,
            tasks=(
                PlanTaskDraft(
                    task_ref=f"round-{len(self.plan_inputs)}",
                    contract_id=capability.contract_id,
                    contract_revision=capability.contract_revision,
                    objective="回显本轮输入",
                    input={"value": value},
                    acceptance_criteria="输出 echo 与输入一致",
                ),
            ),
        )

class _DeterministicAnswerProvider:
    """仅为真实 Root graph 的最终正文步骤提供确定性文本。"""

    async def complete(self, messages: list[Any], **kwargs: Any) -> str:
        return "Worker 两轮执行已完成。"

    async def invoke(self, messages: list[Any], **kwargs: Any) -> str:
        return "Worker 两轮执行已完成。"

    async def invoke_structured(self, messages: list[Any], schema: Any, **kwargs: Any) -> Any:
        raise AssertionError("the injected deterministic Root Agent owns structured decisions")

    async def stream(self, messages: list[Any], **kwargs: Any):
        yield LLMTextDelta("Worker 两轮执行已完成。")
        yield LLMStreamEnd("stop")

    def count_text_tokens(self, text: str) -> int:
        return max(1, len(text))


class _RecordingRootAgent(RootAgent):
    """记录真实 Root Agent 的边界输入与结构化动作，不改变其输出。"""

    def __init__(self, delegate: RootAgent) -> None:
        self._delegate = delegate
        self.decisions: list[dict[str, object]] = []
        self.failures: list[dict[str, object]] = []

    @staticmethod
    def _safe_exception_chain(error: BaseException) -> list[dict[str, object]]:
        chain: list[dict[str, object]] = []
        current: BaseException | None = error
        while current is not None:
            item: dict[str, object] = {
                "class": type(current).__name__,
                "module": type(current).__module__,
            }
            if isinstance(current, NameError):
                item["name"] = current.name
                frames = traceback.extract_tb(current.__traceback__)
                if frames:
                    frame = frames[-1]
                    item["frame"] = {
                        "file": frame.filename,
                        "function": frame.name,
                        "line": frame.lineno,
                    }
            for name in ("status_code", "status"):
                value = getattr(current, name, None)
                if isinstance(value, int) and not isinstance(value, bool):
                    item[name] = value
            if isinstance(current, ValidationError):
                item["errors"] = [
                    {
                        "loc": [str(part) for part in entry.get("loc", ())],
                        "type": entry.get("type"),
                    }
                    for entry in current.errors(
                        include_input=False, include_url=False
                    )
                ]
            chain.append(item)
            current = current.__cause__ or current.__context__
        return chain

    def _record_failure(self, method: str, error: BaseException) -> None:
        if isinstance(error, asyncio.CancelledError):
            raise error
        self.failures.append(
            {"method": method, "chain": self._safe_exception_chain(error)}
        )

    async def decide(self, inputs: RootAgentInput) -> RootDecision:
        try:
            decision = await self._delegate.decide(inputs)
        except BaseException as error:
            self._record_failure("decide", error)
            raise
        self.decisions.append(
            {
                "method": "decide",
                "capability_count": len(inputs.worker_capabilities),
                "action": decision.action,
                "kind": getattr(decision, "kind", None),
                "instructions_length": len(getattr(decision, "instructions", "")),
            }
        )
        return decision

    async def plan(self, inputs: RootPlanInput) -> RootPlanResult:
        try:
            result = await self._delegate.plan(inputs)
        except BaseException as error:
            self._record_failure("plan", error)
            if isinstance(error, asyncio.CancelledError):
                raise
            cause = error.__cause__
            validation_errors = (
                cause.errors(include_input=False, include_url=False)
                if isinstance(cause, ValidationError)
                else []
            )
            self.decisions.append(
                {
                    "method": "plan",
                    "capability_count": len(inputs.worker_capabilities),
                    "completed_plan_count": len(inputs.completed_plans),
                    "error_type": type(error).__name__,
                    "cause_type": type(cause).__name__ if cause is not None else None,
                    "cause_errors": [
                        {
                            "loc": [str(part) for part in item.get("loc", ())],
                            "type": item.get("type"),
                        }
                        for item in validation_errors
                    ],
                }
            )
            raise
        self.decisions.append(
            {
                "method": "plan",
                "capability_count": len(inputs.worker_capabilities),
                "completed_plan_count": len(inputs.completed_plans),
                "action": result.action,
            }
        )
        return result


class _RecordingLLMProvider:
    """透明记录结构化模型响应的类型和字段形状，不保留字段值。"""

    def __init__(self, delegate: ConfiguredLLMProvider) -> None:
        self._delegate = delegate
        self.structured_responses: list[dict[str, object]] = []
        self.failures: list[dict[str, object]] = []

    @staticmethod
    def _safe_exception_chain(error: BaseException) -> list[dict[str, object]]:
        return _RecordingRootAgent._safe_exception_chain(error)

    async def invoke_structured(self, messages: list[Any], schema: Any, **kwargs: Any) -> Any:
        try:
            response = await self._delegate.invoke_structured(messages, schema, **kwargs)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                raise
            self.failures.append(
                {
                    "method": "invoke_structured",
                    "schema": getattr(schema, "__name__", type(schema).__name__),
                    "chain": self._safe_exception_chain(error),
                }
            )
            raise
        if isinstance(response, Mapping):
            fields = {str(key): type(value).__name__ for key, value in response.items()}
        elif isinstance(response, BaseModel):
            fields = {
                str(key): type(value).__name__
                for key, value in response.model_dump(mode="python").items()
            }
        else:
            fields = {}
        self.structured_responses.append(
            {
                "schema": getattr(schema, "__name__", type(schema).__name__),
                "response_type": type(response).__name__,
                "fields": fields,
            }
        )
        return response

    async def invoke(self, messages: list[Any], **kwargs: Any) -> Any:
        return await self._delegate.invoke(messages, **kwargs)

    async def complete(self, messages: list[Any], **kwargs: Any) -> Any:
        return await self._delegate.complete(messages, **kwargs)

    async def stream(self, messages: list[Any], **kwargs: Any):
        async for item in self._delegate.stream(messages, **kwargs):
            yield item

    def count_text_tokens(self, text: str) -> int:
        return self._delegate.count_text_tokens(text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_fields"),
    [
        (
            PlanIntent(action="plan", goal="goal", instructions="plan"),
                {
                    "action": "str",
                    "goal": "str",
                    "instructions": "str",
                    "progress": "NoneType",
                },
        ),
        (
            {"action": "plan", "tasks": []},
            {"action": "str", "tasks": "list"},
        ),
    ],
    ids=("pydantic-model", "mapping"),
)
async def test_recording_llm_provider_accepts_model_and_mapping_responses(
    response: Any, expected_fields: dict[str, str]
) -> None:
    """验证透明记录器兼容 provider 的两种合法结构化响应类型。"""

    class _FakeProvider:
        async def invoke_structured(self, messages: list[Any], schema: Any, **kwargs: Any) -> Any:
            return response

        async def invoke(self, messages: list[Any], **kwargs: Any) -> str:
            return ""

        async def complete(self, messages: list[Any], **kwargs: Any) -> str:
            return ""

        async def stream(self, messages: list[Any], **kwargs: Any):
            if False:
                yield None

        def count_text_tokens(self, text: str) -> int:
            return 0

    recorder = _RecordingLLMProvider(_FakeProvider())  # type: ignore[arg-type]
    result = await recorder.invoke_structured([], PlanIntent)

    assert result is response
    assert recorder.structured_responses == [
        {
            "schema": "PlanIntent",
            "response_type": type(response).__name__,
            "fields": expected_fields,
        }
    ]
    assert recorder.failures == []


@pytest_asyncio.fixture
async def s7_worker_store():
    """只创建并清理随机临时 PostgreSQL 数据库。"""
    if not _DATABASE_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    database = f"athena_root_worker_s7_{uuid4().hex}"
    base_url = make_url(_DATABASE_URL)
    if base_url.drivername in {"postgres", "postgresql"}:
        base_url = base_url.set(drivername="postgresql+psycopg")
    admin_engine = create_async_engine(base_url)
    database_url = base_url.set(database=database)
    try:
        async with admin_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        yield None, database, database_url, database_url
    finally:
        async with admin_engine.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.exec_driver_sql(
                f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'
            )
        await admin_engine.dispose()


async def _worker_ready(client: httpx.AsyncClient, origin: str) -> bool:
    """探测独立 Worker 是否已启动且可接收任务。"""
    try:
        response = await client.get(
            f"{origin}/worker-api/v1/health",
            headers={
                "Authorization": f"Bearer {_ORCHESTRATOR_TOKEN}",
                "X-Athena-Protocol-Version": "1.0",
            },
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200 and response.json().get("status") == "ready"


async def _wait_for_file(path: Path, *, timeout: float = 30) -> None:
    """等待代理记录到实际送达 Worker 的命令内容。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"HTTPS Worker command was not observed: {path.name}")


async def _wait_until(predicate, *, timeout: float = 30.0) -> None:
    """等待真实 PostgreSQL/HTTP 状态达到验收条件。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("S7 Root/Worker condition was not reached before timeout")


async def _wait_until_run_condition(
    predicate,
    run_status,
    on_failure,
    *,
    timeout: float,
    label: str,
) -> None:
    """等待 Run 条件，并在失败/取消终态立即输出统一诊断。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await run_status()
        if status in {"failed", "cancelled"}:
            await on_failure(label)
            raise AssertionError(f"Run entered terminal state while waiting for {label}")
        if await predicate():
            return
        await asyncio.sleep(0.1)
    await on_failure(label)
    raise AssertionError(f"Run condition timed out while waiting for {label}")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
async def test_wait_until_run_condition_fails_fast_on_terminal_run(
    terminal_status: str,
) -> None:
    """Run 进入失败或取消终态时应立即诊断且不执行成功谓词。"""
    failure_labels: list[str] = []
    predicate_calls = 0
    started = time.monotonic()

    async def run_status() -> str:
        return terminal_status

    async def on_failure(label: str) -> None:
        failure_labels.append(label)

    async def predicate() -> bool:
        nonlocal predicate_calls
        predicate_calls += 1
        return True

    with pytest.raises(AssertionError, match="terminal state"):
        await _wait_until_run_condition(
            predicate,
            run_status,
            on_failure,
            timeout=30,
            label=f"terminal_{terminal_status}",
        )
    assert failure_labels == [f"terminal_{terminal_status}"]
    assert predicate_calls == 0
    assert time.monotonic() - started < 1


def _settings_for_s7(
    base_url,
    *,
    center_origin: str,
    worker_origin: str,
    ca_file: Path,
    storage_root: Path,
) -> Settings:
    """为隔离测试构造生产 PostgreSQL/HTTPS Worker 配置。"""
    return Settings().model_copy(
        update={
            "postgres_host": base_url.host or "127.0.0.1",
            "postgres_port": base_url.port or 8190,
            "postgres_user": base_url.username or "",
            "postgres_password": base_url.password or "",
            "postgres_db": base_url.database or "",
            "postgres_create_schema": True,
            "postgres_pool_size": 8,
            "postgres_max_overflow": 4,
            "workers_enabled": True,
            "worker_protocol_service_tokens": {
                _SERVICE_PRINCIPAL: SecretStr(_SERVICE_TOKEN)
            },
            "worker_orchestrator_token": SecretStr(_ORCHESTRATOR_TOKEN),
            "worker_allowed_origins": (worker_origin,),
            "worker_allowed_networks": ("127.0.0.0/8",),
            "worker_protocol_ca_file": str(ca_file),
            "worker_execution_grant_secret": SecretStr(_GRANT_SECRET),
            "worker_resource_storage_path": str(storage_root),
            "worker_heartbeat_interval_seconds": 1,
            "worker_heartbeat_request_timeout_seconds": 1,
            "worker_dispatch_http_timeout_seconds": 2,
            "worker_dispatch_retry_initial_seconds": 0.1,
            "worker_dispatch_retry_max_seconds": 1,
            "worker_dispatch_retry_jitter_ratio": 0,
            "worker_job_poll_interval_seconds": 0.05,
            "worker_timeout_scan_interval_seconds": 0.1,
            "root_execution_lease_seconds": 30,
            "worker_job_lease_seconds": 30,
            "sandbox_enabled": False,
        }
    )


async def _run_root_worker_rounds(
    *,
    store,
    tmp_path: Path,
    root_agent: RootAgent | None,
    deterministic_answer: bool,
    user_message: str,
) -> dict[str, Any]:
    """运行完整 Root graph、持久队列、HTTPS Worker 与结果恢复流程。"""
    _, database, database_url, base_url = store
    ca_file, certificate, private_key = _create_test_certificates(tmp_path)
    center_port, worker_port = _free_port(), _free_port()
    center_origin = f"https://127.0.0.1:{center_port}"
    worker_port_origin = f"https://127.0.0.1:{worker_port}"
    proxy_port = _free_port()
    worker_origin = f"https://127.0.0.1:{proxy_port}"
    capture_dir = tmp_path / "captured-worker-tasks"
    worker_data_dir = tmp_path / "worker-data"
    capability_file = tmp_path / "capability.json"
    capability_file.write_text(json.dumps(_CAPABILITY), encoding="utf-8")

    dependencies = None
    observed_root_agent: _RecordingRootAgent | None = None
    observed_llm_provider: _RecordingLLMProvider | None = None
    worker = None
    proxy = multiprocessing.get_context("spawn").Process(
        target=__import__(
            "tests.integration.root_worker_rounds_https_s7_server",
            fromlist=["run_capture_proxy"],
        ).run_capture_proxy,
        kwargs={
            "upstream_origin": worker_port_origin,
            "ca_file": str(ca_file),
            "capture_dir": str(capture_dir),
            "certificate": str(certificate),
            "private_key": str(private_key),
            "host": "127.0.0.1",
            "port": proxy_port,
        },
    )
    server: uvicorn.Server | None = None
    server_task: asyncio.Task[bool] | None = None
    background: list[Any] = []
    resources_started: list[Any] = []
    try:
        proxy.start()
        settings = _settings_for_s7(
            base_url,
            center_origin=center_origin,
            worker_origin=worker_origin,
            ca_file=ca_file,
            storage_root=tmp_path / "worker-resources",
        )
        llm_provider = _DeterministicAnswerProvider() if deterministic_answer else None
        effective_root_agent = root_agent
        if root_agent is None:
            configured_llm = ConfiguredLLMProvider(coerce_llm_options(settings))
            observed_llm_provider = _RecordingLLMProvider(configured_llm)
            llm_provider = observed_llm_provider
            configured_agent = ConfiguredRootAgent(llm_provider)
            observed_root_agent = _RecordingRootAgent(configured_agent)
            effective_root_agent = observed_root_agent
        dependencies = build_postgres_dependencies(
            settings,
            root_agent=effective_root_agent,
            llm_provider=llm_provider,
        )
        engine = dependencies.resources[0]
        checkpointer = dependencies.resources[1]
        await engine.start()
        resources_started.append(engine)
        async with engine.engine.connect() as connection:
            current_database = await connection.scalar(text("SELECT current_database()"))
            current_schema = await connection.scalar(text("SELECT current_schema()"))
            assert current_database == database
            assert current_schema == "public"
            missing_tables = await connection.execute(
                text(
                    "SELECT required.table_name FROM unnest(CAST(:tables AS text[])) AS required(table_name) "
                    "LEFT JOIN pg_tables actual ON actual.schemaname = 'public' "
                    "AND actual.tablename = required.table_name "
                    "WHERE actual.tablename IS NULL"
                ),
                {
                    "tables": [
                        "agent_runs",
                        "agent_plans",
                        "agent_tasks",
                        "task_executions",
                        "orchestrator_root_resume_jobs",
                    ],
                },
            )
            assert not missing_tables.all()
        await checkpointer.start()
        resources_started.append(checkpointer)
        # Worker 注册后的 ready 状态由生产健康监督器通过心跳刷新；先启动它再等待注册结果。
        health_supervisor = dependencies.resources[-2]
        await health_supervisor.start()
        resources_started.append(health_supervisor)

        app = FastAPI()
        for router in dependencies.routers:
            app.include_router(router)
        install_worker_protocol_handlers(app)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=center_port,
                ssl_certfile=str(certificate),
                ssl_keyfile=str(private_key),
                log_level="warning",
                access_log=False,
            )
        )
        server_task = asyncio.create_task(server.serve())
        background.append(server_task)
        tls_context = ssl.create_default_context(cafile=str(ca_file))
        async with httpx.AsyncClient(verify=tls_context, timeout=5) as client:
            await _wait_until(
                lambda: _center_ready(client, center_origin), timeout=20
            )
            worker = _worker_process(
                base_url=center_origin,
                public_url=worker_origin,
                data_dir=worker_data_dir,
                capability_file=capability_file,
                certificate=certificate,
                private_key=private_key,
                ca_file=ca_file,
                port=worker_port,
            )
            await _wait_until(lambda: _worker_ready(client, worker_origin), timeout=30)

            sessions = engine.session_factory
            async def registered_and_ready() -> bool:
                async with sessions() as session:
                    instance = await session.scalar(select(WorkerInstanceModel))
                    return instance is not None and instance.status == "ready"

            await _wait_until(registered_and_ready, timeout=30)

            project_service = dependencies.service("project_service")
            session_service = dependencies.service("session_service")
            run_service = dependencies.service("run_service")
            project = await project_service.create("S7 isolated integration")
            session = await session_service.create(
                project_id=project.id,
                creation_request_id=str(uuid4()),
                title="S7 Root Worker rounds",
            )
            submitted = await run_service.submit(
                session.id,
                request_id=str(uuid4()),
                message=user_message,
            )
            run_id = submitted.run_id

            retry = settings.llm_retry
            llm_call_budget = (
                retry.max_attempts * retry.timeout_ms / 1000
                + max(0, retry.max_attempts - 1) * retry.min_delay_ms / 1000
            )
            root_phase_timeout = llm_call_budget * 3 + 30
            final_answer_timeout = (
                llm_call_budget
                + settings.answer_streaming.total_timeout_ms / 1000
                + 30
            )

            async def fail_with_snapshot(wait_label: str) -> None:
                async with sessions() as snapshot_session:
                    run = await snapshot_session.get(AgentRunModel, run_id)
                    plans = list(
                        (
                            await snapshot_session.scalars(
                                select(OrchestratorPlanModel)
                                .where(OrchestratorPlanModel.run_id == run_id)
                                .order_by(OrchestratorPlanModel.sequence)
                            )
                        ).all()
                    )
                    tasks = list(
                        (
                            await snapshot_session.scalars(
                                select(OrchestratorTaskModel)
                                .join(
                                    OrchestratorPlanModel,
                                    OrchestratorTaskModel.plan_id
                                    == OrchestratorPlanModel.plan_id,
                                )
                                .where(OrchestratorPlanModel.run_id == run_id)
                                .order_by(OrchestratorPlanModel.sequence)
                            )
                        ).all()
                    )
                    executions = list(
                        (
                            await snapshot_session.scalars(
                                select(OrchestratorExecutionModel)
                                .join(
                                    OrchestratorTaskModel,
                                    OrchestratorExecutionModel.task_id
                                    == OrchestratorTaskModel.task_id,
                                )
                                .join(
                                    OrchestratorPlanModel,
                                    OrchestratorTaskModel.plan_id
                                    == OrchestratorPlanModel.plan_id,
                                )
                                .where(OrchestratorPlanModel.run_id == run_id)
                            )
                        ).all()
                    )
                    commands = list(
                        (
                            await snapshot_session.scalars(
                                select(OrchestratorWorkerCommandModel)
                                .join(
                                    OrchestratorExecutionModel,
                                    OrchestratorWorkerCommandModel.execution_id
                                    == OrchestratorExecutionModel.execution_id,
                                )
                                .join(
                                    OrchestratorTaskModel,
                                    OrchestratorExecutionModel.task_id
                                    == OrchestratorTaskModel.task_id,
                                )
                                .join(
                                    OrchestratorPlanModel,
                                    OrchestratorTaskModel.plan_id
                                    == OrchestratorPlanModel.plan_id,
                                )
                                .where(OrchestratorPlanModel.run_id == run_id)
                            )
                        ).all()
                    )
                    receipts = list(
                        (
                            await snapshot_session.scalars(
                                select(WorkerResultReceiptModel)
                                .join(
                                    OrchestratorExecutionModel,
                                    WorkerResultReceiptModel.execution_id
                                    == OrchestratorExecutionModel.execution_id,
                                )
                                .join(
                                    OrchestratorTaskModel,
                                    OrchestratorExecutionModel.task_id
                                    == OrchestratorTaskModel.task_id,
                                )
                                .join(
                                    OrchestratorPlanModel,
                                    OrchestratorTaskModel.plan_id
                                    == OrchestratorPlanModel.plan_id,
                                )
                                .where(OrchestratorPlanModel.run_id == run_id)
                            )
                        ).all()
                    )
                    jobs = list(
                        (
                            await snapshot_session.scalars(
                                select(OrchestratorRootResumeJobModel)
                                .where(OrchestratorRootResumeJobModel.run_id == run_id)
                                .order_by(
                                    OrchestratorRootResumeJobModel.root_wait_generation
                                )
                            )
                        ).all()
                    )
                worker_db = worker_data_dir / "worker.sqlite3"
                outbox_snapshot: list[dict[str, object]] = []
                if worker_db.is_file():
                    with sqlite3.connect(worker_db) as connection:
                        outbox_snapshot = [
                            {
                                "kind": row[0],
                                "state": row[1],
                                "attempt": row[2],
                                "last_error_code": (
                                    str(row[3]).split("_", 1)[0]
                                    if row[3] is not None
                                    else None
                                ),
                            }
                            for row in connection.execute(
                                "SELECT kind, state, attempt, last_error FROM outbox ORDER BY outbox_id"
                            )
                        ]
                events_path = capture_dir / "result-http-events.jsonl"
                snapshot = {
                    "wait": wait_label,
                    "run": run.status if run else None,
                    "run_error": (
                        {
                            key: value
                            for key, value in run.error_json.items()
                            if key in {"code", "node"}
                            and isinstance(value, str)
                        }
                        if run is not None and isinstance(run.error_json, dict)
                        else None
                    ),
                    "plans": [
                        {"sequence": item.sequence, "status": item.status}
                        for item in plans
                    ],
                    "tasks": [
                        {
                            "status": item.status,
                            "active_execution_id": item.active_execution_id,
                            "execution_generation": item.execution_generation,
                            "cancellation_epoch": item.cancellation_epoch,
                        }
                        for item in tasks
                    ],
                    "executions": [
                        {
                            "status": item.status,
                            "instance_id": item.instance_id,
                            "generation": item.generation,
                            "cancellation_epoch": item.cancellation_epoch,
                            "capacity_reserved": item.capacity_reserved,
                        }
                        for item in executions
                    ],
                    "commands": [
                        {
                            "command_type": item.command_type,
                            "status": item.status,
                            "instance_id": item.instance_id,
                            "attempt": item.attempt,
                            "send_attempt_count": item.send_attempt_count,
                            "delivery_uncertain_before_send": (
                                item.delivery_uncertain_before_send
                            ),
                            "lease_owner_present": item.lease_owner is not None,
                            "lease_until": (
                                item.lease_until.isoformat()
                                if item.lease_until is not None
                                else None
                            ),
                            "available_at": item.available_at.isoformat(),
                            "receipt_present": item.receipt_json is not None,
                            "created_at": item.created_at.isoformat(),
                            "updated_at": item.updated_at.isoformat(),
                        }
                        for item in commands
                    ],
                    "result_receipts": [item.disposition for item in receipts],
                    "jobs": [
                        {
                            "generation": item.root_wait_generation,
                            "status": item.status,
                        }
                        for item in jobs
                    ],
                    "captured_commands": len(list(capture_dir.glob("task-*.json"))),
                    "result_http_events": [
                        json.loads(line)
                        for line in events_path.read_text(encoding="utf-8").splitlines()
                    ]
                    if events_path.exists()
                    else [],
                    "worker_outbox": outbox_snapshot,
                    "recorded_root_calls": (
                        observed_root_agent.decisions
                        if observed_root_agent is not None
                        else []
                    ),
                    "root_agent_failures": (
                        observed_root_agent.failures
                        if observed_root_agent is not None
                        else []
                    ),
                    "structured_responses": (
                        observed_llm_provider.structured_responses
                        if observed_llm_provider is not None
                        else []
                    ),
                    "llm_failures": (
                        observed_llm_provider.failures
                        if observed_llm_provider is not None
                        else []
                    ),
                }
                raise AssertionError(
                    "S7 wait failed; safe status snapshot: "
                    + json.dumps(snapshot, sort_keys=True)
                )

            async def current_run_status() -> str | None:
                async with sessions() as status_session:
                    current = await status_session.get(AgentRunModel, run_id)
                    superseded_execute = await status_session.scalar(
                        select(OrchestratorWorkerCommandModel.command_id)
                        .join(
                            OrchestratorExecutionModel,
                            OrchestratorWorkerCommandModel.execution_id
                            == OrchestratorExecutionModel.execution_id,
                        )
                        .join(
                            OrchestratorTaskModel,
                            OrchestratorExecutionModel.task_id
                            == OrchestratorTaskModel.task_id,
                        )
                        .join(
                            OrchestratorPlanModel,
                            OrchestratorTaskModel.plan_id
                            == OrchestratorPlanModel.plan_id,
                        )
                        .where(
                            OrchestratorPlanModel.run_id == run_id,
                            OrchestratorWorkerCommandModel.command_type == "execute",
                            OrchestratorWorkerCommandModel.status == "superseded",
                        )
                        .limit(1)
                    )
                    if (
                        current is not None
                        and current.status == "running"
                        and superseded_execute is not None
                    ):
                        # execute command 不可能在父级仍有效时永久 supersede；
                        # 立即进入统一安全快照，避免等待真实 LLM 预算耗尽。
                        return "failed"
                    return current.status if current is not None else None

            async def wait_for_run_condition(
                predicate, *, timeout: float, label: str
            ) -> None:
                await _wait_until_run_condition(
                    predicate,
                    current_run_status,
                    fail_with_snapshot,
                    timeout=timeout,
                    label=label,
                )

            task_dispatcher = dependencies.service("worker_task_dispatcher")
            run_consumer = dependencies.service("run_consumer")
            root_resume_dispatcher = dependencies.service("root_resume_dispatcher")
            await task_dispatcher.start()
            background.append(task_dispatcher)
            await run_consumer.start()
            background.append(run_consumer)

            async def first_plan_waiting_for_resume() -> bool:
                async with sessions() as session:
                    plan = await session.scalar(
                        select(OrchestratorPlanModel).where(
                            OrchestratorPlanModel.run_id == run_id,
                            OrchestratorPlanModel.sequence == 1,
                        )
                    )
                    job = await session.scalar(
                        select(OrchestratorRootResumeJobModel).where(
                            OrchestratorRootResumeJobModel.run_id == run_id,
                            OrchestratorRootResumeJobModel.plan_id
                            == (plan.plan_id if plan is not None else ""),
                        )
                    )
                    return (
                        plan is not None
                        and plan.status == "completed"
                        and job is not None
                        and job.status == "pending"
                    )

            await wait_for_run_condition(
                first_plan_waiting_for_resume,
                timeout=root_phase_timeout,
                label="first_plan_waiting_for_resume",
            )
            await root_resume_dispatcher.start()
            background.append(root_resume_dispatcher)

            async def second_plan_created() -> bool:
                async with sessions() as session:
                    return (
                        await session.scalar(
                            select(func.count()).select_from(OrchestratorPlanModel).where(
                                OrchestratorPlanModel.run_id == run_id,
                                OrchestratorPlanModel.sequence == 2,
                            )
                        )
                    ) == 1

            await wait_for_run_condition(
                second_plan_created,
                timeout=root_phase_timeout,
                label="second_plan_created",
            )

            async def run_completed() -> bool:
                async with sessions() as session:
                    run = await session.get(AgentRunModel, run_id)
                    return run is not None and run.status == "completed"

            await wait_for_run_condition(
                run_completed,
                timeout=final_answer_timeout,
                label="run_completed",
            )
            await root_resume_dispatcher.stop()
            background.remove(root_resume_dispatcher)

            await wait_for_run_condition(
                lambda: _at_least_two_captures(capture_dir),
                timeout=20,
                label="two_task_captures",
            )
            captures = sorted(capture_dir.glob("task-*.json"))
            captured_tasks = [json.loads(path.read_text(encoding="utf-8")) for path in captures]
            ordered_commands = [item["body"] for item in captured_tasks]
            assert len(ordered_commands) == 2
            assert all(
                item["headers"]["idempotency_key"] == command["command_id"]
                for item, command in zip(captured_tasks, ordered_commands, strict=True)
            )

            async with sessions() as session:
                plans = list(
                    (
                        await session.scalars(
                            select(OrchestratorPlanModel)
                            .where(OrchestratorPlanModel.run_id == run_id)
                            .order_by(OrchestratorPlanModel.sequence)
                        )
                    ).all()
                )
                tasks = list(
                    (
                        await session.scalars(
                            select(OrchestratorTaskModel)
                            .join(
                                OrchestratorPlanModel,
                                OrchestratorTaskModel.plan_id == OrchestratorPlanModel.plan_id,
                            )
                            .where(OrchestratorPlanModel.run_id == run_id)
                            .order_by(OrchestratorPlanModel.sequence)
                        )
                    ).all()
                )
                executions = list(
                    (
                        await session.scalars(
                            select(OrchestratorExecutionModel)
                            .join(
                                OrchestratorTaskModel,
                                OrchestratorExecutionModel.task_id == OrchestratorTaskModel.task_id,
                            )
                            .join(
                                OrchestratorPlanModel,
                                OrchestratorTaskModel.plan_id == OrchestratorPlanModel.plan_id,
                            )
                            .where(OrchestratorPlanModel.run_id == run_id)
                        )
                    ).all()
                )
                jobs = list(
                    (
                        await session.scalars(
                            select(OrchestratorRootResumeJobModel)
                            .where(OrchestratorRootResumeJobModel.run_id == run_id)
                            .order_by(OrchestratorRootResumeJobModel.root_wait_generation)
                        )
                    ).all()
                )
                run = await session.get(AgentRunModel, run_id)

            assert [plan.sequence for plan in plans] == [1, 2]
            assert all(plan.status == "completed" for plan in plans)
            assert len(tasks) == 2 and all(task.status == "completed" for task in tasks)
            assert len(executions) == 2 and all(item.status == "completed" for item in executions)
            assert len(jobs) == 2 and len({item.plan_id for item in jobs}) == 2
            assert all(item.status == "completed" for item in jobs)
            assert run is not None and run.status == "completed"

            root_graph = dependencies.service("root_graph")
            root_snapshot = await root_graph._compiled_graph().aget_state(
                {"configurable": {"thread_id": f"run:{run_id}"}}
            )
            answer_input = root_snapshot.values.get("answer_input")
            answer_kind = (
                answer_input.get("kind")
                if isinstance(answer_input, dict)
                else None
            )
            assert answer_kind == "final"

            first_command, second_command = ordered_commands
            assert first_command["plan_id"] == plans[0].plan_id
            assert second_command["plan_id"] == plans[1].plan_id
            assert first_command["plan_id"] != second_command["plan_id"]
            assert tasks[0].plan_id == plans[0].plan_id
            assert tasks[1].plan_id == plans[1].plan_id
            first_output = tasks[0].output_json
            assert first_output is not None
            assert isinstance(first_command["input"].get("value"), str)
            assert isinstance(second_command["input"].get("value"), str)
            first_value = first_command["input"]["value"]
            assert first_value in json.dumps(first_output, ensure_ascii=False)
            second_input_value = json.loads(second_command["input"]["value"])
            assert second_input_value == first_output
            assert first_value in second_command["input"]["value"]
            assert run.response_json is not None
            return {
                "run_id": run_id,
                "plans": [plan.plan_id for plan in plans],
                "worker_commands": [value["command_id"] for value in ordered_commands],
                "first_output": first_output,
                "second_input": second_command["input"],
                "recorded_root_calls": (
                    observed_root_agent.decisions
                    if observed_root_agent is not None
                    else []
                ),
                "response_json": run.response_json,
                "answer_kind": answer_kind,
                "root_agent_failures": (
                    observed_root_agent.failures
                    if observed_root_agent is not None
                    else []
                ),
                "structured_responses": (
                    observed_llm_provider.structured_responses
                    if observed_llm_provider is not None
                    else []
                ),
                "llm_failures": (
                    observed_llm_provider.failures
                    if observed_llm_provider is not None
                    else []
                ),
            }
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None and not server_task.done():
            try:
                await asyncio.wait_for(server_task, timeout=5)
            except asyncio.TimeoutError:
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)
            if server_task in background:
                background.remove(server_task)
        for resource in reversed(background):
            try:
                if isinstance(resource, asyncio.Task):
                    resource.cancel()
                    await asyncio.gather(resource, return_exceptions=True)
                else:
                    await resource.stop()
            except Exception:
                pass
        for resource in reversed(resources_started):
            try:
                await resource.stop()
            except Exception:
                pass
        if worker is not None:
            _terminate(worker)
        if proxy.is_alive():
            proxy.terminate()
            proxy.join(timeout=5)


async def _center_ready(client: httpx.AsyncClient, origin: str) -> bool:
    try:
        response = await client.get(f"{origin}/openapi.json")
    except httpx.HTTPError:
        return False
    return response.status_code == 200


async def _at_least_two_captures(capture_dir: Path) -> bool:
    return len(list(capture_dir.glob("task-*.json"))) >= 2


def _start_worker(
    *,
    center_origin: str,
    worker_origin: str,
    data_dir: Path,
    capability_file: Path,
    certificate: Path,
    private_key: Path,
    ca_file: Path,
    port: int,
) -> subprocess.Popen[str]:
    """启动持久化参考 Worker 子进程并注册固定的逐轮能力。"""
    capability_file.write_text(json.dumps(_CAPABILITY), encoding="utf-8")
    return _worker_process(
        base_url=center_origin,
        public_url=worker_origin,
        data_dir=data_dir,
        capability_file=capability_file,
        certificate=certificate,
        private_key=private_key,
        ca_file=ca_file,
        port=port,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_real_root_creates_second_plan_from_first_worker_result(
    s7_worker_store, tmp_path: Path
) -> None:
    """生产 Root 图经持久暂停/恢复，让第一轮 Worker 结果影响第二轮输入。"""
    _, schema, _, _ = s7_worker_store
    agent = _DeterministicRoundAgent()
    result = await _run_root_worker_rounds(
        store=s7_worker_store,
        tmp_path=tmp_path,
        root_agent=agent,
        deterministic_answer=True,
        user_message="请使用已注册的 Worker 逐轮执行，并在第二轮输入中使用第一轮实际返回的完整结果。",
    )
    assert len(agent.plan_inputs) == 2
    assert len(agent.plan_inputs[0].worker_capabilities) == 1
    assert result["answer_kind"] == "final"
    assert agent.plan_inputs[1].completed_plans[0].tasks[0].output == result["first_output"]
    assert json.loads(result["second_input"]["value"]) == result["first_output"]
    recorded_json = json.dumps(result["recorded_root_calls"], ensure_ascii=False)
    assert all("instructions" not in call for call in result["recorded_root_calls"])
    assert all(
        free_text not in recorded_json
        for free_text in (
            "执行一轮 Worker 任务",
            "使用上一轮真实输出",
            "返回 Worker 两轮结果",
        )
    )


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.getenv(_LLM_OPT_IN) != "1",
    reason=f"set {_LLM_OPT_IN}=1 to enable the provider-backed S7 acceptance",
)
async def test_real_provider_drives_two_root_worker_rounds(s7_worker_store, tmp_path: Path) -> None:
    """可选真实 Provider 验收；由实际配置的 LLM 选择并使用 Worker 能力。"""
    result = await _run_root_worker_rounds(
        store=s7_worker_store,
        tmp_path=tmp_path,
        root_agent=None,
        deterministic_answer=False,
        user_message=(
            "Use the registered echo Worker for two independent rounds. In the first round, "
            "send a unique string beginning with s7-live-marker-. After the first result is "
            "completed, create the second plan and set its value to the exact JSON string "
            "of that first Worker output. Do not precompute the second input. Then summarize both results."
        ),
    )
    assert result["answer_kind"] == "final"
    assert "s7-live-marker-" in result["second_input"]["value"]


__all__ = ["_ORCHESTRATOR_TOKEN", "_SERVICE_TOKEN", "_CAPABILITY", "_create_test_certificates", "_free_port", "_terminate", "_wait_until", "_worker_process"]
