"""会话中断恢复管理器 — 主动恢复中断的 Agent 运行.

功能：
- 启动时扫描中断的会话
- 判断恢复起点（_determine_resume_point）
- 检查待审批请求（_check_pending_approvals）
- 处理中断的工具调用（_handle_interrupted_tool）
- 自动重新触发 Agent 运行（带指数退避重试）
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from athena.db.database import Database
from athena.gateway.ws.manager import WebSocketManager
from athena.schemas.events import EventType, build_event
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class ResumePoint(StrEnum):
    """恢复起点类型."""

    NONE = "none"  # 无需恢复
    RE_RUN_AGENT = "re_run_agent"  # 用户消息未响应，重新执行 Agent
    RE_EXECUTE_TOOLS = "re_execute_tools"  # 工具未执行，重新执行工具
    RE_RUN_LLM = "re_run_llm"  # 工具结果未处理，重新调用 LLM


class InterruptedToolStrategy(StrEnum):
    """中断工具的处理策略."""

    RETRY = "retry"  # 安全重试
    SKIP = "skip"  # 跳过（已正确完成）
    NOTIFY_USER = "notify_user"  # 通知用户确认


class SessionRecovery:
    """会话恢复管理器."""

    MAX_RECOVERY_RETRIES = 3

    def __init__(
        self,
        db: Database,
        ws_manager: WebSocketManager | None = None,
        agent_workflow: Any = None,
    ) -> None:
        self._db = db
        self._ws = ws_manager
        self._workflow = agent_workflow

    async def recover_on_startup(self) -> None:
        """启动时恢复中断的会话."""
        interrupted = await self._db.query_sessions(status=["running", "interrupted"])
        if not interrupted:
            logger.info("no_interrupted_sessions")
            return

        logger.info("recovering_interrupted_sessions", count=len(interrupted))
        for session in interrupted:
            session_id = session["id"]
            try:
                await self._recover_session(session_id)
            except Exception as e:
                logger.error("session_recovery_failed", session_id=session_id, error=str(e))
                await self._db.update_session(session_id, status="idle")

    async def _recover_session(self, session_id: str) -> None:
        """恢复单个会话."""
        # 1. 检查待审批请求
        await self._check_pending_approvals(session_id)

        # 2. 检查中断的工具调用
        await self._handle_interrupted_tools(session_id)

        # 3. 判断恢复起点
        messages = await self._db.get_messages(session_id)
        resume_point = self._determine_resume_point(messages)

        if resume_point == ResumePoint.NONE:
            await self._db.update_session(session_id, status="idle")
            logger.info("session_recovery_not_needed", session_id=session_id)
            return

        # 4. 标记为恢复中
        await self._db.update_session(session_id, status="recovering")

        # 5. 构造恢复系统消息
        recovery_prompt = self._build_recovery_prompt(resume_point, messages)

        # 6. 重新触发 Agent 运行（带重试）
        if self._workflow is None:
            logger.warning("no_workflow_for_recovery", session_id=session_id)
            await self._db.update_session(session_id, status="idle")
            return

        for attempt in range(self.MAX_RECOVERY_RETRIES):
            try:
                await self._workflow.process_message(
                    session_id=session_id,
                    user_message=recovery_prompt,
                    system_prompt="[系统] 正在恢复中断的会话，请从现有对话历史继续。",
                )
                await self._db.update_session(session_id, status="idle")
                logger.info("session_recovery_success", session_id=session_id, attempt=attempt + 1)
                return
            except Exception as e:
                logger.warning(
                    "recovery_attempt_failed",
                    session_id=session_id,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < self.MAX_RECOVERY_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)  # 指数退避

        # 恢复失败
        await self._db.update_session(session_id, status="failed")
        logger.error("session_recovery_failed_all_attempts", session_id=session_id)

    def _determine_resume_point(self, messages: list[dict[str, Any]]) -> ResumePoint:
        """判断恢复起点.

        根据最后一条消息的角色决定从哪里恢复：
        - user: 用户消息未响应 → 重新执行 Agent
        - assistant + tool_calls: 工具未执行 → 重新执行工具
        - tool: 工具结果未处理 → 重新调用 LLM
        - assistant (无 tool_calls): 正常完成 → 无需恢复
        """
        if not messages:
            return ResumePoint.NONE

        last_msg = messages[-1]
        role = last_msg.get("role", "")

        if role == "user":
            return ResumePoint.RE_RUN_AGENT
        elif role == "assistant":
            tool_calls = last_msg.get("tool_calls", [])
            if tool_calls:
                return ResumePoint.RE_EXECUTE_TOOLS
            return ResumePoint.NONE
        elif role == "tool":
            return ResumePoint.RE_RUN_LLM
        elif role == "system":
            # 系统消息可能是恢复消息，检查前一条
            if len(messages) > 1:
                return self._determine_resume_point(messages[:-1])
            return ResumePoint.NONE

        return ResumePoint.RE_RUN_AGENT

    async def _check_pending_approvals(self, session_id: str) -> None:
        """检查中断时的待审批请求并通知用户."""
        running_tools = await self._db.query_tool_calls(session_id=session_id, status="running")

        for tc in running_tools:
            approval = await self._db.query_approval(tool_call_id=tc["id"])
            if not approval:
                # 审批中断，更新状态并通知用户
                await self._db.update_tool_call(tc["id"], {
                    "status": "interrupted",
                    "error_message": "进程中断，审批未完成",
                })

                if self._ws is not None:
                    await self._ws.send_to_session(
                        session_id,
                        build_event(
                            EventType.APPROVAL_INTERRUPTED,
                            {
                                "tool_call_id": tc["id"],
                                "tool_name": tc["tool_name"],
                                "arguments": tc.get("arguments", {}),
                                "message": f"上次中断在等待审批：{tc['tool_name']}",
                            },
                            session_id=session_id,
                        ),
                    )
                logger.info(
                    "pending_approval_detected",
                    session_id=session_id,
                    tool_call_id=tc["id"],
                    tool_name=tc["tool_name"],
                )

    async def _handle_interrupted_tools(self, session_id: str) -> None:
        """处理中断的工具调用."""
        running_tools = await self._db.query_tool_calls(session_id=session_id, status="running")

        for tc in running_tools:
            strategy = self._get_interrupted_tool_strategy(tc)

            if strategy == InterruptedToolStrategy.RETRY:
                # 标记为待重试
                await self._db.update_tool_call(tc["id"], {
                    "status": "pending_retry",
                    "error_message": "进程中断，待重试",
                })
            elif strategy == InterruptedToolStrategy.SKIP:
                # 标记为已完成
                await self._db.update_tool_call(tc["id"], {
                    "status": "completed",
                    "error_message": "进程中断，但操作可能已完成",
                })
            else:  # NOTIFY_USER
                await self._db.update_tool_call(tc["id"], {
                    "status": "interrupted",
                    "error_message": "进程中断，状态未知",
                })

                if self._ws is not None:
                    await self._ws.send_to_session(
                        session_id,
                        build_event(
                            EventType.TOOL_INTERRUPTED,
                            {
                                "tool_call_id": tc["id"],
                                "tool_name": tc["tool_name"],
                                "message": f"工具 {tc['tool_name']} 执行中断，状态未知",
                            },
                            session_id=session_id,
                        ),
                    )

    def _get_interrupted_tool_strategy(self, tool_call: dict[str, Any]) -> InterruptedToolStrategy:
        """根据工具类型确定中断后的处理策略."""
        tool_name = tool_call.get("tool_name", "")

        # 只读操作，安全重试
        if tool_name in ("read_file", "list_directory"):
            return InterruptedToolStrategy.RETRY

        # 写操作，检查是否已正确完成
        if tool_name == "write_file":
            args = tool_call.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return InterruptedToolStrategy.NOTIFY_USER

            file_path = Path(args.get("path", ""))
            expected_content = args.get("content", "")

            if file_path.exists():
                try:
                    actual_content = file_path.read_text()
                    if actual_content == expected_content:
                        return InterruptedToolStrategy.SKIP
                except Exception:
                    pass
            return InterruptedToolStrategy.RETRY

        # Shell 命令，状态不确定
        if tool_name == "exec_shell":
            return InterruptedToolStrategy.NOTIFY_USER

        # 其他工具，通知用户
        return InterruptedToolStrategy.NOTIFY_USER

    def _build_recovery_prompt(
        self, resume_point: ResumePoint, messages: list[dict[str, Any]]
    ) -> str:
        """构造恢复提示词."""
        if resume_point == ResumePoint.RE_RUN_AGENT:
            return (
                "[系统] 你的上一轮执行被中断，用户消息未得到响应。"
                "请从现有对话历史继续，回复用户的问题。"
            )
        elif resume_point == ResumePoint.RE_EXECUTE_TOOLS:
            last_msg = messages[-1] if messages else {}
            tool_calls = last_msg.get("tool_calls", [])
            tool_names = [tc.get("name", "unknown") for tc in tool_calls]
            return (
                f"[系统] 你的上一轮执行被中断，以下工具调用未完成：{', '.join(tool_names)}。"
                "请重新执行这些工具调用。"
            )
        elif resume_point == ResumePoint.RE_RUN_LLM:
            return (
                "[系统] 你的上一轮执行被中断，工具结果未被处理。"
                "请根据现有对话历史继续推理。"
            )
        return "[系统] 会话恢复，请继续。"


class GracefulShutdown:
    """优雅关闭管理器."""

    DRAINING_TIMEOUT = 300  # 5 分钟

    def __init__(self, db: Database) -> None:
        self._db = db
        self._accepting = True
        self._active_sessions: dict[str, asyncio.Task] = {}

    @property
    def is_accepting(self) -> bool:
        return self._accepting

    def register_active_session(self, session_id: str, task: asyncio.Task) -> None:
        """注册活跃会话."""
        self._active_sessions[session_id] = task

    def unregister_active_session(self, session_id: str) -> None:
        """注销活跃会话."""
        self._active_sessions.pop(session_id, None)

    async def shutdown(self) -> None:
        """优雅关闭流程."""
        logger.info("graceful_shutdown_started")
        self._accepting = False

        # 为活跃会话标记 interrupted
        for session_id in list(self._active_sessions.keys()):
            await self._db.update_session(session_id, status="interrupted")

        # 等待活跃任务完成
        deadline = asyncio.get_event_loop().time() + self.DRAINING_TIMEOUT
        while self._active_sessions and asyncio.get_event_loop().time() < deadline:
            done = []
            for session_id, task in self._active_sessions.items():
                if task.done():
                    done.append(session_id)
            for sid in done:
                self._active_sessions.pop(sid, None)
            if self._active_sessions:
                await asyncio.sleep(1)

        # 强制取消剩余任务
        for session_id, task in self._active_sessions.items():
            if not task.done():
                task.cancel()
                logger.warning("force_cancelled_session", session_id=session_id)

        self._active_sessions.clear()
        logger.info("graceful_shutdown_completed")
