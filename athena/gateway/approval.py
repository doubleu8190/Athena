"""HITL 审批管理器 — 异步队列 + Future 等待模式.

核心约束（来自 project_memory）：
- 维护异步队列，所有审批请求按 FIFO 顺序处理
- 通过 asyncio.Future 实现请求方等待，request_approval 立即返回 Future
- 同一时刻只处理一个请求，防止审批风暴
- 每个工具调用生成唯一 approval_id
- 子 Agent 通过依赖注入共享父 ApprovalManager 实例
"""

from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import datetime
from typing import Any

from athena.models.approval import ApprovalDecision, ApprovalRequest
from athena.schemas.events import EventType, build_event
from athena.utils.logging import get_logger

logger = get_logger(__name__)


class ApprovalManager:
    """审批管理器 - 异步队列模式."""

    def __init__(
        self,
        approval_timeout: int = 120,
        websocket_manager: Any | None = None,
        db: Any | None = None,
    ) -> None:
        self._queue: deque[ApprovalRequest] = deque()
        self._pending: dict[str, ApprovalRequest] = {}  # approval_id → request
        self._processing: bool = False
        self._processor_task: asyncio.Task | None = None
        self._timeout = approval_timeout
        self._websocket_manager = websocket_manager
        self._db = db
        self._lock = asyncio.Lock()

    def set_websocket_manager(self, ws_manager: Any) -> None:
        self._websocket_manager = ws_manager

    def set_db(self, db: Any) -> None:
        self._db = db

    @property
    def queue_length(self) -> int:
        return len(self._queue) + (1 if self._processing else 0)

    # ------------------------------------------------------------------
    # 请求 / 响应
    # ------------------------------------------------------------------

    async def request_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        risk_level: str,
        session_id: str,
        run_id: str,
        tool_call_id: str | None = None,
    ) -> ApprovalRequest:
        """请求审批 — 立即返回 ApprovalRequest（含 Future）.

        关键：此方法立即返回，不等待审批完成。
        调用方通过 await request.future 等待审批结果。
        """
        approval_id = str(uuid.uuid4())
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()

        request = ApprovalRequest(
            id=approval_id,
            tool_name=tool_name,
            arguments=arguments,
            risk_level=risk_level,
            timeout=self._timeout,
            created_at=datetime.now(),
            session_id=session_id,
            run_id=run_id,
            future=future,
            tool_call_id=tool_call_id,
        )

        async with self._lock:
            self._pending[approval_id] = request
            self._queue.append(request)
            should_start = not self._processing

        logger.info(
            "approval_requested",
            approval_id=approval_id,
            tool=tool_name,
            session_id=session_id,
            queue_len=len(self._queue),
        )

        if should_start:
            self._processor_task = asyncio.create_task(self._process_queue())

        return request

    async def respond_approval(self, approval_id: str, action: str) -> bool:
        """响应审批请求.

        Args:
            approval_id: 审批请求 ID
            action: "allow" / "deny"

        Returns:
            是否成功处理（请求存在且未完成）
        """
        async with self._lock:
            request = self._pending.get(approval_id)
            if request is None:
                logger.warning("approval_not_found", approval_id=approval_id)
                return False
            if request.future.done():
                logger.warning("approval_already_resolved", approval_id=approval_id)
                return False

            approved = action.lower() in ("allow", "approved", "approve")
            request.future.set_result(approved)
            request.resolved = True
            request.resolution = "approved" if approved else "denied"
            request.decided_at = datetime.now()

        logger.info(
            "approval_responded",
            approval_id=approval_id,
            decision=request.resolution,
        )
        return True

    async def cancel_approval(self, approval_id: str) -> None:
        """取消单个审批请求."""
        async with self._lock:
            request = self._pending.pop(approval_id, None)
            if request is None:
                return
            # 从队列中移除
            try:
                self._queue.remove(request)
            except ValueError:
                pass
            if not request.future.done():
                request.future.set_result(False)
                request.resolved = True
                request.resolution = "cancelled"

    # ------------------------------------------------------------------
    # 队列处理
    # ------------------------------------------------------------------

    async def _process_queue(self) -> None:
        """顺序处理审批队列 — 同一时刻只处理一个请求."""
        async with self._lock:
            if self._processing:
                return
            self._processing = True

        try:
            while True:
                async with self._lock:
                    if not self._queue:
                        break
                    request = self._queue.popleft()

                await self._handle_single(request)
        finally:
            async with self._lock:
                self._processing = False
                self._processor_task = None

    async def _handle_single(self, request: ApprovalRequest) -> None:
        """处理单个审批请求：推送 → 等待结果 → 记录日志."""
        # 1. 推送审批请求到前端
        await self._push_request(request)

        # 2. 等待审批结果（带超时）
        # 注意：不能直接将 future 传给 wait_for，否则超时会取消 future 本身。
        # 使用 shield 保护 future，仅让 wait_for 的等待超时。
        try:
            approved = await asyncio.wait_for(
                asyncio.shield(request.future), timeout=request.timeout
            )
            if not request.resolved:
                request.resolved = True
                request.resolution = "approved" if approved else "denied"
                request.decided_at = datetime.now()
        except asyncio.TimeoutError:
            request.resolved = True
            request.resolution = "timeout"
            request.decided_at = datetime.now()
            if not request.future.done():
                request.future.set_result(False)

        # 3. 记录审批日志
        await self._log_approval(request)

        # 4. 推送结果到前端
        await self._push_result(request)

        # 5. 从 pending 中移除
        async with self._lock:
            self._pending.pop(request.id, None)

    async def _push_request(self, request: ApprovalRequest) -> None:
        """推送审批请求事件到前端."""
        if self._websocket_manager is None:
            return
        await self._websocket_manager.send_to_session(
            request.session_id,
            build_event(
                EventType.APPROVAL_QUEUE_STATUS,
                {
                    "queue_length": self.queue_length,
                    "position": 1,
                },
                session_id=request.session_id,
                run_id=request.run_id,
            ),
        )
        await self._websocket_manager.send_to_session(
            request.session_id,
            build_event(
                EventType.APPROVAL_REQUEST,
                {
                    "approval_id": request.id,
                    "tool_name": request.tool_name,
                    "arguments": request.arguments,
                    "risk_level": request.risk_level,
                    "timeout": request.timeout,
                    "tool_call_id": request.tool_call_id,
                },
                session_id=request.session_id,
                run_id=request.run_id,
            ),
        )

    async def _push_result(self, request: ApprovalRequest) -> None:
        """推送审批结果事件到前端."""
        if self._websocket_manager is None:
            return
        event_type = (
            EventType.APPROVAL_RESULT
            if request.resolution in ("approved", "denied")
            else EventType.APPROVAL_TIMEOUT
        )
        await self._websocket_manager.send_to_session(
            request.session_id,
            build_event(
                event_type,
                {
                    "approval_id": request.id,
                    "decision": request.resolution,
                    "tool_name": request.tool_name,
                },
                session_id=request.session_id,
                run_id=request.run_id,
            ),
        )

    async def _log_approval(self, request: ApprovalRequest) -> None:
        """记录审批日志到数据库."""
        if self._db is None:
            return
        decision_time_ms = 0.0
        if request.decided_at and request.created_at:
            decision_time_ms = (request.decided_at - request.created_at).total_seconds() * 1000

        try:
            decision_map = {
                "approved": ApprovalDecision.APPROVED,
                "denied": ApprovalDecision.DENIED,
                "timeout": ApprovalDecision.TIMEOUT,
                "cancelled": ApprovalDecision.DENIED,
            }
            from datetime import datetime

            await self._db.save_approval_log({
                "id": str(uuid.uuid4()),
                "session_id": request.session_id,
                "tool_call_id": request.tool_call_id or request.id,
                "tool_name": request.tool_name,
                "arguments": request.arguments,
                "risk_level": request.risk_level,
                "decision": str(decision_map.get(request.resolution, ApprovalDecision.DENIED)),
                "decision_time_ms": decision_time_ms,
                "timestamp": datetime.now().isoformat(),
            })
        except Exception as e:
            logger.error("approval_log_failed", approval_id=request.id, error=str(e))

    # ------------------------------------------------------------------
    # 批量操作
    # ------------------------------------------------------------------

    async def cancel_all_pending(self, session_id: str) -> None:
        """取消指定会话的所有待审批请求."""
        async with self._lock:
            to_cancel = [
                req for req in list(self._queue) if req.session_id == session_id
            ]
            for req in to_cancel:
                try:
                    self._queue.remove(req)
                except ValueError:
                    pass
            pending_to_cancel = [
                req for req in self._pending.values() if req.session_id == session_id
            ]

        for req in to_cancel + pending_to_cancel:
            if not req.future.done():
                req.future.set_result(False)
                req.resolved = True
                req.resolution = "cancelled"
                req.decided_at = datetime.now()
            async with self._lock:
                self._pending.pop(req.id, None)

        logger.info(
            "cancelled_pending_approvals",
            session_id=session_id,
            count=len(to_cancel) + len(pending_to_cancel),
        )

    def get_pending(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """获取待审批请求列表（供 API 查询）."""
        requests = list(self._pending.values())
        if session_id:
            requests = [r for r in requests if r.session_id == session_id]
        return [
            {
                "approval_id": r.id,
                "tool_name": r.tool_name,
                "arguments": r.arguments,
                "risk_level": r.risk_level,
                "created_at": r.created_at.isoformat(),
                "session_id": r.session_id,
                "run_id": r.run_id,
                "tool_call_id": r.tool_call_id,
                "resolved": r.resolved,
                "resolution": r.resolution,
            }
            for r in requests
        ]


# 全局单例
_approval_manager: ApprovalManager | None = None


def get_approval_manager(
    approval_timeout: int = 120,
    websocket_manager: Any | None = None,
    db: Any | None = None,
) -> ApprovalManager:
    """获取审批管理器单例."""
    global _approval_manager
    if _approval_manager is None:
        _approval_manager = ApprovalManager(
            approval_timeout=approval_timeout,
            websocket_manager=websocket_manager,
            db=db,
        )
    return _approval_manager


def set_approval_manager(manager: ApprovalManager) -> None:
    global _approval_manager
    _approval_manager = manager


def reset_approval_manager() -> None:
    """重置单例（测试用）."""
    global _approval_manager
    _approval_manager = None
