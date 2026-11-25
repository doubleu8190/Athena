"""Athena v2 企业场景测试数据体系 V3 四大准生产深度专项回归测试。

覆盖范围（全部基于真实后端领域服务、真实仓储与真实资产，零伪断言）：
1. 单轮 6 图截断（MAX_VISION_IMAGES_PER_RUN=5）、EXIF/WebP 归一化、损坏图隔离与方案 A2 降级；
2. 组织调岗实时撤权与新权即时生效（复用同一 auth_session_id）及离职停用同事务吊销原 Cookie；
3. 50 条满额显式记忆配额（真实 SqlAlchemyPreferenceRepository）与双编码器 1500 Token 截断；
4. 多月份（2026-11 vs 2026-12）真实 stdio MCP + SK-5 / SK-7 沙箱端到端闭环及非法月份拦截。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import tiktoken
from mcp import Client
from openpyxl import load_workbook  # type: ignore[import-untyped]
from PIL import Image
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.access import SqlAlchemyAccessControlRepository
from adapter.auth.model import UserModel
from adapter.auth.repository import SqlAlchemyAuthRepository
from adapter.knowledge.repository import KnowledgeRepository
from adapter.mcp.repository import SqlAlchemyMcpRepository
from adapter.models import Base
from adapter.org.model import DepartmentModel, UserDepartmentModel
from adapter.org.repository import SqlAlchemyOrgRepository
from adapter.preference.repository import SqlAlchemyPreferenceRepository
from adapter.skill.execution import PythonScriptExecutor
from adapter.skill.repository import SqlAlchemySkillRepository
from adapter.subagent.repository import SqlAlchemySubagentRepository
from app.access import AccessPolicyService
from app.auth import AuthService
from app.file.service import SessionFileService
from app.knowledge.service import KnowledgeService
from app.org import OrgService
from app.preference.context import render_preference_context
from app.preference.service import PreferenceService
from app.run.config import RunWorkerConfig
from app.run.executor import RunExecutor
from app.skill.package import SkillPackageValidator, validate_parameter_schema
from app.skill.service import SkillService
from app.subagent.service import SubagentService
from app.tool.manager import ToolManager
from app.tool.ports import RunToolRuntime
from app.tool.runtime import REQUEST_USER_INPUT_DEFINITION, RunToolRuntimeService
from domain.access import (
    GrantTargetType,
    ResourceAccessTarget,
    ResourceType,
    VisibilityGrantInput,
    VisibilityScope,
)
from domain.auth import UserRole, UserStatus
from domain.file import (
    MAX_VISION_IMAGES_PER_RUN,
    FileObjectStore,
    SessionFile,
    SessionFileRepository,
    SessionFileSource,
)
from domain.knowledge.ports import OriginalStore
from domain.llm.model import (
    LlmImagePart,
    LlmRequest,
    LlmResponse,
    LlmRole,
    LlmStreamEvent,
    LlmText,
    LlmUsage,
    ModelProfileVersion,
    ResponseCompleted,
    RuntimeModelProfile,
    TextDelta,
)
from domain.llm.ports import LlmStreamProvider
from domain.llm.profile_ports import ModelProfileRepository
from domain.mcp import DiscoveredMcpTool, McpConnectionInput, StreamableHttpConfig
from domain.message import (
    ConversationMessage,
    ConversationRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
)
from domain.org import DepartmentStatus
from domain.preference import (
    DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
    MAX_USER_MEMORIES_PER_USER,
    UserMemoryLimitReachedError,
    UserMemorySource,
)
from domain.run.model import (
    FailRunCommand,
    FinishRunCommand,
    RecordRunUsageCommand,
    Run,
    RunEvent,
    RunInputFiles,
    RunLease,
    RunStatus,
    RunUsage,
)
from domain.run.ports import RunRepository
from domain.skill import SkillScope
from domain.skill.execution import JsonValue, ScriptExecutionRequest
from domain.subagent import CreateSubagentCommand
from domain.system_prompt import SystemPromptKind, SystemPromptVersion
from domain.tool import InitializeRunToolsCommand, RuntimeToolSnapshot
from infrastructure.file import LocalVisionImageNormalizer
from infrastructure.mcp.stdio import McpStdioTransport
from infrastructure.security.mcp_secrets import McpSecretCipher

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_ROOT = REPO_ROOT / "datasets" / "enterprise_hroa"
GENERATED_DIR = DATASET_ROOT / "generated"
ATTACHMENTS_DIR = GENERATED_DIR / "attachments"
SKILL_PACKAGES_DIR = GENERATED_DIR / "skill_packages"
HROA_DIR = REPO_ROOT / "mcp_servers" / "hroa"
HROA_SERVER_PATH = HROA_DIR / "mcp_server.py"
SK5_ZIP_PATH = SKILL_PACKAGES_DIR / "hroa-lifecycle-audit-pack.zip"
SK7_ZIP_PATH = SKILL_PACKAGES_DIR / "travel-expense-audit-pack.zip"

NOW = datetime(2026, 12, 20, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 回归 1：单轮 6 图截断（MAX_VISION_IMAGES_PER_RUN=5）、损坏图隔离与方案 A2 降级
# ---------------------------------------------------------------------------


def _make_vision_profile(*, profile_id: str, supports_vision: bool) -> RuntimeModelProfile:
    """构造支持或不支持识图的运行时模型配置档案。"""
    return RuntimeModelProfile(
        version=ModelProfileVersion(
            id=profile_id,
            name=profile_id,
            protocol="chat_completions",
            base_url="https://llm.xingchen.test/v1",
            model=profile_id,
            has_api_key=True,
            context_window_tokens=16384,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            max_output_tokens=512,
            request_timeout_seconds=30,
            execution_timeout_seconds=60,
            max_concurrency=2,
            temperature=None,
            enabled=True,
            created_at=NOW,
            supports_vision=supports_vision,
        ),
        api_key="test-enterprise-key",
    )


def _make_session_file_from_disk(
    file_id: str, path: Path, mime_type: str
) -> tuple[SessionFile, bytes]:
    """从真实生成附件构建 SessionFile 元数据与原始字节。"""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    meta = SessionFile(
        id=file_id,
        session_id="ses_v3_vision",
        owner_id="mock_fin_admin",
        source=SessionFileSource.UPLOAD,
        filename=path.name,
        label=None,
        mime_type=mime_type,
        size_bytes=len(raw),
        sha256=digest,
        object_key=f"session-files/{digest[:2]}/{file_id}",
        source_call_id=None,
        output_index=None,
        created_at=NOW,
    )
    return meta, raw


class _TrackingObjectStore:
    """记录对象存储读取次数与键列表，用于严格证明超限图与非识图模型零读取。"""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self._blobs = dict(blobs)
        self.get_bytes_calls: list[tuple[str, int]] = []

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        self.get_bytes_calls.append((key, maximum_bytes))
        return self._blobs[key]


class _StaticSessionFileRepo:
    """按会话与归属人隔离返回真实企业测试附件元数据。"""

    def __init__(self, files: Sequence[SessionFile]) -> None:
        self._by_id = {item.id: item for item in files}

    async def get_visible_many(
        self,
        session_id: str,
        owner_id: str,
        file_ids: tuple[str, ...],
    ) -> tuple[SessionFile, ...]:
        result: list[SessionFile] = []
        for fid in file_ids:
            item = self._by_id[fid]
            assert item.session_id == session_id and item.owner_id == owner_id
            result.append(item)
        return tuple(result)


@dataclass
class _StaticPromptRepo:
    """提供主系统提示与压缩提示的轻量仓储替身。"""

    async def get_pair_by_ids(
        self, main_id: str, cmp_id: str
    ) -> tuple[SystemPromptVersion, SystemPromptVersion]:
        return (
            SystemPromptVersion(
                id=main_id,
                kind=SystemPromptKind.MAIN,
                content="你是星辰智联企业办公助手 Athena。",
                created_by="mock_super_admin",
                created_at=NOW,
                restored_from_version_id=None,
            ),
            SystemPromptVersion(
                id=cmp_id,
                kind=SystemPromptKind.COMPRESSION,
                content="请压缩历史对话并保留核心业务事实。",
                created_by="mock_super_admin",
                created_at=NOW,
                restored_from_version_id=None,
            ),
        )


class _MinimalRunRepo:
    """支持真实 RunExecutor 完成单轮视觉上下文构建与终态落盘的轻量仓储。"""

    def __init__(self, run: Run, trigger_message: ConversationMessage) -> None:
        self.run = run
        self.messages: list[ConversationMessage] = [trigger_message]
        self.final_command: FinishRunCommand | None = None
        self.fail_command: FailRunCommand | None = None

    async def is_live_execution(self, run_id: str, execution_epoch: int) -> bool:
        return run_id == self.run.id and execution_epoch == self.run.execution_epoch

    async def list_retry_context(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[Any, ...]:
        del session_id, owner_id, run_id
        return ()

    async def initialize_tools(self, command: InitializeRunToolsCommand) -> RuntimeToolSnapshot:
        self.run = replace(self.run, tool_snapshot=command.snapshot)
        return command.snapshot

    async def get_resumable_tool_batch(self, run_id: str) -> None:
        del run_id
        return None

    async def list_context_messages(
        self, session_id: str, owner_id: str, run_id: str
    ) -> tuple[ConversationMessage, ...]:
        del session_id, owner_id, run_id
        return tuple(self.messages)

    async def list_successfully_loaded_skill_ids(self, run_id: str) -> tuple[str, ...]:
        del run_id
        return ()

    async def reserve_llm_request(self, command: Any) -> int:
        del command
        self.run = replace(self.run, llm_request_count=self.run.llm_request_count + 1)
        return self.run.llm_request_count

    async def record_usage(self, command: RecordRunUsageCommand) -> Run:
        del command
        return self.run

    async def finish(self, command: FinishRunCommand) -> Run:
        self.final_command = command
        self.run = replace(self.run, status=RunStatus.COMPLETED)
        return self.run

    async def fail(self, command: FailRunCommand) -> Run:
        self.fail_command = command
        self.run = replace(
            self.run, status=RunStatus.FAILED, safe_error_code=command.safe_error_code
        )
        return self.run

    async def list_events(self, *args: Any, **kwargs: Any) -> tuple[RunEvent, ...]:
        del args, kwargs
        return ()


class _CapturingStreamProvider:
    """捕获真实 RunExecutor 发出的完整 LlmRequest，便于断言多模态载荷与降级状态。"""

    def __init__(self, reply_text: str) -> None:
        self._reply_text = reply_text
        self.requests: list[LlmRequest] = []

    @asynccontextmanager
    async def stream(
        self, runtime: RuntimeModelProfile, request: LlmRequest
    ) -> AsyncIterator[AsyncIterator[LlmStreamEvent]]:
        del runtime
        self.requests.append(request)
        response = LlmResponse(
            output=(LlmText(self._reply_text),),
            status="completed",
            incomplete_reason=None,
            model="xingchen-vision-test",
            response_id="resp_v3_vis",
            request_id=None,
            usage=LlmUsage(input_tokens=1200, output_tokens=32, total_tokens=1232),
        )

        async def _iter() -> AsyncIterator[LlmStreamEvent]:
            yield TextDelta(output_index=0, content_index=0, text=self._reply_text)
            yield ResponseCompleted(response)

        yield _iter()


def _extract_untrusted_file_metadata(request: LlmRequest) -> dict[str, dict[str, Any]]:
    """从主模型请求消息中提取 Athena 注入的附件元数据 JSON 并按 file_id 索引。"""
    msg_content = next(
        m.content for m in request.messages if "<untrusted_input_file_metadata>" in m.content
    )
    json_segment = msg_content.split("<untrusted_input_file_metadata>\n", 1)[1].split(
        "\n</untrusted_input_file_metadata>", 1
    )[0]
    items = cast(list[dict[str, Any]], json.loads(json_segment))
    return {str(item["file_id"]): item for item in items}


def _build_vision_run_and_executor(
    *,
    files: Sequence[SessionFile],
    store: _TrackingObjectStore,
    supports_vision: bool,
) -> tuple[RunExecutor, _MinimalRunRepo, _CapturingStreamProvider, RunLease]:
    """装配真实图片归一化器、SessionFileService、RunToolRuntimeService 与 RunExecutor。"""
    file_service = SessionFileService(
        cast(SessionFileRepository, _StaticSessionFileRepo(files)),
        cast(FileObjectStore, store),
        image_normalizer=LocalVisionImageNormalizer(),
    )
    auth_stub = SimpleNamespace(
        get_current_session_user=lambda owner_id, auth_sid, now: asyncio.sleep(
            0, result=SimpleNamespace(id=owner_id)
        )
    )
    sessions_stub = SimpleNamespace(
        get_visible=lambda scope: asyncio.sleep(
            0, result=SimpleNamespace(id=scope.session_id, project_id=None)
        )
    )
    mcp_catalog_stub = SimpleNamespace(list_runtime_tools=lambda limit: asyncio.sleep(0, result=()))
    tool_runtime = RunToolRuntimeService(
        auth=cast(Any, auth_stub),
        sessions=cast(Any, sessions_stub),
        mcp_catalog=cast(Any, mcp_catalog_stub),
        mcp_executor=cast(Any, SimpleNamespace()),
        knowledge=None,
        project_knowledge=None,
        session_files=file_service,
        # 显式注入固定时钟，确保任意未来运行日期下与 NOW 对齐、不触发墙上时钟漂移。
        clock=lambda: NOW,
    )

    profile = _make_vision_profile(
        profile_id="mpv_v3_vision" if supports_vision else "mpv_v3_text_only",
        supports_vision=supports_vision,
    )
    run = Run(
        id=f"run_v3_vis_{'on' if supports_vision else 'off'}",
        session_id="ses_v3_vision",
        owner_id="mock_fin_admin",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_v3_vis_1",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id=profile.version.id,
        main_prompt_version_id="spv_main",
        compression_prompt_version_id="spv_cmp",
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_v3_vis",
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_started_at=NOW,
        execution_deadline_at=NOW + timedelta(seconds=60),
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        tool_snapshot=RuntimeToolSnapshot(tools=(REQUEST_USER_INPUT_DEFINITION,)),
        auth_session_id="auth_v3_vis",
        input_files=RunInputFiles(file_ids=tuple(item.id for item in files)),
    )
    trigger = ConversationMessage(
        id=run.trigger_message_id,
        session_id=run.session_id,
        sequence=1,
        role=ConversationRole.USER,
        status=MessageStatus.COMPLETED,
        parts=(TextPart("请核验本次提交的 7 份差旅与 HR 凭证图片。"),),
        reply_to_message_id=None,
        run_id=None,
        client_message_id=None,
        created_at=NOW,
        completed_at=NOW,
        metadata=MessageMetadata(),
        context_turn_id=run.trigger_message_id,
    )
    repo = _MinimalRunRepo(run, trigger)
    profiles = SimpleNamespace(
        get_runtime=lambda profile_id: asyncio.sleep(0, result=profile),
        get_compression_policy=lambda policy_id: asyncio.sleep(0, result=None),
    )
    provider = _CapturingStreamProvider("已完成多模态附件核验。")
    executor = RunExecutor(
        cast(RunRepository, repo),
        cast(ModelProfileRepository, profiles),
        cast(LlmStreamProvider, provider),
        system_prompts=cast(Any, _StaticPromptRepo()),
        tool_runtime=cast(RunToolRuntime, tool_runtime),
        config=RunWorkerConfig(),
        # 显式注入与 RunLease 基准一致的固定时钟，杜绝 2026-12-20 之后的 execution_timeout。
        clock=lambda: NOW,
    )
    lease = RunLease(
        run=run,
        lease_expires_at=NOW + timedelta(seconds=60),
        execution_deadline_at=NOW + timedelta(seconds=60),
    )
    return executor, repo, provider, lease


def test_regression1_six_image_truncation_corrupt_isolation_and_plan_a2_degradation() -> None:
    """回归 1：6 张实图 + ATT-19 损坏图验证 5 图截断、EXIF/WebP 归一化及方案 A2 降级。"""
    # 将 ATT-19(损坏图)置于首位，验证单图失败绝不挤占 5 图配额亦不阻断 Run；
    # 随后 5 张有效图含 ATT-07(EXIF/GPS)与 ATT-15(WebP)，第 6 张 ATT-13 触发 skipped_limit。
    asset_specs: tuple[tuple[str, str, str], ...] = (
        ("att_19_corrupt", "ATT-19_伪装PNG损坏图片_负样本.png", "image/png"),
        ("att_05_png", "ATT-05_高明轩_OA年假超额申请截图_XC1005.png", "image/png"),
        ("att_06_png", "ATT-06_跨部门调岗三方会签流程图.png", "image/png"),
        ("att_07_jpg_exif", "ATT-07_韩景明_IT设备签收单拍照件_XC1003.jpg", "image/jpeg"),
        ("att_15_webp", "ATT-15_成都分部出差登机牌与行程图_TRV-2026-1202.webp", "image/webp"),
        ("att_08_png", "ATT-08_方若琳_三甲门诊病假证明_XC1008.png", "image/png"),
        ("att_13_png_6th", "ATT-13_高明轩_上海张江酒店电子发票_TRV-2026-1102.png", "image/png"),
    )
    files: list[SessionFile] = []
    blobs: dict[str, bytes] = {}
    for fid, filename, mime in asset_specs:
        meta, raw = _make_session_file_from_disk(fid, ATTACHMENTS_DIR / filename, mime)
        files.append(meta)
        blobs[meta.object_key] = raw

    # A. supports_vision=True：前 5 张 attached、第 6 张 skipped_limit 且零读取、ATT-19 隔离。
    store_on = _TrackingObjectStore(blobs)
    exec_on, repo_on, provider_on, lease_on = _build_vision_run_and_executor(
        files=files, store=store_on, supports_vision=True
    )
    asyncio.run(exec_on.execute(lease_on))

    assert repo_on.final_command is not None
    assert repo_on.fail_command is None
    assert len(provider_on.requests) == 1

    req_on = provider_on.requests[0]
    meta_on = _extract_untrusted_file_metadata(req_on)
    assert meta_on["att_19_corrupt"]["vision_status"] == "image_invalid"
    for attached_id in ("att_05_png", "att_06_png", "att_07_jpg_exif", "att_15_webp", "att_08_png"):
        assert meta_on[attached_id]["vision_status"] == "attached"
    assert meta_on["att_13_png_6th"]["vision_status"] == "skipped_limit"

    # 第 6 张有效图被配额截断，绝不得触发对象存储读取；共读取 1 次损坏图 + 5 次有效图 = 6 次。
    read_keys_on = [key for key, _ in store_on.get_bytes_calls]
    assert len(read_keys_on) == 6
    assert files[-1].object_key not in read_keys_on

    # 校验送入模型的 5 张图：ATT-07 EXIF/GPS 清除、ATT-15 为 WebP、SYSTEM 消息不含图片。
    assert all(not m.has_image_parts() for m in req_on.messages if m.role is LlmRole.SYSTEM)
    sent_image_parts = [
        part
        for msg in req_on.messages
        for part in msg.content_parts
        if isinstance(part, LlmImagePart)
    ]
    assert len(sent_image_parts) == MAX_VISION_IMAGES_PER_RUN == 5

    # 索引 2 为 ATT-07 JPEG（0:ATT-05, 1:ATT-06, 2:ATT-07, 3:ATT-15, 4:ATT-08）。
    att07_part = sent_image_parts[2]
    assert att07_part.mime_type == "image/jpeg"
    att07_clean_bytes = base64.b64decode(att07_part.data_base64)
    assert b"AthenaMockCam" not in att07_clean_bytes
    with Image.open(io.BytesIO(att07_clean_bytes)) as clean_jpg:
        assert clean_jpg.format == "JPEG"
        assert len(clean_jpg.getexif()) == 0
        assert len(clean_jpg.getexif().get_ifd(0x8825)) == 0
        assert "exif" not in clean_jpg.info

    att15_part = sent_image_parts[3]
    assert att15_part.mime_type == "image/webp"
    att15_clean_bytes = base64.b64decode(att15_part.data_base64)
    with Image.open(io.BytesIO(att15_clean_bytes)) as clean_webp:
        assert clean_webp.format == "WEBP"
        assert clean_webp.width > 0 and clean_webp.height > 0

    # B. supports_vision=False（方案 A2 降级）：全量 unsupported_by_current_model 且零对象存储读取。
    store_off = _TrackingObjectStore(blobs)
    exec_off, repo_off, provider_off, lease_off = _build_vision_run_and_executor(
        files=files, store=store_off, supports_vision=False
    )
    asyncio.run(exec_off.execute(lease_off))

    assert repo_off.final_command is not None
    assert repo_off.fail_command is None
    assert store_off.get_bytes_calls == []
    req_off = provider_off.requests[0]
    meta_off = _extract_untrusted_file_metadata(req_off)
    for fid, _, _ in asset_specs:
        assert meta_off[fid]["vision_status"] == "unsupported_by_current_model"
    assert not req_off.has_image_parts()


# ---------------------------------------------------------------------------
# 回归 2：组织调岗实时撤权 & 离职即时吊销同一 Cookie（真实 SQLite + 领域服务）
# ---------------------------------------------------------------------------


class _DeterministicPasswordHasher:
    """快速确定性密码哈希替身，用于驱动真实 AuthService.login 签发会话令牌。"""

    dummy_hash = "fake:dummy"

    def hash_password(self, password: str) -> str:
        return f"fake:{password}"

    def verify_password(self, password: str, password_hash: str) -> bool:
        return password_hash != self.dummy_hash and password_hash == f"fake:{password}"


class _SimpleTokenizer:
    """供 KnowledgeService 构造使用的轻量分词器。"""

    def tokenize(self, text: str) -> tuple[str, ...]:
        return tuple(part for part in text.strip().split() if part)


class _InMemoryOriginalStore(OriginalStore):
    """供 KnowledgeService 构造使用的内存原件存储。"""

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> None:
        self._items[key] = content

    async def get(self, key: str) -> bytes:
        return self._items[key]

    async def delete(self, key: str) -> None:
        self._items.pop(key, None)

    async def iter_orphan_candidates(
        self, *, older_than_seconds: int = 86400
    ) -> AsyncIterator[str]:
        del older_than_seconds
        if False:
            yield ""


class _InMemorySkillObjectStore:
    """供 SkillService 导入真实 SK-7 ZIP 包使用的内存对象存储。"""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_file(self, key: str, path: Path, *, content_type: str) -> None:
        del content_type
        self.objects[key] = path.read_bytes()

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        self.objects[key] = content

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        return self.objects[key][:maximum_bytes]

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


class _StaticMcpDiscovery:
    """为财务专属与 HR 专属 MCP Server 返回确定性远端工具列表。"""

    def __init__(self, tools_by_url: Mapping[str, tuple[DiscoveredMcpTool, ...]]) -> None:
        self._tools_by_url = tools_by_url

    async def discover(self, connection: McpConnectionInput) -> tuple[DiscoveredMcpTool, ...]:
        url = getattr(connection.config, "url", "")
        return self._tools_by_url.get(url, ())


class _AlwaysValidModelChecker:
    async def is_model_available(self, model_profile_id: str) -> bool:
        del model_profile_id
        return True


class _DbToolCatalog:
    def __init__(self, mcp_repo: SqlAlchemyMcpRepository) -> None:
        self._mcp_repo = mcp_repo

    async def list_available_tool_groups(self) -> tuple[Any, ...]:
        return ()

    async def list_available_tool_ids(self) -> frozenset[str]:
        page = await self._mcp_repo.list_public(limit=100, offset=0)
        return frozenset(tool.tool_id for tool in page.items)


async def _create_sqlite_db(db_path: Path) -> tuple[Any, async_sessionmaker[AsyncSession]]:
    """创建启用外键约束的 SQLite 异步库并建全套表结构。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", poolclass=NullPool)

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.create_function("clock_timestamp", 0, lambda: NOW.isoformat())
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_regression2_org_transfer_realtime_revocation_and_offboarding_cookie_invalidation(
    tmp_path: Path,
) -> None:
    """回归 2：财务专员平调至 HR 后复用同一会话实时撤权/赋权，离职停用同事务吊销原 Cookie。"""

    async def scenario() -> None:
        engine, session_factory = await _create_sqlite_db(tmp_path / "org_lifecycle.db")
        try:
            # 1. 初始化星辰智联 5 级组织树及超管周明远 + 财务专员沈佳禾（mock_fin_member）。
            async with session_factory() as session, session.begin():
                session.add_all(
                    [
                        DepartmentModel(
                            id="dept_mock_root",
                            external_id="ext_root",
                            name="集团总部",
                            parent_id=None,
                            path="/dept_mock_root/",
                            status=DepartmentStatus.ACTIVE.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        DepartmentModel(
                            id="dept_mock_fin",
                            external_id="ext_fin",
                            name="财务部",
                            parent_id="dept_mock_root",
                            path="/dept_mock_root/dept_mock_fin/",
                            status=DepartmentStatus.ACTIVE.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                        DepartmentModel(
                            id="dept_mock_hr",
                            external_id="ext_hr",
                            name="人力资源部",
                            parent_id="dept_mock_root",
                            path="/dept_mock_root/dept_mock_hr/",
                            status=DepartmentStatus.ACTIVE.value,
                            created_at=NOW,
                            updated_at=NOW,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        UserModel(
                            id="mock_super_admin",
                            email="super@xingchen.com",
                            password_hash="fake:pass1234",
                            display_name="周明远",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.SUPER_ADMIN.value,
                            auth_provider="local",
                            created_at=NOW,
                            updated_at=NOW,
                            deleted_time=None,
                        ),
                        UserModel(
                            id="mock_fin_member",
                            email="fin.member@xingchen.com",
                            password_hash="fake:pass1234",
                            display_name="沈佳禾",
                            status=UserStatus.ACTIVE.value,
                            role=UserRole.MEMBER.value,
                            auth_provider="local",
                            created_at=NOW,
                            updated_at=NOW,
                            deleted_time=None,
                        ),
                    ]
                )
                await session.flush()
                session.add_all(
                    [
                        UserDepartmentModel(
                            user_id="mock_super_admin",
                            department_id="dept_mock_root",
                            is_primary=True,
                        ),
                        UserDepartmentModel(
                            user_id="mock_fin_member",
                            department_id="dept_mock_fin",
                            is_primary=True,
                        ),
                    ]
                )

            # 2. 装配全部真实权限引擎、认证、组织与四条业务线领域服务。
            access_repo = SqlAlchemyAccessControlRepository(session_factory)
            access_policy = AccessPolicyService(access_repo, clock=lambda: NOW)
            auth_repo = SqlAlchemyAuthRepository(session_factory)
            org_repo = SqlAlchemyOrgRepository(session_factory)
            auth_service = AuthService(
                auth_repo,
                _DeterministicPasswordHasher(),
                org_reader=org_repo,
                clock=lambda: NOW,
            )
            org_service = OrgService(org_repo, auth_repo, clock=lambda: NOW)
            kb_service = KnowledgeService(
                KnowledgeRepository(session_factory),
                _SimpleTokenizer(),
                originals=_InMemoryOriginalStore(),
                access_policy=access_policy,
                clock=lambda: NOW,
            )
            skill_service = SkillService(
                SqlAlchemySkillRepository(session_factory),
                cast(Any, _InMemorySkillObjectStore()),
                auth_repo,
                access_policy=access_policy,
                clock=lambda: NOW,
            )
            mcp_repo = SqlAlchemyMcpRepository(
                session_factory,
                McpSecretCipher(b"k" * 32),
                clock=lambda: NOW,
            )
            tool_manager = ToolManager(
                mcp_repo,
                _StaticMcpDiscovery(
                    {
                        "https://hroa-fin-ops.local/mcp": (
                            DiscoveredMcpTool(
                                remote_name="hroa_list_travel_expenses",
                                description="财务差旅报销导出",
                                input_schema={"type": "object", "properties": {}},
                            ),
                        ),
                        "https://hroa-hr-ops.local/mcp": (
                            DiscoveredMcpTool(
                                remote_name="hroa_list_department_roster",
                                description="HR 花名册导出",
                                input_schema={"type": "object", "properties": {}},
                            ),
                        ),
                    }
                ),
                auth_repo,
                access_policy=access_policy,
                clock=lambda: NOW,
            )
            subagent_service = SubagentService(
                SqlAlchemySubagentRepository(session_factory),
                auth_repo,
                access_policy=access_policy,
                model_checker=_AlwaysValidModelChecker(),
                tool_catalog=_DbToolCatalog(mcp_repo),
                clock=lambda: NOW,
            )

            # 3. 真实登录超管与财务专员，签发真实会话令牌与 auth_session_id。
            super_issued = await auth_service.login("super@xingchen.com", "pass1234")
            super_auth = await auth_service.authenticate(super_issued.token)
            assert super_auth is not None
            emp_issued = await auth_service.login("fin.member@xingchen.com", "pass1234")
            emp_auth_before = await auth_service.authenticate(emp_issued.token)
            assert emp_auth_before is not None
            original_session_id = emp_auth_before.session_id

            # 4. 按企业真实权限矩阵创建 KB-1/KB-2/KB-3、SK-7、hroa-fin-ops/hr-ops、SUB-1/SUB-2。
            kb1 = await kb_service.create_base(
                "mock_super_admin",
                "星辰智联公司制度总库",
                "公司级",
                visibility_scope=VisibilityScope.COMPANY,
            )
            kb2 = await kb_service.create_base(
                "mock_super_admin",
                "HR 业务 SOP 与台账库",
                "HR 专属",
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_mock_hr",
                visibility_grants=(
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_hr", True),
                ),
            )
            kb3 = await kb_service.create_base(
                "mock_super_admin",
                "财务制度与跨部门协同库",
                "财务归属并跨部门授权 HR",
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_mock_fin",
                visibility_grants=(
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_fin", True),
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_hr", True),
                ),
            )
            sk7_imported = await skill_service.import_package(
                super_auth,
                SK7_ZIP_PATH,
                idempotency_key="v3-reg2-sk7-import",
                scope=SkillScope.DEPARTMENT,
                owner_department_id="dept_mock_fin",
                visibility_grants=(
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_fin", True),
                ),
            )
            await skill_service.configure(
                super_auth,
                sk7_imported.skill.id,
                expected_revision=sk7_imported.skill.revision,
                enabled=True,
                current_version_id=sk7_imported.versions[0].id,
                script_requires_approval=True,
            )
            fin_mcp = await tool_manager.register_mcp(
                super_auth,
                "hroa-fin-ops",
                StreamableHttpConfig(
                    transport="streamable_http",
                    url="https://hroa-fin-ops.local/mcp",
                ),
                {},
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_mock_fin",
                visibility_grants=(
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_fin", True),
                ),
            )
            hr_mcp = await tool_manager.register_mcp(
                super_auth,
                "hroa-hr-ops",
                StreamableHttpConfig(
                    transport="streamable_http",
                    url="https://hroa-hr-ops.local/mcp",
                ),
                {},
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_department_id="dept_mock_hr",
                visibility_grants=(
                    VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_hr", True),
                ),
            )
            sub1 = await subagent_service.create_subagent(
                super_auth,
                CreateSubagentCommand(
                    slug="hr-policy-auditor",
                    name="HR 制度核验专员",
                    description="HR 专属",
                    system_prompt="HR 专属核验",
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_mock_hr",
                    visibility_grants=(
                        VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_hr", True),
                    ),
                ),
            )
            sub2 = await subagent_service.create_subagent(
                super_auth,
                CreateSubagentCommand(
                    slug="finance-travel-auditor",
                    name="财务差旅稽核专员",
                    description="财务归属并跨部门授权 HR",
                    system_prompt="差旅稽核",
                    visibility_scope=VisibilityScope.DEPARTMENT,
                    owner_department_id="dept_mock_fin",
                    visibility_grants=(
                        VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_fin", True),
                        VisibilityGrantInput(GrantTargetType.DEPARTMENT, "dept_mock_hr", True),
                    ),
                ),
            )

            sub1_target = ResourceAccessTarget(
                resource_type=ResourceType.SUBAGENT,
                resource_id=sub1.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id=sub1.created_by,
                owner_department_id="dept_mock_hr",
            )
            sub2_target = ResourceAccessTarget(
                resource_type=ResourceType.SUBAGENT,
                resource_id=sub2.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id=sub2.created_by,
                owner_department_id="dept_mock_fin",
            )
            kb1_target = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id=kb1.id,
                visibility_scope=VisibilityScope.COMPANY,
                owner_id="mock_super_admin",
                owner_department_id=None,
            )
            kb2_target = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id=kb2.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="mock_super_admin",
                owner_department_id="dept_mock_hr",
            )
            kb3_target = ResourceAccessTarget(
                resource_type=ResourceType.KNOWLEDGE_BASE,
                resource_id=kb3.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="mock_super_admin",
                owner_department_id="dept_mock_fin",
            )
            sk7_target = ResourceAccessTarget(
                resource_type=ResourceType.SKILL,
                resource_id=sk7_imported.skill.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="mock_super_admin",
                owner_department_id="dept_mock_fin",
            )
            fin_mcp_target = ResourceAccessTarget(
                resource_type=ResourceType.MCP_SERVER,
                resource_id=fin_mcp.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="mock_super_admin",
                owner_department_id="dept_mock_fin",
            )
            hr_mcp_target = ResourceAccessTarget(
                resource_type=ResourceType.MCP_SERVER,
                resource_id=hr_mcp.id,
                visibility_scope=VisibilityScope.DEPARTMENT,
                owner_id="mock_super_admin",
                owner_department_id="dept_mock_hr",
            )

            # 5. 调岗前（dept_mock_fin）：可读 KB-3/SK-7/fin-ops/SUB-2，不可读 KB-2/hr-ops/SUB-1。
            kb_ids_before = {
                b.id for b in await kb_service.list_bases("mock_fin_member", page=1, page_size=50)
            }
            skill_slugs_before = {
                s.slug for s in await skill_service.user_directory(emp_auth_before)
            }
            tool_names_before = {
                t.description
                for t in (await tool_manager.list_public_tools(emp_auth_before, limit=50)).items
            }
            assert kb3.id in kb_ids_before and kb2.id not in kb_ids_before
            assert "travel-expense-audit-pack" in skill_slugs_before
            assert "财务差旅报销导出" in tool_names_before
            assert "HR 花名册导出" not in tool_names_before
            assert await access_policy.can_read_target("mock_fin_member", sub2_target) is True
            assert await access_policy.can_read_target("mock_fin_member", sub1_target) is False

            # 6. 仅更新 user_departments 从 dept_mock_fin 平调至 dept_mock_hr（不重新登录）。
            async with session_factory() as session, session.begin():
                await session.execute(
                    update(UserDepartmentModel)
                    .where(UserDepartmentModel.user_id == "mock_fin_member")
                    .values(department_id="dept_mock_hr")
                )

            # 复用同一会话令牌鉴权，确认 session_id 保持不变且权限在数据库实时重算生效！
            emp_auth_after_transfer = await auth_service.authenticate(emp_issued.token)
            assert emp_auth_after_transfer is not None
            assert emp_auth_after_transfer.session_id == original_session_id

            kb_ids_after = {
                b.id for b in await kb_service.list_bases("mock_fin_member", page=1, page_size=50)
            }
            skill_slugs_after = {
                s.slug for s in await skill_service.user_directory(emp_auth_after_transfer)
            }
            tool_names_after = {
                t.description
                for t in (
                    await tool_manager.list_public_tools(emp_auth_after_transfer, limit=50)
                ).items
            }
            # 财务专属 SK-7 与 hroa-fin-ops 实时撤权；HR 专属 KB-2/hroa-hr-ops/SUB-1 实时获得；
            # 跨部门共享 KB-3 与 SUB-2 继续保持可读。
            assert "travel-expense-audit-pack" not in skill_slugs_after
            assert "财务差旅报销导出" not in tool_names_after
            assert "HR 花名册导出" in tool_names_after
            assert kb2.id in kb_ids_after and kb3.id in kb_ids_after
            assert await access_policy.can_read_target("mock_fin_member", sk7_target) is False
            assert await access_policy.can_read_target("mock_fin_member", fin_mcp_target) is False
            assert await access_policy.can_read_target("mock_fin_member", hr_mcp_target) is True
            assert await access_policy.can_read_target("mock_fin_member", sub1_target) is True
            assert await access_policy.can_read_target("mock_fin_member", sub2_target) is True

            # 7. 模拟离职停用：通过 OrgService.update_user_status 停用账号并同事务吊销原 Cookie。
            disabled_detail = await org_service.update_user_status(
                super_auth,
                "mock_fin_member",
                status=UserStatus.DISABLED,
            )
            assert disabled_detail.user.status is UserStatus.DISABLED
            assert (
                await auth_repo.get_current_session_user(
                    "mock_fin_member", original_session_id, NOW
                )
                is None
            )
            assert await auth_service.authenticate(emp_issued.token) is None

            # 停用后对任何公司级/部门级资源均不可读。
            for target in (
                kb1_target,
                kb2_target,
                kb3_target,
                sk7_target,
                fin_mcp_target,
                hr_mcp_target,
                sub1_target,
                sub2_target,
            ):
                assert await access_policy.can_read_target("mock_fin_member", target) is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 回归 3：50 条满额记忆配额 + 高密度 Token 截断基准（真实 SQLAlchemy 仓储）
# ---------------------------------------------------------------------------


def test_regression3_fifty_memory_quota_and_high_density_token_truncation_benchmark(
    tmp_path: Path,
) -> None:
    """回归 3：真实 PreferenceRepository 验证 50 条配额上限与双编码器 1500 Token 整条裁剪。"""

    async def scenario() -> None:
        engine, session_factory = await _create_sqlite_db(tmp_path / "preference_quota.db")
        try:
            async with session_factory() as session, session.begin():
                session.add(
                    UserModel(
                        id="mock_hr_member",
                        email="hr.member@xingchen.com",
                        password_hash="fake:pass1234",
                        display_name="苏晚晴",
                        status=UserStatus.ACTIVE.value,
                        role=UserRole.MEMBER.value,
                        auth_provider="local",
                        created_at=NOW,
                        updated_at=NOW,
                        deleted_time=None,
                    )
                )

            tick = 0

            def _stepping_clock() -> datetime:
                nonlocal tick
                tick += 1
                return NOW + timedelta(minutes=tick)

            repo = SqlAlchemyPreferenceRepository(session_factory)
            service = PreferenceService(
                repo,
                max_memories=MAX_USER_MEMORIES_PER_USER,
                clock=_stepping_clock,
            )
            await service.update_profile(
                "mock_hr_member",
                custom_instructions="我是星辰智联 HRBP 苏晚晴（MOCK006），负责全生命周期合规稽核。",
                memory_enabled=True,
            )

            # 1. 写入 1..50 条高密度企业备忘（每条 > 110 tokens，50 条总计 > 5,000 tokens）。
            created_ids: list[str] = []
            for idx in range(1, MAX_USER_MEMORIES_PER_USER + 1):
                content = (
                    f"星辰智联 2026-12 满额备忘 #{idx:02d}："
                    "请持续核查员工 XC1003 韩景明离职四项清查阻断项"
                    "（未还设备 IT-MBP-2023-088 / 序列号 XC-SN-88412、"
                    "未核销差旅备用金 FIN-ADV-2026-0914 共 2,400.00 元），"
                    "同步跟进 XC1004 郑语桐 12-01 调入人力资源部后的年假 4.0 天结转"
                    "与原财务 ERP 权限 3 工作日回收，并提醒 XC1008 方若琳 60 天 PIP "
                    "及 2026-12-02 申诉截止窗口与跨年法定年假结转上限 3.0 天至 "
                    f"2027-03-31。[END-{idx:02d}]"
                )
                item = await service.create_memory(
                    "mock_hr_member",
                    content,
                    source=UserMemorySource.MANUAL,
                )
                created_ids.append(item.id)

            listing_full = await service.list_memories("mock_hr_member")
            assert listing_full.total == 50
            assert listing_full.limit == 50

            # 2. 第 51 条必须精确抛出 UserMemoryLimitReachedError。
            with pytest.raises(UserMemoryLimitReachedError, match="user_memory_limit_reached"):
                await service.create_memory(
                    "mock_hr_member",
                    "超出 50 条配额硬上限的第 51 条备忘应被真实数据库事务拦截。",
                    source=UserMemorySource.MANUAL,
                )

            # 3. 删除 1 条最旧记忆后重新写入第 51 号最新记忆须立即成功。
            await service.delete_memory("mock_hr_member", created_ids[0])
            assert (await service.list_memories("mock_hr_member")).total == 49
            newest_content = (
                "星辰智联 2026-12 最新高优备忘 #51：年末封网期 2026-12-24 至 2027-01-03 严格执行 "
                "XC-RD-ARCH-032 双签规范，同步核对 TRV-2026-1203 顾行知北京超标 340.00 元与 "
                "TRV-2026-1206 许知夏深圳超标 260.00 元及逾期备用金 FIN-ADV-2026-1122"
                "（1,800.00 元），确认 2026-12-25 18:00 年度财务关账前全量结清。[END-51]"
            )
            newest_item = await service.create_memory(
                "mock_hr_member",
                newest_content,
                source=UserMemorySource.MANUAL,
            )
            assert newest_item.content == newest_content

            # 4. 读取真实仓储渲染上下文：校验 50 条 > 5,000 tokens 且双编码器严守 1500 Token。
            context = await service.get_context("mock_hr_member")
            assert len(context.memories) == 50
            assert context.memories[0].id == newest_item.id
            raw_joined = "\n".join(f"- {item.content}" for item in context.memories)

            for enc_name in ("cl100k_base", "o200k_base"):
                encoding = tiktoken.get_encoding(enc_name)
                total_raw_tokens = len(encoding.encode_ordinary(raw_joined))
                assert total_raw_tokens > 5000, f"density_too_low:{enc_name}:{total_raw_tokens}"

                rendered = render_preference_context(
                    context,
                    tokenizer_encoding=enc_name,
                    memory_token_budget=DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET,
                )
                assert rendered is not None
                start_tag = "<user_explicit_memories>\n"
                end_tag = "\n</user_explicit_memories>"
                body = rendered[
                    rendered.index(start_tag) + len(start_tag) : rendered.index(end_tag)
                ]
                lines = body.splitlines()

                # 必然触发预算裁剪、总 Token <= 1500、最新一条在首行且每条正文 100% 完整无半句截断。
                assert 0 < len(lines) < 50
                assert len(encoding.encode_ordinary(body)) <= DEFAULT_PREFERENCE_MEMORY_TOKEN_BUDGET
                assert lines[0] == f"- {newest_content}"
                for idx, line in enumerate(lines):
                    assert line == f"- {context.memories[idx].content}"
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 回归 4：多月份（2026-11 vs 2026-12）真实 MCP + SK-5 / SK-7 沙箱端到端闭环
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _hroa_stdio_client(profile: str, state_file: Path) -> AsyncIterator[Client]:
    """启动真实 stdio 子进程并连接指定 Profile 的 HR/OA MCP Server。"""
    transport = McpStdioTransport(
        sys.executable,
        (
            str(HROA_SERVER_PATH),
            "--profile",
            profile,
            "--state-file",
            str(state_file),
        ),
        cwd=str(HROA_DIR),
        base_environment={"PATH": os.defpath},
        maximum_line_bytes=2 * 1024 * 1024,
        maximum_total_bytes=2 * 1024 * 1024,
    )
    async with Client(transport, mode="auto", read_timeout_seconds=10, cache=None) as client:
        yield client


def _extract_skill_zip(zip_path: Path, work_dir: Path) -> None:
    """展开 Skill ZIP 包至临时沙箱目录并准备 outputs 目录。"""
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(work_dir)
    (work_dir / "outputs").mkdir()


def _read_excel_rows(workbook_path: Path, sheet_name: str) -> list[dict[str, object]]:
    """读取 Excel 工作表全部数据行并按首行表头构造字典列表。"""
    wb = load_workbook(workbook_path, data_only=True, read_only=True)
    try:
        sheet = wb[sheet_name]
        rows_iter = sheet.iter_rows(values_only=True)
        headers = tuple(str(col) for col in next(rows_iter))
        return [
            {header: val for header, val in zip(headers, row, strict=True)} for row in rows_iter
        ]
    finally:
        wb.close()


def _mcp_error_text(result: Any) -> str:
    """提取 MCP ToolError 返回文本。"""
    return "\n".join(
        str(getattr(block, "text", ""))
        for block in getattr(result, "content", ())
        if isinstance(getattr(block, "text", None), str)
    )


def test_regression4_multimonth_mcp_and_sk5_sk7_sandbox_e2e_closed_loop(tmp_path: Path) -> None:
    """回归 4：真实 hr_ops/fin_ops stdio MCP 双月份切换 + SK-5/SK-7 沙箱闭环及非法月份拦截。"""
    state_file = tmp_path / "v3-multimonth-state.json"

    async def scenario() -> None:
        sk5_schema = SkillPackageValidator().validate(SK5_ZIP_PATH).manifest.scripts[0].parameters
        sk7_schema = SkillPackageValidator().validate(SK7_ZIP_PATH).manifest.scripts[0].parameters

        # 1. hr_ops MCP：校验 2026-11/2026-12 花名册跃迁、month_unsupported 及 SK-5 Schema。
        async with _hroa_stdio_client("hr_ops", state_file) as hr:
            bad_roster = await hr.call_tool(
                "hroa_list_department_roster",
                {"department_id": "ALL", "month": "2026-10"},
            )
            assert bad_roster.is_error is True
            assert "month_unsupported" in _mcp_error_text(bad_roster)

            r11_res = await hr.call_tool(
                "hroa_list_department_roster",
                {"department_id": "ALL", "month": "2026-11"},
            )
            assert r11_res.is_error is False
            roster_11 = cast(dict[str, object], r11_res.structured_content)
            assert "year_end_carryover_rule" not in roster_11
            assert (
                validate_parameter_schema(
                    sk5_schema,
                    {"month": "2026-11", "department_id": "ALL", "hroa_inputs": roster_11},
                )
                is None
            )

            r12_res = await hr.call_tool(
                "hroa_list_department_roster",
                {"department_id": "ALL", "month": "2026-12"},
            )
            assert r12_res.is_error is False
            roster_12 = cast(dict[str, object], r12_res.structured_content)
            assert roster_12["month"] == "2026-12"
            assert roster_12["baseline_date"] == "2026-12-20"
            carryover = cast(dict[str, object], roster_12["year_end_carryover_rule"])
            assert carryover["statutory_annual_carryover_cap_days"] == 3.0
            assert carryover["statutory_carryover_expire_date"] == "2027-03-31"
            assert carryover["tenure_annual_carryover_allowed"] is False

            # 12 月 MCP 返回结构须 100% 通过 SK-5 Draft 2020-12 additionalProperties=false 校验。
            schema_err_12 = validate_parameter_schema(
                sk5_schema,
                {"month": "2026-12", "department_id": "ALL", "hroa_inputs": roster_12},
            )
            assert schema_err_12 is None, f"sk5_2026_12_schema_error:{schema_err_12}"

            emp12_by_no = {
                str(e["employee_no"]): e
                for e in cast(list[dict[str, object]], roster_12["employees"])
            }
            assert emp12_by_no["XC1001"]["employment_status"] == "probation"
            assert emp12_by_no["XC1002"]["employment_status"] == "active"
            assert emp12_by_no["XC1004"]["department_id"] == "dept_mock_hr"
            assert emp12_by_no["XC1004"]["job_title"] == "薪酬绩效专员"
            assert emp12_by_no["XC1007"]["level"] == "P6"
            assert emp12_by_no["XC1003"]["last_working_day"] == "2026-12-09"

            # 校验 12 月单部门视图：XC1004 郑语桐已归属 dept_mock_hr 而不再归属 dept_mock_fin。
            hr_dept_12 = cast(
                dict[str, Any],
                (
                    await hr.call_tool(
                        "hroa_list_department_roster",
                        {"department_id": "dept_mock_hr", "month": "2026-12"},
                    )
                ).structured_content,
            )
            assert "XC1004" in {e["employee_no"] for e in hr_dept_12["employees"]}

        # 2. 沙箱执行 SK-5 生成 hroa-audit-2026-12.xlsx 与摘要，核验 12 月状态分布与跨年结转。
        work_sk5 = tmp_path / "sandbox-sk5-2026-12"
        work_sk5.mkdir()
        _extract_skill_zip(SK5_ZIP_PATH, work_sk5)
        req_sk5 = ScriptExecutionRequest(
            working_directory=work_sk5,
            script_path="scripts/build_hroa_audit.py",
            parameters={
                "month": "2026-12",
                "department_id": "ALL",
                "hroa_inputs": cast(JsonValue, roster_12),
            },
            input_files={},
            output_directory=work_sk5 / "outputs",
            run_id="run_v3_sk5_12",
            tool_call_id="call_v3_sk5_12",
            worker_instance_id="worker_v3_sk5_12",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_sk5) as exec_sk5:
            assert exec_sk5.succeeded, exec_sk5.stderr.decode(errors="replace")
            sk5_outputs = tuple(exec_sk5.outputs)
            assert [o.path for o in sk5_outputs] == [
                "hroa-audit-2026-12.xlsx",
                "hroa-audit-2026-12-summary.txt",
            ]
            overview_rows = {
                str(r["稽核指标"]): r["数值/范围"]
                for r in _read_excel_rows(sk5_outputs[0].source_path, "汇总总览")
            }
            assert overview_rows["在职人数 (active)"] == 11
            assert overview_rows["待入职人数 (preboarding)"] == 0
            assert overview_rows["试用期人数 (probation)"] == 1
            assert overview_rows["离职办理中 (offboarding)"] == 1
            assert overview_rows["已离职归档 (resigned)"] == 1
            assert "跨年年假结转规则" in overview_rows

            onb_rows_12 = _read_excel_rows(sk5_outputs[0].source_path, "入职与试用期跟踪")
            assert {str(r["员工工号"]) for r in onb_rows_12} == {"XC1001"}
            assert onb_rows_12[0]["阶段"] == "试用期待转正"

            sk5_summary = sk5_outputs[1].source_path.read_text(encoding="utf-8")
            assert "3.0 天" in sk5_summary and "2027-03-31" in sk5_summary

        # 3. fin_ops MCP：校验 2026-11/2026-12 差旅单、month_unsupported 及 12 月稽核回读。
        async with _hroa_stdio_client("fin_ops", state_file) as fin:
            bad_travel = await fin.call_tool(
                "hroa_list_travel_expenses",
                {"month": "2026-10", "department_id": "ALL"},
            )
            assert bad_travel.is_error is True
            assert "month_unsupported" in _mcp_error_text(bad_travel)

            t11_res = await fin.call_tool(
                "hroa_list_travel_expenses",
                {"month": "2026-11", "department_id": "ALL"},
            )
            assert t11_res.is_error is False
            travel_11 = cast(dict[str, object], t11_res.structured_content)
            assert len(cast(list[object], travel_11["expenses"])) == 6
            assert (
                validate_parameter_schema(
                    sk7_schema,
                    {"month": "2026-11", "department_id": "ALL", "travel_inputs": travel_11},
                )
                is None
            )

            t12_res = await fin.call_tool(
                "hroa_list_travel_expenses",
                {"month": "2026-12", "department_id": "ALL"},
            )
            assert t12_res.is_error is False
            travel_12 = cast(dict[str, object], t12_res.structured_content)
            assert travel_12["month"] == "2026-12"
            assert len(cast(list[object], travel_12["expenses"])) == 6
            assert (
                validate_parameter_schema(
                    sk7_schema,
                    {"month": "2026-12", "department_id": "ALL", "travel_inputs": travel_12},
                )
                is None
            )

            # 对 2026-12 差旅单 TRV-2026-1203 持久化稽核结论。
            flag_1203 = await fin.call_tool(
                "hroa_flag_expense_audit_result",
                {
                    "expense_id": "TRV-2026-1203",
                    "decision": "rejected_over_limit",
                    "audit_note": "12 月北京住宿 820.00/晚 × 2 晚超标 340.00 元，需核减或补交特批",
                    "auditor_no": "MOCK002",
                },
            )
            assert flag_1203.is_error is False

        # 跨进程重新连接 fin_ops 回读 2026-12 持久化稽核状态。
        async with _hroa_stdio_client("fin_ops", state_file) as fin_again:
            t12_after = cast(
                dict[str, Any],
                (
                    await fin_again.call_tool(
                        "hroa_list_travel_expenses",
                        {"month": "2026-12", "department_id": "dept_mock_rd"},
                    )
                ).structured_content,
            )
            exp_1203 = next(e for e in t12_after["expenses"] if e["expense_id"] == "TRV-2026-1203")
            assert exp_1203["audit_status"] == "rejected_over_limit"
            assert exp_1203["audited_by"] == "MOCK002"

        # 4. 沙箱执行 SK-7 生成 travel-audit-2026-12.xlsx 与摘要，核验 6 笔差旅单金额与判定。
        work_sk7 = tmp_path / "sandbox-sk7-2026-12"
        work_sk7.mkdir()
        _extract_skill_zip(SK7_ZIP_PATH, work_sk7)
        req_sk7 = ScriptExecutionRequest(
            working_directory=work_sk7,
            script_path="scripts/audit_travel_expenses.py",
            parameters={
                "month": "2026-12",
                "department_id": "ALL",
                "travel_inputs": cast(JsonValue, travel_12),
            },
            input_files={},
            output_directory=work_sk7 / "outputs",
            run_id="run_v3_sk7_12",
            tool_call_id="call_v3_sk7_12",
            worker_instance_id="worker_v3_sk7_12",
        )
        async with PythonScriptExecutor(Path(sys.executable)).execute(req_sk7) as exec_sk7:
            assert exec_sk7.succeeded, exec_sk7.stderr.decode(errors="replace")
            sk7_outputs = tuple(exec_sk7.outputs)
            assert [o.path for o in sk7_outputs] == [
                "travel-audit-2026-12.xlsx",
                "travel-audit-2026-12-summary.txt",
            ]
            sk7_stdout = json.loads(exec_sk7.stdout)
            assert sk7_stdout["total_expenses"] == 6
            assert sk7_stdout["compliant_count"] == 2
            assert sk7_stdout["violation_count"] == 4
            assert sk7_stdout["advance_alert_count"] == 1
            assert sk7_stdout["total_over_limit_cny"] == 690.0

            viol_12 = {
                str(r["报销单号"]): r
                for r in _read_excel_rows(sk7_outputs[0].source_path, "超标与违规明细")
            }
            pass_12 = {
                str(r["报销单号"]): r
                for r in _read_excel_rows(sk7_outputs[0].source_path, "合规放行清单")
            }
            adv_12 = _read_excel_rows(sk7_outputs[0].source_path, "备用金逾期预警")

            assert set(pass_12) == {"TRV-2026-1201", "TRV-2026-1202"}
            assert pass_12["TRV-2026-1202"]["部门"] == "人力资源部"

            assert set(viol_12) == {
                "TRV-2026-1203",
                "TRV-2026-1204",
                "TRV-2026-1205",
                "TRV-2026-1206",
            }
            assert viol_12["TRV-2026-1203"]["住宿超标(元)"] == 340.0
            assert viol_12["TRV-2026-1204"]["餐补超标(元)"] == 90.0
            assert viol_12["TRV-2026-1205"]["交通舱位"] == "first"
            assert viol_12["TRV-2026-1205"]["交通金额(元)"] == 5600.0
            assert viol_12["TRV-2026-1205"]["稽核判定"] == "舱位违规拦截"
            assert viol_12["TRV-2026-1206"]["住宿超标(元)"] == 260.0
            assert viol_12["TRV-2026-1206"]["稽核判定"] == "超标且备用金逾期双拦截"

            assert len(adv_12) == 1
            assert adv_12[0]["报销单号"] == "TRV-2026-1206"
            assert adv_12[0]["备用金单号"] == "FIN-ADV-2026-1122"
            assert adv_12[0]["未核销金额(元)"] == 1800.0

            sk7_summary = sk7_outputs[1].source_path.read_text(encoding="utf-8")
            for anchor in (
                "TRV-2026-1201",
                "TRV-2026-1202",
                "TRV-2026-1203",
                "340.00",
                "TRV-2026-1204",
                "90.00",
                "TRV-2026-1205",
                "5,600.00",
                "TRV-2026-1206",
                "260.00",
                "FIN-ADV-2026-1122",
                "1,800.00",
            ):
                assert anchor in sk7_summary, f"sk7_12_summary_missing:{anchor}"

    asyncio.run(scenario())

    # 5. 验证 --reset-state 可将 2026-12 差旅稽核结论复位回 pending_audit。
    reset_proc = subprocess.run(
        [sys.executable, str(HROA_SERVER_PATH), "--state-file", str(state_file), "--reset-state"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert reset_proc.returncode == 0, reset_proc.stderr

    async def verify_reset() -> None:
        async with _hroa_stdio_client("fin_ops", state_file) as fin_reset:
            data = cast(
                dict[str, Any],
                (
                    await fin_reset.call_tool(
                        "hroa_list_travel_expenses",
                        {"month": "2026-12", "department_id": "dept_mock_rd"},
                    )
                ).structured_content,
            )
            exp_1203 = next(e for e in data["expenses"] if e["expense_id"] == "TRV-2026-1203")
            assert exp_1203["audit_status"] == "pending_audit"

    asyncio.run(verify_reset())
