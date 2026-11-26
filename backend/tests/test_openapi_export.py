"""OpenAPI 完整 Schema 导出脚本与路由响应模型完整度测试。"""

from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
import warnings
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from api.openapi import normalize_openapi_schema
from app.tool import ToolManager
from main import create_app
from scripts.export_openapi import build_openapi_app, export_openapi_schema

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _extract_included_router_targets(file_path: Path, function_name: str) -> list[str]:
    """通过 AST 提取指定函数中 app.include_router(...) 挂载的路由构建器或路由变量名。"""
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            targets: list[str] = []
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute)
                    and child.func.attr == "include_router"
                    and child.args
                ):
                    first_arg = child.args[0]
                    if isinstance(first_arg, ast.Call) and isinstance(first_arg.func, ast.Name):
                        targets.append(first_arg.func.id)
                    elif isinstance(first_arg, ast.Name):
                        targets.append(first_arg.id)
            return targets
    raise AssertionError(f"function {function_name!r} not found in {file_path}")


def _iter_operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    operations: list[tuple[str, str, dict[str, Any]]] = []
    for path, path_item in sorted(schema["paths"].items()):
        for method, operation in sorted(path_item.items()):
            if method in _HTTP_METHODS:
                operations.append((method, path, operation))
    return operations


def test_openapi_export_valid_schema_no_warnings_or_dup_ops() -> None:
    """零依赖导出应生成合法 OpenAPI 3.x 文档，无 Pydantic 告警且 operationId 全局唯一。"""
    with warnings.catch_warnings(record=True) as build_caught:
        warnings.simplefilter("always")
        app = build_openapi_app()

    # 过滤第三方依赖首次导入期的 DeprecationWarning，
    # 其余任何路由注册期或项目代码告警均视为失败。
    unexpected_build_warnings = [
        f"{w.category.__name__} ({w.filename}:{w.lineno}): {w.message}"
        for w in build_caught
        if not (issubclass(w.category, DeprecationWarning) and "site-packages" in str(w.filename))
    ]
    assert unexpected_build_warnings == []

    # OpenAPI Schema 生成阶段本身必须严格零告警。
    with warnings.catch_warnings(record=True) as openapi_caught:
        warnings.simplefilter("always")
        schema = app.openapi()

    assert openapi_caught == [], [f"{w.category.__name__}: {w.message}" for w in openapi_caught]
    assert isinstance(schema.get("openapi"), str)
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Athena"
    assert schema["paths"]
    assert schema["components"]["schemas"]

    operations = _iter_operations(schema)
    assert len(operations) >= 120

    seen_operation_ids: dict[str, tuple[str, str]] = {}
    for method, path, operation in operations:
        operation_id = operation.get("operationId")
        assert isinstance(operation_id, str) and operation_id
        assert operation_id not in seen_operation_ids, (
            f"duplicate operationId {operation_id!r}: "
            f"{seen_operation_ids[operation_id]} vs {(method, path)}"
        )
        seen_operation_ids[operation_id] = (method, path)


def test_openapi_export_router_list_matches_main_create_app() -> None:
    """导出脚本挂载的路由分组清单必须与 backend/src/main.py::create_app 保持全量一致。"""
    main_routers = _extract_included_router_targets(
        _REPO_ROOT / "backend" / "src" / "main.py",
        "create_app",
    )
    export_routers = _extract_included_router_targets(
        _REPO_ROOT / "scripts" / "export_openapi.py",
        "build_openapi_app",
    )
    assert len(main_routers) >= 20
    assert export_routers == main_routers


