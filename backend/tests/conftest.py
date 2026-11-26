"""后端测试根级 pytest 钩子与非集成单测环境隔离夹具。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pg_unit_harness import cleanup_pg_unit_schema

_TESTS_ROOT = Path(__file__).resolve().parent
# 非集成单元测试允许保留的测试专用环境变量前缀；其余 ATHENA_* 运行时开发配置统一隔离。
_PRESERVED_UNIT_ENV_PREFIXES = ("ATHENA_TEST_", "ATHENA_LLM_LIVE_", "ATHENA_HARNESS_")
# 携带真实外部依赖标记的用例需保留宿主机注入的运行时配置（如 ATHENA_MCP_SECRET_KEY）。
_EXEMPT_MARKERS = ("integration", "llm_live", "real_ocr")


def _apply_unit_test_runtime_env_isolation(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """按用例路径与标记决定是否隔离 ATHENA_* 运行时环境变量；返回是否执行了隔离。"""
    test_path = Path(str(request.path)).resolve()
    try:
        rel_parts = test_path.relative_to(_TESTS_ROOT).parts
    except ValueError:
        rel_parts = ()
    if rel_parts and rel_parts[0] in {"integration", "browser"}:
        return False
    if any(request.node.get_closest_marker(marker) is not None for marker in _EXEMPT_MARKERS):
        return False

    for key in list(os.environ):
        if key.startswith("ATHENA_") and not key.startswith(_PRESERVED_UNIT_ENV_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    return True


@pytest.fixture(autouse=True)
def _isolate_unit_test_runtime_env(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """隔离非集成单元测试与 load-test-env.sh 注入的开发服运行时环境变量。

    本地运行 PostgreSQL 单元测试前需 ``source scripts/load-test-env.sh`` 以注入
    ``ATHENA_TEST_DATABASE_URL``，但该脚本同时导出 ``ATHENA_DATABASE_URL``、
    ``ATHENA_AUTH_MOCK_ENABLED``、``ATHENA_PUBLIC_ORIGIN`` 等开发服配置。
    此处仅清理非集成/非浏览器且未标记 ``integration`` / ``llm_live`` / ``real_ocr``
    的单元测试环境变量，使本地环境与 CI 保持一致，且完全不触碰网络连接或数据库。
    """
    _apply_unit_test_runtime_env_isolation(request, monkeypatch)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """测试会话退出时幂等清理临时 unit_* Schema；纯内存单测未初始化时直接空返回。"""
    _ = session, exitstatus
    cleanup_pg_unit_schema()
