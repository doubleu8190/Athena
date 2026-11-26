"""Phase 2-2 模块 D：HTTP MCP 连接池/清单缓存与 Skill 本地只读缓存/解阻塞回归测试。"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import os
import sys
import threading
import zipfile
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx2
import pytest
from mcp import types
from mcp_types import (
    DiscoverResult,
    ListToolsResult,
    ServerCapabilities,
    ToolsCapability,
)
from pydantic import BaseModel, ValidationError
from run_worker import run_worker_process

from adapter.skill.execution import PythonScriptExecutor
from app.skill.execution_service import SkillExecutionService
from app.skill.package import SkillPackageValidator, ValidatedSkillPackage
from app.skill.package_cache import (
    SkillPackageCache,
    resolve_safe_child_path,
    safe_relative_posix_parts,
)
from app.skill.service import SkillService, _decode_utf8_resource_slice
from bootstrap.run_worker import RunWorkerBundle, build_run_worker_dependencies
from common.async_utils import await_task_shielded, run_thread_shielded
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.file import PendingSessionFile
from domain.mcp import (
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
    StreamableHttpConfig,
)
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillSnapshot,
    SkillExecutionRecord,
    SkillExecutionStart,
    SkillExecutionStatus,
    SkillPackageManifest,
    SkillRepository,
    SkillVersion,
)
from domain.skill.execution import (
    ScriptExecutionCancelled,
    ScriptExecutionRequest,
    ScriptExecutionResult,
)
from domain.tool import ToolExecutionContext, ToolOutcomeStatus
from infrastructure.mcp.execution import McpToolCallClient

_NOW = datetime(2026, 11, 25, tzinfo=UTC)


class _InMemoryModernMcpPeer(httpx2.AsyncBaseTransport):
    """基于现代 MCP 2026-07-28 协议的内存对端，验证连接池、Cookie、header 映射与 schema。"""

    def __init__(
        self,
        *,
        set_cookie: str | None = None,
        call_gate: asyncio.Event | None = None,
        call_entered: asyncio.Event | None = None,
    ) -> None:
        self.set_cookie = set_cookie
        self.call_gate = call_gate
        self.call_entered = call_entered
        self.requests: list[httpx2.Request] = []
        self.methods: list[str] = []
        self.call_headers: list[dict[str, str]] = []
        self.next_call_mode: str = "ok"
        self.closed = False

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        raw_body = await request.aread()
        if request.method == "DELETE":
            return httpx2.Response(200, request=request)
        if not raw_body:
            return httpx2.Response(202, request=request)
        message = json.loads(raw_body)
        method = message["method"]
        self.methods.append(method)
        headers: dict[str, str] = {"content-type": "application/json"}
        if self.set_cookie is not None:
            headers["set-cookie"] = self.set_cookie
        if method == "server/discover":
            result = DiscoverResult(
                supported_versions=["2026-07-28"],
                capabilities=ServerCapabilities(tools=ToolsCapability()),
            ).model_dump(by_alias=True, mode="json", exclude_none=True)
            body: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "notifications/initialized":
            return httpx2.Response(202, headers=headers, request=request)
        elif method == "tools/list":
            tool = types.Tool(
                name="tenant_echo",
                input_schema={
                    "type": "object",
                    "properties": {
                        "tenant": {"type": "string", "x-mcp-header": "Tenant"},
                        "query": {"type": "string"},
                    },
                    "required": ["tenant", "query"],
                },
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}, "seen_cookie": {"type": "string"}},
                    "required": ["ok"],
                },
            )
            result = ListToolsResult(tools=[tool]).model_dump(
                by_alias=True,
                mode="json",
                exclude_none=True,
            )
            body = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        elif method == "tools/call":
            self.call_headers.append({k.lower(): v for k, v in request.headers.items()})
            if self.call_entered is not None:
                self.call_entered.set()
            if self.call_gate is not None:
                await self.call_gate.wait()
            mode = self.next_call_mode
            if mode == "mcp_error":
                body = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32603, "message": "boom"},
                }
            elif mode == "tool_error":
                call_result = types.CallToolResult(
                    content=[types.TextContent(type="text", text="biz failed")],
                    is_error=True,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
                body = {"jsonrpc": "2.0", "id": message["id"], "result": call_result}
            elif mode == "bad_schema":
                call_result = types.CallToolResult(
                    content=[types.TextContent(type="text", text="drifted")],
                    structured_content={"ok": "not-a-boolean"},
                    is_error=False,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
                body = {"jsonrpc": "2.0", "id": message["id"], "result": call_result}
            else:
                call_result = types.CallToolResult(
                    content=[types.TextContent(type="text", text="ok")],
                    structured_content={
                        "ok": True,
                        "seen_cookie": request.headers.get("cookie", ""),
                    },
                    is_error=False,
                ).model_dump(by_alias=True, mode="json", exclude_none=True)
                body = {"jsonrpc": "2.0", "id": message["id"], "result": call_result}
        else:
            body = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"},
            }
        return httpx2.Response(
            200,
            headers=headers,
            content=json.dumps(body).encode(),
            request=request,
        )

    async def aclose(self) -> None:
        self.closed = True


def _http_target(
    *,
    server_id: str = "srv_1",
    config_version: int = 1,
    url: str = "https://host-a.example/mcp",
    remote_name: str = "tenant_echo",
    secrets: dict[str, str] | None = None,
) -> McpRuntimeToolTarget:
    return McpRuntimeToolTarget(
        tool_id=f"tool_{server_id}_{remote_name}",
        server_id=server_id,
        remote_name=remote_name,
        config_version=config_version,
        connection=McpConnectionInput(
            StreamableHttpConfig(transport="streamable_http", url=url),
            secrets if secrets is not None else {"Authorization": "Bearer secret-a"},
        ),
    )


def test_mcp_shared_http_transport_reuses_pool_and_isolates_cookies_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP MCP 复用单个共享底层连接池，每次调用独立无 Cookie 客户端隔离凭据与 Set-Cookie。"""

    async def scenario() -> None:
        peer = _InMemoryModernMcpPeer(set_cookie="session=user_a_secret; HttpOnly")
        build_calls = 0

        def fake_build() -> httpx2.AsyncBaseTransport:
            nonlocal build_calls
            build_calls += 1
            return peer

        executor = McpToolCallClient(cleanup_timeout_seconds=2.0)
        monkeypatch.setattr(executor, "_build_shared_http_transport", fake_build)

        target_a = _http_target(
            server_id="srv_a",
            config_version=1,
            url="https://host-a.example/mcp",
            secrets={"Authorization": "Bearer super-secret-token-xyz"},
        )
        # 第一次调用收到 Set-Cookie，随后关闭下发并验证第二次调用绝不回传上次 Cookie。
        first = await executor.call(
            target_a, {"tenant": "t1", "query": "one"}, deadline_seconds=5.0
        )
        peer.set_cookie = None
        second = await executor.call(
            target_a, {"tenant": "t2", "query": "two"}, deadline_seconds=5.0
        )
        assert first.status is McpCallStatus.OK
        assert second.status is McpCallStatus.OK
        assert build_calls == 1
        assert not peer.closed
        assert second.structured == {"ok": True, "seen_cookie": ""}
        assert "cookie" not in peer.call_headers[-1]
        assert peer.call_headers[-1].get("authorization") == "Bearer super-secret-token-xyz"

        # 切换不同域名与不同凭据时继续复用单个共享底层 transport，且凭据按次严格隔离。
        target_b = _http_target(
            server_id="srv_b",
            config_version=2,
            url="https://host-b.example/mcp",
            secrets={"Authorization": "Bearer different-secret-999"},
        )
        res_b = await executor.call(target_b, {"tenant": "t3", "query": "b"}, deadline_seconds=5.0)
        assert res_b.status is McpCallStatus.OK
        assert build_calls == 1
        assert peer.call_headers[-1].get("authorization") == "Bearer different-secret-999"
        assert "cookie" not in peer.call_headers[-1]

        # aclose() 统一释放共享底层 transport，且重复调用幂等。
        await executor.aclose()
        assert peer.closed
        await executor.aclose()

    asyncio.run(scenario())


