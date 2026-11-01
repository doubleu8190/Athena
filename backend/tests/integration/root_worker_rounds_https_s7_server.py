"""S7 逐轮验收使用的 HTTPS Worker 请求记录代理。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response


async def _serve_capture_proxy(
    *,
    upstream_origin: str,
    ca_file: str,
    capture_dir: str,
    certificate: str,
    private_key: str,
    host: str,
    port: int,
) -> None:
    """将 HTTPS 请求转发给独立 Worker，并持久记录其收到的 TaskCommand。"""
    app = FastAPI()
    lock = asyncio.Lock()
    task_index = 0

    @app.api_route(
        "/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    )
    async def proxy(request: Request, path: str) -> Response:
        nonlocal task_index
        body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        }
        async with httpx.AsyncClient(
            verify=ca_file, timeout=30, follow_redirects=False
        ) as client:
            upstream = await client.request(
                request.method,
                f"{upstream_origin}/{path}",
                params=request.query_params,
                headers=headers,
                content=body,
            )
        if path == "worker-api/v1/results":
            result_body: object = {}
            try:
                result_body = upstream.json()
            except ValueError:
                pass
            error_code = None
            if isinstance(result_body, dict):
                error = result_body.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    error_code = error["code"]
            event_path = Path(capture_dir) / "result-http-events.jsonl"
            event_path.parent.mkdir(parents=True, exist_ok=True)
            async with lock:
                with event_path.open("a", encoding="utf-8") as events:
                    events.write(
                        json.dumps(
                            {"status_code": upstream.status_code, "error_code": error_code}
                        )
                        + "\n"
                    )
        if path == "worker-api/v1/tasks" and upstream.status_code == 202:
            async with lock:
                task_index += 1
                capture_path = Path(capture_dir) / f"task-{task_index:04d}.json"
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                capture_path.write_text(
                    json.dumps(
                        {
                            "headers": {
                                "idempotency_key": headers.get("idempotency-key"),
                                "protocol_version": headers.get("x-athena-protocol-version"),
                            },
                            "body": json.loads(body),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
        response_headers = {
            key: value
            for key, value in upstream.headers.items()
            if key.lower() not in {"content-length", "transfer-encoding", "connection"}
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )

    await uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            ssl_certfile=certificate,
            ssl_keyfile=private_key,
            log_level="warning",
            access_log=False,
        )
    ).serve()


def run_capture_proxy(**kwargs: str | int) -> None:
    """以独立进程运行 HTTPS Worker 请求记录代理。"""
    asyncio.run(_serve_capture_proxy(**kwargs))


__all__ = ["run_capture_proxy"]
