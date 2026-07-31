"""WebSocket 连接管理 — 按 session_id 隔离的连接池."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    """WebSocket 连接管理器.

    维护 session_id → WebSocket 连接集合的映射，
    支持同一会话多标签页连接（所有标签页都收到事件）。
    """

    def __init__(self) -> None:
        # session_id -> set of WebSocket connections
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        """接受连接并注册到会话池."""
        await websocket.accept()
        async with self._lock:
            if session_id not in self._connections:
                self._connections[session_id] = set()
            self._connections[session_id].add(websocket)
        logger.info("ws_connected", session_id=session_id)

    async def disconnect(self, session_id: str, websocket: WebSocket) -> None:
        """移除连接."""
        async with self._lock:
            if session_id in self._connections:
                self._connections[session_id].discard(websocket)
                if not self._connections[session_id]:
                    del self._connections[session_id]
        logger.info("ws_disconnected", session_id=session_id)

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        """向指定会话的所有连接推送消息."""
        if session_id not in self._connections:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in self._connections[session_id]:
            try:
                await ws.send_text(text)
            except Exception as e:
                logger.warning("ws_send_failed", session_id=session_id, error=str(e))
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.get(session_id, set()).discard(ws)

    async def send_to_connection(
        self, session_id: str, websocket: WebSocket, message: dict[str, Any]
    ) -> None:
        """向指定会话的单个连接推送消息."""
        text = json.dumps(message, ensure_ascii=False, default=str)
        try:
            await websocket.send_text(text)
        except Exception as e:
            logger.warning("ws_send_failed", error=str(e))
            await self.disconnect(session_id, websocket)

    def has_session(self, session_id: str) -> bool:
        return session_id in self._connections and bool(self._connections[session_id])

    async def close_session(self, session_id: str) -> None:
        """关闭指定会话的所有连接."""
        async with self._lock:
            conns = self._connections.pop(session_id, set())
        for ws in conns:
            try:
                await ws.close()
            except Exception:
                pass


# 全局单例
_ws_manager: WebSocketManager | None = None


def get_websocket_manager() -> WebSocketManager:
    global _ws_manager
    if _ws_manager is None:
        _ws_manager = WebSocketManager()
    return _ws_manager


def set_websocket_manager(manager: WebSocketManager) -> None:
    global _ws_manager
    _ws_manager = manager
