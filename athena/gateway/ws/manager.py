"""WebSocket 连接管理 — 按 session_id 隔离的连接池."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

from athena.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    """WebSocket 连接管理器（单一全局连接 + 订阅制）.

    应用只建一条 /ws 连接，切换会话时通过 SUBSCRIBE 消息切换订阅的会话。
    维护 session_id → WebSocket 连接集合的映射（订阅桶），
    支持同一会话多标签页连接（所有订阅该会话的连接都收到事件）。

    与旧实现的差异：连接不再在创建时由 URL 路径绑定 session，
    而是由客户端显式订阅/退订决定它属于哪个会话桶。
    """

    def __init__(self) -> None:
        # session_id -> set of WebSocket connections（订阅桶）
        self._connections: dict[str, set[WebSocket]] = {}
        # WebSocket -> 当前订阅的 session_id（None = 未订阅）
        self._ws_session: dict[WebSocket, str | None] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """接受连接并注册（初始不订阅任何会话）."""
        await websocket.accept()
        async with self._lock:
            self._ws_session[websocket] = None
        logger.info("ws_connected")

    async def subscribe(self, websocket: WebSocket, session_id: str | None) -> None:
        """把 ws 从旧会话桶移到新会话桶；session_id=None 表示退订."""
        async with self._lock:
            old = self._ws_session.get(websocket)
            if old and old in self._connections:
                self._connections[old].discard(websocket)
                if not self._connections[old]:
                    del self._connections[old]
            self._ws_session[websocket] = session_id
            if session_id:
                self._connections.setdefault(session_id, set()).add(websocket)
        logger.info("ws_subscribed", session_id=session_id)

    def get_subscribed_session(self, websocket: WebSocket) -> str | None:
        """返回连接当前订阅的 session_id（未订阅返回 None）."""
        return self._ws_session.get(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """移除连接及其订阅."""
        async with self._lock:
            sid = self._ws_session.pop(websocket, None)
            if sid and sid in self._connections:
                self._connections[sid].discard(websocket)
                if not self._connections[sid]:
                    del self._connections[sid]
        logger.info("ws_disconnected", session_id=sid)

    async def send_to_session(self, session_id: str, message: dict[str, Any]) -> None:
        """向指定会话的所有连接推送消息."""
        if session_id not in self._connections:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for ws in self._connections[session_id]:
            try:
                await ws.send_text(text)
                logger.info("ws_send_success", session_id=session_id, message=text)
            except Exception as e:
                logger.warning("ws_send_failed", session_id=session_id, error=str(e))
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._connections.get(session_id, set()).discard(ws)

    async def send_to_connection(
        self, websocket: WebSocket, message: dict[str, Any]
    ) -> None:
        """向单个连接推送消息."""
        text = json.dumps(message, ensure_ascii=False, default=str)
        try:
            await websocket.send_text(text)
        except Exception as e:
            logger.warning("ws_send_failed", error=str(e))
            await self.disconnect(websocket)

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
_ws_manager: WebSocketManager


def get_websocket_manager() -> WebSocketManager:
    return _ws_manager


def set_websocket_manager(manager: WebSocketManager) -> None:
    global _ws_manager
    _ws_manager = manager