def test_openapi_export_covers_all_core_prefixes_and_key_routes() -> None:
    """验证全部业务路由前缀与前端关键路径（含 project_knowledge 与 SSE 流）完整存在。"""
    schema = export_openapi_schema()
    paths: dict[str, dict[str, Any]] = schema["paths"]

    required_prefixes = (
        "/api/v1/auth",
        "/api/v1/projects",
        "/api/v1/sessions",
        "/api/v1/admin",
        "/api/v1/tools",
        "/api/v1/knowledge-bases",
        "/api/v1/me",
        "/api/v1/skills",
        "/api/v1/models",
    )
    for prefix in required_prefixes:
        assert any(path == prefix or path.startswith(f"{prefix}/") for path in paths), (
            f"missing route prefix: {prefix}"
        )

    # 逐一校验主路由组的关键路径与方法（含条件挂载的 mock 登录、project_knowledge 与 run SSE）。
    required_endpoints = (
        ("get", "/health"),
        ("get", "/api/v1/auth/providers"),
        ("post", "/api/v1/auth/login"),
        ("post", "/api/v1/auth/logout"),
        ("get", "/api/v1/auth/me"),
        ("patch", "/api/v1/auth/me"),
        ("post", "/api/v1/auth/password"),
        ("get", "/api/v1/auth/mock/users"),
        ("post", "/api/v1/auth/mock/login"),
        ("get", "/api/v1/auth/feishu/authorize"),
        ("get", "/api/v1/auth/feishu/callback"),
        ("get", "/api/v1/projects"),
        ("post", "/api/v1/projects"),
        ("get", "/api/v1/projects/{project_id}/knowledge-bases"),
        ("put", "/api/v1/projects/{project_id}/knowledge-bases/{base_id}"),
        ("delete", "/api/v1/projects/{project_id}/knowledge-bases/{base_id}"),
        ("get", "/api/v1/sessions"),
        ("get", "/api/v1/sessions/{session_id}/messages"),
        ("post", "/api/v1/sessions/{session_id}/runs"),
        ("get", "/api/v1/sessions/{session_id}/runs/{run_id}/stream"),
        ("post", "/api/v1/sessions/{session_id}/files"),
        ("get", "/api/v1/sessions/{session_id}/files/{file_id}/content"),
        ("get", "/api/v1/sessions/{session_id}/files/{file_id}/download"),
        ("get", "/api/v1/sessions/{session_id}/files/{file_id}/preview"),
        ("get", "/api/v1/knowledge-bases"),
        ("post", "/api/v1/knowledge-bases/{base_id}/search"),
        (
            "get",
            "/api/v1/knowledge-bases/{base_id}/documents/{document_id}/versions/{version_id}/source",
        ),
        ("get", "/api/v1/models"),
        ("get", "/api/v1/skills"),
        ("get", "/api/v1/tools"),
        ("get", "/api/v1/me/preferences"),
        ("get", "/api/v1/me/memories"),
        ("get", "/api/v1/me/skills"),
        ("get", "/api/v1/admin/org/departments"),
        ("post", "/api/v1/admin/org/sync"),
        ("get", "/api/v1/admin/llm/model-profiles"),
        ("get", "/api/v1/admin/llm/compression-policy"),
        ("get", "/api/v1/admin/mcp/servers"),
        ("get", "/api/v1/admin/tools"),
        ("get", "/api/v1/admin/system-prompts/{kind}"),
        ("get", "/api/v1/admin/subagents"),
        ("get", "/api/v1/admin/skills"),
        ("get", "/api/v1/admin/skill-executions/{call_id}/log"),
    )
    for method, path in required_endpoints:
        assert path in paths, f"missing path: {path}"
        assert method in paths[path], f"missing method {method.upper()} on {path}"


def test_openapi_responses_have_explicit_json_schemas_clean_204_and_accurate_non_json_media() -> (
    None
):
    """验证全部 JSON 路由具显式响应模型、204 无多余 JSON body、非 JSON 路由声明准确媒体类型。"""
    schema = export_openapi_schema()
    paths: dict[str, dict[str, Any]] = schema["paths"]

    # 1. 全部 204 响应绝不得包含 content；全部 JSON 2xx 响应必须具备非空 schema（非裸 {}）。
    for method, path, operation in _iter_operations(schema):
        responses = operation.get("responses", {})
        if "204" in responses:
            assert "content" not in responses["204"], (
                f"204 should not declare content: {method} {path}"
            )
        for status_code, resp_spec in responses.items():
            if not status_code.startswith("2") or status_code == "204":
                continue
            content = resp_spec.get("content", {})
            assert content, f"2xx response missing content: {method} {path} ({status_code})"
            if "application/json" in content:
                json_schema = content["application/json"].get("schema", {})
                assert json_schema not in ({}, {"type": "object"}), (
                    f"JSON endpoint has untyped response schema: {method.upper()} {path}"
                )

    # 2. 校验 7 个非 JSON 端点（302 重定向、二进制下载/预览、纯文本日志、SSE 流）的媒体类型。
    for redirect_path in ("/api/v1/auth/feishu/authorize", "/api/v1/auth/feishu/callback"):
        redirect_responses = paths[redirect_path]["get"]["responses"]
        assert "302" in redirect_responses
        assert "200" not in redirect_responses

    for binary_path in (
        "/api/v1/sessions/{session_id}/files/{file_id}/download",
        "/api/v1/knowledge-bases/{base_id}/documents/{document_id}/versions/{version_id}/source",
    ):
        binary_content = paths[binary_path]["get"]["responses"]["200"]["content"]
        assert "application/json" not in binary_content
        assert binary_content["application/octet-stream"]["schema"] == {
            "type": "string",
            "format": "binary",
        }

    preview_content = paths["/api/v1/sessions/{session_id}/files/{file_id}/preview"]["get"][
        "responses"
    ]["200"]["content"]
    assert "application/json" not in preview_content
    assert set(preview_content) == {"image/png", "image/jpeg", "image/webp"}

    log_content = paths["/api/v1/admin/skill-executions/{call_id}/log"]["get"]["responses"]["200"][
        "content"
    ]
    assert "application/json" not in log_content
    assert log_content["text/plain"]["schema"] == {"type": "string"}

    sse_content = paths["/api/v1/sessions/{session_id}/runs/{run_id}/stream"]["get"]["responses"][
        "200"
    ]["content"]
    assert "application/json" not in sse_content
    assert sse_content["text/event-stream"]["schema"] == {"type": "string"}


