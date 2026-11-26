"""Skills/文件在真实 PostgreSQL、MinIO 和子进程上的完整闭环。"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sys
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex
from typing import Any, cast

import httpx2
import pytest
from fastapi import FastAPI
from integration.llm_fixtures import TEST_LLM_CIPHER, encrypted_llm_key_fields
from integration.postgres_harness import isolated_postgres_schema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.access.repository import SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.file import (
    MinioFileObjectStore,
    MinioFileStorageConfig,
    SqlAlchemySessionFileRepository,
)
from adapter.file.storage import FileStorageError
from adapter.llm.model.profile import DefaultModelProfileModel, ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.message.repository import SqlAlchemyMessageRepository
from adapter.run.model import RunToolCallModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session import SqlAlchemySessionRepository
from adapter.session.model import SessionModel
from adapter.skill import SqlAlchemySkillRepository
from adapter.skill.execution import PythonScriptExecutor
from adapter.skill.model import SkillExecutionModel, SkillVersionModel
from adapter.system_prompt.repository import SqlAlchemySystemPromptRepository
from api.file import build_session_file_router
from api.messages import build_message_router
from api.runs import build_run_router
from api.skill import build_skill_admin_router, build_skill_user_router
from app.access import AccessPolicyService
from app.auth import AuthService
from app.file import FileObjectMaintenance, SessionFileService
from app.message import MessageService
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.run.service import RunService
from app.run.stream import RunStreamService
from app.skill import SkillService
from app.skill.execution_service import SkillExecutionService
from app.tool.runtime import RunToolRuntimeService
from domain.file import FileObjectScanEntry, FileObjectStore
from domain.llm.model import (
    LlmRequest,
    LlmResponse,
    LlmStreamEvent,
    LlmText,
    LlmToolCall,
    LlmUsage,
    ResponseCompleted,
)
from domain.llm.model.profile import RuntimeModelProfile
from domain.mcp import (
    McpCallResult,
    McpRuntimeToolTarget,
)
from domain.tool import RuntimeToolDefinition
from infrastructure.security import Argon2PasswordHasher

DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
_TEST_MINIO_ENV = (
    "ATHENA_TEST_MINIO_ENDPOINT",
    "ATHENA_TEST_MINIO_ACCESS_KEY",
    "ATHENA_TEST_MINIO_SECRET_KEY",
    "ATHENA_TEST_MINIO_BUCKET",
)
MINIO_CONFIGURED = all(os.getenv(name) for name in _TEST_MINIO_ENV)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL or not MINIO_CONFIGURED,
        reason=(
            "ATHENA_TEST_DATABASE_URL and isolated ATHENA_TEST_MINIO_* configuration are required"
        ),
    ),
]


@asynccontextmanager
async def _database() -> AsyncGenerator[
    tuple[async_sessionmaker[AsyncSession], object, object, str],
    None,
]:
    """创建独立 schema 并运行完整迁移链，退出时只删除该 schema。"""
    database_url = DATABASE_URL
    if database_url is None:
        pytest.skip("ATHENA_TEST_DATABASE_URL is not configured")
    async with isolated_postgres_schema(database_url, prefix="athena_v2_skills_e2e") as db:
        yield db.session_factory, db.engine, db.admin_engine, db.schema


class _RecordingStore:
    """记录本测试写入的真实 MinIO key，确保清理不会触碰其他对象。"""

    def __init__(self, delegate: FileObjectStore) -> None:
        self.delegate = delegate
        self.created_keys: set[str] = set()
        self.deleted_keys: set[str] = set()

    async def start(self) -> None:
        await self.delegate.start()

    async def put_file(
        self,
        key: str,
        path: Path,
        *,
        content_type: str,
    ) -> None:
        self.created_keys.add(key)
        await self.delegate.put_file(key, path, content_type=content_type)

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        self.created_keys.add(key)
        await self.delegate.put_bytes(key, content, content_type=content_type)

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        return await self.delegate.get_bytes(key, maximum_bytes=maximum_bytes)

    async def get_range(self, key: str, *, offset: int, length: int) -> bytes:
        return await self.delegate.get_range(key, offset=offset, length=length)

    async def iter_range(
        self,
        key: str,
        *,
        offset: int = 0,
        chunk_size: int = 64 * 1024,
    ) -> AsyncIterator[bytes]:
        async for chunk in self.delegate.iter_range(
            key,
            offset=offset,
            chunk_size=chunk_size,
        ):
            yield chunk

    async def delete(self, key: str) -> None:
        if key not in self.created_keys:
            raise AssertionError("attempted to delete a shared bucket object")
        self.deleted_keys.add(key)
        await self.delegate.delete(key)

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        remaining_items = maximum_items
        next_after_key = after_key
        while remaining_items > 0:
            requested_items = remaining_items
            examined_items = 0
            last_object_key: str | None = None
            async for entry in self.delegate.iter_maintenance_candidates(
                older_than_seconds=older_than_seconds,
                after_key=next_after_key,
                maximum_items=requested_items,
            ):
                examined_items += 1
                last_object_key = entry.object_key
                if entry.object_key not in self.created_keys:
                    continue
                yield entry
                remaining_items -= 1
                if remaining_items == 0:
                    return
            # 共享桶的底层页可能几乎全是他人对象；必须继续填满本测试的
            # 逻辑页，或持续到底层耗尽，不能把过滤后的短页误判为扫描结束。
            if examined_items < requested_items:
                return
            if last_object_key is None:
                return
            next_after_key = last_object_key

    async def aclose(self) -> None:
        await self.delegate.aclose()

    async def cleanup(self) -> None:
        """只删除本测试创建的对象。"""
        for key in sorted(self.created_keys):
            try:
                await self.delegate.delete(key)
            except FileStorageError:
                pass


class _PagedFakeFileObjectStore:
    """只实现维护分页协议的内存存储，用于验证共享桶过滤边界。"""

    def __init__(self, object_keys: tuple[str, ...]) -> None:
        self._object_keys = tuple(sorted(object_keys))
        self.calls: list[tuple[str | None, int]] = []

    async def iter_maintenance_candidates(
        self,
        *,
        older_than_seconds: int = 86400,
        after_key: str | None = None,
        maximum_items: int = 1000,
    ) -> AsyncIterator[FileObjectScanEntry]:
        del older_than_seconds
        self.calls.append((after_key, maximum_items))
        examined_items = 0
        for object_key in self._object_keys:
            if after_key is not None and object_key <= after_key:
                continue
            if examined_items >= maximum_items:
                return
            examined_items += 1
            yield FileObjectScanEntry(object_key=object_key, is_candidate=True)


def test_recording_store_fills_logical_page_across_shared_bucket_pages() -> None:
    """过滤 1000+ 非本测试对象后，不得把短物理页误判为扫描结束。"""
    first_created_key = "session-files/aa/created-before"
    last_created_key = "session-files/aa/zz-created-after"
    shared_keys = tuple(f"session-files/aa/shared-{index:04d}" for index in range(1005))
    delegate = _PagedFakeFileObjectStore((first_created_key, *shared_keys, last_created_key))
    store = _RecordingStore(cast(FileObjectStore, delegate))
    store.created_keys.update({first_created_key, last_created_key})

    async def scenario() -> list[str]:
        keys: list[str] = []
        async for entry in store.iter_maintenance_candidates(maximum_items=10):
            keys.append(entry.object_key)
        return keys

    assert asyncio.run(scenario()) == [first_created_key, last_created_key]
    assert len(delegate.calls) > 10
    assert delegate.calls[0] == (None, 10)


class _EmptyMcpCatalog:
    async def list_readable_runtime_tools(
        self,
        snapshot: Any,
        *,
        limit: int = 200,
    ) -> tuple[RuntimeToolDefinition, ...]:
        del snapshot, limit
        return ()

    async def get_call_target(
        self,
        tool_id: str,
        server_id: str,
        remote_name: str,
        config_version: int,
    ) -> McpRuntimeToolTarget | None:
        del tool_id, server_id, remote_name, config_version
        return None


class _UnusedMcpExecutor:
    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, Any],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del target, arguments, deadline_seconds
        raise AssertionError("Skills 闭环不应调用 MCP")


class _ScriptedProvider:
    """按预设顺序返回工具调用和最终回答的可控 LLM。"""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(
        self,
        runtime: RuntimeModelProfile,
        request: LlmRequest,
    ) -> AsyncGenerator[AsyncIterator[LlmStreamEvent], None]:
        del runtime
        self.requests.append(request)
        response = self._responses.pop(0)

        async def events() -> AsyncIterator[LlmStreamEvent]:
            yield ResponseCompleted(response)

        yield events()

    async def complete(self, runtime: RuntimeModelProfile, request: LlmRequest) -> LlmResponse:
        del runtime, request
        raise AssertionError("complete is not expected in this test")


def _response(
    *,
    call: tuple[str, str, dict[str, object]] | None = None,
    text: str = "",
    attempt: int,
) -> LlmResponse:
    output: list[LlmText | LlmToolCall] = []
    if text:
        output.append(LlmText(text))
    if call is not None:
        output.append(LlmToolCall(*call))
    return LlmResponse(
        output=tuple(output),
        status="completed",
        incomplete_reason=None,
        model="skills-e2e-model",
        response_id=f"skills-e2e-response-{attempt}",
        request_id=None,
        usage=LlmUsage(input_tokens=10, output_tokens=2, total_tokens=12),
    )


def _skill_zip(*, guide: str) -> bytes:
    """构造包含登记脚本的合法管理员包。"""
    metadata = """
