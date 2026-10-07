"""迁移期脚手架的最小启动探针。"""

from __future__ import annotations

from bootstrap.app import create_app
from bootstrap.dependencies import build_default_dependencies


def check_app() -> None:
    """构建应用并确认健康路由已注册。

    参数：
        无。
    返回值：
        None: 探针成功完成。
    异常：
        RuntimeError: 健康路由未注册时抛出。
    """
    graph = build_default_dependencies()
    app = create_app(dependencies=graph)
    routes = {getattr(route, "path", None) for route in app.routes}
    if "/health" not in routes:
        raise RuntimeError("migration app health route is not registered")
    if not graph.factories:
        raise RuntimeError("migration dependency graph is empty")


if __name__ == "__main__":
    check_app()