def test_export_openapi_cli_writes_deterministic_json_from_subdirectory(tmp_path: Path) -> None:
    """从 frontend/ 子目录运行导出脚本也应正常写入确定性、可复现的 OpenAPI JSON 文件。"""
    script_path = _REPO_ROOT / "scripts" / "export_openapi.py"
    out_file = tmp_path / "nested" / "openapi.json"

    frontend_dir = _REPO_ROOT / "frontend"
    run_cwd = frontend_dir if frontend_dir.is_dir() else _REPO_ROOT
    completed = subprocess.run(
        [sys.executable, str(script_path), "--output", str(out_file)],
        cwd=run_cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    assert out_file.is_file()

    stdout_run = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=_REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    written_text = out_file.read_text(encoding="utf-8")
    assert written_text == stdout_run.stdout
    parsed = json.loads(written_text)
    assert parsed["openapi"].startswith("3.")
    assert "/api/v1/projects/{project_id}/knowledge-bases" in parsed["paths"]


def _assert_all_refs_resolvable(schema: dict[str, Any]) -> None:
    """递归校验 OpenAPI 文档不含局部 $defs / #/$defs/ 残留，且所有组件引用均可解析。"""
    component_schemas: dict[str, Any] = schema.get("components", {}).get("schemas", {})
    ref_count = 0

    def _walk(node: Any, location: str, *, in_properties: bool = False) -> None:
        nonlocal ref_count
        if isinstance(node, list):
            for idx, item in enumerate(node):
                _walk(item, f"{location}[{idx}]")
            return
        if not isinstance(node, dict):
            return

        for key, value in node.items():
            child_loc = f"{location}.{key}"
            if key == "$defs" and not in_properties:
                raise AssertionError(f"unexpected local $defs remaining at {child_loc}")
            if key == "$ref" and isinstance(value, str):
                ref_count += 1
                assert not value.startswith("#/$defs/"), (
                    f"dangling local $defs ref at {child_loc}: {value}"
                )
                assert value.startswith("#/components/schemas/"), (
                    f"unexpected $ref target at {child_loc}: {value}"
                )
                target = value.removeprefix("#/components/schemas/")
                assert target in component_schemas, (
                    f"unresolved $ref target {target!r} at {child_loc}"
                )
            elif key == "discriminator" and isinstance(value, dict):
                mapping = value.get("mapping")
                if isinstance(mapping, dict):
                    for map_key, map_val in mapping.items():
                        assert isinstance(map_val, str)
                        assert not map_val.startswith("#/$defs/"), (
                            f"dangling discriminator.mapping ref at {child_loc}.{map_key}: "
                            f"{map_val}"
                        )
                        assert map_val.startswith("#/components/schemas/"), (
                            f"unexpected discriminator.mapping target at "
                            f"{child_loc}.{map_key}: {map_val}"
                        )
                        target = map_val.removeprefix("#/components/schemas/")
                        assert target in component_schemas, (
                            f"unresolved discriminator.mapping target {target!r} "
                            f"at {child_loc}.{map_key}"
                        )
            _walk(value, child_loc, in_properties=(key == "properties"))

    _walk(schema, "$")
    assert ref_count > 0


def test_openapi_export_has_no_dangling_refs_or_local_defs() -> None:
    """导出文档中的所有 $ref 与 discriminator.mapping 必须无 #/$defs/ 残留且全部可解析。"""
    schema = export_openapi_schema()
    _assert_all_refs_resolvable(schema)

    component_schemas: dict[str, Any] = schema["components"]["schemas"]
    for expected_name in (
        "HttpServerCreateRequest",
        "StdioServerCreateRequest",
        "HttpServerUpdateRequest",
        "StdioServerUpdateRequest",
        "VisibilityGrantRequest",
    ):
        assert expected_name in component_schemas

    post_schema = schema["paths"]["/api/v1/admin/mcp/servers"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "$defs" not in post_schema
    assert post_schema["discriminator"]["mapping"] == {
        "stdio": "#/components/schemas/StdioServerCreateRequest",
        "streamable_http": "#/components/schemas/HttpServerCreateRequest",
    }

    put_schema = schema["paths"]["/api/v1/admin/mcp/servers/{server_id}"]["put"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "$defs" not in put_schema
    assert put_schema["discriminator"]["mapping"] == {
        "stdio": "#/components/schemas/StdioServerUpdateRequest",
        "streamable_http": "#/components/schemas/HttpServerUpdateRequest",
    }

    patch_schema = schema["paths"]["/api/v1/admin/mcp/servers/{server_id}"]["patch"]["requestBody"][
        "content"
    ]["application/json"]["schema"]
    assert "$defs" not in patch_schema
    assert (
        patch_schema["properties"]["visibility_grants"]["anyOf"][0]["items"]["$ref"]
        == "#/components/schemas/VisibilityGrantRequest"
    )


def test_create_app_online_openapi_json_has_no_dangling_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """通过 create_app 挂载 MCP 路由暴露的在线 /openapi.json 也必须无悬空 $ref。"""
    monkeypatch.delenv("ATHENA_DATABASE_URL", raising=False)
    app = create_app(tool_manager=cast(ToolManager, object()))

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    doc = response.json()
    _assert_all_refs_resolvable(doc)
    assert "HttpServerCreateRequest" in doc["components"]["schemas"]
    assert "StdioServerUpdateRequest" in doc["components"]["schemas"]


def test_normalize_openapi_schema_handles_same_name_conflicts_and_preserves_input() -> None:
    """同名同构定义复用、同名异构定义按确定性后缀避让，且不原地修改原始输入对象。"""
    raw_doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "Shared": {"type": "object", "properties": {"id": {"type": "string"}}},
                "Item": {"type": "object", "properties": {"existing": {"type": "string"}}},
            }
        },
        "paths": {
            "/demo": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$defs": {
                                        "Shared": {
                                            "type": "object",
                                            "properties": {"id": {"type": "string"}},
                                        },
                                        "Item": {
                                            "type": "object",
                                            "properties": {
                                                "kind": {"const": "alt"},
                                                "shared": {"$ref": "#/$defs/Shared"},
                                            },
                                        },
                                        "Wrapper": {
                                            "type": "object",
                                            "properties": {"item": {"$ref": "#/$defs/Item"}},
                                        },
                                    },
                                    "oneOf": [
                                        {"$ref": "#/$defs/Item"},
                                        {"$ref": "#/$defs/Wrapper"},
                                    ],
                                    "discriminator": {
                                        "propertyName": "kind",
                                        "mapping": {"alt": "#/$defs/Item"},
                                    },
                                }
                            }
                        }
                    }
                }
            }
        },
    }
    snapshot = copy.deepcopy(raw_doc)

    normalized = normalize_openapi_schema(raw_doc)

    # 原始输入保持不可变，避免污染 FastAPI 路由上的 openapi_extra 引用。
    assert raw_doc == snapshot
    _assert_all_refs_resolvable(normalized)

    schemas = normalized["components"]["schemas"]
    # 原有 Item 绝不被覆盖，同名异构定义被分配确定性后缀 Item_2，Shared 同构复用。
    assert schemas["Item"] == {"type": "object", "properties": {"existing": {"type": "string"}}}
    assert "Shared_2" not in schemas
    assert schemas["Item_2"]["properties"]["shared"]["$ref"] == "#/components/schemas/Shared"
    assert schemas["Wrapper"]["properties"]["item"]["$ref"] == "#/components/schemas/Item_2"

    post_schema = normalized["paths"]["/demo"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert "$defs" not in post_schema
    assert post_schema["oneOf"] == [
        {"$ref": "#/components/schemas/Item_2"},
        {"$ref": "#/components/schemas/Wrapper"},
    ]
    assert post_schema["discriminator"]["mapping"] == {"alt": "#/components/schemas/Item_2"}
