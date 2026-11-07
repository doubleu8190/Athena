"""Knowledge infrastructure imports remain safe without optional jieba."""

from __future__ import annotations

import os
import subprocess
import sys


def test_infrastructure_import_does_not_load_optional_jieba() -> None:
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'jieba' or name.startswith('jieba.'):
        raise ImportError('optional dependency unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import infrastructure.knowledge
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