def test_mcp_uses_public_list_tools_without_private_hook_and_handles_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """统一通过公开 session.list_tools 校验工具与准备 schema，不依赖 SDK 私有 Hook。"""

    async def scenario() -> None:
        peer = _InMemoryModernMcpPeer()
        executor = McpToolCallClient()
        assert not hasattr(executor, "_tool_listing_cache")
        monkeypatch.setattr(executor, "_build_shared_http_transport", lambda: peer)
        target = _http_target()

        # 1. 每次调用均通过公开 tools/list 准备协议层状态，保留 Mcp-Param-Tenant 与 schema 校验
        r1 = await executor.call(target, {"tenant": "acme", "query": "1"}, deadline_seconds=5.0)
        assert r1.status is McpCallStatus.OK
        assert peer.methods.count("tools/list") == 1
        assert peer.call_headers[-1].get("mcp-param-tenant") == "acme"

        r2 = await executor.call(target, {"tenant": "beta", "query": "2"}, deadline_seconds=5.0)
        assert r2.status is McpCallStatus.OK
        assert peer.methods.count("tools/list") == 2
        assert peer.call_headers[-1].get("mcp-param-tenant") == "beta"

        # 2. 业务返回 is_error=True -> ERROR
        peer.next_call_mode = "tool_error"
        r3 = await executor.call(target, {"tenant": "acme", "query": "3"}, deadline_seconds=5.0)
        assert r3.status is McpCallStatus.ERROR
        assert peer.methods.count("tools/list") == 3

        # 3. 远端输出结构漂移触发 validate_tool_result 失败 -> UNKNOWN
        peer.next_call_mode = "bad_schema"
        r4 = await executor.call(target, {"tenant": "acme", "query": "4"}, deadline_seconds=5.0)
        assert r4.status is McpCallStatus.UNKNOWN
        assert peer.methods.count("tools/list") == 4

        # 4. call_tool 抛 MCPError -> UNKNOWN (mcp_tool_protocol_unknown)
        peer.next_call_mode = "mcp_error"
        r5 = await executor.call(target, {"tenant": "acme", "query": "5"}, deadline_seconds=5.0)
        assert r5.status is McpCallStatus.UNKNOWN
        assert r5.code == "mcp_tool_protocol_unknown"
        assert peer.methods.count("tools/list") == 5

        await executor.aclose()

        # 5. call_tool 抛 pydantic ValidationError -> UNKNOWN，且绝不触发私有 _absorb_tool_listing
        class _StrictModel(BaseModel):
            x: int

        class _ValidationErrorSession:
            def __init__(self) -> None:
                self.list_calls = 0

            def _absorb_tool_listing(self, *_args: object, **_kwargs: object) -> Any:
                raise AssertionError("不得调用 MCP SDK 私有方法 _absorb_tool_listing")

            async def list_tools(self, *, params: Any = None) -> types.ListToolsResult:
                del params
                self.list_calls += 1
                return types.ListToolsResult(
                    tools=[types.Tool(name="tenant_echo", input_schema={"type": "object"})]
                )

            async def call_tool(self, *args: object, **kwargs: object) -> Any:
                del args, kwargs
                _StrictModel.model_validate({"x": "not-an-int"})

        session = _ValidationErrorSession()

        class _StubClient:
            server_capabilities = SimpleNamespace(tools=object())

            def __init__(self) -> None:
                self.session = session

            async def __aenter__(self) -> _StubClient:
                return self

            async def __aexit__(self, *args: object) -> None:
                del args

        val_executor = McpToolCallClient(
            client_factory=lambda _t, _d: _StubClient(),
            transport_factory=lambda _: cast(Any, object()),
        )
        res_v = await val_executor.call(target, {}, deadline_seconds=2.0)
        assert res_v.status is McpCallStatus.UNKNOWN
        assert session.list_calls == 1
        # 确认异常确实来自 pydantic ValidationError 语义
        with pytest.raises(ValidationError):
            _StrictModel.model_validate({"x": "not-an-int"})

    asyncio.run(scenario())


