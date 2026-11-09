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
import app.tool.dependencies
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
