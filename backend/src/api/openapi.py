"""OpenAPI Schema 规范化工具。

FastAPI 不会自动提升路由 ``openapi_extra`` 中内嵌的 Pydantic 局部 ``$defs``，
导致在线 ``/openapi.json`` 或离线导出文档中出现无法被 OpenAPI 3.1 工具链
（如 Redocly / openapi-typescript）解析的 ``#/$defs/...`` 悬空引用。
本模块将文档内残留的局部 ``$defs`` 统一提升至顶层 ``components.schemas``，
并在存在同名异构定义冲突时分配确定性后缀名称，避免覆盖已有组件定义。
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

_LOCAL_DEF_PREFIX = "#/$defs/"
_COMPONENT_SCHEMA_PREFIX = "#/components/schemas/"


def _rewrite_local_refs(value: Any, target_map: dict[str, str]) -> Any:
    """将命中 target_map 的 #/$defs/... 引用（含 discriminator.mapping）改写为顶层组件引用。"""
    if isinstance(value, list):
        return [_rewrite_local_refs(item, target_map) for item in value]
    if not isinstance(value, dict):
        return value

    rewritten: dict[str, Any] = {}
    for key, item in value.items():
        if key == "$ref" and isinstance(item, str) and item.startswith(_LOCAL_DEF_PREFIX):
            local_name = item.removeprefix(_LOCAL_DEF_PREFIX)
            target_name = target_map.get(local_name)
            rewritten[key] = (
                f"{_COMPONENT_SCHEMA_PREFIX}{target_name}" if target_name is not None else item
            )
        elif key == "discriminator" and isinstance(item, dict):
            disc_copy: dict[str, Any] = _rewrite_local_refs(item, target_map)
            mapping = disc_copy.get("mapping")
            if isinstance(mapping, dict):
                disc_copy["mapping"] = {
                    map_key: (
                        f"{_COMPONENT_SCHEMA_PREFIX}"
                        f"{target_map[map_val.removeprefix(_LOCAL_DEF_PREFIX)]}"
                        if isinstance(map_val, str)
                        and map_val.startswith(_LOCAL_DEF_PREFIX)
                        and map_val.removeprefix(_LOCAL_DEF_PREFIX) in target_map
                        else map_val
                    )
                    for map_key, map_val in mapping.items()
                }
            rewritten[key] = disc_copy
        else:
            rewritten[key] = _rewrite_local_refs(item, target_map)
    return rewritten


def _allocate_non_conflicting_name(
    local_name: str,
    raw_def: Any,
    target_map: dict[str, str],
    component_schemas: dict[str, Any],
) -> str:
    """为与已有 components.schemas 发生同名异构冲突的局部定义分配确定性无冲突名称。"""
    used_by_siblings = {mapped for name, mapped in target_map.items() if name != local_name}
    suffix = 2
    while True:
        candidate = f"{local_name}_{suffix}"
        if candidate not in used_by_siblings:
            if candidate not in component_schemas:
                return candidate
            candidate_rewritten = _rewrite_local_refs(
                raw_def, {**target_map, local_name: candidate}
            )
            if component_schemas[candidate] == candidate_rewritten:
                return candidate
        suffix += 1


def _resolve_and_register_defs(
    local_defs: dict[str, Any],
    component_schemas: dict[str, Any],
) -> dict[str, str]:
    """解析一组局部 $defs 的目标组件名并注册到 components.schemas，严格避免覆盖不同定义。"""
    sorted_names = sorted(local_defs)
    target_map: dict[str, str] = {name: name for name in sorted_names}

    # 采用不动点迭代：若兄弟定义因同名冲突被重命名，引用它的同组定义会在下一轮重新比对与改写。
    rewritten_defs: dict[str, Any] = {}
    for _ in range(len(sorted_names) + 1):
        changed = False
        rewritten_defs = {
            name: _rewrite_local_refs(local_defs[name], target_map) for name in sorted_names
        }
        for name in sorted_names:
            target_name = target_map[name]
            existing = component_schemas.get(target_name)
            if existing is not None and existing != rewritten_defs[name]:
                target_map[name] = _allocate_non_conflicting_name(
                    name, local_defs[name], target_map, component_schemas
                )
                changed = True
        if not changed:
            break

    for name in sorted_names:
        target_name = target_map[name]
        if target_name not in component_schemas:
            component_schemas[target_name] = rewritten_defs[name]
    return target_map