def test_run_worker_bootstrap_and_shutdown_closes_mcp_executor_and_shares_skill_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组合根共享 SkillPackageCache（不以 skill- 开头），Worker 退出时释放 mcp_executor。"""

    async def scenario() -> None:
        mcp_closed = False

        class FakeMcpExecutor:
            async def aclose(self) -> None:
                nonlocal mcp_closed
                mcp_closed = True

        class FakeDatabase:
            session_factory = object()

            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            async def dispose(self) -> None:
                return None

        class FakeProvider:
            async def aclose(self) -> None:
                return None

        class FakeObjectStore:
            async def start(self) -> None:
                return None

            async def aclose(self) -> None:
                return None

        class FakeWorker:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def run(self) -> None:
                return None

            async def stop(self) -> None:
                return None

        skill_root = tmp_path / "skill-exec-root"
        monkeypatch.setenv("ATHENA_STORAGE_ENDPOINT", "minio.internal:9000")
        monkeypatch.setenv("ATHENA_STORAGE_ACCESS_KEY", "ak")
        monkeypatch.setenv("ATHENA_STORAGE_SECRET_KEY", "sk")
        monkeypatch.setenv("ATHENA_STORAGE_BUCKET", "athena-test")
        monkeypatch.setenv("ATHENA_STORAGE_SECURE", "false")
        monkeypatch.setenv("ATHENA_EMBEDDING_BASE_URL", "http://127.0.0.1:8000/v1")
        monkeypatch.setenv("ATHENA_SKILL_TEMP_ROOT", str(skill_root))
        monkeypatch.delenv("ATHENA_REDIS_URL", raising=False)

        class FakeSharedStorageClient:
            async def start(self) -> None:
                return None

            async def aclose(self) -> None:
                return None

        class FakeQueryServices:
            knowledge = None
            project_knowledge = None

            async def start(self) -> None:
                return None

            async def aclose(self) -> None:
                return None

        captured_skills: list[SkillService] = []
        original_skill_service = SkillService

        def spy_skill_service(*args: Any, **kwargs: Any) -> SkillService:
            svc = original_skill_service(*args, **kwargs)
            captured_skills.append(svc)
            return svc

        with (
            patch("bootstrap.run_worker.McpSecretCipher.from_environment", return_value=object()),
            patch("bootstrap.database.Database", FakeDatabase),
            patch("bootstrap.run_worker.build_mcp_executor", return_value=FakeMcpExecutor()),
            patch(
                "bootstrap.run_worker.build_shared_storage_client",
                return_value=FakeSharedStorageClient(),
            ),
            patch(
                "bootstrap.run_worker.build_query_services",
                return_value=FakeQueryServices(),
            ),
            patch(
                "bootstrap.run_worker.MinioFileObjectStore",
                return_value=FakeObjectStore(),
            ),
            patch("bootstrap.run_worker.SkillService", spy_skill_service),
            patch("bootstrap.run_worker.OpenAIChatCompletionsAdapter", FakeProvider),
            patch("bootstrap.run_worker.RunExecutor", lambda *_a, **_kw: object()),
            patch("bootstrap.run_worker.RunWorker", FakeWorker),
        ):
            bundle = RunWorkerBundle()
            build_run_worker_dependencies(bundle, database_url="postgresql://unused")
            assert bundle.mcp_executor is not None
            assert bundle.storage_client is not None
            assert bundle.query_services is not None
            assert bundle.file_object_store is not None
            assert bundle.skill_execution is not None
            assert bundle.skill_package_cache is not None
            assert len(captured_skills) == 1
            assert captured_skills[0]._object_store is bundle.file_object_store
            assert captured_skills[0]._package_cache is bundle.skill_package_cache
            assert bundle.skill_execution._package_cache is bundle.skill_package_cache
            await bundle.skill_package_cache.aclose()

        # 验证 run_worker_process 的 finally 调用 mcp_executor 与 skill_package_cache 的 aclose()
        mcp_closed = False
        cache_closed = False
        original_cache_aclose = SkillPackageCache.aclose

        async def spy_cache_aclose(self: SkillPackageCache) -> None:
            nonlocal cache_closed
            cache_closed = True
            await original_cache_aclose(self)

        with (
            patch("run_worker.prepare_run_tokenizers", _async_noop),
            patch("bootstrap.run_worker.McpSecretCipher.from_environment", return_value=object()),
            patch("bootstrap.database.Database", FakeDatabase),
            patch("bootstrap.run_worker.build_mcp_executor", return_value=FakeMcpExecutor()),
            patch(
                "bootstrap.run_worker.build_shared_storage_client",
                return_value=FakeSharedStorageClient(),
            ),
            patch(
                "bootstrap.run_worker.build_query_services",
                return_value=FakeQueryServices(),
            ),
            patch(
                "bootstrap.run_worker.MinioFileObjectStore",
                return_value=FakeObjectStore(),
            ),
            patch("bootstrap.run_worker.OpenAIChatCompletionsAdapter", FakeProvider),
            patch("bootstrap.run_worker.RunExecutor", lambda *_a, **_kw: object()),
            patch("bootstrap.run_worker.RunWorker", FakeWorker),
            patch.object(SkillPackageCache, "aclose", spy_cache_aclose),
        ):
            await run_worker_process(database_url="postgresql://unused")
        assert mcp_closed
        assert cache_closed

    asyncio.run(scenario())


async def _async_noop() -> None:
    return None


def _build_skill_zip(
    tmp_path: Path,
    *,
    filename: str = "pkg.zip",
    slug: str = "cache-demo",
    extra_files: dict[str, bytes] | None = None,
    script_body: str = 'print("ok")\n',
) -> tuple[Path, bytes, SkillPackageManifest]:
    zip_path = tmp_path / filename
    markdown = f"""
---
name: 缓存技能 {slug}
slug: {slug}
description: 用于测试本地只读缓存与分段读取。
dependencies: []
scripts:
  - path: scripts/run.py
    label: 运行脚本
    purpose: 执行测试脚本。
    parameters:
      type: object
      additionalProperties: false
      properties: {{}}
