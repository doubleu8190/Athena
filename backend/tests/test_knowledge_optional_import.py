"""Knowledge infrastructure imports remain safe without optional SDKs."""

from __future__ import annotations

import os
import subprocess
import sys


def test_knowledge_imports_do_not_load_optional_sdks() -> None:
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'jieba' or name.startswith('jieba.'):
        raise ImportError('optional dependency unavailable')
    if name == 'minio' or name.startswith('minio.'):
        raise ImportError('optional dependency unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import infrastructure.knowledge
import adapter.knowledge.storage
import bootstrap.tool
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _run_import_probe(script: str) -> None:
    """在独立解释器中执行导入探针，避免受 pytest 已加载模块影响。"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_knowledge_worker_import_does_not_load_adapter_infrastructure_or_sqlalchemy() -> None:
    """应用层 worker 只依赖领域端口，导入时不得加载 adapter/infrastructure/sqlalchemy。"""
    script = """
import sys
import app.knowledge.worker
assert 'infrastructure.knowledge.parsers' not in sys.modules
loaded = sorted(
    name
    for name in sys.modules
    if name.split('.', 1)[0] in {'adapter', 'infrastructure', 'sqlalchemy'}
)
assert loaded == [], loaded
"""
    _run_import_probe(script)


def test_knowledge_api_import_does_not_load_adapter_infrastructure_or_sqlalchemy() -> None:
    """API 层通过端口注入依赖，导入时不得加载 adapter/infrastructure/sqlalchemy。"""
    script = """
import sys
import api.knowledge
loaded = sorted(
    name
    for name in sys.modules
    if name.split('.', 1)[0] in {'adapter', 'infrastructure', 'sqlalchemy'}
)
assert loaded == [], loaded
"""
    _run_import_probe(script)
