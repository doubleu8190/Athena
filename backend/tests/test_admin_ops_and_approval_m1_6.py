"""M1.6 管理后台核心运维与工具业务化审批映射端到端测试套件。"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pg_unit_harness import create_pg_unit_db
from sqlalchemy import DateTime, func, literal, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from adapter.access import ResourceVisibilityGrantModel, SqlAlchemyAccessControlRepository
from adapter.auth.model import AuthSessionModel, UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.llm.model.profile import ModelProfileVersionModel
from adapter.llm.repository import SqlAlchemyModelProfileRepository
from adapter.mcp.model import McpServerModel, McpToolModel
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.message.repository.message import SqlAlchemyMessageRepository
from adapter.org.model import DepartmentModel, UserDepartmentModel
from adapter.run.model import RunModel
from adapter.run.repository import SqlAlchemyRunRepository
from adapter.session.model import SessionModel
from adapter.session.repository import SqlAlchemySessionRepository
from adapter.subagent.model import SubagentModel
from adapter.system_prompt.model import SystemPromptSettingModel, SystemPromptVersionModel
from app.access import AccessPolicyService
from app.auth import AuthService
from app.llm import ModelProfileService
from app.run.service import RunService
from app.tool import ToolManager
from app.tool.runtime import RunToolRuntimeService
from common.ids import new_id
from domain.auth import UserRole, UserStatus
from domain.mcp import (
    DiscoveredMcpTool,
    McpCallResult,
    McpCallStatus,
    McpConnectionInput,
    McpRuntimeToolTarget,
)
from domain.message import (
    ExecutionMessageDraft,
    ExecutionRole,
    MessageMetadata,
    TextPart,
    ToolCallPart,
)
from domain.org import DepartmentStatus
from domain.run.model import (
    BeginSubagentBatchCommand,
    BeginSubagentBatchItem,
    RunStatus,
)
from domain.skill import (
    SKILL_EXECUTE_TOOL_ID,
    SafeSkillApprovalProjection,
    SkillParameterSummaryItem,
)
from domain.tool import (
    AuthorizedRunContext,
    BeginToolCallBatchCommand,
    BeginToolCallBatchItem,
    InitializeRunToolsCommand,
    RuntimeToolSnapshot,
    ToolExecutionContext,
    ToolKind,
)
from infrastructure.security.mcp_secrets import McpSecretCipher
from main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "backend" / "alembic.ini"
NOW = datetime(2026, 12, 4, 10, 0, tzinfo=UTC)
SECRET_KEY = b"7" * 32


def _run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """在同步测试函数中运行异步协程。"""
    return asyncio.run(coroutine)


class _FakePasswordHasher:
    """确定性快速密码校验器，用于建立真实 Cookie 登录态。"""

    dummy_hash = "fake:dummy"

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash == f"fake:{password}"


class _MutableClock:
    """可控单调时钟，用于断言 updated_at 与版本号刷新。"""

    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int = 5) -> datetime:
        self.current = self.current + timedelta(seconds=seconds)
        return self.current


class _ScriptedDiscovery:
    """可编程 MCP 远端发现替身，记录收到的连接配置与凭据并在需要时触发并发钩子。"""

    def __init__(
        self,
        tools: Sequence[DiscoveredMcpTool],
        *,
        on_discover: Any = None,
    ) -> None:
        self.tools = tuple(tools)
        self.on_discover = on_discover
        self.calls: list[tuple[Any, dict[str, str]]] = []

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        self.calls.append((connection.config, dict(connection.secrets)))
        if self.on_discover is not None:
            result = self.on_discover(connection)
            if asyncio.iscoroutine(result):
                await result
        return self.tools


class _RecordingMcpExecutor:
    """记录远端调用次数的 MCP 执行器替身。"""

    def __init__(self) -> None:
        self.calls: list[tuple[McpRuntimeToolTarget, dict[str, object]]] = []

    async def call(
        self,
        target: McpRuntimeToolTarget,
        arguments: dict[str, object],
        *,
        deadline_seconds: float,
    ) -> McpCallResult:
        del deadline_seconds
        self.calls.append((target, dict(arguments)))
        return McpCallResult(McpCallStatus.OK, "mcp_tool_completed", "ok", text="ok")


class _M16Harness:
    """共享 PostgreSQL 异步引擎、全量仓储、服务与 FastAPI 应用的端到端测试夹具。"""

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        clock: _MutableClock,
        cipher: McpSecretCipher,
        discovery: _ScriptedDiscovery,
        mcp_executor: _RecordingMcpExecutor,
        profile_repo: SqlAlchemyModelProfileRepository,
        mcp_repo: SqlAlchemyMcpRepository,
        run_repo: SqlAlchemyRunRepository,
        tool_runtime: RunToolRuntimeService,
        access_policy: AccessPolicyService,
        app: Any,
    ) -> None:
        self.engine = engine
        self.session_factory = session_factory
        self.clock = clock
        self.cipher = cipher
        self.discovery = discovery
        self.mcp_executor = mcp_executor
        self.profile_repo = profile_repo
        self.mcp_repo = mcp_repo
        self.run_repo = run_repo
        self.tool_runtime = tool_runtime
        self.access_policy = access_policy
        self.app = app

    async def aclose(self) -> None:
        await self.engine.dispose()


async def _create_harness(
    monkeypatch: pytest.MonkeyPatch,
    initial_tools: Sequence[DiscoveredMcpTool] = (),
) -> _M16Harness:
    clock = _MutableClock(NOW)
    # 注入可控时钟表达式，确保测试中推进租约与时间断言可复现。
    monkeypatch.setattr(
        func,
        "clock_timestamp",
        lambda: literal(clock(), type_=DateTime(timezone=True)),
    )
    engine, session_factory = await create_pg_unit_db()
    cipher = McpSecretCipher(SECRET_KEY, key_version=1)

    auth_repo = SqlAlchemyAuthRepository(session_factory)
    auth_service = AuthService(
        auth_repo,
        _FakePasswordHasher(),
        clock=clock,
    )
    access_repo = SqlAlchemyAccessControlRepository(session_factory)
    access_policy = AccessPolicyService(access_repo, clock=clock)
    profile_repo = SqlAlchemyModelProfileRepository(session_factory, cipher, clock=clock)
    profile_service = ModelProfileService(profile_repo, auth_repo, clock=clock)
    mcp_repo = SqlAlchemyMcpRepository(session_factory, cipher, clock=clock)
    discovery = _ScriptedDiscovery(initial_tools)
    tool_manager = ToolManager(
        mcp_repo,
        discovery,
        auth_repo,
        access_policy=access_policy,
        clock=clock,
    )
    message_repo = SqlAlchemyMessageRepository(session_factory)
    run_repo = SqlAlchemyRunRepository(session_factory, message_repo)
    run_service = RunService(run_repo, clock=clock)
    session_repo = SqlAlchemySessionRepository(session_factory)
    mcp_executor = _RecordingMcpExecutor()
    tool_runtime = RunToolRuntimeService(
        auth=auth_repo,
        sessions=session_repo,
        mcp_catalog=mcp_repo,
        mcp_executor=mcp_executor,
        knowledge=None,
        project_knowledge=None,
        access_policy=access_policy,
        clock=clock,
    )

    # 预置组织部门、超级管理员、普通员工以及默认系统提示词。
    async with session_factory() as session, session.begin():
        session.add_all(
            [
                DepartmentModel(
                    id="dept_root",
                    external_id="ext_root",
                    name="总部",
                    parent_id=None,
                    path="/dept_root/",
                    order_index=0,
                    status=DepartmentStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                DepartmentModel(
                    id="dept_fin",
                    external_id="ext_fin",
                    name="财务部",
                    parent_id="dept_root",
                    path="/dept_root/dept_fin/",
                    order_index=1,
                    status=DepartmentStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                DepartmentModel(
                    id="dept_hr",
                    external_id="ext_hr",
                    name="人事部",
                    parent_id="dept_root",
                    path="/dept_root/dept_hr/",
                    order_index=2,
                    status=DepartmentStatus.ACTIVE.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                UserModel(
                    id="usr_admin",
                    email="admin@example.com",
                    password_hash="fake:AdminPass123!",
                    display_name="超管",
                    status=UserStatus.ACTIVE.value,
                    role=UserRole.SUPER_ADMIN.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                UserModel(
                    id="usr_member",
                    email="member@example.com",
                    password_hash="fake:MemberPass123!",
                    display_name="财务员工",
                    status=UserStatus.ACTIVE.value,
                    role=UserRole.MEMBER.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                UserModel(
                    id="usr_hr",
                    email="hr@example.com",
                    password_hash="fake:HrPass123!",
                    display_name="人事员工",
                    status=UserStatus.ACTIVE.value,
                    role=UserRole.MEMBER.value,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                SystemPromptVersionModel(
                    id="sp_main_1",
                    kind="main",
                    content="你是企业助手。",
                    created_by="usr_admin",
                    created_at=NOW,
                ),
                SystemPromptVersionModel(
                    id="sp_comp_1",
                    kind="compression",
                    content="请压缩上下文。",
                    created_by="usr_admin",
                    created_at=NOW,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                UserDepartmentModel(
                    user_id="usr_member",
                    department_id="dept_fin",
                    is_primary=True,
                ),
                UserDepartmentModel(
                    user_id="usr_hr",
                    department_id="dept_hr",
                    is_primary=True,
                ),
                SystemPromptSettingModel(
                    kind="main",
                    current_version_id="sp_main_1",
                ),
                SystemPromptSettingModel(
                    kind="compression",
                    current_version_id="sp_comp_1",
                ),
                SessionModel(
                    id="sess_member",
                    owner_id="usr_member",
                    project_id=None,
                    title="测试会话",
                    last_message_sequence=0,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=None,
                ),
            ]
        )

    app = create_app(
        auth_service=auth_service,
        model_profile_service=profile_service,
        tool_manager=tool_manager,
        run_service=run_service,
    )
    return _M16Harness(
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        cipher=cipher,
        discovery=discovery,
        mcp_executor=mcp_executor,
        profile_repo=profile_repo,
        mcp_repo=mcp_repo,
        run_repo=run_repo,
        tool_runtime=tool_runtime,
        access_policy=access_policy,
        app=app,
    )


def _login(client: TestClient, email: str, password: str) -> str:
    """执行登录并返回当前 auth_session_id。"""
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.cookies.get("athena_session") or "")


def _create_model_profile(
    client: TestClient,
    *,
    name: str = "通用对话模型",
    model: str = "gpt-4o",
    api_key: str = "sk-original-secret-123",
    scenario_label: str | None = "日常办公",
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/admin/llm/model-profiles",
        json={
            "name": name,
            "base_url": "https://llm.example.com/v1",
            "model": model,
            "api_key": api_key,
            "context_window_tokens": 128_000,
            "max_output_tokens": 4096,
            "temperature": 0.7,
            "visible_to_users": True,
            "scenario_label": scenario_label,
            "scenario_description": "适合通用文档起草与问答",
            "supports_vision": False,
        },
    )
    assert resp.status_code == 201, resp.text
    return cast(dict[str, Any], resp.json())


# ---------------------------------------------------------------------------
# 1. 模型配置可编辑、软删除、强引用保护与 Run 模型快照解耦
# ---------------------------------------------------------------------------


def test_model_profile_patch_preserves_or_rotates_secret_and_enforces_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """局部编辑保留/轮换密钥，并严格校验乐观锁版本冲突与全部领域不变量。"""
    h = _run(_create_harness(monkeypatch))
    try:
        with TestClient(h.app) as admin_client, TestClient(h.app) as member_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            _login(member_client, "member@example.com", "MemberPass123!")

            created = _create_model_profile(admin_client)
            profile_id = created["id"]
            assert created["version"] == 1
            assert created["updated_at"] == created["created_at"]
            assert "has_api_key" not in created

            # 普通用户无权编辑模型配置。
            forbidden = member_client.patch(
                f"/api/v1/admin/llm/model-profiles/{profile_id}",
                json={"name": "越权改名"},
            )
            assert forbidden.status_code == 403

            # 记录初始加密密文，用于断言不传 api_key / 传 null 时密文丝毫未动。
            async def _read_cipher_tuple() -> tuple[bytes | None, bytes | None, int | None]:
                async with h.session_factory() as s:
                    row = await s.get(ModelProfileVersionModel, profile_id)
                    assert row is not None
                    return row.api_key_ciphertext, row.api_key_nonce, row.api_key_key_version

            before_cipher = _run(_read_cipher_tuple())
            h.clock.advance(10)

            # 1) 不传 api_key，局部修改名称、温度与场景标签。
            patched_omit = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{profile_id}",
                json={
                    "expected_version": 1,
                    "name": "高阶推理模型",
                    "temperature": 0.2,
                    "scenario_label": "深度分析",
                    "supports_vision": True,
                },
            )
            assert patched_omit.status_code == 200, patched_omit.text
            body_1 = patched_omit.json()
            assert body_1["name"] == "高阶推理模型"
            assert body_1["temperature"] == 0.2
            assert body_1["scenario_label"] == "深度分析"
            assert body_1["supports_vision"] is True
            assert body_1["version"] == 2
            assert body_1["updated_at"] != body_1["created_at"]
            assert _run(_read_cipher_tuple()) == before_cipher
            rt_1 = _run(h.profile_repo.get_runtime(profile_id))
            assert rt_1 is not None and rt_1.api_key == "sk-original-secret-123"

            # 2) 显式传入 api_key: null，仍严格保留原加密凭据。
            h.clock.advance(10)
            patched_null = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{profile_id}",
                json={
                    "expected_version": 2,
                    "api_key": None,
                    "scenario_description": "深度合同审阅",
                },
            )
            assert patched_null.status_code == 200, patched_null.text
            assert patched_null.json()["version"] == 3
            assert _run(_read_cipher_tuple()) == before_cipher
            rt_2 = _run(h.profile_repo.get_runtime(profile_id))
            assert rt_2 is not None and rt_2.api_key == "sk-original-secret-123"

            # 3) 传入新 api_key 时重新加密替换，响应严禁包含密钥明文。
            h.clock.advance(10)
            patched_rotate = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{profile_id}",
                json={
                    "expected_version": 3,
                    "api_key": "sk-rotated-secret-999",
                },
            )
            assert patched_rotate.status_code == 200, patched_rotate.text
            assert "sk-rotated-secret-999" not in patched_rotate.text
            assert patched_rotate.json()["version"] == 4
            assert _run(_read_cipher_tuple()) != before_cipher
            rt_3 = _run(h.profile_repo.get_runtime(profile_id))
            assert rt_3 is not None and rt_3.api_key == "sk-rotated-secret-999"

            # 4) 乐观锁版本冲突返回 409 model_profile_version_conflict。
            conflict = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{profile_id}",
                json={"expected_version": 2, "name": "过期版本写入"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["detail"] == "model_profile_version_conflict"

            # 5) 空白或空字符串 api_key、空更新体、非空字段显式 null、违反领域不变量均拒绝 422。
            for bad_payload in (
                {"api_key": ""},
                {"api_key": "   \t "},
                {},
                {"expected_version": 4},
                {"name": None},
                {"max_output_tokens": 128_000},
                {"context_window_tokens": 4096},
                {"base_url": "http://insecure.example.com/v1"},
                {"supports_tools": False, "supports_parallel_tool_calls": True},
                {"scenario_label": "非法\x00标签"},
            ):
                bad_resp = admin_client.patch(
                    f"/api/v1/admin/llm/model-profiles/{profile_id}",
                    json=bad_payload,
                )
                assert bad_resp.status_code == 422, (bad_payload, bad_resp.text)

            # 6) 不存在的 profile 返回 404 model_profile_not_found。
            missing = admin_client.patch(
                "/api/v1/admin/llm/model-profiles/llmprof_missing",
                json={"name": "不存在"},
            )
            assert missing.status_code == 404
            assert missing.json()["detail"] == "model_profile_not_found"
    finally:
        _run(h.aclose())


def test_run_persists_secret_free_model_snapshot_decoupled_from_edits_and_soft_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主 Run、重试 Run 与子 Run 均固化不含密钥的模型快照，后续编辑/软删不破坏历史快照与外键。"""
    h = _run(_create_harness(monkeypatch))
    try:
        with TestClient(h.app) as admin_client, TestClient(h.app) as member_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            _login(member_client, "member@example.com", "MemberPass123!")

            default_prof = _create_model_profile(
                admin_client,
                name="默认模型",
                model="gpt-4o-mini",
                api_key="sk-default-key",
                scenario_label="默认通用",
            )
            custom_prof = _create_model_profile(
                admin_client,
                name="财务专用模型v1",
                model="gpt-4o-finance",
                api_key="sk-finance-secret-xyz",
                scenario_label="财税分析",
            )
            assert (
                admin_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": default_prof["id"]},
                ).status_code
                == 200
            )

            # 提交主 Run，指定 custom_prof。
            submitted = member_client.post(
                "/api/v1/sessions/sess_member/runs",
                json={
                    "request_id": "req-snap-1",
                    "text": "请分析本月财务报表",
                    "model_profile_id": custom_prof["id"],
                },
            )
            assert submitted.status_code == 201, submitted.text
            run_body = submitted.json()
            run_id = run_body["id"]
            assert run_body["model_snapshot"] == {
                "id": custom_prof["id"],
                "name": "财务专用模型v1",
                "model": "gpt-4o-finance",
                "scenario_label": "财税分析",
                "supports_vision": False,
                "version": 1,
            }
            # 普通用户响应绝不暴露 base_url 或任何密钥。
            assert "base_url" not in submitted.text
            assert "sk-finance-secret-xyz" not in submitted.text

            # 检查数据库中 runs.model_snapshot_json 包含完整非密钥元数据且无任何 api_key 键。
            async def _read_run_snapshot(target_run_id: str) -> dict[str, object] | None:
                async with h.session_factory() as s:
                    row = await s.get(RunModel, target_run_id)
                    assert row is not None
                    return row.model_snapshot_json

            db_snap = _run(_read_run_snapshot(run_id))
            assert db_snap is not None
            assert db_snap["id"] == custom_prof["id"]
            assert db_snap["name"] == "财务专用模型v1"
            assert db_snap["base_url"] == "https://llm.example.com/v1"
            assert db_snap["model"] == "gpt-4o-finance"
            assert db_snap["version"] == 1
            for forbidden_key in (
                "api_key",
                "api_key_ciphertext",
                "api_key_nonce",
                "api_key_key_version",
            ):
                assert forbidden_key not in db_snap
            assert "sk-finance-secret-xyz" not in str(db_snap)

            # 编辑模型名称与场景标签至 version=2。
            h.clock.advance(5)
            edited = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{custom_prof['id']}",
                json={
                    "name": "财务专用模型v2-已更名",
                    "scenario_label": "新版标签",
                },
            )
            assert edited.status_code == 200
            assert edited.json()["version"] == 2

            # 历史 Run 读取仍展示创建时固化的 v1 快照，不受后续编辑影响。
            detail_after_edit = member_client.get(f"/api/v1/sessions/sess_member/runs/{run_id}")
            assert detail_after_edit.status_code == 200
            assert detail_after_edit.json()["model_snapshot"]["name"] == "财务专用模型v1"
            assert detail_after_edit.json()["model_snapshot"]["version"] == 1

            # 模拟主 Run 失败并重试，验证重试 Run 固化最新 version=2 的快照。
            async def _mark_run_failed_and_seed_subagent() -> str:
                async with h.session_factory() as s, s.begin():
                    run_row = await s.get(RunModel, run_id)
                    assert run_row is not None
                    run_row.status = RunStatus.FAILED.value
                    run_row.safe_error_code = "upstream_timeout"
                    run_row.finished_at = h.clock()
                    sub = SubagentModel(
                        id="sub_fin",
                        slug="fin_analyst",
                        name="财务专家",
                        description="财务分析子智能体",
                        system_prompt="你是财务专家",
                        model_profile_id=custom_prof["id"],
                        allowed_tool_ids=[],
                        enabled=True,
                        visibility_scope="company",
                        owner_department_id=None,
                        version=1,
                        created_by="usr_admin",
                        deleted_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                    s.add(sub)
                return "sub_fin"

            _run(_mark_run_failed_and_seed_subagent())

            retried = member_client.post(
                f"/api/v1/sessions/sess_member/runs/{run_id}/retry",
                json={"request_id": "req-retry-snap-1"},
            )
            assert retried.status_code == 200, retried.text
            retry_run_id = retried.json()["id"]
            assert retried.json()["model_snapshot"]["name"] == "财务专用模型v2-已更名"
            assert retried.json()["model_snapshot"]["version"] == 2

            # 认领重试 Run 并派发子 Run，验证子 Run 同样固化 model_snapshot_json。
            async def _dispatch_child_run() -> str:
                leases = await h.run_repo.claim(
                    "worker-1", lease_seconds=60, execution_seconds=120, limit=5
                )
                lease = next(item for item in leases if item.run.id == retry_run_id)
                sub_run_id = new_id("run")
                call_msg_id = new_id("exec")
                batch_res = await h.run_repo.begin_subagent_batch(
                    BeginSubagentBatchCommand(
                        run_id=retry_run_id,
                        worker_id="worker-1",
                        execution_epoch=lease.run.execution_epoch,
                        tool_call_id="tc_disp_1",
                        call_message=ExecutionMessageDraft(
                            id=call_msg_id,
                            session_id="sess_member",
                            owner_id="usr_member",
                            run_id=retry_run_id,
                            thread_id=retry_run_id,
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    tool_call_id="tc_disp_1",
                                    name="dispatch_subagents",
                                    arguments={
                                        "tasks": [
                                            {"subagent_slug": "fin_analyst", "task": "核对损益表"}
                                        ]
                                    },
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id=None,
                            created_at=h.clock(),
                            metadata=MessageMetadata(),
                        ),
                        record_id=new_id("rtc"),
                        items=(
                            BeginSubagentBatchItem(
                                sub_run_id=sub_run_id,
                                subagent_profile_id="sub_fin",
                                subagent_slug="fin_analyst",
                                subagent_name="财务专家",
                                system_prompt="你是财务专家",
                                model_profile_id=custom_prof["id"],
                                tool_snapshot=RuntimeToolSnapshot(),
                                skill_snapshot=None,
                                input_file_ids=(),
                                task_message=ExecutionMessageDraft(
                                    id=new_id("exec"),
                                    session_id="sess_member",
                                    owner_id="usr_member",
                                    run_id=sub_run_id,
                                    thread_id=sub_run_id,
                                    role=ExecutionRole.USER,
                                    parts=(TextPart("任务：\n核对损益表"),),
                                    tool_call_id=None,
                                    source_message_id=None,
                                    created_at=h.clock(),
                                    metadata=MessageMetadata(),
                                ),
                            ),
                        ),
                        requested_at=h.clock(),
                    )
                )
                assert len(batch_res.sub_runs) == 1
                assert batch_res.sub_runs[0].model_snapshot is not None
                assert batch_res.sub_runs[0].model_snapshot.name == "财务专用模型v2-已更名"
                return sub_run_id

            sub_run_id = _run(_dispatch_child_run())
            sub_db_snap = _run(_read_run_snapshot(sub_run_id))
            assert sub_db_snap is not None
            assert sub_db_snap["id"] == custom_prof["id"]
            assert "api_key" not in sub_db_snap
    finally:
        _run(h.aclose())


def test_model_profile_soft_delete_enforces_strong_reference_protections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除默认模型、压缩策略模型与活跃子智能体绑定模型均返回对应 409；闲置模型可安全软删除。"""
    h = _run(_create_harness(monkeypatch))
    try:
        with TestClient(h.app) as admin_client, TestClient(h.app) as member_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            _login(member_client, "member@example.com", "MemberPass123!")

            p_default = _create_model_profile(admin_client, name="默认模型", model="m-default")
            p_comp = _create_model_profile(admin_client, name="压缩模型", model="m-comp")
            p_sub = _create_model_profile(admin_client, name="子智能体模型", model="m-sub")
            p_idle = _create_model_profile(admin_client, name="闲置模型", model="m-idle")

            # 设置默认模型、当前压缩策略和活跃子智能体强引用。
            assert (
                admin_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": p_default["id"]},
                ).status_code
                == 200
            )
            assert (
                admin_client.put(
                    "/api/v1/admin/llm/compression-policy",
                    json={"compression_model_profile_id": p_comp["id"]},
                ).status_code
                == 200
            )

            async def _bind_subagent() -> None:
                async with h.session_factory() as s, s.begin():
                    s.add(
                        SubagentModel(
                            id="sub_bound",
                            slug="bound_agent",
                            name="绑定专家",
                            description="绑定测试模型",
                            system_prompt="系统提示",
                            model_profile_id=p_sub["id"],
                            allowed_tool_ids=[],
                            enabled=True,
                            visibility_scope="company",
                            owner_department_id=None,
                            version=1,
                            created_by="usr_admin",
                            deleted_at=None,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )

            _run(_bind_subagent())

            # 1) 删除默认模型 -> 409 model_profile_in_use_as_default
            del_def = admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_default['id']}")
            assert del_def.status_code == 409
            assert del_def.json()["detail"] == "model_profile_in_use_as_default"

            # 2) 删除当前压缩策略模型 -> 409 model_profile_in_use_by_compression_policy
            del_comp = admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_comp['id']}")
            assert del_comp.status_code == 409
            assert del_comp.json()["detail"] == "model_profile_in_use_by_compression_policy"

            # 3) 删除活跃子智能体绑定模型 -> 409 model_profile_in_use_by_subagent
            del_sub = admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_sub['id']}")
            assert del_sub.status_code == 409
            assert del_sub.json()["detail"] == "model_profile_in_use_by_subagent"

            # 子智能体软删除后，该模型解除强引用保护，可正常软删除。
            async def _soft_delete_subagent() -> None:
                async with h.session_factory() as s, s.begin():
                    row = await s.get(SubagentModel, "sub_bound")
                    assert row is not None
                    row.deleted_at = h.clock()

            _run(_soft_delete_subagent())
            assert (
                admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_sub['id']}").status_code
                == 204
            )

            # 4) 先用闲置模型创建一个历史 Run，再软删除该模型：软删成功且不破坏 runs 外键。
            run_resp = member_client.post(
                "/api/v1/sessions/sess_member/runs",
                json={
                    "request_id": "req-idle-fk",
                    "text": "测试外键完好",
                    "model_profile_id": p_idle["id"],
                },
            )
            assert run_resp.status_code == 201
            del_idle = admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_idle['id']}")
            assert del_idle.status_code == 204

            # 软删除后在管理员列表和普通用户模型目录均不可见。
            admin_ids = {
                item["id"]
                for item in admin_client.get("/api/v1/admin/llm/model-profiles").json()["items"]
            }
            user_ids = {item["id"] for item in member_client.get("/api/v1/models").json()["items"]}
            assert p_idle["id"] not in admin_ids
            assert p_idle["id"] not in user_ids

            # 重复删除返回 404 model_profile_not_found。
            del_again = admin_client.delete(f"/api/v1/admin/llm/model-profiles/{p_idle['id']}")
            assert del_again.status_code == 404
            assert del_again.json()["detail"] == "model_profile_not_found"

            # 数据库行仍保留（deleted_at 非空、enabled=False），历史 Run 仍完整可读。
            async def _verify_soft_deleted_row() -> None:
                async with h.session_factory() as s:
                    row = await s.get(ModelProfileVersionModel, p_idle["id"])
                    assert row is not None
                    assert row.deleted_at is not None
                    assert row.enabled is False
                    assert row.version == 2
                    run_row = await s.get(RunModel, run_resp.json()["id"])
                    assert run_row is not None
                    assert run_row.model_config_version_id == p_idle["id"]

            _run(_verify_soft_deleted_row())
    finally:
        _run(h.aclose())


def test_model_profile_patch_protects_active_default_and_compression_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """禁止通过 PATCH 破坏当前默认模型的流式/工具/启用能力，或禁用当前压缩策略模型。"""
    from adapter.run.repository._shared import _is_skill_tool_call

    h = _run(_create_harness(monkeypatch))
    try:
        with TestClient(h.app) as admin_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            p_default = _create_model_profile(admin_client, name="默认模型", model="m-default")
            p_comp = _create_model_profile(admin_client, name="压缩模型", model="m-comp")

            assert (
                admin_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": p_default["id"]},
                ).status_code
                == 200
            )
            assert (
                admin_client.put(
                    "/api/v1/admin/llm/compression-policy",
                    json={"compression_model_profile_id": p_comp["id"]},
                ).status_code
                == 200
            )

            # 1) 默认模型禁止通过 PATCH 关闭流式、关闭工具或禁用，均返回 409。
            for payload in (
                {"supports_stream": False},
                {"supports_tools": False},
                {"enabled": False},
            ):
                resp = admin_client.patch(
                    f"/api/v1/admin/llm/model-profiles/{p_default['id']}",
                    json=payload,
                )
                assert resp.status_code == 409, (payload, resp.text)
                assert resp.json()["detail"] == "model_profile_in_use_as_default"

            # 默认模型的无害属性编辑仍可正常成功（version 从 1 递增为 2）。
            safe_default = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{p_default['id']}",
                json={"expected_version": 1, "name": "默认模型-已重命名"},
            )
            assert safe_default.status_code == 200, safe_default.text
            assert safe_default.json()["version"] == 2
            assert safe_default.json()["enabled"] is True
            assert safe_default.json()["supports_stream"] is True
            assert safe_default.json()["supports_tools"] is True

            # 2) 当前压缩策略引用的模型禁止通过 PATCH 禁用，返回 409。
            comp_disable = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{p_comp['id']}",
                json={"enabled": False},
            )
            assert comp_disable.status_code == 409, comp_disable.text
            assert comp_disable.json()["detail"] == "model_profile_in_use_by_compression_policy"

            # 非默认压缩模型关闭 stream/tools 不影响压缩调用，可正常更新。
            comp_non_default = admin_client.patch(
                f"/api/v1/admin/llm/model-profiles/{p_comp['id']}",
                json={"expected_version": 1, "supports_stream": False, "supports_tools": False},
            )
            assert comp_non_default.status_code == 200, comp_non_default.text
            assert comp_non_default.json()["version"] == 2
            assert comp_non_default.json()["enabled"] is True

            # 3) 确认既有 PUT /enabled 行为保持向后兼容，不受 PATCH 限制影响。
            legacy_toggle = admin_client.put(
                f"/api/v1/admin/llm/model-profiles/{p_default['id']}/enabled",
                json={"enabled": False},
            )
            assert legacy_toggle.status_code == 200, legacy_toggle.text
            assert legacy_toggle.json()["enabled"] is False

        # 顺手回归校验：显式 MCP 工具不做 Skill 名称后缀误判，但保留显式 Skill tool_id 兜底。
        assert (
            _is_skill_tool_call("tool_mcp_1", "custom_skill_execute", ToolKind.MCP.value) is False
        )
        assert _is_skill_tool_call(SKILL_EXECUTE_TOOL_ID, "mcp__abc", ToolKind.MCP.value) is True
        assert _is_skill_tool_call(None, "builtin_skill_execute", ToolKind.BUILTIN.value) is True
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 2. MCP Server 保密局部更新、启停、独立重发现与软删除
# ---------------------------------------------------------------------------


def test_mcp_patch_preserves_secrets_and_rediscover_preserves_tool_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PATCH 保密局部更新完整保留旧密钥；rediscover 保留存活工具 ID/审批/中文映射并增删变更工具。"""
    initial_tools = (
        DiscoveredMcpTool(
            remote_name="query_invoice",
            description="查询发票 v1",
            input_schema={
                "type": "object",
                "properties": {"invoice_no": {"type": "string"}},
            },
        ),
        DiscoveredMcpTool(
            remote_name="legacy_export",
            description="旧版导出",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    h = _run(_create_harness(monkeypatch, initial_tools))
    try:
        with TestClient(h.app) as admin_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")

            # 注册含多组加密凭据的 HTTP MCP Server。
            reg = admin_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "ERP Connector",
                    "transport": "streamable_http",
                    "url": "https://erp.example.com/mcp",
                    "secrets": {
                        "Authorization": "Bearer secret-alpha-111",
                        "X-Tenant-Key": "tenant-secret-222",
                        "X-Legacy-Token": "legacy-secret-333",
                    },
                    "visibility_scope": "company",
                },
            )
            assert reg.status_code == 201, reg.text
            server = reg.json()
            server_id = server["id"]
            assert server["enabled"] is True
            assert server["config_version"] == 1
            assert "secret-alpha-111" not in reg.text

            # 为 query_invoice 配置审批开关及中文业务展示元信息。
            tools_before = admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}/tools").json()[
                "items"
            ]
            invoice_tool = next(t for t in tools_before if t["remote_name"] == "query_invoice")
            meta_resp = admin_client.patch(
                f"/api/v1/admin/tools/{invoice_tool['tool_id']}",
                json={
                    "requires_approval": True,
                    "display_name": "查询ERP发票",
                    "approval_summary_template": "查询发票 {invoice_no}",
                    "parameter_labels": {"invoice_no": "发票号码"},
                },
            )
            assert meta_resp.status_code == 200, meta_resp.text

            # 1) PATCH 仅改名、改 URL 与部门可见性，不传密钥且 rediscover=False：
            #    既有密钥完整保留，工具目录与审批元信息不受影响。
            patch_no_secret = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": 1,
                    "name": "ERP Connector v2",
                    "config": {"url": "https://erp-v2.example.com/mcp"},
                    "visibility_scope": "department",
                    "owner_department_id": "dept_fin",
                },
            )
            assert patch_no_secret.status_code == 200, patch_no_secret.text
            p1 = patch_no_secret.json()
            assert p1["config_version"] == 2
            assert p1["name"] == "ERP Connector v2"
            assert p1["config"] == {"url": "https://erp-v2.example.com/mcp"}
            assert p1["visibility_scope"] == "department"
            assert p1["owner_department_id"] == "dept_fin"
            assert len(h.discovery.calls) == 1  # 未触发 rediscover

            state_1 = _run(h.mcp_repo.get_connection_state(server_id))
            assert state_1 is not None
            assert dict(state_1.secrets) == {
                "Authorization": "Bearer secret-alpha-111",
                "X-Tenant-Key": "tenant-secret-222",
                "X-Legacy-Token": "legacy-secret-333",
            }

            # 2) secrets_upsert 与 secrets_remove 交集键被拒绝 422（含 HTTP Header 大小写交集）。
            overlap_resp = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": 2,
                    "secrets_upsert": {"X-Legacy-Token": "new-val"},
                    "secrets_remove": ["x-legacy-token"],
                },
            )
            assert overlap_resp.status_code == 422
            assert "new-val" not in overlap_resp.text

            # 3) 精确局部增删改密钥，校验未提及密钥保留；版本冲突返回 409。
            stale_patch = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={"expected_config_version": 1, "name": "stale"},
            )
            assert stale_patch.status_code == 409
            assert stale_patch.json()["detail"] == "mcp_config_version_conflict"

            patch_secrets = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": 2,
                    "secrets_upsert": {
                        "X-Tenant-Key": "tenant-secret-rotated",
                        "X-Extra-Key": "extra-secret-444",
                    },
                    "secrets_remove": ["X-Legacy-Token"],
                },
            )
            assert patch_secrets.status_code == 200, patch_secrets.text
            assert patch_secrets.json()["config_version"] == 3
            assert "tenant-secret-rotated" not in patch_secrets.text
            assert "extra-secret-444" not in patch_secrets.text

            state_2 = _run(h.mcp_repo.get_connection_state(server_id))
            assert state_2 is not None
            assert dict(state_2.secrets) == {
                "Authorization": "Bearer secret-alpha-111",
                "X-Tenant-Key": "tenant-secret-rotated",
                "X-Extra-Key": "extra-secret-444",
            }

            # 4) 远端工具发生变更：query_invoice 描述更新、legacy_export 删除、新增 create_voucher。
            h.discovery.tools = (
                DiscoveredMcpTool(
                    remote_name="query_invoice",
                    description="查询发票 v2（新增币种）",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "invoice_no": {"type": "string"},
                            "currency": {"type": "string"},
                        },
                    },
                ),
                DiscoveredMcpTool(
                    remote_name="create_voucher",
                    description="生成财务凭证",
                    input_schema={"type": "object", "properties": {"amount": {"type": "number"}}},
                ),
            )
            rediscover_resp = admin_client.post(f"/api/v1/admin/mcp/servers/{server_id}/rediscover")
            assert rediscover_resp.status_code == 200, rediscover_resp.text
            assert rediscover_resp.json()["config_version"] == 4
            # 验证 rediscover 使用了数据库中解密的合并凭据。
            assert h.discovery.calls[-1][1] == {
                "Authorization": "Bearer secret-alpha-111",
                "X-Tenant-Key": "tenant-secret-rotated",
                "X-Extra-Key": "extra-secret-444",
            }

            tools_after = admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}/tools").json()[
                "items"
            ]
            by_remote = {item["remote_name"]: item for item in tools_after}
            assert set(by_remote) == {"query_invoice", "create_voucher"}
            # 同名存活工具保留原 tool_id、model_name、审批开关及中文元信息，仅刷新描述与 schema。
            kept = by_remote["query_invoice"]
            assert kept["tool_id"] == invoice_tool["tool_id"]
            assert kept["model_name"] == invoice_tool["model_name"]
            assert kept["description"] == "查询发票 v2（新增币种）"
            assert "currency" in kept["input_schema"]["properties"]
            assert kept["requires_approval"] is True
            assert kept["display_name"] == "查询ERP发票"
            assert kept["approval_summary_template"] == "查询发票 {invoice_no}"
            assert kept["parameter_labels"] == {"invoice_no": "发票号码"}

            # 新增工具默认免审批、展示字段为空。
            added = by_remote["create_voucher"]
            assert added["requires_approval"] is False
            assert added["display_name"] is None
            assert added["approval_summary_template"] is None
            assert added["parameter_labels"] == {}

            # 5) rediscover 期间若发生并发配置修改，CAS 保护返回 409 版本冲突。
            async def _bump_version_during_discovery(_conn: McpConnectionInput) -> None:
                async with h.session_factory() as s, s.begin():
                    row = await s.get(McpServerModel, server_id)
                    assert row is not None
                    row.config_version += 1

            h.discovery.on_discover = _bump_version_during_discovery
            cas_conflict = admin_client.post(f"/api/v1/admin/mcp/servers/{server_id}/rediscover")
            h.discovery.on_discover = None
            assert cas_conflict.status_code == 409
            assert cas_conflict.json()["detail"] == "mcp_config_version_conflict"

            # 6) 验证 PATCH 带 rediscover=True 使用合并后配置与密钥同步工具并保留存活元信息。
            latest_ver = admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}").json()[
                "config_version"
            ]
            patch_rediscover = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": latest_ver,
                    "secrets_upsert": {"Authorization": "Bearer secret-beta-555"},
                    "rediscover": True,
                },
            )
            assert patch_rediscover.status_code == 200, patch_rediscover.text
            assert h.discovery.calls[-1][1]["Authorization"] == "Bearer secret-beta-555"
            kept_after_patch = admin_client.get(
                f"/api/v1/admin/tools/{invoice_tool['tool_id']}"
            ).json()
            assert kept_after_patch["display_name"] == "查询ERP发票"
            assert kept_after_patch["requires_approval"] is True
    finally:
        _run(h.aclose())