def _hoist_nested_defs(
    node: Any,
    get_component_schemas: Callable[[], dict[str, Any]],
) -> Any:
    """自底向上递归扫描节点，剥离局部 $defs 并改写当前作用域内的局部引用。"""
    if isinstance(node, list):
        return [_hoist_nested_defs(item, get_component_schemas) for item in node]
    if not isinstance(node, dict):
        return node

    raw_defs = node.get("$defs")
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "$defs":
            continue
        if key == "properties" and isinstance(value, dict):
            # properties 下的键是字段名而非 JSON Schema 关键字，仅对其属性 Schema 值递归。
            cleaned[key] = {
                prop_name: _hoist_nested_defs(prop_schema, get_component_schemas)
                for prop_name, prop_schema in value.items()
            }
        else:
            cleaned[key] = _hoist_nested_defs(value, get_component_schemas)

    if isinstance(raw_defs, dict):
        if not raw_defs:
            return cleaned
        normalized_local_defs = {
            def_name: _hoist_nested_defs(def_schema, get_component_schemas)
            for def_name, def_schema in raw_defs.items()
        }
        target_map = _resolve_and_register_defs(
            normalized_local_defs,
            get_component_schemas(),
        )
        return _rewrite_local_refs(cleaned, target_map)

    if "$defs" in node:
        # 非字典形式的异常字段保持原样，避免误删。
        cleaned["$defs"] = _hoist_nested_defs(raw_defs, get_component_schemas)
    return cleaned


def normalize_openapi_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """返回规范化后的 OpenAPI 文档副本（不原地修改入参，避免污染路由 openapi_extra）。"""
    normalized: dict[str, Any] = copy.deepcopy(schema)

    def _get_component_schemas() -> dict[str, Any]:
        components = normalized.setdefault("components", {})
        if not isinstance(components, dict):
            components = {}
            normalized["components"] = components
        schemas = components.setdefault("schemas", {})
        if not isinstance(schemas, dict):
            schemas = {}
            components["schemas"] = schemas
        return schemas

    # 先规范化已有 components.schemas（按快照键遍历，避免提升新组件时并发修改迭代视图）。
    existing_components = normalized.get("components")
    if isinstance(existing_components, dict) and isinstance(
        existing_components.get("schemas"), dict
    ):
        existing_schemas: dict[str, Any] = existing_components["schemas"]
        for schema_name in list(existing_schemas):
            existing_schemas[schema_name] = _hoist_nested_defs(
                existing_schemas[schema_name],
                _get_component_schemas,
            )

    for top_key in ("paths", "webhooks"):
        section = normalized.get(top_key)
        if isinstance(section, dict):
            normalized[top_key] = _hoist_nested_defs(section, _get_component_schemas)

    return normalized


def install_openapi_normalizer(app: FastAPI) -> None:
    """为 FastAPI 实例挂载 OpenAPI 规范化包装器，统一保障在线 /openapi.json 与导出脚本输出。"""
    default_openapi = app.openapi

    def _normalized_openapi() -> dict[str, Any]:
        # 复用 FastAPI 原生缓存与路由版本失效机制：仅在首次生成或路由变更重建时执行规范化。
        previous_schema = app.openapi_schema
        schema = default_openapi()
        if previous_schema is None or schema is not previous_schema:
            normalized = normalize_openapi_schema(schema)
            app.openapi_schema = normalized
            return normalized
        return schema

    app.openapi = _normalized_openapi  # type: ignore[method-assign]


__all__ = ["install_openapi_normalizer", "normalize_openapi_schema"]
