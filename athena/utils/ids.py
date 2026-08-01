"""标识符生成工具 — session_id 与 run_id 的生成规范.

session_id: UUID v4，会话级别全局唯一。
run_id: 日期格式字符串（YYYYMMDD），子 Agent 在主 run_id 基础上添加下划线+序号。
message_id: 毫秒级 Unix 时间戳 + 随机后缀，保证时序可排序且并发安全。
"""

from __future__ import annotations

import os
from datetime import datetime


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