def test_mcp_stdio_placeholder_check_enabled_toggle_runtime_cutoff_and_soft_delete_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 stdio 占位符校验、PUT /enabled 立即摘除在途 Run 调用与公共目录，以及软删后同名复用。"""
    initial_tools = (
        DiscoveredMcpTool(
            remote_name="run_report",
            description="生成财务报表",
            input_schema={"type": "object", "properties": {"month": {"type": "string"}}},
        ),
    )
    h = _run(_create_harness(monkeypatch, initial_tools))
    try:
        with TestClient(h.app) as admin_client, TestClient(h.app) as member_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            _login(member_client, "member@example.com", "MemberPass123!")

            reg = admin_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "ReportRunner",
                    "transport": "stdio",
                    "command": "/opt/mcp/report",
                    "args": ["--token=${REPORT_TOKEN}"],
                    "cwd": "/opt/mcp",
                    "secrets": {
                        "REPORT_TOKEN": "tok-123",
                        "AUX_FLAG": "yes",
                    },
                    "visibility_scope": "company",
                },
            )
            assert reg.status_code == 201, reg.text
            server_id = reg.json()["id"]

            # 删除仍被 ${REPORT_TOKEN} 引用的密钥必须拒绝 422。
            bad_remove = admin_client.patch(
                f"/api/v1/admin/mcp/servers/{server_id}",
                json={
                    "expected_config_version": 1,
                    "secrets_remove": ["REPORT_TOKEN"],
                },
            )
            assert bad_remove.status_code == 422

            # 先读取运行态工具快照（模拟在途 Run 已冻结该工具）。
            member_snapshot = _run(h.access_policy.get_user_snapshot("usr_member"))
            assert member_snapshot is not None
            runtime_tools_before = _run(
                h.mcp_repo.list_readable_runtime_tools(member_snapshot, limit=201)
            )
            assert len(runtime_tools_before) == 1
            frozen_def = runtime_tools_before[0]
            tool_id = frozen_def.tool_id
            assert member_client.get("/api/v1/tools").json()["total"] == 1
            assert member_client.get(f"/api/v1/tools/{tool_id}").status_code == 200

            # 查询当前 member 的活跃登录会话 ID，供 RunToolRuntimeService 鉴权使用。
            async def _active_session_id() -> str:
                async with h.session_factory() as s:
                    sid = await s.scalar(
                        select(AuthSessionModel.id).where(AuthSessionModel.user_id == "usr_member")
                    )
                    assert sid is not None
                    return sid

            member_auth_sid = _run(_active_session_id())
            auth_ctx = AuthorizedRunContext(
                owner_id="usr_member",
                chat_session_id="sess_member",
                project_id=None,
                auth_session_id=member_auth_sid,
            )
            exec_ctx = ToolExecutionContext(
                owner_id="usr_member",
                chat_session_id="sess_member",
                project_id=None,
                run_id="run_inflight",
                execution_epoch=1,
                auth_session_id=member_auth_sid,
                deadline_at=h.clock() + timedelta(seconds=60),
            )

            # 1) 停用 MCP Server：公共与运行态目录摘除，在途 Run 准备与执行均立刻失效。
            disabled = admin_client.put(
                f"/api/v1/admin/mcp/servers/{server_id}/enabled",
                json={"enabled": False, "expected_config_version": 1},
            )
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["enabled"] is False
            assert disabled.json()["config_version"] == 2

            assert _run(h.mcp_repo.list_readable_runtime_tools(member_snapshot, limit=201)) == ()
            assert member_client.get("/api/v1/tools").json()["total"] == 0
            assert member_client.get(f"/api/v1/tools/{tool_id}").status_code == 404
            # 管理员目录仍可见停用 Server 及其工具以便运维。
            assert admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}").status_code == 200
            assert admin_client.get(f"/api/v1/admin/tools/{tool_id}").status_code == 200

            # 在途 Run 拿着停用前冻结的工具定义调用 prepare_call 与 execute，均立刻拦截。
            prep = _run(
                h.tool_runtime.prepare_call(
                    frozen_def,
                    frozen_def.model_name,
                    {"month": "2026-11"},
                    cast(Any, object()),
                    auth_ctx,
                    call_id="tc_disabled_1",
                )
            )
            assert prep.outcome is not None
            assert prep.outcome.code == "tool_unavailable"
            exec_res = _run(
                h.tool_runtime.execute(
                    frozen_def,
                    frozen_def.model_name,
                    {"month": "2026-11"},
                    exec_ctx,
                    deadline_seconds=10.0,
                    run_remaining_seconds=30.0,
                )
            )
            assert exec_res.outcome.code == "tool_unavailable"
            assert h.mcp_executor.calls == []

            # 2) 重新启用 MCP Server 后立即恢复可见与可执行。
            stale_enable = admin_client.put(
                f"/api/v1/admin/mcp/servers/{server_id}/enabled",
                json={"enabled": True, "expected_config_version": 1},
            )
            assert stale_enable.status_code == 409
            assert stale_enable.json()["detail"] == "mcp_config_version_conflict"

            reenabled = admin_client.put(
                f"/api/v1/admin/mcp/servers/{server_id}/enabled",
                json={"enabled": True, "expected_config_version": 2},
            )
            assert reenabled.status_code == 200
            assert reenabled.json()["enabled"] is True
            assert reenabled.json()["config_version"] == 3
            assert (
                len(_run(h.mcp_repo.list_readable_runtime_tools(member_snapshot, limit=201))) == 1
            )
            assert member_client.get("/api/v1/tools").json()["total"] == 1

            # 3) 软删除 Server：清空密文、删除工具与授权，并允许使用相同 name 重新注册。
            deleted = admin_client.delete(f"/api/v1/admin/mcp/servers/{server_id}")
            assert deleted.status_code == 204
            assert admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}").status_code == 404
            assert admin_client.delete(f"/api/v1/admin/mcp/servers/{server_id}").status_code == 404

            async def _verify_soft_delete_cleanup() -> None:
                async with h.session_factory() as s:
                    row = await s.get(McpServerModel, server_id)
                    assert row is not None
                    assert row.deleted_at is not None
                    assert row.enabled is False
                    assert row.secrets_ciphertext is None
                    assert row.secrets_nonce is None
                    assert row.key_version is None
                    tools_left = (
                        await s.scalars(
                            select(McpToolModel).where(McpToolModel.server_id == server_id)
                        )
                    ).all()
                    assert tools_left == []
                    grants_left = (
                        await s.scalars(
                            select(ResourceVisibilityGrantModel).where(
                                ResourceVisibilityGrantModel.resource_id == server_id
                            )
                        )
                    ).all()
                    assert grants_left == []

            _run(_verify_soft_delete_cleanup())

            # 复用完全相同的名称重新注册新 Server（依赖部分唯一索引 uq_mcp_servers_name_active）。
            rereg = admin_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "ReportRunner",
                    "transport": "stdio",
                    "command": "/opt/mcp/report",
                    "args": [],
                    "secrets": {},
                    "visibility_scope": "company",
                },
            )
            assert rereg.status_code == 201, rereg.text
            assert rereg.json()["id"] != server_id
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 3. 工具元信息局部更新、原子批量审批开关与 Run 待审批结构化投影
# ---------------------------------------------------------------------------


def test_tool_metadata_patch_and_atomic_batch_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证单工具元信息局部更新/显式清空，及 batch-approval 路由优先、校验与原子回滚。"""
    initial_tools = (
        DiscoveredMcpTool("t_one", "工具一", {"type": "object", "properties": {}}),
        DiscoveredMcpTool("t_two", "工具二", {"type": "object", "properties": {}}),
        DiscoveredMcpTool("t_three", "工具三", {"type": "object", "properties": {}}),
    )
    h = _run(_create_harness(monkeypatch, initial_tools))
    try:
        with TestClient(h.app) as admin_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            reg = admin_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "ToolBatchSrv",
                    "transport": "streamable_http",
                    "url": "https://tools.example.com/mcp",
                    "secrets": {},
                },
            )
            assert reg.status_code == 201
            tools = admin_client.get("/api/v1/admin/tools").json()["items"]
            t1, t2, t3 = [item["tool_id"] for item in tools]

            # 1) 单工具局部更新 + 显式 null 清空展示字段。
            p1 = admin_client.patch(
                f"/api/v1/admin/tools/{t1}",
                json={
                    "requires_approval": True,
                    "display_name": "  发起报销审批  ",
                    "approval_summary_template": "报销人 {applicant}\n金额 {amount}",
                    "parameter_labels": {"applicant": "申请人", "amount": "报销金额"},
                },
            )
            assert p1.status_code == 200, p1.text
            b1 = p1.json()
            assert b1["requires_approval"] is True
            assert b1["display_name"] == "发起报销审批"
            assert b1["approval_summary_template"] == "报销人 {applicant}\n金额 {amount}"
            assert b1["parameter_labels"] == {"applicant": "申请人", "amount": "报销金额"}

            # 仅清空 display_name 与 parameter_labels，保留 requires_approval 与模板不变。
            p2 = admin_client.patch(
                f"/api/v1/admin/tools/{t1}",
                json={"display_name": None, "parameter_labels": None},
            )
            assert p2.status_code == 200, p2.text
            b2 = p2.json()
            assert b2["requires_approval"] is True
            assert b2["display_name"] is None
            assert b2["approval_summary_template"] == "报销人 {applicant}\n金额 {amount}"
            assert b2["parameter_labels"] == {}

            # 非法元信息输入拒绝 422。
            for invalid_body in (
                {},
                {"requires_approval": None},
                {"display_name": "   "},
                {"display_name": "a" * 121},
                {"display_name": "bad\x00name"},
                {"approval_summary_template": "   "},
                {"approval_summary_template": "a" * 501},
                {"approval_summary_template": "bad\x07bell"},
                {"parameter_labels": {f"k{i}": f"标签{i}" for i in range(51)}},
                {"parameter_labels": {"bad\x00key": "标签"}},
            ):
                resp = admin_client.patch(f"/api/v1/admin/tools/{t1}", json=invalid_body)
                assert resp.status_code == 422, (invalid_body, resp.text)

            # 2) batch-approval 原子批量切换全部目标工具。
            batch_ok = admin_client.patch(
                "/api/v1/admin/tools/batch-approval",
                json={"tool_ids": [t2, t3], "requires_approval": True},
            )
            assert batch_ok.status_code == 200, batch_ok.text
            batch_body = batch_ok.json()
            assert batch_body["updated_count"] == 2
            assert [item["tool_id"] for item in batch_body["items"]] == [t2, t3]
            assert all(item["requires_approval"] is True for item in batch_body["items"])

            # 3) 超 100 项、重复 ID、空数组拒绝 422。
            assert (
                admin_client.patch(
                    "/api/v1/admin/tools/batch-approval",
                    json={"tool_ids": [], "requires_approval": True},
                ).status_code
                == 422
            )
            assert (
                admin_client.patch(
                    "/api/v1/admin/tools/batch-approval",
                    json={"tool_ids": [t1, t1], "requires_approval": False},
                ).status_code
                == 422
            )
            assert (
                admin_client.patch(
                    "/api/v1/admin/tools/batch-approval",
                    json={
                        "tool_ids": [f"tool_{i:032x}" for i in range(101)],
                        "requires_approval": False,
                    },
                ).status_code
                == 422
            )

            # 4) 混入不存在的 tool_id -> 返回 404 mcp_tool_not_found 且整批回滚（t2/t3 保持 True）。
            batch_missing = admin_client.patch(
                "/api/v1/admin/tools/batch-approval",
                json={"tool_ids": [t2, "tool_nonexistent", t3], "requires_approval": False},
            )
            assert batch_missing.status_code == 404
            assert batch_missing.json()["detail"] == "mcp_tool_not_found"
            assert admin_client.get(f"/api/v1/admin/tools/{t2}").json()["requires_approval"] is True
            assert admin_client.get(f"/api/v1/admin/tools/{t3}").json()["requires_approval"] is True
    finally:
        _run(h.aclose())


