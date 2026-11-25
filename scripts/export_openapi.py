"""零外部依赖的 Athena 完整 OpenAPI 3.x Schema 导出脚本。

不连接 PostgreSQL、Redis 或 MinIO，也不依赖业务环境变量，直接挂载全部路由分组，
为前端 openapi-typescript / openapi-fetch 提供单一、确定性的接口契约真相源。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC_DIR = REPO_ROOT / "backend" / "src"


def _ensure_backend_src_on_path() -> None:
    """兼容从仓库根目录或 frontend/ 等子目录直接调用，将 backend/src 加入模块搜索路径。"""
    src_str = str(BACKEND_SRC_DIR)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


def build_openapi_app() -> FastAPI:
    """构造挂载全部路由分组的纯 Schema 导出用 FastAPI 实例，不触发任何外部 I/O。"""
    _ensure_backend_src_on_path()

    from api.auth import AuthCookieConfig, build_auth_router
    from api.file import build_session_file_router
    from api.health import router as health_router
    from api.knowledge import build_knowledge_router
    from api.llm import build_llm_admin_router, build_llm_user_router
    from api.mcp import build_mcp_router
    from api.messages import build_message_router
    from api.openapi import install_openapi_normalizer
    from api.org import build_org_admin_router
    from api.preference import build_preference_router
    from api.project_knowledge import build_project_knowledge_router
    from api.projects import build_project_router
    from api.runs import build_run_router
    from api.security import DEFAULT_COOKIE_NAME
    from api.sessions import build_session_router
    from api.skill import (
        build_skill_admin_router,
        build_skill_personal_router,
        build_skill_user_router,
    )
    from api.subagent import build_subagent_admin_router
    from api.system_prompt import build_system_prompt_admin_router
    from api.tools import build_tools_router

    # 使用轻量哨兵对象替代真实业务服务；auth 哨兵显式开启 mock_enabled=True，
    # 保证非生产 Mock 登录端点以及 run SSE 流、project_knowledge 绑定端点均完整出现在 Schema 中。
    sentinel: Any = object()
    auth_sentinel: Any = SimpleNamespace(mock_enabled=True)

    app = FastAPI(title="Athena", version="0.1.0")
    # 与 backend/src/main.py::create_app 共用同一 OpenAPI 规范化钩子，确保局部 $defs 提升一致。
    install_openapi_normalizer(app)
    # 路由挂载清单严格与 backend/src/main.py::create_app 保持全量一致。
    app.include_router(health_router)
    app.include_router(build_org_admin_router(sentinel))
    app.include_router(build_llm_admin_router(sentinel))
    app.include_router(build_llm_user_router(sentinel))
    app.include_router(build_system_prompt_admin_router(sentinel))
    app.include_router(build_preference_router(sentinel))
    app.include_router(build_subagent_admin_router(sentinel))
    app.include_router(build_skill_admin_router(sentinel))
    app.include_router(build_skill_user_router(sentinel))
    app.include_router(build_skill_personal_router(sentinel))
    app.include_router(build_session_file_router(sentinel))
    app.include_router(build_project_router(sentinel))
    app.include_router(build_session_router(sentinel))
    app.include_router(build_run_router(sentinel, sentinel))
    app.include_router(build_message_router(sentinel))
    app.include_router(
        build_auth_router(
            auth_sentinel,
            cookie=AuthCookieConfig(name=DEFAULT_COOKIE_NAME, secure=False),
        )
    )
    app.include_router(build_mcp_router(sentinel))
    app.include_router(build_tools_router(sentinel))
    app.include_router(build_knowledge_router(sentinel, sentinel))
    app.include_router(build_project_knowledge_router(sentinel, sentinel))
    return app


def export_openapi_schema() -> dict[str, Any]:
    """生成完整的 OpenAPI 3.x 字典对象。"""
    return build_openapi_app().openapi()


def render_openapi_json() -> str:
    """序列化为确定性、UTF-8 友好的格式化 OpenAPI JSON 字符串。"""
    schema = export_openapi_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="导出 Athena 全部 FastAPI 路由的完整 OpenAPI JSON Schema（零外部依赖）。"
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="将 OpenAPI JSON 写入指定文件路径（不指定时输出到 stdout）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：按参数输出到 stdout 或写入目标文件。"""
    args = _build_parser().parse_args(argv)
    rendered = render_openapi_json()
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        output_path: Path = args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
