"""标识符生成工具 — 统一的时间格式 ID 生成规范.

- generate_time_id: 通用时间格式 ID（微秒级时间戳），全局公用 ID 生成器。
  用于消息 ID、工具调用 ID、run_id 等需要单调递增的场景（进程重启也不重号）。
- session_id: 时间格式字符串（YYYY_MM_DD_HH_MM_SS_mmm），会话级别唯一。
- 子 Agent run_id 在主 run_id 基础上添加下划线+序号（generate_sub_run_id），
  归组时去掉末尾 "_序号" 即回到父任务。
"""

from __future__ import annotations

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


def generate_session_id() -> str:
    """生成 session_id（UUID v4）."""
    now = datetime.now()
    return f"{now.strftime('%Y_%m_%d_%H_%M_%S')}_{now.microsecond // 1000:03d}"


def generate_sub_run_id(main_run_id: str, index: int) -> str:
    """生成子 Agent run_id.

    主 run_id 为 generate_time_id()（20 位微秒时间戳，无下划线），
    子 run_id = "主run_序号"（如 20260808153000123456_1），
    前端按 run 归组时去掉末尾 "_序号" 即回到父任务。
    """
    return f"{main_run_id}_{index}"