def test_run_pending_approvals_structured_projection_for_mcp_subagent_and_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证主 Run 与子智能体待审批的结构化投影、安全模板渲染、回退与 Skill 隐藏参数零泄漏。"""
    initial_tools = (
        DiscoveredMcpTool(
            remote_name="transfer_funds",
            description="发起对外付款",
            input_schema={
                "type": "object",
                "properties": {
                    "payee": {"type": "string"},
                    "amount": {"type": "number"},
                    "urgent": {"type": "boolean"},
                    "note": {"type": ["string", "null"]},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "extra_ref": {"type": "string"},
                },
            },
        ),
        DiscoveredMcpTool(
            remote_name="raw_lookup",
            description="无业务映射工具",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}, "strict": {"type": "boolean"}},
            },
        ),
    )
    h = _run(_create_harness(monkeypatch, initial_tools))
    try:
        with TestClient(h.app) as admin_client, TestClient(h.app) as member_client:
            _login(admin_client, "admin@example.com", "AdminPass123!")
            _login(member_client, "member@example.com", "MemberPass123!")

            prof = _create_model_profile(admin_client, name="审批测试模型", model="gpt-4o")
            assert (
                admin_client.put(
                    "/api/v1/admin/llm/model-profiles/default",
                    json={"profile_id": prof["id"]},
                ).status_code
                == 200
            )
            reg = admin_client.post(
                "/api/v1/admin/mcp/servers",
                json={
                    "name": "PaymentMCP",
                    "transport": "streamable_http",
                    "url": "https://pay.example.com/mcp",
                    "secrets": {},
                },
            )
            assert reg.status_code == 201
            server_id = reg.json()["id"]
            tools = admin_client.get(f"/api/v1/admin/mcp/servers/{server_id}/tools").json()["items"]
            pay_tool = next(item for item in tools if item["remote_name"] == "transfer_funds")
            raw_tool = next(item for item in tools if item["remote_name"] == "raw_lookup")

            # 为 pay_tool 配置中文展示名、模板（含属性注入探针）及参数标签；raw_tool 留空。
            assert (
                admin_client.patch(
                    f"/api/v1/admin/tools/{pay_tool['tool_id']}",
                    json={
                        "requires_approval": True,
                        "display_name": "发起对外付款",
                        "approval_summary_template": (
                            "向 {payee} 付款 {amount} 元（加急：{urgent}，探针：{payee.__class__}）"
                        ),
                        "parameter_labels": {
                            "amount": "付款金额",
                            "payee": "收款方",
                            "urgent": "是否加急",
                            "note": "备注",
                        },
                    },
                ).status_code
                == 200
            )
            assert (
                admin_client.patch(
                    f"/api/v1/admin/tools/{raw_tool['tool_id']}",
                    json={"requires_approval": True},
                ).status_code
                == 200
            )

            # 创建主 Run，并写入一批包含：1) 有映射 MCP；2) 无映射 MCP；3) Skill 脚本 的待审批调用。
            sub_resp = member_client.post(
                "/api/v1/sessions/sess_member/runs",
                json={"request_id": "req-appr-main", "text": "执行付款并运行脚本"},
            )
            assert sub_resp.status_code == 201
            main_run_id = sub_resp.json()["id"]

            async def _enter_main_waiting_for_approval() -> None:
                leases = await h.run_repo.claim(
                    "worker-a", lease_seconds=60, execution_seconds=120, limit=5
                )
                lease = next(item for item in leases if item.run.id == main_run_id)
                member_snap = await h.access_policy.get_user_snapshot("usr_member")
                assert member_snap is not None
                runtime_defs = await h.mcp_repo.list_readable_runtime_tools(member_snap, limit=201)
                await h.run_repo.initialize_tools(
                    InitializeRunToolsCommand(
                        run_id=main_run_id,
                        worker_id="worker-a",
                        execution_epoch=lease.run.execution_epoch,
                        snapshot=RuntimeToolSnapshot(tools=runtime_defs),
                    )
                )
                long_extra = "超长备注" * 200
                call_msg_id = new_id("exec")
                skill_rec_id = new_id("rtc")
                await h.run_repo.begin_tool_call_batch(
                    BeginToolCallBatchCommand(
                        run_id=main_run_id,
                        worker_id="worker-a",
                        execution_epoch=lease.run.execution_epoch,
                        call_message=ExecutionMessageDraft(
                            id=call_msg_id,
                            session_id="sess_member",
                            owner_id="usr_member",
                            run_id=main_run_id,
                            thread_id=main_run_id,
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    tool_call_id="tc_pay_1",
                                    name=pay_tool["model_name"],
                                    arguments={
                                        "extra_ref": long_extra,
                                        "payee": "星辰科技",
                                        "amount": 18800,
                                        "urgent": True,
                                        "note": None,
                                        "tags": ["对公", "月结"],
                                    },
                                ),
                                ToolCallPart(
                                    tool_call_id="tc_raw_2",
                                    name=raw_tool["model_name"],
                                    arguments={"query": "Q4合同", "strict": False},
                                ),
                                ToolCallPart(
                                    tool_call_id="tc_skill_3",
                                    name="skill_execute",
                                    arguments={
                                        "skill_id": "sk_payroll",
                                        "script_path": "scripts/run.py",
                                        "parameters": {
                                            "month": "2026-11",
                                            "secret_db_password": "SUPER_HIDDEN_PASSWORD_999",
                                        },
                                    },
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id=None,
                            created_at=h.clock(),
                            metadata=MessageMetadata(),
                        ),
                        calls=(
                            BeginToolCallBatchItem(
                                record_id=new_id("rtc"),
                                tool_call_id="tc_pay_1",
                                model_name=pay_tool["model_name"],
                                tool_id=pay_tool["tool_id"],
                                tool_kind=ToolKind.MCP,
                                requires_approval=True,
                            ),
                            BeginToolCallBatchItem(
                                record_id=new_id("rtc"),
                                tool_call_id="tc_raw_2",
                                model_name=raw_tool["model_name"],
                                tool_id=raw_tool["tool_id"],
                                tool_kind=ToolKind.MCP,
                                requires_approval=True,
                            ),
                            BeginToolCallBatchItem(
                                record_id=skill_rec_id,
                                tool_call_id="tc_skill_3",
                                model_name="skill_execute",
                                tool_id=SKILL_EXECUTE_TOOL_ID,
                                tool_kind=ToolKind.BUILTIN,
                                requires_approval=True,
                                safe_skill_projection=SafeSkillApprovalProjection(
                                    call_id=skill_rec_id,
                                    skill_id="sk_payroll",
                                    skill_name="薪酬核算技能",
                                    skill_version_id="skv_1",
                                    skill_version_label="v1.0",
                                    script_label="生成月度薪酬表",
                                    purpose="计算并导出部门薪酬汇总",
                                    parameter_summary=(
                                        SkillParameterSummaryItem(
                                            name="month",
                                            label="结算月份",
                                            value="2026-11",
                                        ),
                                    ),
                                    hidden_parameter_count=1,
                                    can_approve=True,
                                ),
                            ),
                        ),
                        requested_at=h.clock(),
                    )
                )

            _run(_enter_main_waiting_for_approval())

            detail_resp = member_client.get(f"/api/v1/sessions/sess_member/runs/{main_run_id}")
            assert detail_resp.status_code == 200, detail_resp.text
            detail = detail_resp.json()
            assert detail["status"] == "waiting_for_approval"
            approvals = detail["pending_approvals"]
            assert len(approvals) == 3

            # 1) 有业务映射的 MCP 工具：中文展示名、防注入安全模板、有序键值表与截断。
            a_pay = approvals[0]
            assert a_pay["display_name"] == "发起对外付款"
            assert (
                a_pay["approval_summary"]
                == "向 星辰科技 付款 18800 元（加急：是，探针：{payee.__class__}）"
            )
            pay_args = a_pay["formatted_arguments"]
            # 已映射字段优先排列在未映射字段之前（PostgreSQL JSONB 不保留对象键插入顺序，
            # 因此核验前 4 项恰好为全部已映射字段且标签与格式化值完全准确，未映射字段在后）。
            assert {item["key"] for item in pay_args[:4]} == {"amount", "payee", "urgent", "note"}
            mapped_by_key = {item["key"]: item for item in pay_args[:4]}
            assert mapped_by_key["amount"] == {
                "key": "amount",
                "label": "付款金额",
                "value": "18800",
            }
            assert mapped_by_key["payee"] == {
                "key": "payee",
                "label": "收款方",
                "value": "星辰科技",
            }
            assert mapped_by_key["urgent"] == {
                "key": "urgent",
                "label": "是否加急",
                "value": "是",
            }
            assert mapped_by_key["note"] == {
                "key": "note",
                "label": "备注",
                "value": "无",
            }
            tail_by_key = {item["key"]: item for item in pay_args[4:]}
            assert tail_by_key["tags"] == {"key": "tags", "label": "tags", "value": "对公、月结"}
            assert tail_by_key["extra_ref"]["label"] == "extra_ref"
            assert len(tail_by_key["extra_ref"]["value"]) == 500
            assert tail_by_key["extra_ref"]["value"].endswith("…")

            # 2) 无任何映射的 MCP 工具：display_name 优雅回退为 model_name，参数仍返回可读键值表。
            a_raw = approvals[1]
            assert a_raw["display_name"] == raw_tool["model_name"]
            assert a_raw["approval_summary"] is None
            assert a_raw["formatted_arguments"] == [
                {"key": "query", "label": "query", "value": "Q4合同"},
                {"key": "strict", "label": "strict", "value": "否"},
            ]

            # 3) Skill 脚本工具：取 script_label，仅含脱敏可见参数，严禁泄漏隐藏参数！
            a_skill = approvals[2]
            assert a_skill["display_name"] == "生成月度薪酬表"
            assert a_skill["approval_summary"] is None
            assert a_skill["formatted_arguments"] == [
                {"key": "month", "label": "结算月份", "value": "2026-11"}
            ]
            assert "SUPER_HIDDEN_PASSWORD_999" not in detail_resp.text
            assert "secret_db_password" not in detail_resp.text

            # 4) 软删除 MCP Server 后，历史待审批仍能回退至 Run 冻结的 tool_snapshot 元信息。
            assert admin_client.delete(f"/api/v1/admin/mcp/servers/{server_id}").status_code == 204
            fallback_detail = member_client.get(
                f"/api/v1/sessions/sess_member/runs/{main_run_id}"
            ).json()
            assert fallback_detail["pending_approvals"][0]["display_name"] == "发起对外付款"
            fallback_labels = {
                item["key"]: item["label"]
                for item in fallback_detail["pending_approvals"][0]["formatted_arguments"][:4]
            }
            assert fallback_labels == {
                "amount": "付款金额",
                "payee": "收款方",
                "urgent": "是否加急",
                "note": "备注",
            }

            # 5) 子智能体待审批（PendingSubagentApproval）同样返回结构化投影（REST 与 SSE）。
            async def _setup_subagent_pending_approval() -> str:
                async with h.session_factory() as s, s.begin():
                    # 结束第一个主 Run，创建第二个主 Run 与子智能体等待审批。
                    old_main = await s.get(RunModel, main_run_id)
                    assert old_main is not None
                    old_main.status = RunStatus.COMPLETED.value
                    old_main.finished_at = h.clock()
                    s.add(
                        SubagentModel(
                            id="sub_pay",
                            slug="pay_bot",
                            name="付款助手",
                            description="执行付款",
                            system_prompt="付款助手",
                            model_profile_id=prof["id"],
                            allowed_tool_ids=[],
                            enabled=True,
                            visibility_scope="company",
                            owner_department_id=None,
                            version=1,
                            created_by="usr_admin",
                            deleted_at=None,
                            created_at=NOW,
                            updated_at=NOW,
                        )
                    )
                # 重新注册一个启用中的 MCP Server 用于子智能体测试。
                reg2 = admin_client.post(
                    "/api/v1/admin/mcp/servers",
                    json={
                        "name": "PaymentMCP",
                        "transport": "streamable_http",
                        "url": "https://pay.example.com/mcp",
                        "secrets": {},
                    },
                )
                assert reg2.status_code == 201
                srv2_id = reg2.json()["id"]
                srv2_tools = admin_client.get(f"/api/v1/admin/mcp/servers/{srv2_id}/tools").json()[
                    "items"
                ]
                sub_pay_tool = next(
                    item for item in srv2_tools if item["remote_name"] == "transfer_funds"
                )
                assert (
                    admin_client.patch(
                        f"/api/v1/admin/tools/{sub_pay_tool['tool_id']}",
                        json={
                            "requires_approval": True,
                            "display_name": "子任务对外付款",
                            "approval_summary_template": "子任务向 {payee} 转账 {amount} 元",
                            "parameter_labels": {"payee": "收款对象", "amount": "转账金额"},
                        },
                    ).status_code
                    == 200
                )
                parent_resp = member_client.post(
                    "/api/v1/sessions/sess_member/runs",
                    json={"request_id": "req-parent-sub-appr", "text": "委派子任务付款"},
                )
                assert parent_resp.status_code == 201
                parent_id = str(parent_resp.json()["id"])
                leases = await h.run_repo.claim(
                    "worker-b", lease_seconds=60, execution_seconds=120, limit=5
                )
                parent_lease = next(item for item in leases if item.run.id == parent_id)
                member_snap = await h.access_policy.get_user_snapshot("usr_member")
                assert member_snap is not None
                sub_runtime_defs = await h.mcp_repo.list_readable_runtime_tools(
                    member_snap, limit=201
                )
                sub_run_id = new_id("run")
                await h.run_repo.begin_subagent_batch(
                    BeginSubagentBatchCommand(
                        run_id=parent_id,
                        worker_id="worker-b",
                        execution_epoch=parent_lease.run.execution_epoch,
                        tool_call_id="tc_disp_pay",
                        call_message=ExecutionMessageDraft(
                            id=new_id("exec"),
                            session_id="sess_member",
                            owner_id="usr_member",
                            run_id=parent_id,
                            thread_id=parent_id,
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    tool_call_id="tc_disp_pay",
                                    name="dispatch_subagents",
                                    arguments={
                                        "tasks": [{"subagent_slug": "pay_bot", "task": "转账 500"}]
                                    },
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id=None,
                            created_at=h.clock(),
                            metadata=MessageMetadata(),
                        ),
                        record_id=new_id("rtc"),
                        items=(
                            BeginSubagentBatchItem(
                                sub_run_id=sub_run_id,
                                subagent_profile_id="sub_pay",
                                subagent_slug="pay_bot",
                                subagent_name="付款助手",
                                system_prompt="付款助手",
                                model_profile_id=prof["id"],
                                tool_snapshot=RuntimeToolSnapshot(tools=sub_runtime_defs),
                                skill_snapshot=None,
                                input_file_ids=(),
                                task_message=ExecutionMessageDraft(
                                    id=new_id("exec"),
                                    session_id="sess_member",
                                    owner_id="usr_member",
                                    run_id=sub_run_id,
                                    thread_id=sub_run_id,
                                    role=ExecutionRole.USER,
                                    parts=(TextPart("任务：\n转账 500"),),
                                    tool_call_id=None,
                                    source_message_id=None,
                                    created_at=h.clock(),
                                    metadata=MessageMetadata(),
                                ),
                            ),
                        ),
                        requested_at=h.clock(),
                    )
                )
                child_leases = await h.run_repo.claim(
                    "worker-c", lease_seconds=60, execution_seconds=120, limit=5
                )
                child_lease = next(item for item in child_leases if item.run.id == sub_run_id)
                await h.run_repo.begin_tool_call_batch(
                    BeginToolCallBatchCommand(
                        run_id=sub_run_id,
                        worker_id="worker-c",
                        execution_epoch=child_lease.run.execution_epoch,
                        call_message=ExecutionMessageDraft(
                            id=new_id("exec"),
                            session_id="sess_member",
                            owner_id="usr_member",
                            run_id=sub_run_id,
                            thread_id=sub_run_id,
                            role=ExecutionRole.ASSISTANT,
                            parts=(
                                ToolCallPart(
                                    tool_call_id="tc_sub_pay_1",
                                    name=sub_pay_tool["model_name"],
                                    arguments={
                                        "payee": "银河供应商",
                                        "amount": 500,
                                        "memo": "运费",
                                    },
                                ),
                            ),
                            tool_call_id=None,
                            source_message_id=None,
                            created_at=h.clock(),
                            metadata=MessageMetadata(),
                        ),
                        calls=(
                            BeginToolCallBatchItem(
                                record_id=new_id("rtc"),
                                tool_call_id="tc_sub_pay_1",
                                model_name=sub_pay_tool["model_name"],
                                tool_id=sub_pay_tool["tool_id"],
                                tool_kind=ToolKind.MCP,
                                requires_approval=True,
                            ),
                        ),
                        requested_at=h.clock(),
                    )
                )
                return parent_id

            parent_id = _run(_setup_subagent_pending_approval())
            parent_detail = member_client.get(
                f"/api/v1/sessions/sess_member/runs/{parent_id}"
            ).json()
            assert len(parent_detail["pending_subagent_approvals"]) == 1
            sub_appr = parent_detail["pending_subagent_approvals"][0]
            assert sub_appr["display_name"] == "子任务对外付款"
            assert sub_appr["approval_summary"] == "子任务向 银河供应商 转账 500 元"
            assert sub_appr["formatted_arguments"] == [
                {"key": "payee", "label": "收款对象", "value": "银河供应商"},
                {"key": "amount", "label": "转账金额", "value": "500"},
                {"key": "memo", "label": "memo", "value": "运费"},
            ]

            # SSE 快照同样携带完整结构化待审批字段。
            stream_snap = _run(
                h.run_repo.get_stream_snapshot("sess_member", "usr_member", parent_id)
            )
            assert stream_snap is not None
            assert len(stream_snap.pending_subagent_approvals) == 1
            assert stream_snap.pending_subagent_approvals[0].display_name == "子任务对外付款"
            assert (
                stream_snap.pending_subagent_approvals[0].approval_summary
                == "子任务向 银河供应商 转账 500 元"
            )
    finally:
        _run(h.aclose())


# ---------------------------------------------------------------------------
# 4. Alembic 迁移单 head 与离线 upgrade/downgrade SQL 校验
# ---------------------------------------------------------------------------


def test_alembic_baseline_contains_admin_ops_and_approval_ddl() -> None:
    """校验收敛后的基线迁移 0001_initial_schema 保持单一 head，包含管理运维与审批 DDL。"""
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+asyncpg://user:password@127.0.0.1/athena_test",
    )
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0001_initial_schema"]
    revision = script.get_revision("0001_initial_schema")
    assert revision is not None
    assert revision.down_revision is None

    up_buffer = StringIO()
    with redirect_stdout(up_buffer):
        command.upgrade(cfg, "head", sql=True)
    up_sql = up_buffer.getvalue()
    assert "version INTEGER DEFAULT 1 NOT NULL" in up_sql
    assert "ck_llm_profile_version" in up_sql
    assert "updated_at TIMESTAMP WITH TIME ZONE NOT NULL" in up_sql
    assert "model_snapshot_json JSONB NOT NULL" in up_sql
    assert "enabled BOOLEAN DEFAULT true NOT NULL" in up_sql
    assert "deleted_at TIMESTAMP WITH TIME ZONE" in up_sql
    assert "CREATE UNIQUE INDEX uq_mcp_servers_name_active" in up_sql
    assert "WHERE deleted_at IS NULL" in up_sql
    assert "display_name VARCHAR(120)" in up_sql
    assert "approval_summary_template VARCHAR(500)" in up_sql
    assert "parameter_labels_json JSONB DEFAULT '{}' NOT NULL" in up_sql

    down_buffer = StringIO()
    with redirect_stdout(down_buffer):
        command.downgrade(cfg, "0001_initial_schema:base", sql=True)
    down_sql = down_buffer.getvalue()
    assert "DROP TABLE mcp_tools" in down_sql
    assert "DROP TABLE mcp_servers" in down_sql
    assert "DROP TABLE runs" in down_sql
    assert "DROP TABLE llm_model_profile_versions" in down_sql