---
测试指南内容。
""".strip()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", markdown)
        archive.writestr("scripts/run.py", script_body)
        for rel_path, content in (extra_files or {}).items():
            archive.writestr(rel_path, content)
    validated = SkillPackageValidator().validate(zip_path)
    return zip_path, zip_path.read_bytes(), validated.manifest


def _dep_check() -> DependencyCheck:
    return DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
        python_implementation=sys.implementation.name,
        items=(),
        checked_at=_NOW,
    )


def _skill_version(
    *,
    version_id: str,
    skill_id: str,
    manifest: SkillPackageManifest,
    object_key: str | None,
    package_bytes: bytes | None = None,
    package_sha256: str | None = None,
) -> SkillVersion:
    digest = (
        package_sha256
        if package_sha256 is not None
        else (hashlib.sha256(package_bytes).hexdigest() if package_bytes is not None else "1" * 64)
    )
    return SkillVersion(
        id=version_id,
        skill_id=skill_id,
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=object_key,
        package_sha256=digest,
        dependency_requirements=(),
        dependency_check=_dep_check(),
        created_by=None,
        created_at=_NOW,
    )


def test_skill_package_cache_single_flight_sha256_integrity_and_path_defense(
    tmp_path: Path,
) -> None:
    """验证内存 ZIP 缓存 single-flight 去重、SHA-256 完整性校验、aclose 释放与路径穿越防御。"""
    cache = SkillPackageCache()
    _, pkg_bytes, manifest = _build_skill_zip(
        tmp_path,
        extra_files={"references/notes.txt": "缓存测试\n".encode()},
    )
    version = _skill_version(
        version_id="skillver_sf",
        skill_id="skill_sf",
        manifest=manifest,
        object_key="skills/sf/pkg.zip",
        package_bytes=pkg_bytes,
    )

    async def scenario() -> None:
        fetch_calls = 0
        fetch_gate = asyncio.Event()

        async def slow_fetch() -> bytes:
            nonlocal fetch_calls
            fetch_calls += 1
            await fetch_gate.wait()
            return pkg_bytes

        # 1) 5 个并发请求同 key 命中 single-flight，仅触发 1 次对象存储回源。
        tasks = [
            asyncio.create_task(cache.get_zip_bytes(version, fetch_bytes=slow_fetch))
            for _ in range(5)
        ]
        await asyncio.sleep(0.02)
        fetch_gate.set()
        results = await asyncio.gather(*tasks)
        assert fetch_calls == 1
        assert all(item == pkg_bytes for item in results)

        # 命中缓存不再触发回源；同一 object_key 若期望摘要失配必须立即拒绝。
        cached_hit = await cache.get_zip_bytes(version, fetch_bytes=slow_fetch)
        assert cached_hit == pkg_bytes
        assert fetch_calls == 1

        wrong_digest_same_key = _skill_version(
            version_id="skillver_sf_wrong",
            skill_id="skill_sf",
            manifest=manifest,
            object_key="skills/sf/pkg.zip",
            package_sha256="f" * 64,
        )
        with pytest.raises(ValueError, match="skill_package_integrity_invalid"):
            await cache.get_zip_bytes(wrong_digest_same_key, fetch_bytes=slow_fetch)

        # 2) 回源字节 SHA-256 与 version.package_sha256 不匹配时拒绝且不入池，后续重试可恢复。
        bad_version = _skill_version(
            version_id="skillver_bad",
            skill_id="skill_bad",
            manifest=manifest,
            object_key="skills/bad/pkg.zip",
            package_bytes=pkg_bytes,
        )
        with pytest.raises(ValueError, match="skill_package_integrity_invalid"):
            await cache.get_zip_bytes(
                bad_version,
                fetch_bytes=lambda: _return_bytes(b"corrupted-zip"),
            )
        recovered = await cache.get_zip_bytes(
            bad_version,
            fetch_bytes=lambda: _return_bytes(pkg_bytes),
        )
        assert recovered == pkg_bytes

        # 3) object_key 为 None 的纯 Markdown 版本不允许进入 ZIP 缓存。
        null_key_version = _skill_version(
            version_id="skillver_md",
            skill_id="skill_md",
            manifest=manifest,
            object_key=None,
            package_bytes=pkg_bytes,
        )
        with pytest.raises(ValueError, match="skill_package_integrity_invalid"):
            await cache.get_zip_bytes(null_key_version, fetch_bytes=slow_fetch)

        # 4) 全部等待者取消时中止孤立回源下载，且后续重试可恢复入池。
        cancel_started = asyncio.Event()
        cancel_observed = asyncio.Event()
        cancel_version = _skill_version(
            version_id="skillver_cancel",
            skill_id="skill_cancel",
            manifest=manifest,
            object_key="skills/cancel/pkg.zip",
            package_bytes=pkg_bytes,
        )

        async def blocking_fetch() -> bytes:
            cancel_started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancel_observed.set()
                raise
            return pkg_bytes

        waiter = asyncio.create_task(
            cache.get_zip_bytes(cancel_version, fetch_bytes=blocking_fetch)
        )
        await cancel_started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert cancel_observed.is_set()
        assert (
            await cache.get_zip_bytes(
                cancel_version,
                fetch_bytes=lambda: _return_bytes(pkg_bytes),
            )
            == pkg_bytes
        )

        # 5) aclose 清空缓存；关闭后清理在途任务不抛出未处理异常。
        await cache.aclose()
        assert cache._entries == {}
        assert cache._total_bytes == 0

    asyncio.run(scenario())

    # 5) 路径穿越与符号链接防御仍由 package_cache 统一提供并严格生效。
    for evil_path in ("../escape.txt", "/etc/passwd", "a\\b.txt", "a/\x00b", "a/./b", "C:evil.txt"):
        with pytest.raises(ValueError, match="skill_package_integrity_invalid"):
            safe_relative_posix_parts(evil_path)

    base = tmp_path / "safe-root"
    base.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = base / "link.txt"
    os.symlink(outside, link)
    with pytest.raises(ValueError, match="skill_package_integrity_invalid"):
        resolve_safe_child_path(base, "link.txt")
    assert resolve_safe_child_path(base, "references/notes.txt") == (
        base.resolve() / "references" / "notes.txt"
    )


async def _return_bytes(data: bytes) -> bytes:
    return data


def test_skill_package_cache_lru_evicts_by_entry_count_and_total_bytes(
    tmp_path: Path,
) -> None:
    """验证内存 ZIP 缓存按 max_entries 与 max_total_bytes 执行严格 LRU 驱逐，超限单包不入池。"""
    _, pkg1, m1 = _build_skill_zip(
        tmp_path,
        filename="p1.zip",
        slug="skill-one",
        extra_files={"references/data.txt": b"A" * 500},
    )
    _, pkg2, m2 = _build_skill_zip(
        tmp_path,
        filename="p2.zip",
        slug="skill-two",
        extra_files={"references/data.txt": b"B" * 500},
    )
    _, pkg3, m3 = _build_skill_zip(
        tmp_path,
        filename="p3.zip",
        slug="skill-three",
        extra_files={"references/data.txt": b"C" * 500},
    )
    v1 = _skill_version(
        version_id="v1", skill_id="s1", manifest=m1, object_key="k1", package_bytes=pkg1
    )
    v2 = _skill_version(
        version_id="v2", skill_id="s2", manifest=m2, object_key="k2", package_bytes=pkg2
    )
    v3 = _skill_version(
        version_id="v3", skill_id="s3", manifest=m3, object_key="k3", package_bytes=pkg3
    )

    async def scenario() -> None:
        fetches = {"k1": 0, "k2": 0, "k3": 0}

        async def fetch(key: str, data: bytes) -> bytes:
            fetches[key] += 1
            return data

        # 1) 按 max_entries=2 淘汰最久未访问条目（先访问 k1 刷新 LRU，放入 k3 后应淘汰 k2）
        cache = SkillPackageCache(max_entries=2)
        await cache.get_zip_bytes(v1, fetch_bytes=lambda: fetch("k1", pkg1))
        await cache.get_zip_bytes(v2, fetch_bytes=lambda: fetch("k2", pkg2))
        await cache.get_zip_bytes(v1, fetch_bytes=lambda: fetch("k1", pkg1))
        await cache.get_zip_bytes(v3, fetch_bytes=lambda: fetch("k3", pkg3))
        assert set(cache._entries) == {"k1", "k3"}
        # k1 命中不回源，k2 已被驱逐再次访问需回源
        await cache.get_zip_bytes(v1, fetch_bytes=lambda: fetch("k1", pkg1))
        await cache.get_zip_bytes(v2, fetch_bytes=lambda: fetch("k2", pkg2))
        assert fetches == {"k1": 1, "k2": 2, "k3": 1}

        # 2) 按 max_total_bytes 驱逐：上限仅够放 1 份 ZIP，放入第 2 份后第 1 份被淘汰
        single_cap = max(len(pkg1), len(pkg2)) + 16
        byte_cache = SkillPackageCache(max_entries=10, max_total_bytes=single_cap)
        await byte_cache.get_zip_bytes(v1, fetch_bytes=lambda: _return_bytes(pkg1))
        assert set(byte_cache._entries) == {"k1"}
        await byte_cache.get_zip_bytes(v2, fetch_bytes=lambda: _return_bytes(pkg2))
        assert set(byte_cache._entries) == {"k2"}

        # 3) 超出 max_total_bytes 的单个巨包直接透传返回且不清空已有热点缓存
        tiny_cache = SkillPackageCache(max_entries=4, max_total_bytes=len(pkg1) - 1)
        got = await tiny_cache.get_zip_bytes(v1, fetch_bytes=lambda: _return_bytes(pkg1))
        assert got == pkg1
        assert tiny_cache._entries == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("use_cache", [False, True])
def test_skill_service_read_resource_o1_utf8_boundaries(
    tmp_path: Path,
    use_cache: bool,
) -> None:
    """验证 read_resource 内存 ZIP 缓存命中直读与严格 O(1) UTF-8 多字节/续字节/损坏字节边界校验。"""
    text_sample = "Aé中😀B中文😀end"
    raw_sample = text_sample.encode("utf-8")
    _, pkg_bytes, manifest = _build_skill_zip(
        tmp_path,
        filename=f"utf8-{use_cache}.zip",
        extra_files={"references/utf8.txt": raw_sample},
    )
    version = _skill_version(
        version_id="skillver_utf8",
        skill_id="skill_utf8",
        manifest=manifest,
        object_key="skills/utf8/pkg.zip",
        package_bytes=pkg_bytes,
    )

    class _Repo:
        async def get_version(self, version_id: str) -> SkillVersion | None:
            return version if version_id == version.id else None

    class _Store:
        def __init__(self) -> None:
            self.get_bytes_calls = 0

        async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
            del key, maximum_bytes
            self.get_bytes_calls += 1
            return pkg_bytes

    store = _Store()
    cache = SkillPackageCache() if use_cache else None
    service = SkillService(
        cast(SkillRepository, _Repo()),
        cast(Any, store),
        cast(Any, None),
        package_cache=cache,
    )
    snapshot = RunSkillSnapshot(
        skills=(
            RunSkillEntry(
                skill_id=version.skill_id,
                slug=manifest.slug,
                name=manifest.name,
                description=manifest.description,
                version_id=version.id,
                version_label=version.label,
                requires_approval=False,
            ),
        ),
        selected_skill_ids=(version.skill_id,),
    )

    async def scenario() -> None:
        # 1. 小步长分页读完整文，多字节字符（2/3/4 字节）完整不乱码
        offset = 0
        parts: list[str] = []
        while True:
            sliced = await service.read_resource(
                snapshot,
                skill_id=version.skill_id,
                path="references/utf8.txt",
                offset=offset,
                limit_bytes=5,
            )
            parts.append(sliced.content)
            offset = sliced.next_offset
            if sliced.eof:
                break
        assert "".join(parts) == text_sample
        assert offset == len(raw_sample)

        # 2. offset 精准落在 4 字节 emoji '😀' 之后（offset=10，指向 'B'）与文件尾（offset==size）
        after_emoji = len("Aé中😀".encode())
        after_slice = await service.read_resource(
            snapshot,
            skill_id=version.skill_id,
            path="references/utf8.txt",
            offset=after_emoji,
            limit_bytes=16,
        )
        assert after_slice.content.startswith("B")

        eof_slice = await service.read_resource(
            snapshot,
            skill_id=version.skill_id,
            path="references/utf8.txt",
            offset=len(raw_sample),
            limit_bytes=16,
        )
        assert eof_slice.content == ""
        assert eof_slice.eof is True

        # 3. offset 落在 2/3/4 字节字符续字节上必须抛 skill_resource_offset_invalid
        for bad_offset in (
            2,  # 'é' 第 2 字节
            4,  # '中' 第 2 字节
            5,  # '中' 第 3 字节
            7,  # '😀' 第 2 字节
            8,  # '😀' 第 3 字节
            9,  # '😀' 第 4 字节
            len(raw_sample) + 1,
        ):
            with pytest.raises(ValueError, match="skill_resource_offset_invalid"):
                await service.read_resource(
                    snapshot,
                    skill_id=version.skill_id,
                    path="references/utf8.txt",
                    offset=bad_offset,
                    limit_bytes=8,
                )

        # 4. limit_bytes 小于当前起始多字节字符长度必须抛 skill_resource_range_invalid
        emoji_offset = len("Aé中".encode())
        with pytest.raises(ValueError, match="skill_resource_range_invalid"):
            await service.read_resource(
                snapshot,
                skill_id=version.skill_id,
                path="references/utf8.txt",
                offset=emoji_offset,
                limit_bytes=3,
            )

        if use_cache:
            # 启用本地缓存时，十余次分段读仅触发 1 次对象存储下载
            assert store.get_bytes_calls == 1
        else:
            assert store.get_bytes_calls > 1

    asyncio.run(scenario())

    # 5. 直接覆盖损坏/截断 UTF-8 边界字节（含 offset == file_size）
    for corrupted, off in (
        (b"ab\xe4\xb8", 4),  # 3 字节字符在 EOF 被截断
        (b"a\xf0\x9f\x98", 4),  # 4 字节字符在 EOF 被截断
        (b"a\x80b", 2),  # 孤立续字节
        (b"a\xffb", 2),  # 非法起始字节 0xFF
        (b"\xc0\xafb", 2),  # 非法过长编码
        (b"\x80\x80\x80\x80b", 4),  # 连续 4 个续字节
    ):
        prefix = corrupted[max(0, off - 4) : off]
        chunk = corrupted[off : off + 10]
        with pytest.raises(ValueError, match="skill_resource_offset_invalid"):
            _decode_utf8_resource_slice(prefix, chunk, offset=off, file_size=len(corrupted))


def test_skill_execution_service_uses_package_cache_with_0600_write_isolation(
    tmp_path: Path,
) -> None:
    """命中缓存通过 shutil.copyfile 落入工作目录并设 0o600，脚本改写不影响只读缓存与后续执行。"""
    mutating_script = """
