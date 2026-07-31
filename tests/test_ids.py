"""标识符生成工具测试."""

from __future__ import annotations

from athena.utils.ids import RunIdGenerator, generate_session_id


def test_generate_session_id_is_uuid():
    sid = generate_session_id()
    assert len(sid) == 36
    assert sid.count("-") == 4


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
