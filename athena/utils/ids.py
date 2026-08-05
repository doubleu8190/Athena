"""标识符生成工具 — 统一的时间格式 ID 生成规范.

- generate_time_id: 通用时间格式 ID（微秒级时间戳），全局公用 ID 生成器。
- message_id: 毫秒级 Unix 时间戳 + 随机后缀，保证时序可排序且并发安全。
- session_id: 时间格式字符串（YYYY_MM_DD_HH_MM_SS_mmm），会话级别唯一。
- run_id: 日期格式字符串（YYYYMMDD），子 Agent 在主 run_id 基础上添加下划线+序号。
"""

from __future__ import annotations

import os
import threading
from datetime import datetime

# generate_time_id 并发保护状态（同一微秒内追加序号保证唯一）
_tid_lock = threading.Lock()
_tid_last: str = ""  # 上一次生成的时间基准（微秒级）
_tid_seq: int = 0  # 同一微秒内的自增序号


def generate_time_id() -> str:
    """生成通用时间格式 ID（微秒级精度，并发安全）.

    作为全局公用 ID 生成器，替换散落在各模块的 uuid.uuid4() 用法。

    格式: YYYYMMDDHHMMSSmmmmmm（20 位数字，如 20260804103000123456）
    - 微秒级时间戳，ID 按生成时间可排序
    - 同一微秒内并发生成时，追加 2 位自增序号保证唯一（如 …12345601）

    Returns:
        形如 "20260804103000123456" 的时间格式字符串
    """
    global _tid_last, _tid_seq
    now = datetime.now()
    base = now.strftime("%Y%m%d%H%M%S%f")
    with _tid_lock:
        if base == _tid_last:
            _tid_seq += 1
            return f"{base}{_tid_seq:02d}"
        _tid_last = base
        _tid_seq = 0
        return base


def generate_message_id() -> str:
    """生成消息 ID（毫秒级时间戳 + 随机后缀）.

    格式: msg_{unix_ms}_{random_hex}
    - unix_ms: 毫秒级 Unix 时间戳，保证 ID 按时间可排序
    - random_hex: 4 字节随机十六进制，防止同毫秒并发场景下的 ID 冲突

    Returns:
        形如 "msg_1722425678123_a3f2b1c9" 的字符串
    """
    unix_ms = int(datetime.now().timestamp() * 1000)
    random_hex = os.urandom(4).hex()
    return f"msg_{unix_ms}_{random_hex}"


def generate_session_id() -> str:
    """生成 session_id（UUID v4）."""
    now = datetime.now()
    return f"{now.strftime('%Y_%m_%d_%H_%M_%S')}_{now.microsecond // 1000:03d}"


class RunIdGenerator:
    """运行 ID 生成器.

    主 run_id 采用时间格式字符串（如 20260730），同一天多次运行通过序号补充唯一性。
    子 Agent run_id 在主 run_id 基础上添加下划线和序号（如 20260730_1）。
    """

    _counter: dict[str, int] = {}  # 按日期计数

    @classmethod
    def generate_main_run_id(cls) -> str:
        """生成主 Agent run_id."""
        today = datetime.now().strftime("%Y%m%d")
        if today not in cls._counter:
            cls._counter[today] = 0
        cls._counter[today] += 1

        if cls._counter[today] == 1:
            return today
        return f"{today}_{cls._counter[today]}"

    @classmethod
    def generate_sub_run_id(cls, main_run_id: str, index: int) -> str:
        """生成子 Agent run_id."""
        return f"{main_run_id}_{index}"

    @classmethod
    def reset_counter(cls) -> None:
        """重置计数器（测试用）."""
        cls._counter.clear()
