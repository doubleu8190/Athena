"""Evaluation dataset and run HTTP contracts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
import json
from pydantic import BaseModel, Field

from application.evaluation import EvaluationQueryService, EvaluationService, FixtureChatTarget, make_evaluation_config, make_gate_rules


class DatasetImportRequest(BaseModel):
    path: str
    name: str | None = None


class EvaluationRunRequest(BaseModel):
    dataset_path: str
    name: str = "evaluation"
    mode: str = "retrieval"
    target: dict[str, Any] = Field(default_factory=dict)
    evaluators: list[str] = Field(default_factory=lambda: ["retrieval_metrics"])
    gates: list[dict[str, Any]] = Field(default_factory=list)
    concurrency: int = Field(default=1, ge=1, le=64)
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    max_attempts: int = Field(default=1, ge=1, le=5)
    baseline_run_id: str | None = None


class GateEvaluationRequest(BaseModel):
    gates: list[dict[str, Any]]


def build_evaluation_router(
    *,
    repository_factory: Callable[[], Any],
    target_factory: Callable[[Any], Any] | None = None,
    dataset_loader_factory: Callable[[str], Any] | None = None,
    event_sink_factory: Callable[[], Any] | None = None,
    progress_hub_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/evaluations", tags=["evaluations"])

    @router.get("/runs/{run_id}/events")
    async def evaluation_events(run_id: str, request: Request) -> StreamingResponse:
        if progress_hub_factory is None:
            raise HTTPException(status_code=503, detail="evaluation_progress_not_configured")
        async def stream():
            async for event in progress_hub_factory().subscribe(run_id):
                if await request.is_disconnected():
                    break
                yield f"event: {event['event_type']}\ndata: {json.dumps(event['payload'], ensure_ascii=False)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/datasets")
    async def list_datasets() -> dict[str, Any]:
        service = EvaluationQueryService(repository_factory())
        if not hasattr(service.repository, "list_manifests"):
            return {"items": []}
        return {"items": await service.list_datasets()}

    @router.get("/datasets/{dataset_id}/versions")
    async def list_dataset_versions(dataset_id: str) -> dict[str, Any]:
        service = EvaluationQueryService(repository_factory())
        values = [item for item in await service.list_datasets() if item["dataset_name"] == dataset_id]
        return {"items": values}

    @router.post("/datasets/validate")
    async def validate_dataset(body: DatasetImportRequest) -> dict[str, Any]:
        try:
            if dataset_loader_factory is None:
                raise RuntimeError("evaluation_dataset_loader_not_configured")
            manifest = dataset_loader_factory(body.path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"dataset_name": manifest.dataset_name, "version_id": manifest.version_id, "manifest_sha256": manifest.manifest_sha256, "case_count": manifest.case_count, "tag_counts": manifest.tag_counts}

    @router.post("/datasets")
    async def create_dataset(body: DatasetImportRequest) -> dict[str, Any]:
        if dataset_loader_factory is None:
            raise HTTPException(status_code=503, detail="evaluation_dataset_loader_not_configured")
        try:
            manifest = dataset_loader_factory(body.path)
            await repository_factory().create_manifest(manifest)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"dataset_name": manifest.dataset_name, "version_id": manifest.version_id, "manifest_sha256": manifest.manifest_sha256, "case_count": manifest.case_count}

    @router.get("/runs")
    async def list_runs(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), status: str | None = None, dataset_version: str | None = None) -> dict[str, Any]:
        items, total = await EvaluationQueryService(repository_factory()).list_runs(limit=limit, offset=offset, status=status, dataset_version=dataset_version)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        value = await EvaluationQueryService(repository_factory()).get_run(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        return value

    @router.get("/runs/{run_id}/metrics")
    async def get_metrics(run_id: str, metric_name: str | None = None) -> dict[str, Any]:
        value = await EvaluationQueryService(repository_factory()).get_run(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        metrics = value["metrics"] if metric_name is None else [item for item in value["metrics"] if item["metric_name"] == metric_name]
        return {"run_id": run_id, "items": metrics}

    @router.get("/runs/{run_id}/cases")
    async def get_cases(run_id: str) -> dict[str, Any]:
        value = await EvaluationQueryService(repository_factory()).get_run(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        return {"run_id": run_id, "items": value["cases"]}

    @router.get("/runs/{run_id}/compare")
    async def compare_run(run_id: str, baseline_run_id: str = Query(...)) -> dict[str, Any]:
        value = await EvaluationQueryService(repository_factory()).compare(run_id, baseline_run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        return value

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        value = await repository_factory().cancel_run(run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        if str(value.status) != "cancelled":
            raise HTTPException(status_code=409, detail="evaluation_run_already_completed")
        return {"run_id": value.run_id, "status": str(value.status)}

    @router.post("/runs/{run_id}/gates")
    async def recalculate_gates(run_id: str, body: GateEvaluationRequest) -> dict[str, Any]:
        try:
            rules = make_gate_rules(body.gates)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid_gate_rule") from exc
        items = await EvaluationQueryService(repository_factory()).evaluate_gates(run_id, rules)
        if items is None:
            raise HTTPException(status_code=404, detail="evaluation_run_not_found")
        return {"run_id": run_id, "items": items}

    @router.post("/runs")
    async def create_run(body: EvaluationRunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
        if body.mode not in {"retrieval", "chat", "agent"}:
            raise HTTPException(status_code=422, detail="evaluation_mode_not_supported")
        try:
            if dataset_loader_factory is None:
                raise RuntimeError("evaluation_dataset_loader_not_configured")
            manifest = dataset_loader_factory(body.dataset_path)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        repository = repository_factory()
        await repository.create_manifest(manifest)
        config = make_evaluation_config(body.model_dump(), manifest.version_id)
        if target_factory is None and body.mode == "retrieval":
            raise HTTPException(status_code=503, detail="evaluation_target_not_configured")
        target = target_factory(manifest) if target_factory is not None and body.mode == "retrieval" else FixtureChatTarget()
        service = EvaluationService(repository, target=target, event_sink=event_sink_factory() if event_sink_factory is not None else None)
        queued = await service.queue(manifest, config)
        background_tasks.add_task(service.run, manifest, config, run_id=queued.run_id)
        return {"run_id": queued.run_id, "status": str(queued.status), "case_count": queued.case_count}

    return router


__all__ = ["build_evaluation_router"]
