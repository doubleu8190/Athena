"""让迁移区测试优先导入独立的 backend 包。"""

from __future__ import annotations

import sys
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 无参数 create_app() 在测试中使用显式的内存替身；生产入口仍走
# backend/src/bootstrap/dependencies.py 中的 PostgreSQL 图。
from bootstrap import app as bootstrap_app
from backend.tests.fakes.dependencies import build_test_dependencies

bootstrap_app.build_default_dependencies = build_test_dependencies
