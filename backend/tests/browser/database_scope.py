"""测试入口专用数据库 search_path 注入。"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any, cast


def install_search_path(schema: str) -> None:
    """在业务 Database 构造前包装引擎工厂，业务实现本身保持不变。"""
    if not schema or not schema.replace("_", "").isalnum():
        raise ValueError("e2e_schema_invalid")
    database_module = cast(Any, import_module("infrastructure.database"))

    existing = getattr(database_module, "_athena_e2e_original_create_async_engine", None)
    if existing is not None:
        return
    original: Callable[..., Any] = database_module.create_async_engine

    def scoped_create_async_engine(*args: Any, **kwargs: Any) -> Any:
        connect_args = dict(kwargs.pop("connect_args", {}) or {})
        server_settings = dict(connect_args.get("server_settings", {}) or {})
        server_settings["search_path"] = f"{schema},public"
        connect_args["server_settings"] = server_settings
        return original(*args, connect_args=connect_args, **kwargs)

    database_module._athena_e2e_original_create_async_engine = original
    database_module.create_async_engine = scoped_create_async_engine


__all__ = ["install_search_path"]
