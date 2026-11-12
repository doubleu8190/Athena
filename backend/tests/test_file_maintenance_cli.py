"""离线文件维护命令的安全接线测试。"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest
from cli import file_maintenance

from app.file import ObjectReclamationResult


def _result(*, failed_count: int = 0) -> ObjectReclamationResult:
    """构造不含对象键的稳定维护汇总。"""
    return ObjectReclamationResult(
        examined_count=4,
        candidate_count=3,
        referenced_count=1,
        eligible_count=2,
        deleted_count=2 if failed_count == 0 else 1,
        failed_count=failed_count,
    )


def _set_arguments(monkeypatch: pytest.MonkeyPatch, *arguments: str) -> None:
    """设置最小 CLI 参数和仅供测试使用的数据库地址。"""
    monkeypatch.setenv(
        "ATHENA_DATABASE_URL",
        "postgresql+asyncpg://test:test@127.0.0.1/test",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["file_maintenance", *arguments],
    )


def test_main_defaults_to_dry_run_and_forwards_defaults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未传切换参数时必须只执行 dry-run。"""
    _set_arguments(monkeypatch)
    calls: list[dict[str, Any]] = []

    async def maintain(**arguments: Any) -> ObjectReclamationResult:
        calls.append(arguments)
        return _result()

    monkeypatch.setattr(file_maintenance, "_maintain", maintain)

    assert file_maintenance.main() == 0
    assert calls == [
        {
            "database_url": "postgresql+asyncpg://test:test@127.0.0.1/test",
            "execute": False,
            "writers_stopped": False,
            "retention_seconds": 86400,
            "page_size": 1000,
            "delete_batch_size": 100,
        }
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry-run"
    assert payload["deleted_count"] == 2


def test_main_rejects_execute_without_writer_stop_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """显式 execute 缺少停写确认时必须在创建任何依赖前退出。"""
    _set_arguments(monkeypatch, "--execute")

    async def maintain(**arguments: Any) -> ObjectReclamationResult:
        raise AssertionError(f"maintenance must not run: {arguments}")

    monkeypatch.setattr(file_maintenance, "_maintain", maintain)

    with pytest.raises(SystemExit) as exit_info:
        file_maintenance.main()

    assert exit_info.value.code == 2
    assert "--execute requires --confirm-all-writers-stopped" in capsys.readouterr().err


def test_main_forwards_explicit_execute_and_all_maintenance_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """双确认及维护参数必须原样传入一次维护调用。"""
    _set_arguments(
        monkeypatch,
        "--execute",
        "--confirm-all-writers-stopped",
        "--retention-seconds",
        "172800",
        "--page-size",
        "250",
        "--delete-batch-size",
        "25",
    )
    calls: list[dict[str, Any]] = []

    async def maintain(**arguments: Any) -> ObjectReclamationResult:
        calls.append(arguments)
        return _result()

    monkeypatch.setattr(file_maintenance, "_maintain", maintain)

    assert file_maintenance.main() == 0
    assert calls == [
        {
            "database_url": "postgresql+asyncpg://test:test@127.0.0.1/test",
            "execute": True,
            "writers_stopped": True,
            "retention_seconds": 172800,
            "page_size": 250,
            "delete_batch_size": 25,
        }
    ]


def test_main_returns_nonzero_when_maintenance_reports_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """引用或单删失败计数非零时，命令必须返回失败状态。"""
    _set_arguments(monkeypatch)

    async def maintain(**arguments: Any) -> ObjectReclamationResult:
        del arguments
        return _result(failed_count=1)

    monkeypatch.setattr(file_maintenance, "_maintain", maintain)

    assert file_maintenance.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed_count"] == 1


def test_main_hides_exception_details_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """依赖异常只输出稳定错误码，不回显网络或凭据细节。"""
    _set_arguments(monkeypatch)

    async def maintain(**arguments: Any) -> ObjectReclamationResult:
        del arguments
        raise RuntimeError("network detail must stay private")

    monkeypatch.setattr(file_maintenance, "_maintain", maintain)

    with pytest.raises(SystemExit) as exit_info:
        file_maintenance.main()

    captured = capsys.readouterr()
    assert exit_info.value.code == 1
    assert captured.err == "file_maintenance_failed\n"
    assert "network detail" not in captured.err
