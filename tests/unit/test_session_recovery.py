"""会话恢复单元测试."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from athena.core.recovery.session_recovery import (
    ResumePoint,
    SessionRecovery,
)


class TestDetermineResumePoint:
    """恢复起点判断测试."""

    def setup_method(self):
        self.db = AsyncMock()
        self.ws = AsyncMock()
        self.recovery = SessionRecovery(db=self.db, ws_manager=self.ws)

    def test_empty_messages(self):
        result = self.recovery._determine_resume_point([])
        assert result == ResumePoint.NONE

    def test_user_message_unanswered(self):
        messages = [
            {"role": "user", "content": "hello"},
        ]
        result = self.recovery._determine_resume_point(messages)
        assert result == ResumePoint.RE_RUN_AGENT

    def test_assistant_normal_response(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = self.recovery._determine_resume_point(messages)
        assert result == ResumePoint.NONE

    def test_assistant_with_tool_calls(self):
        messages = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "read_file", "args": {}}]},
        ]
        result = self.recovery._determine_resume_point(messages)
        assert result == ResumePoint.RE_EXECUTE_TOOLS

    def test_tool_message_unprocessed(self):
        messages = [
            {"role": "user", "content": "read file"},
            {"role": "assistant", "content": "", "tool_calls": [{"name": "read_file", "args": {}}]},
            {"role": "tool", "content": "file content"},
        ]
        result = self.recovery._determine_resume_point(messages)
        assert result == ResumePoint.RE_RUN_LLM

    def test_system_message_checks_previous(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "recovery prompt"},
        ]
        result = self.recovery._determine_resume_point(messages)
        assert result == ResumePoint.RE_RUN_AGENT


class TestInterruptedToolStrategy:
    """中断工具处理策略测试."""

    def setup_method(self):
        self.db = AsyncMock()
        self.ws = AsyncMock()
        self.recovery = SessionRecovery(db=self.db, ws_manager=self.ws)

    def test_read_file_is_retryable(self):
        tool_call = {"tool_name": "read_file", "arguments": {"path": "/workspace/file.txt"}}
        strategy = self.recovery._get_interrupted_tool_strategy(tool_call)
        from athena.core.recovery.session_recovery import InterruptedToolStrategy
        assert strategy == InterruptedToolStrategy.RETRY

    def test_shell_is_notify_user(self):
        tool_call = {"tool_name": "exec_shell", "arguments": {"command": "ls"}}
        strategy = self.recovery._get_interrupted_tool_strategy(tool_call)
        from athena.core.recovery.session_recovery import InterruptedToolStrategy
        assert strategy == InterruptedToolStrategy.NOTIFY_USER

    def test_unknown_tool_is_notify_user(self):
        tool_call = {"tool_name": "unknown_tool", "arguments": {}}
        strategy = self.recovery._get_interrupted_tool_strategy(tool_call)
        from athena.core.recovery.session_recovery import InterruptedToolStrategy
        assert strategy == InterruptedToolStrategy.NOTIFY_USER


class TestBuildRecoveryPrompt:
    """恢复提示词构建测试."""

    def setup_method(self):
        self.db = AsyncMock()
        self.ws = AsyncMock()
        self.recovery = SessionRecovery(db=self.db, ws_manager=self.ws)

    def test_re_run_agent_prompt(self):
        prompt = self.recovery._build_recovery_prompt(ResumePoint.RE_RUN_AGENT, [])
        assert "用户消息未得到响应" in prompt

    def test_re_execute_tools_prompt(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"name": "read_file"}, {"name": "write_file"}]}
        ]
        prompt = self.recovery._build_recovery_prompt(ResumePoint.RE_EXECUTE_TOOLS, messages)
        assert "read_file" in prompt
        assert "write_file" in prompt

    def test_re_run_llm_prompt(self):
        prompt = self.recovery._build_recovery_prompt(ResumePoint.RE_RUN_LLM, [])
        assert "工具结果未被处理" in prompt