import json
import os
import stat
from pathlib import Path

wd = Path(os.environ["ATHENA_INPUT_PATH"]).parent
skill_md = wd / "SKILL.md"
mode = stat.S_IMODE(skill_md.stat().st_mode)
original = skill_md.read_text(encoding="utf-8")
skill_md.write_text("MUTATED BY SCRIPT", encoding="utf-8")
(wd / "outputs" / "manifest.json").write_text('{"outputs": []}', encoding="utf-8")
print(json.dumps({"mode": oct(mode), "mutated_before": original == "MUTATED BY SCRIPT"}))
""".strip()
    _, pkg_bytes, manifest = _build_skill_zip(
        tmp_path,
        filename="exec-cache.zip",
        slug="exec-cache",
        script_body=mutating_script + "\n",
    )
    check = _dep_check()
    version = _skill_version(
        version_id="skillver_exec_cache",
        skill_id="skill_exec_cache",
        manifest=manifest,
        object_key="skills/exec/cache.zip",
        package_bytes=pkg_bytes,
    )

    class _SkillsStub:
        async def prepare_script_target(self, snapshot: RunSkillSnapshot, **kwargs: Any) -> Any:
            entry = snapshot.by_id(kwargs["skill_id"])
            assert entry is not None
            return entry, version, manifest.scripts[0], check

    class _FilesStub:
        async def get_many(self, *_args: Any) -> tuple[object, ...]:
            return ()

        async def prepare_artifact(self, **kwargs: Any) -> PendingSessionFile:
            raise AssertionError(f"unexpected artifact: {kwargs}")

        async def abandon_artifacts(self, _artifacts: tuple[PendingSessionFile, ...]) -> None:
            return None

    class _ObjectStoreStub:
        def __init__(self) -> None:
            self.package_fetches = 0
            self.logs: list[bytes] = []

        async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
            del key, maximum_bytes
            self.package_fetches += 1
            return pkg_bytes

        async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
            del key, content_type
            self.logs.append(content)

        async def delete(self, key: str) -> None:
            del key

    class _RepoStub:
        async def start_script_execution(self, **kwargs: Any) -> SkillExecutionStart:
            return SkillExecutionStart(
                record=SkillExecutionRecord(
                    id=kwargs["execution_id"],
                    run_id=kwargs["run_id"],
                    session_id=kwargs["session_id"],
                    tool_call_record_id=kwargs["tool_call_record_id"],
                    skill_id=kwargs["skill_id"],
                    skill_version_id=kwargs["skill_version_id"],
                    script_path=kwargs["script_path"],
                    input_file_ids=kwargs["input_file_ids"],
                    parameters_sha256=kwargs["parameters_sha256"],
                    python_version=kwargs["python_version"],
                    dependency_check=kwargs["dependency_check"],
                    status=SkillExecutionStatus.RUNNING,
                    log_object_key=None,
                    log_truncated=False,
                    log_error_code=None,
                    error_code=None,
                    cleanup_error_code=None,
                    result_artifacts=(),
                    created_at=kwargs["created_at"],
                    finished_at=None,
                ),
                created=True,
            )

    temp_root = tmp_path / "exec-root"
    cache = SkillPackageCache()
    store = _ObjectStoreStub()
    service = SkillExecutionService(
        skills=cast(Any, _SkillsStub()),
        files=cast(Any, _FilesStub()),
        repository=cast(SkillRepository, _RepoStub()),
        object_store=cast(Any, store),
        runner=PythonScriptExecutor(Path(sys.executable)),
        temp_root=temp_root,
        package_cache=cache,
        clock=lambda: _NOW,
    )
    entry = RunSkillEntry(
        skill_id=version.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=version.id,
        version_label="v1",
        requires_approval=False,
    )

    def make_context(call_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            owner_id="usr_1",
            chat_session_id="ses_1",
            project_id=None,
            run_id="run_1",
            execution_epoch=1,
            auth_session_id="auth_1",
            deadline_at=_NOW + timedelta(minutes=2),
            tool_call_record_id=call_id,
            skill_snapshot=RunSkillSnapshot(skills=(entry,), selected_skill_ids=(entry.skill_id,)),
        )

    args = {
        "skill_id": entry.skill_id,
        "script_path": "scripts/run.py",
        "parameters": {},
        "input_file_ids": [],
    }

    async def scenario() -> None:
        r1 = await service.execute(args, make_context("toolcall_exec_1"))
        r2 = await service.execute(args, make_context("toolcall_exec_2"))
        assert r1.outcome.status is ToolOutcomeStatus.OK
        assert r2.outcome.status is ToolOutcomeStatus.OK
        # 两次脚本执行只下载 1 次技能包
        assert store.package_fetches == 1
        out1 = json.loads(store.logs[0].decode("utf-8"))
        out2 = json.loads(store.logs[1].decode("utf-8"))
        # 工作目录副本均为 0o600 可写隔离；第一次改写未污染内存缓存与第二次执行
        assert out1 == {"mode": "0o600", "mutated_before": False}
        assert out2 == {"mode": "0o600", "mutated_before": False}
        # 临时目录下不留任何残留工作目录或磁盘缓存子目录
        assert list(temp_root.glob("skill-*")) == []
        assert not (temp_root / ".package-cache").exists()
        await cache.aclose()

    asyncio.run(scenario())


def test_skill_service_validate_offloop_waits_for_thread_before_propagating_cancel(
    tmp_path: Path,
) -> None:
    """import_package 校验移入工作线程，取消时必须等线程退出再重抛以防 staged_upload 竞态。"""
    zip_path, _, manifest = _build_skill_zip(tmp_path, filename="cancel-validate.zip")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    worker_thread_id: list[int] = []

    class _SlowValidator(SkillPackageValidator):
        def validate(self, path: Path) -> ValidatedSkillPackage:
            worker_thread_id.append(threading.get_ident())
            entered.set()
            try:
                release.wait(timeout=5.0)
                return ValidatedSkillPackage(
                    manifest=manifest,
                    package_sha256="2" * 64,
                    zip_size_bytes=path.stat().st_size,
                    uncompressed_size_bytes=128,
                )
            finally:
                exited.set()

    admin_user = User(
        id="usr_admin",
        email="admin@example.com",
        password_hash=None,
        display_name="Admin",
        status=UserStatus.ACTIVE,
        created_at=_NOW,
        updated_at=_NOW,
        role=UserRole.SUPER_ADMIN,
    )

    class _AuthStub:
        async def get_current_session_user(self, *_args: Any) -> User:
            return admin_user

    actor = AuthContext(
        user=admin_user,
        session_id="sess_admin",
    )
    service = SkillService(
        cast(SkillRepository, object()),
        cast(Any, object()),
        cast(Any, _AuthStub()),
        package_validator=_SlowValidator(),
    )

    async def scenario() -> None:
        main_tid = threading.get_ident()
        task = asyncio.create_task(
            service.import_package(actor, zip_path, idempotency_key="cancel-validate-1")
        )
        while not entered.is_set():
            await asyncio.sleep(0.005)
        assert worker_thread_id == [worker_thread_id[0]]
        assert worker_thread_id[0] != main_tid

        # 取消任务，但校验线程尚未释放时 task 不能提前退出
        task.cancel()
        await asyncio.sleep(0.03)
        assert not task.done()
        assert not exited.is_set()

        # 释放校验线程后，task 才传播 CancelledError，此时线程已完全退出
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert exited.is_set()

    asyncio.run(scenario())


def test_skill_execution_service_prepare_cancellation_waits_for_thread_then_cleans_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录准备期取消时，先协同中止并等待后台写线程退出，再异步清理目录避免写删竞态。"""
    _, pkg_bytes, manifest = _build_skill_zip(tmp_path, filename="prep-cancel.zip")
    check = _dep_check()
    version = _skill_version(
        version_id="skillver_prep_cancel",
        skill_id="skill_prep_cancel",
        manifest=manifest,
        object_key="skills/prep/cancel.zip",
    )

    prep_entered = threading.Event()
    prep_exited = threading.Event()
    cancel_seen = threading.Event()
    cleanup_order: list[str] = []

    def blocking_extract(
        working_directory: Path,
        *,
        package: bytes,
        version: SkillVersion,
        cancel_event: threading.Event,
    ) -> None:
        del package, version
        (working_directory / "partial.txt").write_text("writing", encoding="utf-8")
        prep_entered.set()
        try:
            if cancel_event.wait(timeout=5.0):
                cancel_seen.set()
            cleanup_order.append("prep_thread_exited")
        finally:
            prep_exited.set()

    original_remove = SkillExecutionService._remove_unentered_directory

    def traced_remove(path: Path | None) -> str | None:
        cleanup_order.append("remove_directory_started")
        assert prep_exited.is_set()
        return original_remove(path)

    monkeypatch.setattr("app.skill.execution_service._extract_package_files_sync", blocking_extract)
    monkeypatch.setattr(
        SkillExecutionService,
        "_remove_unentered_directory",
        staticmethod(traced_remove),
    )

    class _SkillsStub:
        async def prepare_script_target(self, snapshot: RunSkillSnapshot, **kwargs: Any) -> Any:
            entry = snapshot.by_id(kwargs["skill_id"])
            assert entry is not None
            return entry, version, manifest.scripts[0], check

    class _FilesStub:
        async def get_many(self, *_args: Any) -> tuple[object, ...]:
            return ()

    class _ObjectsStub:
        async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
            del key, maximum_bytes
            return pkg_bytes

    class _RepoStub:
        def __init__(self) -> None:
            self.finished: dict[str, Any] | None = None

        async def start_script_execution(self, **kwargs: Any) -> SkillExecutionStart:
            return SkillExecutionStart(
                record=SkillExecutionRecord(
                    id=kwargs["execution_id"],
                    run_id=kwargs["run_id"],
                    session_id=kwargs["session_id"],
                    tool_call_record_id=kwargs["tool_call_record_id"],
                    skill_id=kwargs["skill_id"],
                    skill_version_id=kwargs["skill_version_id"],
                    script_path=kwargs["script_path"],
                    input_file_ids=kwargs["input_file_ids"],
                    parameters_sha256=kwargs["parameters_sha256"],
                    python_version=kwargs["python_version"],
                    dependency_check=kwargs["dependency_check"],
                    status=SkillExecutionStatus.RUNNING,
                    log_object_key=None,
                    log_truncated=False,
                    log_error_code=None,
                    error_code=None,
                    cleanup_error_code=None,
                    result_artifacts=(),
                    created_at=kwargs["created_at"],
                    finished_at=None,
                ),
                created=True,
            )

        async def finish_script_execution(
            self,
            execution_id: str,
            *,
            status: SkillExecutionStatus,
            log_object_key: str | None,
            log_truncated: bool,
            log_error_code: str | None,
            error_code: str | None,
            cleanup_error_code: str | None,
            artifacts: Sequence[PendingSessionFile],
            finished_at: datetime,
        ) -> None:
            self.finished = {
                "execution_id": execution_id,
                "status": status,
                "error_code": error_code,
                "cleanup_error_code": cleanup_error_code,
            }

    class _UnusedRunner:
        @asynccontextmanager
        async def execute(
            self, _request: ScriptExecutionRequest
        ) -> AsyncIterator[ScriptExecutionResult]:
            raise AssertionError("runner must not start when preparation is cancelled")
            yield  # pragma: no cover

    temp_root = tmp_path / "prep-cancel-root"
    repo = _RepoStub()
    service = SkillExecutionService(
        skills=cast(Any, _SkillsStub()),
        files=cast(Any, _FilesStub()),
        repository=cast(SkillRepository, repo),
        object_store=cast(Any, _ObjectsStub()),
        runner=cast(Any, _UnusedRunner()),
        temp_root=temp_root,
        clock=lambda: _NOW,
    )
    entry = RunSkillEntry(
        skill_id=version.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=version.id,
        version_label="v1",
        requires_approval=False,
    )
    context = ToolExecutionContext(
        owner_id="usr_1",
        chat_session_id="ses_1",
        project_id=None,
        run_id="run_1",
        execution_epoch=1,
        auth_session_id="auth_1",
        deadline_at=_NOW + timedelta(minutes=2),
        tool_call_record_id="toolcall_prep_cancel",
        skill_snapshot=RunSkillSnapshot(skills=(entry,), selected_skill_ids=(entry.skill_id,)),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            service.execute(
                {
                    "skill_id": entry.skill_id,
                    "script_path": "scripts/run.py",
                    "parameters": {},
                    "input_file_ids": [],
                },
                context,
            )
        )
        while not prep_entered.is_set():
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert cancel_seen.is_set()
        assert cleanup_order == ["prep_thread_exited", "remove_directory_started"]
        assert list(temp_root.glob("skill-*")) == []
        assert repo.finished is not None
        assert repo.finished["status"] is SkillExecutionStatus.UNKNOWN
        assert repo.finished["error_code"] == "skill_execution_cancelled"
        assert repo.finished["cleanup_error_code"] is None

    asyncio.run(scenario())


