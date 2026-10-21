"""阶段 3 的低风险查询 Controller。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from application.configuration import ProviderQueryService, SettingsQueryService
from application.memory import MemoryQueryService, MemoryService
from application.retrieval import RetrievalQueryService
from application.knowledge import KnowledgeBaseQueryService
from application.tools import ToolQueryService
from application.knowledge.responses import KnowledgeBaseResponse as ApplicationKnowledgeBaseResponse
from application.tools.responses import ToolResponse as ApplicationToolResponse
from application.memory import MemoryListRequest, MemorySearchRequest


class ToolResponse(BaseModel):
    name: str
    description: str
    risk_level: str
    execution_mode: str
    require_approval: bool
    enabled: bool
    parameters: dict[str, Any]
    last_called_at: str | None = None

    @classmethod
    def from_application(cls, item: ApplicationToolResponse, *, last_called_at: str | None = None) -> "ToolResponse":
        return cls(
            name=item.name,
            description=item.description,
            risk_level=item.risk_level.value if hasattr(item.risk_level, "value") else item.risk_level,
            execution_mode=item.execution_mode.value if hasattr(item.execution_mode, "value") else item.execution_mode,
            require_approval=item.require_approval,
            enabled=item.enabled,
            parameters=item.parameters,
            last_called_at=last_called_at,
        )


class ProviderResponse(BaseModel):
    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key_configured: bool = False
    api_key_masked: str = ""
    temperature: float = 0
    max_tokens: int = 0


class SettingsResponse(BaseModel):
    """设置只读 DTO；字段由配置端口提供。"""

    model_config = {"extra": "allow"}


class KnowledgeBaseResponse(BaseModel):
    id: str
    name: str
    description: str
    document_count: int
    ready_document_count: int
    total_size_bytes: int
    created_at: Any
    updated_at: Any

    @classmethod
    def from_application(cls, item: ApplicationKnowledgeBaseResponse) -> "KnowledgeBaseResponse":
        return cls(
            id=item.id,
            name=item.name,
            description=item.description,
            document_count=item.document_count,
            ready_document_count=item.ready_document_count,
            total_size_bytes=item.total_size_bytes,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


def build_query_router(
    *,
    tool_factory: Callable[[], ToolQueryService] | None = None,
    provider_factory: Callable[[], ProviderQueryService] | None = None,
    settings_factory: Callable[[], SettingsQueryService] | None = None,
    memory_factory: Callable[[], MemoryQueryService] | None = None,
    memory_service_factory: Callable[[], MemoryService] | None = None,
    retrieval_factory: Callable[[], RetrievalQueryService] | None = None,
    knowledge_factory: Callable[[], KnowledgeBaseQueryService] | None = None,
    include_tools: bool = True,
) -> APIRouter:
    """创建只依赖 application service 工厂的查询路由。"""

    router = APIRouter(tags=["queries"])

    if include_tools:
        @router.get("/tools")
        async def list_tools() -> dict[str, Any]:
            if tool_factory is None:
                raise HTTPException(status_code=503, detail="tool_query_not_configured")
            service = tool_factory()
            last_called, calls_today = await service.usage()
            items = [ToolResponse.from_application(item, last_called_at=last_called.get(item.name)) for item in await service.list_tools()]
            return {
                "items": [item.model_dump() for item in items],
                "total": len(items),
                "enabled": sum(item.enabled for item in items),
                "high_risk": sum(item.risk_level == "high" for item in items),
                "calls_today": calls_today,
            }

    @router.get("/providers", response_model=list[ProviderResponse])
    async def list_providers() -> list[ProviderResponse]:
        if provider_factory is None:
            raise HTTPException(status_code=503, detail="provider_query_not_configured")
        return [ProviderResponse(
            name=item.name,
            provider=item.provider,
            model=item.model,
            base_url=item.base_url,
            api_key_configured=item.api_key_configured,
            api_key_masked=item.api_key_masked,
            temperature=item.temperature,
            max_tokens=item.max_tokens,
        ) for item in await provider_factory().list_providers()]

    @router.post("/providers/test")
    async def test_provider(body: dict[str, Any]) -> dict[str, Any]:
        if provider_factory is None:
            raise HTTPException(status_code=503, detail="provider_query_not_configured")
        values = await provider_factory().list_providers()
        name = str(body.get("provider") or body.get("name") or "")
        matched = next((item for item in values if item.get("name") == name), None)
        return {"status": "ok" if matched is not None else "not_found", "provider": name}

    @router.get("/settings", response_model=SettingsResponse)
    async def get_settings() -> SettingsResponse:
        if settings_factory is None:
            raise HTTPException(status_code=503, detail="settings_query_not_configured")
        value = await settings_factory().get_settings()
        return SettingsResponse.model_validate(value.values if hasattr(value, "values") else value)

    @router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
    async def list_knowledge_bases() -> list[KnowledgeBaseResponse]:
        if knowledge_factory is None:
            raise HTTPException(status_code=503, detail="knowledge_query_not_configured")
        return [
            KnowledgeBaseResponse.from_application(item)
            for item in await knowledge_factory().list_knowledge_bases()
        ]

    @router.post("/memory/search")
    async def search_memory(body: dict[str, Any]) -> list[dict[str, Any]]:
        if memory_service_factory is not None:
            values = await memory_service_factory().search(
                MemorySearchRequest(
                    query=str(body.get("query", "")),
                    limit=int(body.get("n_results", 5)),
                    filters=body.get("where"),
                )
            )
            return [_memory_response(value) for value in values]
        if memory_factory is None:
            raise HTTPException(status_code=503, detail="memory_query_not_configured")
        values = await memory_factory().search(
            query=str(body.get("query", "")),
            n_results=int(body.get("n_results", 5)),
            where=body.get("where"),
        )
        return [_memory_response(value) for value in values]

    @router.get("/memory")
    async def list_memory(
        limit: int = Query(default=50, ge=1),
        offset: int = Query(default=0, ge=0),
        expired: bool = False,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if memory_service_factory is not None:
            page = await memory_service_factory().list_memories(
                MemoryListRequest(
                    limit=limit,
                    offset=offset,
                    expired_only=expired,
                    session_id=session_id,
                )
            )
            return {"items": [_memory_response(value) for value in page.items], **page.stats, "total": page.total}
        if memory_factory is None:
            raise HTTPException(status_code=503, detail="memory_query_not_configured")
        page = await memory_factory().list_memories(limit=limit, offset=offset, expired=expired, session_id=session_id)
        if hasattr(page, "items"):
            return {"items": [_memory_response(value) for value in page.items], **page.stats, "total": page.total}
        return page

    @router.get("/memory/{memory_id}")
    async def get_memory(memory_id: str) -> dict[str, Any]:
        if memory_service_factory is not None:
            value = await memory_service_factory().get(memory_id)
            if value is None:
                raise HTTPException(status_code=404, detail="memory_not_found")
            return _memory_response(value)
        if memory_factory is None:
            raise HTTPException(status_code=503, detail="memory_query_not_configured")
        value = await memory_factory().get(memory_id)
        if value is None:
            raise HTTPException(status_code=404, detail="memory_not_found")
        return _memory_response(value)

    @router.get("/memory/{memory_id}/revisions")
    async def list_memory_revisions(memory_id: str) -> list[dict[str, Any]]:
        if memory_service_factory is not None:
            values = await memory_service_factory().revisions(memory_id)
            if not values:
                raise HTTPException(status_code=404, detail="memory_not_found")
            return [_memory_response(value) for value in values]
        if memory_factory is None:
            raise HTTPException(status_code=503, detail="memory_query_not_configured")
        values = await memory_factory().revisions(memory_id)
        if not values:
            raise HTTPException(status_code=404, detail="memory_not_found")
        return [_memory_response(value) for value in values]

    @router.get("/retrieval/runs")
    async def list_retrieval_runs(
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        scope: str | None = None,
        status: str | None = None,
        session_id: str | None = None,
        agent_run_id: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        if retrieval_factory is None:
            raise HTTPException(status_code=503, detail="retrieval_query_not_configured")
        items, total = await retrieval_factory().list_runs(
            limit=limit,
            offset=offset,
            scope=scope,
            status=status,
            session_id=session_id,
            agent_run_id=agent_run_id,
            query=q,
        )
        return {"items": [asdict(item) for item in items], "total": total, "limit": limit, "offset": offset}

    @router.get("/retrieval/runs/{run_id}")
    async def get_retrieval_run(run_id: str) -> dict[str, Any]:
        if retrieval_factory is None:
            raise HTTPException(status_code=503, detail="retrieval_query_not_configured")
        value = await retrieval_factory().get_run(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="retrieval_run_not_found")
        return asdict(value)

    return router


def _memory_response(value: Any) -> dict[str, Any]:
    """将应用层记忆响应转换为 HTTP JSON 结构。"""
    result = {
        "id": value.id,
        "content": value.content,
        "metadata": dict(value.metadata),
        "created_at": value.created_at,
        "expires_at": value.expires_at,
        "last_accessed_at": value.last_accessed_at,
        "access_count": value.access_count,
        "last_observed_at": value.last_observed_at,
        "valid_until": value.valid_until,
        "validity_status": value.validity_status,

    }
    if value.score is not None:
        result["score"] = value.score
    if value.source is not None:
        result["source"] = value.source
    if getattr(value, "status", None) is not None:
        result["status"] = value.status
    return result
