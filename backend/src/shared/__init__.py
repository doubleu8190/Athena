"""无业务含义的共享基础设施。"""
from .ids import new_id, now_utc
from .logging import get_logger
from .serialization import dumps, loads

__all__ = ["dumps", "get_logger", "loads", "new_id", "now_utc"]