def test_python_script_executor_directory_removal_offloop_and_survives_double_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute finally 中的目录清理在工作线程运行且抗二次取消，不遗留工作目录。"""
    working_directory = (tmp_path / "work").resolve()
    (working_directory / "scripts").mkdir(parents=True)
    output_directory = working_directory / "outputs"
    output_directory.mkdir()
    (working_directory / "scripts" / "run.py").write_text(
        "from pathlib import Path\nimport os\n"
        'wd = Path(os.environ["ATHENA_INPUT_PATH"]).parent\n'
        '(wd / "outputs" / "manifest.json").write_text(\'{"outputs": []}\', encoding="utf-8")\n',
        encoding="utf-8",
    )
    request = ScriptExecutionRequest(
        working_directory=working_directory,
        script_path="scripts/run.py",
        parameters={},
        input_files={},
        output_directory=output_directory,
        run_id="run_cleanup_test",
        tool_call_id="toolcall_cleanup_test",
        worker_instance_id="worker_cleanup_test",
    )

    remove_entered = threading.Event()
    remove_release = threading.Event()
    remove_thread_id: list[int] = []
    original_remove = PythonScriptExecutor._remove_working_directory

    def slow_remove(self: PythonScriptExecutor, path: Path) -> Any:
        remove_thread_id.append(threading.get_ident())
        remove_entered.set()
        remove_release.wait(timeout=5.0)
        return original_remove(self, path)

    monkeypatch.setattr(PythonScriptExecutor, "_remove_working_directory", slow_remove)

    async def scenario() -> None:
        main_tid = threading.get_ident()
        ticks = 0

        async def ticker_loop() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        async def run_exec() -> None:
            executor = PythonScriptExecutor(Path(sys.executable))
            async with executor.execute(request) as result:
                assert result.succeeded

        ticker = asyncio.create_task(ticker_loop())
        task = asyncio.create_task(run_exec())
        try:
            while not remove_entered.is_set():
                await asyncio.sleep(0.005)
            # 确认目录删除在工作线程执行且不阻塞事件循环
            assert remove_thread_id == [remove_thread_id[0]]
            assert remove_thread_id[0] != main_tid
            await asyncio.sleep(0.025)
            assert ticks > 0

            # 在目录删除期间连续投递两次取消：必须等待删除收敛并抛出 ScriptExecutionCancelled
            task.cancel()
            await asyncio.sleep(0.015)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.015)
            assert not task.done()

            remove_release.set()
            with pytest.raises(ScriptExecutionCancelled) as error:
                await task
            assert error.value.cleanup_error_codes == ()
            assert not working_directory.exists()
        finally:
            remove_release.set()
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(scenario())


def test_shielded_helpers_preserve_outer_cancel_over_background_exception() -> None:
    """共享 shield helper 遇到外部取消+后台异常时严禁让后台异常覆盖外层 CancelledError。"""

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))
        try:
            # 1) 无外部取消时正常透传后台线程业务异常。
            def fail_sync() -> None:
                raise ValueError("boom_sync")

            with pytest.raises(ValueError, match="boom_sync"):
                await run_thread_shielded(fail_sync, name="test-sync-fail")

            # 2) 收到两次外部取消且后台线程随后抛异常时，必须等待线程退出并只抛外层 CancelledError。
            entered = threading.Event()
            on_cancel_fired = threading.Event()
            exited = threading.Event()

            def failing_worker() -> None:
                entered.set()
                on_cancel_fired.wait(timeout=5.0)
                try:
                    raise RuntimeError("background failure must not mask CancelledError")
                finally:
                    exited.set()

            task = asyncio.create_task(
                run_thread_shielded(
                    failing_worker,
                    name="test-cancel-priority",
                    on_cancel=on_cancel_fired.set,
                )
            )
            while not entered.is_set():
                await asyncio.sleep(0.005)
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert on_cancel_fired.is_set()
            assert exited.is_set()

            # 3) await_task_shielded 直接等待异步任务：外部取消优先于任务内部异常。
            gate = asyncio.Event()

            async def failing_coro() -> None:
                await gate.wait()
                raise RuntimeError("coro failure")

            bg = asyncio.create_task(failing_coro())
            outer_cancelled: list[asyncio.CancelledError | None] = []

            async def runner() -> None:
                outer_cancelled.append(await await_task_shielded(bg, on_cancel=gate.set))

            wrapper = asyncio.create_task(runner())
            await asyncio.sleep(0.005)
            wrapper.cancel()
            await wrapper
            assert isinstance(outer_cancelled[0], asyncio.CancelledError)
            assert bg.done()

            # 4) 等待被主动 cancel 的后台任务（无外部取消）时返回 None 且不得触发 on_cancel。
            on_cancel_calls: list[int] = []
            inner_started = asyncio.Event()

            async def victim() -> None:
                inner_started.set()
                await asyncio.sleep(30)

            victim_task = asyncio.create_task(victim())
            await inner_started.wait()
            victim_task.cancel()
            outer_err = await await_task_shielded(
                victim_task,
                on_cancel=lambda: on_cancel_calls.append(1),
            )
            assert outer_err is None
            assert victim_task.cancelled()
            assert on_cancel_calls == []

            # 确认全程零 exception in shielded future 与未检索任务异常告警。
            await asyncio.sleep(0.02)
            gc.collect()
            assert loop_errors == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_skill_package_cache_multi_waiter_cancel_and_no_unretrieved_task_exceptions(
    tmp_path: Path,
) -> None:
    """验证多等待者首取消不影响其余等待者，且同 tick 取消+失败与 aclose 多异常零未检索告警。"""
    _, pkg_bytes, manifest = _build_skill_zip(
        tmp_path,
        filename="sf-cancel-race.zip",
        slug="sf-cancel-race",
    )

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_errors: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))
        try:
            cache = SkillPackageCache()

            # 1) 多等待者并发：等待者 1 取消，等待者 2 照常拿到结果且底层只回源 1 次。
            v_multi = _skill_version(
                version_id="v_multi",
                skill_id="s_multi",
                manifest=manifest,
                object_key="skills/multi/pkg.zip",
                package_bytes=pkg_bytes,
            )
            fetch_calls = 0
            gate = asyncio.Event()

            async def gated_fetch() -> bytes:
                nonlocal fetch_calls
                fetch_calls += 1
                await gate.wait()
                return pkg_bytes

            w1 = asyncio.create_task(cache.get_zip_bytes(v_multi, fetch_bytes=gated_fetch))
            w2 = asyncio.create_task(cache.get_zip_bytes(v_multi, fetch_bytes=gated_fetch))
            await asyncio.sleep(0.01)
            w1.cancel()
            with pytest.raises(asyncio.CancelledError):
                await w1
            gate.set()
            assert await w2 == pkg_bytes
            assert fetch_calls == 1

            # 2) 单等待者取消与回源失败同 tick 竞态：不得产生 Task exception was never retrieved。
            v_race = _skill_version(
                version_id="v_race",
                skill_id="s_race",
                manifest=manifest,
                object_key="skills/race/pkg.zip",
                package_bytes=pkg_bytes,
            )
            race_gate = asyncio.Event()

            async def failing_fetch() -> bytes:
                await race_gate.wait()
                raise RuntimeError("transient_storage_failure")

            waiter = asyncio.create_task(cache.get_zip_bytes(v_race, fetch_bytes=failing_fetch))
            await asyncio.sleep(0.01)
            # 同 tick 同时唤醒回源失败与取消等待者。
            race_gate.set()
            waiter.cancel()
            with pytest.raises((asyncio.CancelledError, RuntimeError)):
                await waiter

            # 3) aclose 面对多个在途失败任务，且 aclose 期间再受外部取消：
            #    全部任务收敛、异常被完全消费且 aclose 最终重抛外部 CancelledError。
            close_gate = asyncio.Event()
            started = 0

            async def slow_fail_on_cancel() -> bytes:
                nonlocal started
                started += 1
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    await close_gate.wait()
                    raise RuntimeError("cleanup_failed_in_inflight") from None
                return pkg_bytes

            bg_waiters: list[asyncio.Task[bytes]] = []
            for idx in range(2):
                v_bg = _skill_version(
                    version_id=f"v_bg_{idx}",
                    skill_id=f"s_bg_{idx}",
                    manifest=manifest,
                    object_key=f"skills/bg/{idx}.zip",
                    package_bytes=pkg_bytes,
                )
                bg_waiters.append(
                    asyncio.create_task(cache.get_zip_bytes(v_bg, fetch_bytes=slow_fail_on_cancel))
                )
            while started < 2:
                await asyncio.sleep(0.005)

            aclose_task = asyncio.create_task(cache.aclose())
            await asyncio.sleep(0.01)
            aclose_task.cancel()
            await asyncio.sleep(0.01)
            assert not aclose_task.done()
            close_gate.set()
            with pytest.raises(asyncio.CancelledError):
                await aclose_task
            await asyncio.gather(*bg_waiters, return_exceptions=True)

            # 强制 GC 回收全部已完成任务，确认事件循环零未检索异常告警。
            await asyncio.sleep(0.02)
            gc.collect()
            assert loop_errors == []
        finally:
            loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())
