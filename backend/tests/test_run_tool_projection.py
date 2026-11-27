"""工具调用详情的严格白名单安全投影与脱敏回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from api.converter.run import tool_call_to_response
from domain.preference import SAVE_USER_MEMORY_TOOL_ID
from domain.skill import (
    SESSION_FILE_LIST_TOOL_ID,
    SESSION_FILE_READ_TOOL_ID,
    SKILL_EXECUTE_TOOL_ID,
    SKILL_LOAD_TOOL_ID,
    SKILL_READ_RESOURCE_TOOL_ID,
    SafeSkillApprovalProjection,
    SkillParameterSummaryItem,
)
from domain.subagent import DISPATCH_SUBAGENTS_TOOL_ID
from domain.tool import (
    RunToolCallDetails,
    RunToolCallRecord,
    ToolCallStatus,
    ToolKind,
    ToolOutcome,
    ToolOutcomeStatus,
    build_safe_tool_call_projection,
    format_tool_arguments,
    project_mcp_whitelisted_results,
    project_whitelisted_fields,
)

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def _details(
    tool_id: str | None,
    model_name: str = "opaque_model_name",
    *,
    tool_kind: ToolKind = ToolKind.BUILTIN,
    arguments: dict[str, object] | None = None,
    outcome: ToolOutcome | None = None,
    safe_skill_projection: SafeSkillApprovalProjection | None = None,
    safe_error_code: str | None = None,
) -> RunToolCallDetails:
    """构造带敏感参数和内部结果数据的工具调用详情测试夹具。"""
    record_id = tool_id or "none"
    resolved_outcome = outcome or ToolOutcome(
        status=ToolOutcomeStatus.OK,
        code="skill_executed",
        message="raw internal message /host/private",
        data={"internal_path": "/host/private", "password": "secret"},
    )
    record = RunToolCallRecord(
        id=f"record_{record_id}",
        run_id="run_one",
        session_id="session_one",
        tool_call_id=f"call_{record_id}",
        ordinal=1,
        tool_id=tool_id,
        tool_kind=tool_kind,
        model_name=model_name,
        execution_epoch=1,
        status=ToolCallStatus.COMPLETED,
        call_message_id="message_call",
        result_message_id="message_result",
        started_at=NOW,
        finished_at=NOW,
        safe_error_code=safe_error_code,
        outcome=resolved_outcome,
        safe_skill_projection=safe_skill_projection,
    )
    return RunToolCallDetails(
        record=record,
        arguments=arguments
        if arguments is not None
        else {"internal_path": "/host/private", "password": "secret"},
        output="private log",
        safe_skill_projection=safe_skill_projection,
    )


@pytest.mark.parametrize(
    "tool_id",
    (
        SKILL_EXECUTE_TOOL_ID,
        SKILL_LOAD_TOOL_ID,
        SKILL_READ_RESOURCE_TOOL_ID,
    ),
)
def test_all_skill_tool_details_hide_arguments_and_outcome_data(tool_id: str) -> None:
    response = tool_call_to_response(_details(tool_id))
    dumped = response.model_dump()

    assert "arguments" not in dumped
    assert response.formatted_arguments == []
    assert response.business_summary is None
    assert response.outcome is not None
    assert response.outcome.formatted_results == []
    assert "private" not in response.model_dump_json()
    assert "secret" not in response.model_dump_json()


@pytest.mark.parametrize(
    ("tool_id", "model_name"),
    (
        (None, "skill_execute"),
        ("tool_public", "skill_load"),
        ("tool_public", "  CUSTOM_SKILL_READ_RESOURCE  "),
    ),
)
def test_skill_model_names_are_redacted_without_trusting_tool_id(
    tool_id: str | None,
    model_name: str,
) -> None:
    """tool_id 缺失、冲突或带前缀时，仍必须按 modelName 隐藏 Skill 详情。"""
    response = tool_call_to_response(_details(tool_id, model_name))
    dumped = response.model_dump()

    assert "arguments" not in dumped
    assert response.formatted_arguments == []
    assert response.business_summary is None
    assert response.outcome is not None
    assert response.outcome.formatted_results == []
    assert "private" not in response.model_dump_json()
    assert "secret" not in response.model_dump_json()


def test_skill_execute_preserves_hidden_count_without_exposing_raw_logs() -> None:
    skill_proj = SafeSkillApprovalProjection(
        call_id="call_1",
        skill_id="skill_1",
        skill_name="薪酬核算",
        skill_version_id="ver_1",
        skill_version_label="v1",
        script_label="生成月度薪酬表",
        purpose="生成当月薪酬报表",
        input_files=(),
        parameter_summary=(
            SkillParameterSummaryItem(name="month", label="结算月份", value="2026-11"),
        ),
        hidden_parameter_count=2,
        can_approve=True,
    )
    response = tool_call_to_response(
        _details(
            SKILL_EXECUTE_TOOL_ID,
            "skill_execute",
            safe_skill_projection=skill_proj,
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.ERROR,
                code="skill_script_failed",
                message="stderr: /var/app/secret_script.py failed",
                data={"stderr": "/var/app/secret_script.py"},
            ),
            safe_error_code="skill_script_failed",
        )
    )
    assert response.display_name == "生成月度薪酬表"
    assert response.formatted_arguments == []
    assert response.hidden_argument_count == 2
    assert response.business_summary is None
    assert response.outcome is not None
    assert response.outcome.summary == "技能执行失败。"
    assert response.outcome.formatted_results == []
    assert "/var/app" not in response.model_dump_json()
    assert "stderr" not in response.model_dump_json()


def test_strict_whitelist_fields_and_mcp_results_never_fallback_to_raw_keys() -> None:
    """未配置白名单标签的字段一律计入隐藏计数或丢弃，绝不回退为原始英文键名。"""
    formatted, hidden = project_whitelisted_fields(
        {"payee": "星辰科技", "secret_token": "tok-123", "amount": 500, "extra": True},
        {"amount": "付款金额", "payee": "收款方"},
    )
    assert [(item.key, item.label, item.value) for item in formatted] == [
        ("amount", "付款金额", "500"),
        ("payee", "收款方", "星辰科技"),
    ]
    assert hidden == 2
    assert format_tool_arguments({"secret_token": "tok-123"}, {}) == ()

    # MCP 出参白名单：优先读取 structured，其次解析顶层 JSON text，解析失败静默忽略。
    assert project_mcp_whitelisted_results({"structured": {"order_no": "PO-1"}}, {}) == ()
    structured_items = project_mcp_whitelisted_results(
        {
            "structured": {"order_no": "PO-1", "internal_id": "row_99"},
            "text": '{"order_no":"IGNORED"}',
        },
        {"order_no": "订单编号"},
    )
    assert [(i.key, i.label, i.value) for i in structured_items] == [
        ("order_no", "订单编号", "PO-1")
    ]
    text_items = project_mcp_whitelisted_results(
        {"text": '{"status_label": "已审批", "secret": "x"}'},
        {"status_label": "审批状态"},
    )
    assert [(i.key, i.label, i.value) for i in text_items] == [
        ("status_label", "审批状态", "已审批")
    ]
    assert project_mcp_whitelisted_results({"text": "plain text not json"}, {"a": "A"}) == ()


def test_mcp_tool_projection_hides_raw_message_and_data_and_maps_all_statuses() -> None:
    """MCP 工具详情严禁暴露原始 outcome.message/data 与未映射入参。"""
    projection = build_safe_tool_call_projection(
        tool_id="tool_1234",
        tool_kind=ToolKind.MCP,
        model_name="mcp__1234",
        arguments={"payee": "星辰科技", "amount": 18800, "api_secret": "sk-live"},
        outcome=ToolOutcome(
            status=ToolOutcomeStatus.OK,
            code="ok",
            message='{"internal_trace":"leak-me","order_id":"ORD-88"}',
            data={
                "structured": {"order_id": "ORD-88", "internal_token": "tok-leak"},
                "text": "raw text leak",
            },
        ),
        meta_display_name="发起对外付款",
        meta_template="向 {payee} 付款 {amount} 元",
        meta_parameter_labels={"payee": "收款方", "amount": "付款金额"},
        meta_result_labels={"order_id": "单据编号"},
    )
    assert projection.display_name == "发起对外付款"
    assert projection.business_summary == "向 星辰科技 付款 18800 元"
    assert [(i.key, i.label, i.value) for i in projection.formatted_arguments] == [
        ("payee", "收款方", "星辰科技"),
        ("amount", "付款金额", "18800"),
    ]
    assert projection.hidden_argument_count == 1
    assert projection.safe_outcome_summary == "已完成业务工具调用。"
    assert [(i.key, i.label, i.value) for i in projection.formatted_results] == [
        ("order_id", "单据编号", "ORD-88")
    ]

    response = tool_call_to_response(
        RunToolCallDetails(
            record=RunToolCallRecord(
                id="call_mcp",
                run_id="run_1",
                session_id="sess_1",
                tool_call_id="tc_1",
                ordinal=1,
                tool_id="tool_1234",
                tool_kind=ToolKind.MCP,
                model_name="mcp__1234",
                execution_epoch=1,
                status=ToolCallStatus.COMPLETED,
                call_message_id="m1",
                result_message_id="m2",
                started_at=NOW,
                finished_at=NOW,
                safe_error_code=None,
                outcome=ToolOutcome(
                    status=ToolOutcomeStatus.OK,
                    code="ok",
                    message='{"internal_trace":"leak-me"}',
                    data={"structured": {"order_id": "ORD-88", "internal_token": "tok-leak"}},
                ),
                display_name=projection.display_name,
            ),
            arguments={"payee": "星辰科技", "amount": 18800, "api_secret": "sk-live"},
            output="raw",
            display_name=projection.display_name,
            business_summary=projection.business_summary,
            formatted_arguments=projection.formatted_arguments,
            hidden_argument_count=projection.hidden_argument_count,
            safe_outcome_summary=projection.safe_outcome_summary,
            formatted_results=projection.formatted_results,
        )
    )
    serialized = response.model_dump_json()
    assert "sk-live" not in serialized
    assert "api_secret" not in serialized
    assert "leak-me" not in serialized
    assert "tok-leak" not in serialized
    assert "internal_token" not in serialized

    for status, code, truncated, expected_summary in (
        (ToolOutcomeStatus.OK, "ok", True, "已完成业务工具调用（部分内容已省略）。"),
        (ToolOutcomeStatus.ERROR, "tool_approval_denied", False, "已拒绝执行该业务操作。"),
        (ToolOutcomeStatus.ERROR, "tool_permission_denied", False, "当前账号无权执行该业务操作。"),
        (ToolOutcomeStatus.ERROR, "tool_unavailable", False, "该业务工具当前不可用或配置已变更。"),
        (ToolOutcomeStatus.ERROR, "tool_arguments_invalid", False, "业务调用参数校验未通过。"),
        (ToolOutcomeStatus.ERROR, "mcp_deadline_exhausted", False, "业务工具调用超时。"),
        (ToolOutcomeStatus.ERROR, "mcp_tool_error", False, "业务工具返回执行失败。"),
        (ToolOutcomeStatus.CANCELLED, "tool_not_executed", False, "业务工具调用已取消。"),
        (
            ToolOutcomeStatus.UNKNOWN,
            "mcp_response_unknown",
            False,
            "业务工具执行结果未知，请先核实外部系统状态。",
        ),
    ):
        proj = build_safe_tool_call_projection(
            tool_id="tool_1",
            tool_kind=ToolKind.MCP,
            model_name="mcp__1",
            arguments={},
            outcome=ToolOutcome(
                status=status,
                code=code,
                message="RAW_BACKEND_ERROR_SHOULD_NOT_APPEAR",
                truncated=truncated,
            ),
            safe_error_code=None if status is ToolOutcomeStatus.OK else code,
        )
        assert proj.safe_outcome_summary == expected_summary


def test_builtin_tools_apply_fixed_whitelists_and_hide_internal_ids_and_content() -> None:
    """全部平台内置工具使用固定白名单，严禁透出内部 ID、原文内容与未授权字段。"""
    kb_resp = tool_call_to_response(
        _details(
            "builtin_knowledge_search",
            "knowledge_search",
            arguments={"query": "差旅报销标准", "limit": 8},
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="knowledge_search_ok",
                message="已找到相关知识片段。",
                data={
                    "status": "ok",
                    "items": [
                        {
                            "base_id": "kb_secret_1",
                            "document_id": "doc_secret_1",
                            "version_id": "ver_secret_1",
                            "chunk_ids": ["chunk_1"],
                            "title": "财务报销制度",
                            "text": "敏感原文片段不应暴露",
                            "channels": ["vector"],
                        },
                        {"title": "财务报销制度", "text": "重复标题应去重"},
                        {"title": "差旅管理办法", "text": "另一篇原文"},
                    ],
                },
            ),
        )
    )
    assert kb_resp.display_name == "查阅企业知识库"
    assert kb_resp.business_summary == "检索知识库：差旅报销标准"
    assert [(i.key, i.label, i.value) for i in kb_resp.formatted_arguments] == [
        ("query", "检索内容", "差旅报销标准")
    ]
    assert kb_resp.hidden_argument_count == 1
    assert kb_resp.outcome is not None
    assert kb_resp.outcome.summary == "已找到 3 条相关知识片段。"
    assert [(i.key, i.label, i.value) for i in kb_resp.outcome.formatted_results] == [
        ("sources", "来源文档", "财务报销制度、差旅管理办法")
    ]
    kb_json = kb_resp.model_dump_json()
    for secret in ("kb_secret_1", "doc_secret_1", "ver_secret_1", "chunk_1", "敏感原文片段"):
        assert secret not in kb_json

    file_list_resp = tool_call_to_response(
        _details(
            SESSION_FILE_LIST_TOOL_ID,
            "session_file_list",
            arguments={"after_file_id": "file_cursor_1", "limit": 50},
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="session_files_listed",
                message="ok",
                data={
                    "items": [
                        {"file_id": "file_internal_1", "filename": "合同.pdf"},
                        {"file_id": "file_internal_2", "filename": "清单.xlsx"},
                    ]
                },
            ),
        )
    )
    assert file_list_resp.display_name == "查看会话文件"
    assert file_list_resp.formatted_arguments == []
    assert file_list_resp.hidden_argument_count == 2
    assert file_list_resp.outcome is not None
    assert file_list_resp.outcome.summary == "已列出当前会话 2 个文件。"
    assert [(i.key, i.label, i.value) for i in file_list_resp.outcome.formatted_results] == [
        ("filenames", "文件列表", "合同.pdf、清单.xlsx")
    ]
    assert "file_internal_1" not in file_list_resp.model_dump_json()

    file_read_resp = tool_call_to_response(
        _details(
            SESSION_FILE_READ_TOOL_ID,
            "session_file_read",
            arguments={"file_id": "file_internal_9", "offset": 0, "limit_bytes": 1024},
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="session_file_read",
                message="ok",
                data={
                    "file": {"file_id": "file_internal_9", "filename": "预算表.csv"},
                    "source_kind": "extracted_text",
                    "offset": 0,
                    "next_offset": 512,
                    "content": "绝密正文内容",
                },
            ),
        )
    )
    assert file_read_resp.display_name == "读取会话文件"
    assert file_read_resp.formatted_arguments == []
    assert file_read_resp.hidden_argument_count == 3
    assert file_read_resp.outcome is not None
    assert file_read_resp.outcome.summary == "已读取会话文件《预算表.csv》。"
    assert [(i.key, i.label, i.value) for i in file_read_resp.outcome.formatted_results] == [
        ("filename", "文件名", "预算表.csv")
    ]
    file_read_json = file_read_resp.model_dump_json()
    assert "绝密正文内容" not in file_read_json
    assert "file_internal_9" not in file_read_json

    memory_resp = tool_call_to_response(
        _details(
            SAVE_USER_MEMORY_TOOL_ID,
            "save_user_memory",
            arguments={"content": "回复请使用简洁列表"},
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="user_memory_saved",
                message="ok",
                data={"id": "mem_1", "source": "chat_explicit", "created_at": "2026-11-12"},
            ),
        )
    )
    assert memory_resp.display_name == "记录个人工作偏好"
    assert [(i.key, i.label, i.value) for i in memory_resp.formatted_arguments] == [
        ("content", "记忆内容", "回复请使用简洁列表")
    ]
    assert memory_resp.outcome is not None
    assert memory_resp.outcome.summary == "已保存个人工作偏好。"
    assert memory_resp.outcome.formatted_results == []
    assert "mem_1" not in memory_resp.model_dump_json()

    subagent_resp = tool_call_to_response(
        _details(
            DISPATCH_SUBAGENTS_TOOL_ID,
            "dispatch_subagents",
            arguments={
                "tasks": [
                    {"subagent_slug": "hr_bot", "task": "核对考勤", "input_file_ids": ["f1"]},
                    {"subagent_slug": "fin_bot", "task": "核对报销"},
                ]
            },
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="subagents_completed",
                message="done",
                data={
                    "completed_count": 1,
                    "failed_count": 1,
                    "results": [{"sub_run_id": "sub_secret_1", "output": "secret"}],
                },
            ),
        )
    )
    assert subagent_resp.display_name == "协调协作助手"
    assert subagent_resp.business_summary == "已派发 2 项协作任务"
    assert subagent_resp.formatted_arguments == []
    assert subagent_resp.hidden_argument_count == 1
    assert subagent_resp.outcome is not None
    assert subagent_resp.outcome.summary == "协作任务已完成（成功 1，失败 1）。"
    assert "sub_secret_1" not in subagent_resp.model_dump_json()
    assert "hr_bot" not in subagent_resp.model_dump_json()

    input_resp = tool_call_to_response(
        _details(
            "builtin_request_user_input",
            "request_user_input",
            arguments={"question": "请确认出差城市"},
            outcome=ToolOutcome(status=ToolOutcomeStatus.OK, code="ok", message="done"),
        )
    )
    assert input_resp.display_name == "请求补充信息"
    assert [(i.key, i.label, i.value) for i in input_resp.formatted_arguments] == [
        ("question", "澄清问题", "请确认出差城市")
    ]
    assert input_resp.outcome is not None
    assert input_resp.outcome.summary == "已收集用户补充信息。"

    unknown_builtin = tool_call_to_response(
        _details(
            "builtin_unknown_future",
            "future_internal_tool",
            arguments={"secret_arg": "val"},
            outcome=ToolOutcome(
                status=ToolOutcomeStatus.OK,
                code="ok",
                message="secret message",
                data={"raw": "secret"},
            ),
        )
    )
    assert unknown_builtin.formatted_arguments == []
    assert unknown_builtin.hidden_argument_count == 1
    assert unknown_builtin.outcome is not None
    assert unknown_builtin.outcome.summary == "已完成内置工具调用。"
    assert "secret" not in unknown_builtin.model_dump_json()