name: 月度报告
slug: monthly-report
description: 根据会话 CSV 生成月度报表和趋势图。
dependencies: []
scripts:
  - path: scripts/build_report.py
    label: 生成月度报表
    purpose: 读取当前会话 CSV，生成 CSV 报表和趋势图。
    parameters:
      type: object
      additionalProperties: false
      properties:
        period:
          type: string
          title: 报表月份
          enum: ["2026-10"]
        api_token:
          type: string
      required: [period, api_token]
""".strip()
    script = r"""
import json
import os
from pathlib import Path

with open(os.environ["ATHENA_INPUT_PATH"], encoding="utf-8") as source:
    request = json.load(source)
inputs = list(request["input_files"].values())
if len(inputs) != 1:
    raise SystemExit("one input file is required")
source_text = Path(inputs[0]).read_text(encoding="utf-8")
if "2026-10" not in source_text:
    raise SystemExit("period marker missing")
output = Path(request["output_dir"])
output.mkdir(parents=True, exist_ok=True)
(output / "report.csv").write_text(
    "month,total\n2026-10,42\n",
    encoding="utf-8",
)
(output / "trend.png").write_bytes(
    __import__("base64").b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0YQAAAAASUVORK5CYII="
    )
)
(output / "manifest.json").write_text(
    json.dumps(
        {
            "outputs": [
                {"path": "report.csv", "label": "月度报表"},
                {"path": "trend.png", "label": "趋势图"},
            ]
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
""".lstrip()
    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\n{metadata}\n---\n{guide}\n",
        )
        archive.writestr("scripts/build_report.py", script)
    return bundle.getvalue()


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[str, str, str, str]:
    """创建管理员闭环用户、普通权限用户、认证会话和聊天会话。"""
    now = datetime.now(UTC)
    owner_id = "usr_skills_e2e"
    auth_session_id = "auth_skills_e2e"
    ordinary_user_id = "usr_skills_e2e_ordinary"
    ordinary_auth_session_id = "auth_skills_e2e_ordinary"
    session_id = "ses_skills_e2e"
    token = "skills-e2e-cookie-token"
    ordinary_token = "skills-e2e-ordinary-cookie-token"
    async with factory() as session, session.begin():
        session.add(
            UserModel(
                id=owner_id,
                email="skills-e2e@example.test",
                password_hash="unused",
                display_name=None,
                status="active",
                role="super_admin",
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        ordinary_user = UserModel(
            id=ordinary_user_id,
            email="skills-e2e-ordinary@example.test",
            password_hash="unused",
            display_name=None,
            status="active",
            role="member",
            created_at=now,
            updated_at=now,
            deleted_time=None,
        )
        session.add(ordinary_user)
        await session.flush()
        session.add(
            AuthSessionModel(
                id=auth_session_id,
                user_id=owner_id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + timedelta(hours=1),
                revoked_time=None,
                created_at=now,
            )
        )
        session.add(
            AuthSessionModel(
                id=ordinary_auth_session_id,
                user_id=ordinary_user_id,
                token_hash=hashlib.sha256(ordinary_token.encode()).hexdigest(),
                expires_at=now + timedelta(hours=1),
                revoked_time=None,
                created_at=now,
            )
        )
        session.add(
            SessionModel(
                id=session_id,
                owner_id=owner_id,
                title=None,
                project_id=None,
                last_message_sequence=0,
                created_at=now,
                updated_at=now,
                deleted_time=None,
            )
        )
        session.add(
            ModelProfileVersionModel(
                id="llmprof_skills_e2e",
                name="Skills E2E fake provider",
                protocol="chat_completions",
                base_url="https://llm.example.test/v1",
                model="skills-e2e-model",
                **encrypted_llm_key_fields("llmprof_skills_e2e", "test-only-fake-key"),
                context_window_tokens=8192,
                tokenizer_encoding="o200k_base",
                supports_stream=True,
                supports_tools=True,
                max_output_tokens=512,
                request_timeout_seconds=30,
                execution_timeout_seconds=60,
                max_concurrency=1,
                temperature=None,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        session.add(
            DefaultModelProfileModel(
                singleton_id=1,
                profile_id="llmprof_skills_e2e",
            )
        )
    return owner_id, session_id, token, ordinary_token


def _minio_config() -> MinioFileStorageConfig:
    bucket = os.environ["ATHENA_TEST_MINIO_BUCKET"]
    production_bucket = os.getenv("ATHENA_STORAGE_BUCKET")
    # 防止集成测试误指向开发/生产业务桶，隔离测试数据与真实业务对象。
    if production_bucket and production_bucket == bucket:
        raise AssertionError("test MinIO bucket must differ from the production bucket")
    secure = os.getenv("ATHENA_TEST_MINIO_SECURE", "true").strip().lower()
    if secure not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
        raise AssertionError("ATHENA_TEST_MINIO_SECURE_invalid")
    return MinioFileStorageConfig(
        endpoint=os.environ["ATHENA_TEST_MINIO_ENDPOINT"],
        access_key=os.environ["ATHENA_TEST_MINIO_ACCESS_KEY"],
        secret_key=os.environ["ATHENA_TEST_MINIO_SECRET_KEY"],
        bucket=bucket,
        secure=secure in {"true", "1", "yes", "on"},
    )


def test_real_skills_import_approval_dual_artifact_and_next_run_reference() -> None:
    """真实 HTTP/DB/MinIO/脚本闭环，不使用 mock 文件或 mock 脚本执行。"""

    async def scenario() -> None:
        async with _database() as (factory, _engine, _admin, _schema):
            owner_id, session_id, token, ordinary_token = await _seed(factory)
            raw_store = MinioFileObjectStore(
                _minio_config(),
                clock=lambda: datetime.now(UTC) + timedelta(days=2),
            )
            store = _RecordingStore(raw_store)
            await store.start()
            temp_root = Path(f"/tmp/athena-skills-e2e-{token_hex(8)}")
            http_client: httpx2.AsyncClient | None = None
            try:
                auth_repository = SqlAlchemyAuthRepository(factory)
                session_repository = SqlAlchemySessionRepository(factory)
                message_repository = SqlAlchemyMessageRepository(factory)
                message_service = MessageService(message_repository)
                run_repository = SqlAlchemyRunRepository(factory, message_repository)
                skill_repository = SqlAlchemySkillRepository(factory)
                auth_service = AuthService(auth_repository, Argon2PasswordHasher())
                run_service = RunService(run_repository)
                access_policy = AccessPolicyService(SqlAlchemyAccessControlRepository(factory))
                skill_service = SkillService(
                    skill_repository,
                    store,
                    auth_repository,
                    access_policy=access_policy,
                )
                stream_service = RunStreamService(
                    run_repository,
                    auth_service,
                    None,
                )
                file_repository = SqlAlchemySessionFileRepository(factory)
                file_service = SessionFileService(file_repository, store)
                skill_execution = SkillExecutionService(
                    skills=skill_service,
                    files=file_service,
                    repository=skill_repository,
                    object_store=store,
                    runner=PythonScriptExecutor(Path(sys.executable)),
                    temp_root=temp_root,
                )
                runtime = RunToolRuntimeService(
                    auth=auth_repository,
                    sessions=session_repository,
                    mcp_catalog=_EmptyMcpCatalog(),
                    mcp_executor=_UnusedMcpExecutor(),
                    knowledge=None,
                    project_knowledge=None,
                    skills=skill_service,
                    session_files=file_service,
                    skill_execution=skill_execution,
                    access_policy=access_policy,
                )
                app = FastAPI()
                app.state.authenticator = auth_service
                app.state.auth_cookie_name = "athena_session"
                app.state.public_origin = None
                app.include_router(build_skill_admin_router(skill_service))
                app.include_router(build_skill_user_router(skill_service))
                app.include_router(build_session_file_router(file_service))
                app.include_router(build_run_router(run_service, stream_service))
                app.include_router(build_message_router(message_service))
                http_client = httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://testserver",
                )
                http_client.cookies.set("athena_session", token)

                imported = await http_client.post(
                    "/api/v1/admin/skills/import",
                    headers={"Idempotency-Key": "import-monthly-report"},
                    files={
                        "package": (
                            "monthly-report.zip",
                            _skill_zip(guide="读取 CSV，按明确周期生成两个交付文件。"),
                            "application/zip",
                        )
                    },
                )
                assert imported.status_code == 201, imported.text
                imported_body = imported.json()
                skill_id = str(imported_body["id"])
                version_id = str(imported_body["versions"][0]["id"])
                assert imported_body["enabled"] is False

                enabled = await http_client.patch(
                    f"/api/v1/admin/skills/{skill_id}",
                    json={
                        "expected_revision": 1,
                        "current_version_id": version_id,
                        "enabled": True,
                        "script_requires_approval": True,
                    },
                )
                assert enabled.status_code == 200, enabled.text
                assert enabled.json()["enabled"] is True
                assert enabled.json()["script_requires_approval"] is True

                directory = await http_client.get("/api/v1/skills")
                assert directory.status_code == 200
                assert [item["id"] for item in directory.json()["items"]] == [skill_id]

                uploaded = await http_client.post(
                    f"/api/v1/sessions/{session_id}/files",
                    headers={"Idempotency-Key": "source-sales-csv"},
                    files={
                        "file": (
                            "sales.csv",
                            b"month,total\n2026-10,42\n",
                            "text/csv",
                        )
                    },
                )
                assert uploaded.status_code == 201, uploaded.text
                input_file_id = str(uploaded.json()["id"])

                tool_arguments: dict[str, object] = {
                    "skill_id": skill_id,
                    "script_path": "scripts/build_report.py",
                    "parameters": {
                        "period": "2026-10",
                        "api_token": "must-not-appear-in-user-api",
                    },
                    "input_file_ids": [input_file_id],
                }
                provider = _ScriptedProvider(
                    [
                        _response(
                            call=(
                                "call_skills_e2e",
                                "skill_execute",
                                tool_arguments,
                            ),
                            attempt=1,
                        ),
                        _response(
                            text="已生成月度报表和趋势图。",
                            attempt=2,
                        ),
                    ]
                )
                executor = RunExecutor(
                    run_repository,
                    SqlAlchemyModelProfileRepository(factory, TEST_LLM_CIPHER),
                    provider,
                    system_prompts=SqlAlchemySystemPromptRepository(factory),
                    tool_runtime=runtime,
                    config=RunWorkerConfig(),
                )
                submitted = await http_client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "skills-e2e-run",
                        "text": "请使用月度报告技能生成报表。",
                        "skill_ids": [skill_id],
                        "input_file_ids": [input_file_id],
                    },
                )
                assert submitted.status_code == 201, submitted.text
                run_id = str(submitted.json()["id"])

                first_leases = await run_repository.claim(
                    "worker-skills-e2e",
                    lease_seconds=30,
                    execution_seconds=60,
                    limit=1,
                )
                assert len(first_leases) == 1
                await executor.execute(first_leases[0])

                waiting = await http_client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
                assert waiting.status_code == 200, waiting.text
                waiting_body = waiting.json()
                assert waiting_body["status"] == "waiting_for_approval"
                assert len(waiting_body["pending_approvals"]) == 1
                pending = waiting_body["pending_approvals"][0]
                projection = pending["safe_skill_projection"]
                assert projection["skill_id"] == skill_id
                assert projection["hidden_parameter_count"] == 1
                assert projection["input_files"][0]["file_id"] == input_file_id
                assert projection["can_approve"] is True
                assert "must-not-appear" not in waiting.text
                call_id = str(pending["call_id"])
                forbidden_wire_values = (
                    "must-not-appear",
                    "api_token",
                    "session-files/",
                    "skill-logs/",
                    str(temp_root),
                    "ATHENA_INPUT_PATH",
                )
                for value in forbidden_wire_values:
                    assert value not in waiting.text

                detail = await http_client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}"
                )
                assert detail.status_code == 200, detail.text
                assert detail.json()["arguments"] is None
                assert detail.json()["outcome"] is None
                assert detail.json()["safe_skill_projection"] == projection
                for value in forbidden_wire_values:
                    assert value not in detail.text

                events = await http_client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/events"
                )
                assert events.status_code == 200, events.text
                for value in forbidden_wire_values:
                    assert value not in events.text
                stream = await http_client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/stream"
                )
                assert stream.status_code == 200, stream.text
                assert "event: snapshot" in stream.text
                for value in forbidden_wire_values:
                    assert value not in stream.text
                messages = await http_client.get(f"/api/v1/sessions/{session_id}/messages")
                assert messages.status_code == 200, messages.text
                for value in forbidden_wire_values:
                    assert value not in messages.text

                    http_client.cookies.set("athena_session", ordinary_token)
                    admin_execution_by_user = await http_client.get(
                        f"/api/v1/admin/skill-executions/{call_id}"
                    )
                    assert admin_execution_by_user.status_code == 403
                    assert admin_execution_by_user.json() == {"detail": "admin_required"}
                    http_client.cookies.set("athena_session", token)
                answer_with_skill_binding = await http_client.post(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/"
                    "input-requests/input-not-pending/answer",
                    json={
                        "request_id": "answer-must-not-rebind-skill",
                        "text": "继续",
                        "skill_ids": [skill_id],
                    },
                )
                assert answer_with_skill_binding.status_code == 422

                approved = await http_client.post(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}/approval",
                    json={
                        "request_id": "approve-skills-e2e",
                        "decision": "approve",
                    },
                )
                assert approved.status_code == 200, approved.text

                second_leases = await run_repository.claim(
                    "worker-skills-e2e",
                    lease_seconds=30,
                    execution_seconds=60,
                    limit=1,
                )
                assert len(second_leases) == 1
                await executor.execute(second_leases[0])

                completed = await http_client.get(f"/api/v1/sessions/{session_id}/runs/{run_id}")
                assert completed.status_code == 200, completed.text
                assert completed.json()["status"] == "completed"
                completed_detail = await http_client.get(
                    f"/api/v1/sessions/{session_id}/runs/{run_id}/tool-calls/{call_id}"
                )
                assert completed_detail.status_code == 200, completed_detail.text
                completed_detail_body = completed_detail.json()
                assert completed_detail_body["arguments"] is None
                assert completed_detail_body["outcome"]["status"] == "ok", completed_detail_body[
                    "outcome"
                ]
                assert completed_detail_body["outcome"]["data"] == {}
                artifacts = completed_detail_body["artifacts"]
                assert [item["filename"] for item in artifacts] == [
                    "report.csv",
                    "trend.png",
                ], completed_detail_body
                assert [item["label"] for item in artifacts] == ["月度报表", "趋势图"]
                for value in forbidden_wire_values:
                    assert value not in completed_detail.text
                delivered_pages: list[dict[str, Any]] = []
                before_sequence: int | None = None
                while True:
                    params: dict[str, int] = {"limit": 1}
                    if before_sequence is not None:
                        params["before_sequence"] = before_sequence
                    delivered_messages = await http_client.get(
                        f"/api/v1/sessions/{session_id}/messages",
                        params=params,
                    )
                    assert delivered_messages.status_code == 200, delivered_messages.text
                    delivered_pages.append(delivered_messages.json())
                    before_sequence = delivered_messages.json()["next_before_sequence"]
                    if before_sequence is None:
                        break
                delivered_file_parts = [
                    part
                    for page in delivered_pages
                    for item in page["items"]
                    for part in item["parts"]
                    if part["type"] == "file_ref"
                ]
                assert [part["file_id"] for part in delivered_file_parts] == [
                    artifacts[0]["file_id"],
                    artifacts[1]["file_id"],
                ]
                for value in forbidden_wire_values:
                    assert all(value not in str(page) for page in delivered_pages)
                admin_execution = await http_client.get(f"/api/v1/admin/skill-executions/{call_id}")
                assert admin_execution.status_code == 200, admin_execution.text
                assert admin_execution.json()["status"] == "succeeded"
                for value in forbidden_wire_values:
                    assert value not in admin_execution.text

                first_download = await http_client.get(
                    f"/api/v1/sessions/{session_id}/files/{artifacts[0]['file_id']}/download"
                )
                second_download = await http_client.get(
                    f"/api/v1/sessions/{session_id}/files/{artifacts[1]['file_id']}/download"
                )
                assert first_download.status_code == 200
                assert first_download.content == b"month,total\n2026-10,42\n"
                assert second_download.status_code == 200
                assert second_download.content.startswith(b"\x89PNG\r\n\x1a\n")
                assert (
                    'attachment; filename="report.csv"'
                    in first_download.headers["content-disposition"]
                )
                assert first_download.headers["x-content-type-options"] == "nosniff"

                listed = await http_client.get(
                    f"/api/v1/sessions/{session_id}/files",
                    params={"limit": 100},
                )
                assert listed.status_code == 200
                assert [item["filename"] for item in listed.json()["items"]] == [
                    "sales.csv",
                    "report.csv",
                    "trend.png",
                ]

                # 离线维护只通过 _RecordingStore 查看本测试创建的 key。即使
                # 隔离 schema 对共享 bucket 的其他对象没有引用，也不能误删它们。
                input_file = (
                    await file_service.get_many(
                        session_id,
                        owner_id,
                        (input_file_id,),
                    )
                )[0]
                async with factory() as session:
                    skill_object_key = await session.scalar(
                        select(SkillVersionModel.object_key).where(
                            SkillVersionModel.id == version_id
                        )
                    )
                    execution_log_key = await session.scalar(
                        select(SkillExecutionModel.log_object_key).where(
                            SkillExecutionModel.tool_call_record_id == call_id
                        )
                    )
                assert skill_object_key is not None
                assert execution_log_key is not None
                protected_keys = {
                    input_file.object_key,
                    skill_object_key,
                    execution_log_key,
                }
                orphan_key = f"session-files/aa/e2e-orphan-{token_hex(12)}"
                await store.put_bytes(
                    orphan_key,
                    b"unpublished",
                    content_type="application/octet-stream",
                )
                reclaimed = await FileObjectMaintenance(
                    store=store,
                    references=file_repository,
                ).run(execute=True, writers_stopped=True)
                assert reclaimed.deleted_count == 1
                assert reclaimed.failed_count == 0
                with pytest.raises(FileStorageError):
                    await store.get_bytes(orphan_key, maximum_bytes=1024)
                assert orphan_key in store.deleted_keys
                assert protected_keys.isdisjoint(store.deleted_keys)
                for protected_key in protected_keys:
                    try:
                        await store.get_bytes(protected_key, maximum_bytes=64 * 1024 * 1024)
                    except FileStorageError as error:
                        raise AssertionError(
                            f"protected object missing: {protected_key} ({error.code})"
                        ) from error

                next_run = await http_client.post(
                    f"/api/v1/sessions/{session_id}/runs",
                    json={
                        "request_id": "skills-e2e-next-run",
                        "text": "复用上一任务的趋势图继续分析。",
                        "input_file_ids": [artifacts[1]["file_id"]],
                    },
                )
                assert next_run.status_code == 201, next_run.text
                assert next_run.json()["input_file_ids"] == [artifacts[1]["file_id"]]
                assert next_run.json()["skill_snapshot"]["selected_skill_ids"] == []

                async with factory() as session:
                    call_row = await session.get(RunToolCallModel, call_id)
                assert call_row is not None
                assert call_row.outcome is not None
                assert call_row.outcome["status"] == "ok"
                assert owner_id not in detail.text
                assert provider.requests
                first_messages = "\n".join(item.content for item in provider.requests[0].messages)
                assert "读取 CSV，按明确周期生成两个交付文件。" in first_messages
                assert f'"file_id":"{input_file_id}"' in first_messages
                assert '"filename":"sales.csv"' in first_messages
                assert temp_root.is_dir()
                assert list(temp_root.iterdir()) == []
            finally:
                if temp_root.exists():
                    for child in temp_root.iterdir():
                        if child.is_dir():
                            import shutil

                            shutil.rmtree(child, ignore_errors=True)
                    temp_root.rmdir()
                if http_client is not None:
                    await http_client.aclose()
                await store.cleanup()
                await store.aclose()

    asyncio.run(scenario())


__all__: list[str] = []
