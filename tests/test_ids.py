"""标识符生成工具测试."""

from __future__ import annotations

import asyncio
import re

from athena.utils.ids import RunIdGenerator, generate_message_id, generate_session_id


def test_generate_session_id_format():
    """session_id 格式: YYYY_MM_DD_HH_MM_SS_mmm"""
    sid = generate_session_id()
    parts = sid.split("_")
    assert len(parts) == 7  # year, month, day, hour, minute, second, ms
    assert all(p.isdigit() for p in parts)


def test_main_run_id_uses_date_format():
    RunIdGenerator.reset_counter()
    rid = RunIdGenerator.generate_main_run_id()
    assert len(rid) == 8
    assert rid.isdigit()


def test_main_run_id_increments_within_day():
    RunIdGenerator.reset_counter()
    r1 = RunIdGenerator.generate_main_run_id()
    r2 = RunIdGenerator.generate_main_run_id()
    assert r1 != r2
    assert "_" in r2  # 第二次运行应带序号后缀


def test_sub_run_id_format():
    sub = RunIdGenerator.generate_sub_run_id("20260730", 1)
    assert sub == "20260730_1"


# ── generate_message_id 测试 ──

def test_message_id_format():
    """ID 格式: msg_{unix_ms}_{random_hex}"""
    mid = generate_message_id()
    assert mid.startswith("msg_")
    parts = mid.split("_")
    assert len(parts) == 3
    assert parts[1].isdigit()  # unix_ms
    assert len(parts[2]) == 8  # 4 bytes = 8 hex chars


def test_message_id_contains_valid_timestamp():
    """ID 中的时间戳应接近当前 Unix 毫秒时间"""
    import time
    before = int(time.time() * 1000)
    mid = generate_message_id()
    after = int(time.time() * 1000)
    ts = int(mid.split("_")[1])
    assert before <= ts <= after


def test_message_id_uniqueness_sequential():
    """连续生成 1000 个 ID 不应重复"""
    ids = {generate_message_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_message_id_uniqueness_concurrent():
    """并发场景下生成 500 个 ID 不应重复"""
    async def _gen():
        return generate_message_id()

    async def _run():
        tasks = [_gen() for _ in range(500)]
        return await asyncio.gather(*tasks)

    ids = asyncio.run(_run())
    assert len(set(ids)) == 500


def test_message_id_sortable_by_time():
    """先生成的 ID 时间戳应 <= 后生成的"""
    id1 = generate_message_id()
    id2 = generate_message_id()
    ts1 = int(id1.split("_")[1])
    ts2 = int(id2.split("_")[1])
    assert ts1 <= ts2
