"""收紧后 API Response DTO 与转换器的运行时 JSON 兼容性及边界校验测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.converter.auth import user_to_response
from api.converter.knowledge import knowledge_version_to_response
from api.converter.llm import model_profile_to_response
from api.converter.mcp import mcp_server_to_response
from api.converter.run import run_to_response, tool_call_to_response
from api.converter.skill import dependency_check_to_response, execution_to_response
from api.dto.file import SessionFileResponse
from api.dto.knowledge import KnowledgeImportResponse
from api.dto.llm import ModelProfileResponse
from api.dto.mcp import (
    McpHttpConfigResponse,
    McpSecretMetadataResponse,
    McpServerResponse,
    McpStdioConfigResponse,
)
from api.dto.message import ConversationMessageResponse
from api.dto.project import ProjectResponse
from api.dto.session import SessionResponse
from api.dto.skill import SkillDependencyCheckResponse
from domain.access import VisibilityScope
from domain.auth import AuthProvider, User, UserRole, UserStatus
from domain.file import SessionFile, SessionFileExtractionStatus, SessionFileSource
from domain.knowledge.model import DocumentState, DocumentVersion
from domain.llm.model.profile import ModelProfileVersion
from domain.mcp import McpSecretMetadata, McpServer, StdioConfig, StreamableHttpConfig
from domain.message import (
    ConversationMessage,
    ConversationRole,
    MessageMetadata,
    MessageStatus,
    TextPart,
    ToolResultPart,
    ToolResultStatus,
)
from domain.project import Project
from domain.run.model import (
    InputRequestStatus,
    PendingSubagentApproval,
    Run,
    RunAttemptUsage,
    RunInputInteraction,
    RunInputRequest,
    RunModelSnapshot,
    RunStatus,
    RunUsage,
    SubagentTaskSummary,
)
from domain.session import Session
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    RunSkillEntry,
    RunSkillSnapshot,
    SkillExecutionRecord,
    SkillExecutionStatus,
    SkillScope,
)
from domain.tool import (
    PendingToolApproval,
    RunToolCallDetails,
    RunToolCallRecord,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
)

NOW = datetime(2026, 11, 27, 8, 0, 0, tzinfo=UTC)


def test_mcp_server_to_response_json_is_byte_compatible_for_http_and_stdio() -> None:
    """HTTP 与 Stdio MCP Server 转换后的 JSON 键名、null 与空数组表现须与历史完全一致。"""
    http_server = McpServer(
        id="mcp_http_1",
        name="Remote Docs",
        config=StreamableHttpConfig(transport="streamable_http", url="https://mcp.example.com"),
        secret_metadata=(
            McpSecretMetadata(name="Authorization", has_value=True),
            McpSecretMetadata(name="X-Api-Key", has_value=False),
        ),
        key_version=1,
        config_version=2,
        created_by="usr_admin",
        created_at=NOW,
        updated_at=NOW,
        tools_discovered_at=NOW,
        visibility_scope=VisibilityScope.COMPANY,
        owner_department_id=None,
        visibility_grants=(),
        enabled=True,
    )
    http_dto = mcp_server_to_response(http_server)
    assert isinstance(http_dto.config, McpHttpConfigResponse)
    assert all(isinstance(item, McpSecretMetadataResponse) for item in http_dto.secrets)
    http_payload = json.loads(http_dto.model_dump_json())
    assert http_payload["transport"] == "streamable_http"
    assert http_payload["config"] == {"url": "https://mcp.example.com"}
    assert http_payload["secrets"] == [
        {"name": "Authorization", "has_value": True},
        {"name": "X-Api-Key", "has_value": False},
    ]

    # 验证 Stdio 模式在 args 为空元组、cwd 为 None、secrets 为空时的精确 JSON 表现。
    stdio_server = McpServer(
        id="mcp_stdio_1",
        name="Local CLI",
        config=StdioConfig(transport="stdio", command="/usr/bin/mcp-cli", args=(), cwd=None),
        secret_metadata=(),
        key_version=None,
        config_version=1,
        created_by=None,
        created_at=NOW,
        updated_at=NOW,
        tools_discovered_at=None,
        visibility_scope=VisibilityScope.DEPARTMENT,
        owner_department_id="dept_1",
        visibility_grants=(),
        enabled=False,
    )
    stdio_dto = mcp_server_to_response(stdio_server)
    assert isinstance(stdio_dto.config, McpStdioConfigResponse)
    stdio_payload = json.loads(stdio_dto.model_dump_json())
    assert stdio_payload["transport"] == "stdio"
    assert stdio_payload["config"] == {
        "command": "/usr/bin/mcp-cli",
        "args": [],
        "cwd": None,
    }
    assert stdio_payload["secrets"] == []


def test_tightened_enum_converters_serialize_to_plain_json_strings() -> None:
    """所有收紧为 StrEnum / Literal 的响应 DTO 在 JSON 序列化后仍为稳定明文字符串。"""
    profile = ModelProfileVersion(
        id="llm_1",
        name="GPT-4o",
        protocol="chat_completions",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        context_window_tokens=128000,
        tokenizer_encoding="o200k_base",
        supports_stream=True,
        supports_tools=True,
        max_output_tokens=8192,
        request_timeout_seconds=120,
        execution_timeout_seconds=300,
        max_concurrency=4,
        temperature=0.2,
        enabled=True,
        created_at=NOW,
        updated_at=NOW,
    )
    profile_json = json.loads(model_profile_to_response(profile).model_dump_json())
    assert profile_json["protocol"] == "chat_completions"
    assert profile_json["tokenizer_encoding"] == "o200k_base"

    session_file = SessionFile(
        id="file_1",
        session_id="sess_1",
        owner_id="usr_1",
        source=SessionFileSource.ARTIFACT,
        filename="report.pdf",
        label="分析报告",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="a" * 64,
        object_key="sessions/sess_1/file_1",
        source_call_id="call_1",
        output_index=0,
        created_at=NOW,
        extraction_status=SessionFileExtractionStatus.READY,
        extracted_text_bytes=512,
    )
    file_json = json.loads(SessionFileResponse.model_validate(session_file).model_dump_json())
    assert file_json["source"] == "artifact"
    assert file_json["extraction_status"] == "ready"

    kb_version = DocumentVersion(
        id="ver_1",
        document_id="doc_1",
        state=DocumentState.PROCESSING,
        error_code=None,
        warnings=(),
        chunk_count=0,
        created_at=NOW,
        completed_at=None,
    )
    kb_version_json = json.loads(knowledge_version_to_response(kb_version).model_dump_json())
    assert kb_version_json["state"] == "processing"

    kb_import_json = json.loads(
        KnowledgeImportResponse(
            document_id="doc_1",
            version_id="ver_1",
            state=DocumentState.QUEUED,
            status_url="/api/v1/knowledge-bases/kb_1/documents/doc_1",
        ).model_dump_json()
    )
    assert kb_import_json["state"] == "queued"

    dep_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.12.0",
        python_implementation="CPython",
        items=(),
        checked_at=NOW,
    )
    assert (
        json.loads(dependency_check_to_response(dep_check).model_dump_json())["status"]
        == "satisfied"
    )

    skill_exec = SkillExecutionRecord(
        id="exec_1",
        run_id="run_1",
        session_id="sess_1",
        tool_call_record_id="call_1",
        skill_id="skill_1",
        skill_version_id="sver_1",
        script_path="scripts/run.py",
        input_file_ids=("file_1",),
        parameters_sha256="b" * 64,
        python_version="3.12.0",
        dependency_check=dep_check,
        status=SkillExecutionStatus.SUCCEEDED,
        log_object_key="logs/exec_1.txt",
        log_truncated=False,
        log_error_code=None,
        error_code=None,
        cleanup_error_code=None,
        result_artifacts=(),
        created_at=NOW,
        finished_at=NOW,
    )
    assert json.loads(execution_to_response(skill_exec).model_dump_json())["status"] == "succeeded"

    run = Run(
        id="run_1",
        session_id="sess_1",
        owner_id="usr_1",
        status=RunStatus.WAITING_FOR_SUBAGENTS,
        trigger_message_id="msg_1",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="llm_1",
        main_prompt_version_id="sp_main_1",
        compression_prompt_version_id="sp_comp_1",
        prompt_template_version="v1",
        event_sequence=3,
        execution_epoch=1,
        worker_id="worker_1",
        lease_expires_at=NOW,
        execution_started_at=NOW,
        execution_deadline_at=NOW,
        queued_at=NOW,
        first_started_at=NOW,
        finished_at=None,
        created_at=NOW,
        updated_at=NOW,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(
            attempts=(
                RunAttemptUsage(
                    execution_epoch=1,
                    attempt=1,
                    purpose="compression",
                    complete=True,
                ),
            ),
        ),
        model_snapshot=RunModelSnapshot(
            id="llm_1",
            name="GPT-4o",
            protocol="chat_completions",
            base_url="https://api.example.com/v1",
            model="gpt-4o",
            context_window_tokens=128000,
            tokenizer_encoding="o200k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=8192,
            request_timeout_seconds=120,
            execution_timeout_seconds=300,
            max_concurrency=4,
        ),
        skill_snapshot=RunSkillSnapshot(
            skills=(
                RunSkillEntry(
                    skill_id="skill_1",
                    slug="demo-skill",
                    name="演示技能",
                    description="说明",
                    version_id="sver_1",
                    version_label="v1",
                    requires_approval=True,
                    scope=SkillScope.DEPARTMENT,
                ),
            ),
            selected_skill_ids=("skill_1",),
        ),
        input_interactions=(
            RunInputInteraction(
                input_request_id="req_1",
                status=InputRequestStatus.ANSWERED,
                question_text="请补充年份",
                answer_text="2026",
                created_at=NOW,
                answered_at=NOW,
            ),
        ),
    )
    run_dto = run_to_response(
        run,
        pending_input=RunInputRequest(
            id="req_2",
            run_id="run_1",
            session_id="sess_1",
            question_message_id="msg_q",
            status=InputRequestStatus.PENDING,
            answer_message_id=None,
            created_at=NOW,
            answered_at=None,
            closed_at=None,
        ),
        pending_approvals=(
            PendingToolApproval(
                call_id="call_1",
                batch_message_id="msg_b",
                ordinal=1,
                model_name="mcp__demo",
                tool_kind=ToolKind.MCP,
                requested_at=NOW,
            ),
        ),
        subagent_tasks=(
            SubagentTaskSummary(
                sub_run_id="sub_1",
                parent_tool_call_id="tc_1",
                subagent_slug="researcher",
                subagent_name="研究员",
                status=RunStatus.RUNNING,
                created_at=NOW,
            ),
        ),
        pending_subagent_approvals=(
            PendingSubagentApproval(
                sub_run_id="sub_1",
                subagent_slug="researcher",
                subagent_name="研究员",
                call_id="sub_call_1",
                batch_message_id="sub_msg_1",
                ordinal=1,
                model_name="mcp__demo",
                tool_kind=ToolKind.MCP,
                requested_at=NOW,
            ),
            PendingSubagentApproval(
                sub_run_id="sub_1",
                subagent_slug="researcher",
                subagent_name="研究员",
                call_id="sub_call_2",
                batch_message_id="sub_msg_1",
                ordinal=2,
                model_name="unknown_tool",
                tool_kind=None,
                requested_at=NOW,
            ),
        ),
    )
    run_json = json.loads(run_dto.model_dump_json())
    assert run_json["status"] == "waiting_for_subagents"
    assert run_json["usage"]["attempts"][0]["purpose"] == "compression"
    assert run_json["pending_input"]["status"] == "pending"
    assert run_json["input_interactions"][0]["status"] == "answered"
    assert run_json["skill_snapshot"]["skills"][0]["scope"] == "department"
    assert run_json["subagent_tasks"][0]["status"] == "running"
    assert run_json["pending_approvals"][0]["tool_kind"] == "mcp"
    assert run_json["pending_subagent_approvals"][0]["tool_kind"] == "mcp"
    assert run_json["pending_subagent_approvals"][1]["tool_kind"] is None

    call_detail = tool_call_to_response(
        RunToolCallDetails(
            record=RunToolCallRecord(
                id="call_1",
                run_id="run_1",
                session_id="sess_1",
                tool_call_id="tc_1",
                ordinal=1,
                tool_id="tool_demo",
                tool_kind=ToolKind.BUILTIN,
                model_name="knowledge_search",
                execution_epoch=1,
                status=ToolCallStatus.COMPLETED,
                call_message_id="msg_c",
                result_message_id="msg_r",
                started_at=NOW,
                finished_at=NOW,
                safe_error_code=None,
                outcome=ToolOutcome(
                    status=ToolOutcomeStatus.OK,
                    code="ok",
                    message="done",
                    data={"count": 1},
                ),
            ),
            arguments={"query": "合同"},
            output='{"status":"ok"}',
        )
    )
    call_json = json.loads(call_detail.model_dump_json())
    assert call_json["status"] == "completed"
    assert call_json["tool_kind"] == "builtin"
    assert call_json["outcome"]["status"] == "ok"

    message_dto = ConversationMessageResponse.model_validate(
        ConversationMessage(
            id="msg_2",
            session_id="sess_1",
            role=ConversationRole.ASSISTANT,
            status=MessageStatus.COMPLETED,
            sequence=2,
            parts=(
                TextPart(text="完成"),
                ToolResultPart(
                    tool_call_id="tc_1",
                    status=ToolResultStatus.SUCCESS,
                    output="ok",
                ),
            ),
            reply_to_message_id="msg_1",
            run_id="run_1",
            client_message_id="cli_1",
            created_at=NOW,
            completed_at=NOW,
            metadata=MessageMetadata(model="secret-internal-model"),
            context_turn_id="turn_1",
        )
    )
    message_json = json.loads(message_dto.model_dump_json())
    assert message_json["role"] == "assistant"
    assert message_json["status"] == "completed"
    assert message_json["parts"][1]["status"] == "success"
    # 验证直接接收领域对象后，内部元数据与会话轮次标识仍被 DTO 白名单严格屏蔽。
    assert "metadata" not in message_json
    assert "client_message_id" not in message_json
    assert "context_turn_id" not in message_json

    # 同样验证 auth / project / session 直接投影领域实体时不会泄露敏感或软删除内部字段。
    user_json = json.loads(
        user_to_response(
            User(
                id="usr_1",
                email="alice@example.com",
                password_hash="argon2id$secret",
                display_name="Alice",
                status=UserStatus.ACTIVE,
                role=UserRole.MEMBER,
                auth_provider=AuthProvider.LOCAL,
                external_union_id="u_ext",
                external_open_id="o_ext",
                created_at=NOW,
                updated_at=NOW,
                deleted_time=NOW,
            )
        ).model_dump_json()
    )
    assert "password_hash" not in user_json
    assert "status" not in user_json
    assert "deleted_time" not in user_json
    assert "external_union_id" not in user_json
    assert "external_open_id" not in user_json

    project_json = json.loads(
        ProjectResponse.model_validate(
            Project(
                id="proj_1",
                owner_id="usr_1",
                name="Alpha",
                created_at=NOW,
                updated_at=NOW,
                deleted_time=NOW,
            )
        ).model_dump_json()
    )
    assert "owner_id" not in project_json
    assert "deleted_time" not in project_json

    session_json = json.loads(
        SessionResponse.model_validate(
            Session(
                id="sess_1",
                owner_id="usr_1",
                title="Chat",
                project_id="proj_1",
                last_message_sequence=3,
                created_at=NOW,
                updated_at=NOW,
                deleted_time=NOW,
            )
        ).model_dump_json()
    )
    assert "owner_id" not in session_json
    assert "last_message_sequence" not in session_json
    assert "deleted_time" not in session_json


def test_tightened_response_dtos_reject_unknown_enum_values() -> None:
    """收紧后的 DTO 应显式拒绝非法枚举值，防止契约漂移静默透传。"""
    with pytest.raises(ValidationError):
        ModelProfileResponse.model_validate(
            {
                "id": "llm_1",
                "name": "Bad",
                "protocol": "responses_api",
                "base_url": "https://api.example.com",
                "model": "m",
                "context_window_tokens": 1000,
                "tokenizer_encoding": "o200k_base",
                "supports_stream": True,
                "supports_tools": True,
                "supports_parallel_tool_calls": False,
                "max_output_tokens": 100,
                "request_timeout_seconds": 30,
                "execution_timeout_seconds": 60,
                "max_concurrency": 1,
                "temperature": None,
                "enabled": True,
                "visible_to_users": True,
                "scenario_label": None,
                "scenario_description": None,
                "supports_vision": False,
                "version": 1,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )

    with pytest.raises(ValidationError):
        McpServerResponse.model_validate(
            {
                "id": "mcp_1",
                "name": "Bad",
                "transport": "websocket",
                "config": {"url": "https://mcp.example.com"},
                "secrets": [],
                "config_version": 1,
                "enabled": True,
                "visibility_scope": "company",
                "owner_department_id": None,
                "visibility_grants": [],
                "created_by": None,
                "created_at": NOW,
                "updated_at": NOW,
                "tools_discovered_at": None,
            }
        )

    with pytest.raises(ValidationError):
        SessionFileResponse.model_validate(
            {
                "id": "file_1",
                "session_id": "sess_1",
                "source": "external_link",
                "filename": "a.txt",
                "label": None,
                "mime_type": "text/plain",
                "size_bytes": 1,
                "extraction_status": "none",
                "extracted_text_bytes": None,
                "extraction_error_code": None,
                "created_at": NOW,
            }
        )

    with pytest.raises(ValidationError):
        KnowledgeImportResponse.model_validate(
            {
                "document_id": "doc_1",
                "version_id": "ver_1",
                "state": "indexing",
                "status_url": "/api/v1/knowledge-bases/kb_1/documents/doc_1",
            }
        )

    with pytest.raises(ValidationError):
        SkillDependencyCheckResponse.model_validate(
            {
                "status": "unknown_status",
                "python_version": "3.12.0",
                "python_implementation": "CPython",
                "checked_at": NOW,
                "items": [],
            }
        )

    with pytest.raises(ValidationError):
        ConversationMessageResponse.model_validate(
            {
                "id": "msg_1",
                "session_id": "sess_1",
                "role": "system",
                "status": "completed",
                "sequence": 1,
                "parts": [],
                "reply_to_message_id": None,
                "run_id": "run_1",
                "created_at": NOW,
                "completed_at": NOW,
            }
        )

    # 领域 PendingSubagentApproval.tool_kind 已收紧为 ToolKind | None，非法值在领域层即被拒绝。
    with pytest.raises(ValidationError):
        PendingSubagentApproval(
            sub_run_id="sub_1",
            subagent_slug="researcher",
            subagent_name="研究员",
            call_id="sub_call_bad",
            batch_message_id="sub_msg_1",
            ordinal=1,
            model_name="mcp__demo",
            tool_kind="unknown_kind",  # type: ignore[arg-type]
            requested_at=NOW,
        )
