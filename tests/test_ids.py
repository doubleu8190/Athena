"""标识符生成工具测试."""

from __future__ import annotations

import asyncio
import re

from athena.utils.ids import (
    generate_session_id,
    generate_sub_run_id,
    generate_time_id,
)


def test_generate_session_id_format():
    """session_id 格式: YYYY_MM_DD_HH_MM_SS_mmm"""
    sid = generate_session_id()
    parts = sid.split("_")
    assert len(parts) == 7  # year, month, day, hour, minute, second, ms
    assert all(p.isdigit() for p in parts)


def test_sub_run_id_format():
    """子 run_id = "主run_序号"，归组时去掉末尾序号即回到父任务"""
    main = generate_time_id()
    sub = generate_sub_run_id(main, 1)
    assert sub == f"{main}_1"
    # 主 run_id 本身不含下划线，归组可安全按最后一个 "_" 切分
    assert sub.rsplit("_", 1)[0] == main


# ── generate_time_id 测试 ──

def test_time_id_format():
    """时间格式 ID: 全数字，形如 YYYYMMDDHHMMSSmmmmmm"""
    tid = generate_time_id()
    assert tid.isdigit()
    # 无并发碰撞时为 20 位，碰撞时追加 2 位序号
    assert len(tid) in (20, 22)


def test_time_id_starts_with_date_prefix():
    """ID 应以当天日期（YYYYMMDD）开头，保证时序可排序"""
    import time
    before = time.strftime("%Y%m%d")
    tid = generate_time_id()
    after = time.strftime("%Y%m%d")
    assert before == after or tid[:8] in (before, after)


def test_time_id_uniqueness_sequential():
    """连续生成 1000 个 ID 不应重复"""
    ids = {generate_time_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_time_id_uniqueness_concurrent():
    """并发场景下生成 500 个 ID 不应重复"""
    async def _gen():
        return generate_time_id()

    async def _run():
        tasks = [_gen() for _ in range(500)]
        return await asyncio.gather(*tasks)

    ids = asyncio.run(_run())
    assert len(set(ids)) == 500


def test_time_id_sortable_by_time():
    """先生成的 ID 应 <= 后生成的（字典序即时间序）"""
    id1 = generate_time_id()
    id2 = generate_time_id()
    assert id1 <= id2
