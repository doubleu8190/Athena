"""Small Worker runtime collaborators for dispatcher unit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class NoopGraphThreadLock:
    @asynccontextmanager
    async def hold(self, _thread_id: str) -> AsyncIterator[None]:
        yield


class AcceptingWorkerApprovalService:
    async def request(self, _lease: object, _approval: object) -> bool:
        return True


__all__ = ["AcceptingWorkerApprovalService", "NoopGraphThreadLock"]
