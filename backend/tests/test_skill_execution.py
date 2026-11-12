"""Python Skill 脚本执行器的真实子进程协议与生命周期测试。"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
from pathlib import Path

import pytest

from adapter.skill.execution import MAX_SCRIPT_LOG_BYTES, PythonScriptExecutor
from domain.skill.execution import (
    SCRIPT_PROCESS_DIAGNOSTIC_FILENAME,
    JsonValue,
    ScriptCleanupErrorCode,
    ScriptExecutionCancelled,
    ScriptExecutionConfigurationError,
    ScriptExecutionErrorCode,
    ScriptExecutionRequest,
    ScriptExecutionResult,
    ScriptProcessDiagnostic,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="Skill script process groups require POSIX signals",
)


def _prepare_workspace(tmp_path: Path, script_source: str) -> tuple[Path, Path]:
    """创建独立技能脚本与输出目录。

    参数:
        tmp_path: pytest 提供的本次测试临时根目录。
        script_source: 写入 ``scripts/run.py`` 的 Python 源码。

    返回:
        工作目录和一个空白输出目录。

    异常:
        文件系统不可写时传播底层 ``OSError``。
    """
    working_directory = tmp_path / "execution"
    scripts_directory = working_directory / "scripts"
    output_directory = working_directory / "outputs"
    scripts_directory.mkdir(parents=True)
    output_directory.mkdir()
    (scripts_directory / "run.py").write_text(script_source, encoding="utf-8")
    return working_directory, output_directory


def _request(
    working_directory: Path,
    output_directory: Path,
    *,
    parameters: dict[str, JsonValue] | None = None,
    input_files: dict[str, Path] | None = None,
) -> ScriptExecutionRequest:
    """构造指向标准 ``scripts/run.py`` 的执行请求。

    参数:
        working_directory: 本次调用的独立工作目录。
        output_directory: 脚本写入清单和产物的目录。
        parameters: 可选的 JSON 参数；未提供时为空对象。
        input_files: 可选的逻辑名称到输入文件路径映射。

    返回:
        路径全部为绝对路径的执行请求。

    异常:
        本辅助函数不主动抛出业务异常。
    """
    return ScriptExecutionRequest(
        working_directory=working_directory,
        script_path="scripts/run.py",
        parameters=parameters or {},
        input_files=input_files or {},
        output_directory=output_directory,
        run_id="run_skill_execution_test",
        tool_call_id="toolcall_skill_execution_test",
        worker_instance_id="worker_skill_execution_test",
    )


async def _wait_for_path(path: Path, timeout_seconds: float = 3.0) -> None:
    """等待真实子进程创建用于观测的 PID 文件。

    参数:
        path: 子进程将写入的观测文件。
        timeout_seconds: 测试等待上限，不传入执行器。

    返回:
        文件出现后无返回值。

    异常:
        超时后抛出 ``TimeoutError``。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while not path.exists():
        if loop.time() >= deadline:
            raise TimeoutError(f"timed out waiting for {path.name}")
        await asyncio.sleep(0.01)


async def _wait_for_process_group_gone(
    process_group_id: int,
    timeout_seconds: float = 3.0,
) -> None:
    """等待 SIGKILL 后的真实进程组从内核消失。

    参数:
        process_group_id: 启动时记录的独立进程组 ID。
        timeout_seconds: 测试等待上限，不传入执行器。

    返回:
        进程组不存在后无返回值。

    异常:
        超时后抛出 ``AssertionError``。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        try:
            os.killpg(process_group_id, signal.SIGCONT)
        except ProcessLookupError:
            return
        if loop.time() >= deadline:
            raise AssertionError(f"process group {process_group_id} still exists")
        await asyncio.sleep(0.01)


def test_script_execution_uses_json_file_ipc_and_no_shell(tmp_path: Path) -> None:
    """输入正文只通过 JSON 文件和路径环境变量传递。"""
    shell_marker = tmp_path / "shell-marker"
    input_directory = tmp_path / "input-copies"
    input_directory.mkdir()
    input_path = input_directory / "sales.csv"
    input_path.write_text("amount\n42\n", encoding="utf-8")
    sentinel = f"$(touch {shell_marker})"
    script = """
import json
import os
from pathlib import Path
import sys

input_path = Path(os.environ["ATHENA_INPUT_PATH"])
payload = json.loads(input_path.read_text(encoding="utf-8"))
input_file = Path(payload["input_files"]["sales"])
observation = {
    "arguments": sys.argv[1:],
    "input_path": str(input_path),
    "input_text": input_file.read_text(encoding="utf-8"),
    "output_dir": payload["output_dir"],
    "parameters": payload["parameters"],
}
print(json.dumps(observation, ensure_ascii=False, sort_keys=True))
report = Path(payload["output_dir"]) / "report.txt"
report.write_text("generated", encoding="utf-8")
(report.parent / "manifest.json").write_text(
    json.dumps({"outputs": [{"path": "report.txt", "label": "报表"}]}),
    encoding="utf-8",
)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    copied_input = working_directory / "inputs" / "sales.csv"
    copied_input.parent.mkdir()
    shutil.copyfile(input_path, copied_input)
    request = _request(
        working_directory,
        output_directory,
        parameters={"month": "2026-10", "marker": sentinel},
        input_files={"sales": copied_input},
    )

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            observation = json.loads(result.stdout)
            assert result.succeeded
            assert result.error_code is None
            assert observation["parameters"]["month"] == "2026-10"
            assert observation["parameters"]["marker"] == sentinel
            assert observation["input_text"] == "amount\n42\n"
            assert observation["input_path"] == str(working_directory / "input.json")
            assert observation["output_dir"] == str(output_directory)
            assert sentinel not in " ".join(observation["arguments"])
            assert sentinel not in os.environ.get("ATHENA_INPUT_PATH", "")
            assert not shell_marker.exists()
            outputs = tuple(result.outputs)
            assert outputs[0].path == "report.txt"
            assert outputs[0].label == "报表"
            assert outputs[0].source_path.read_text(encoding="utf-8") == "generated"
            assert result.cleanup_error_codes == []

    asyncio.run(scenario())
    assert not working_directory.exists()
    assert not shell_marker.exists()


def test_script_execution_allows_empty_output_manifest_and_cleans_after_body(
    tmp_path: Path,
) -> None:
    """空清单合法，工作目录在上下文退出后删除。"""
    script = """
import json
import os
from pathlib import Path

output_dir = Path(json.loads(Path(os.environ["ATHENA_INPUT_PATH"]).read_text())["output_dir"])
(output_dir / "manifest.json").write_text('{"outputs": []}', encoding="utf-8")
print("ok")
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)
    observed_result: ScriptExecutionResult | None = None

    async def scenario() -> None:
        nonlocal observed_result
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert working_directory.exists()
            assert result.succeeded
            assert tuple(result.outputs) == ()
            assert result.stdout == b"ok\n"
            observed_result = result
            raise RuntimeError("simulated upload failure")

    with pytest.raises(RuntimeError, match="simulated upload failure"):
        asyncio.run(scenario())
    assert observed_result is not None
    assert observed_result.cleanup_error_codes == []
    assert observed_result.cleanup_error_code is None
    assert not working_directory.exists()


def test_script_execution_configuration_failure_cleans_prepared_directory(
    tmp_path: Path,
) -> None:
    """登记入口越界时在执行前失败并删除本次独占工作目录。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "raise SystemExit(0)")
    request = ScriptExecutionRequest(
        working_directory=working_directory,
        script_path="../outside.py",
        parameters={},
        input_files={},
        output_directory=output_directory,
        run_id="run_invalid_path",
        tool_call_id="toolcall_invalid_path",
        worker_instance_id="worker_skill_execution_test",
    )

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        with pytest.raises(ScriptExecutionConfigurationError, match="script_path_invalid"):
            async with executor.execute(request):
                raise AssertionError("invalid script path must not start")

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_exposes_preparation_cleanup_failure(tmp_path: Path) -> None:
    """准备失败且目录无法删除时，配置异常携带稳定清理错误码。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "raise SystemExit(0)")
    request = ScriptExecutionRequest(
        working_directory=working_directory,
        script_path="scripts/missing.py",
        parameters={},
        input_files={},
        output_directory=output_directory,
        run_id="run_missing_script",
        tool_call_id="toolcall_missing_script",
        worker_instance_id="worker_skill_execution_test",
    )
    working_directory.chmod(0o500)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        with pytest.raises(ScriptExecutionConfigurationError) as error:
            async with executor.execute(request):
                raise AssertionError("missing script must not start")
        assert error.value.cleanup_error_codes == (
            ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED,
        )
        assert (
            error.value.cleanup_error_code
            == ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED.value
        )

    try:
        asyncio.run(scenario())
    finally:
        working_directory.chmod(0o700)
        shutil.rmtree(working_directory, ignore_errors=True)


def test_script_execution_open_pipe_failure_exposes_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非取消启动异常保留原类型，同时公开随后目录删除失败。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "raise SystemExit(0)")
    locked_directory = working_directory / "locked"
    locked_directory.mkdir()
    (locked_directory / "contents.txt").write_text("x", encoding="utf-8")
    locked_directory.chmod(0o500)
    request = _request(working_directory, output_directory)

    def reject_pipe() -> tuple[int, int]:
        """模拟系统拒绝分配日志管道。"""
        raise OSError("pipe unavailable")

    monkeypatch.setattr(os, "pipe", reject_pipe)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        with pytest.raises(ScriptExecutionConfigurationError) as error:
            async with executor.execute(request):
                raise AssertionError("pipe creation failure must not start")
        assert error.value.code == "script_log_pipe_invalid"
        assert error.value.cleanup_error_codes == (
            ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED,
        )

    try:
        asyncio.run(scenario())
    finally:
        locked_directory.chmod(0o700)
        shutil.rmtree(working_directory, ignore_errors=True)


def test_script_execution_returns_multiple_manifest_outputs_without_contents(
    tmp_path: Path,
) -> None:
    """多文件按清单顺序返回安全声明和可上传源路径。"""
    script = """
import json
import os
from pathlib import Path

output_dir = Path(json.loads(Path(os.environ["ATHENA_INPUT_PATH"]).read_text())["output_dir"])
(output_dir / "report.xlsx").write_bytes(b"xlsx-bytes")
nested = output_dir / "charts"
nested.mkdir()
(nested / "trend.png").write_bytes(b"png-bytes")
(output_dir / "manifest.json").write_text(
    json.dumps(
        {
            "outputs": [
                {"path": "report.xlsx", "label": "月度报表"},
                {"path": "charts/trend.png", "label": "销售趋势"},
            ]
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.succeeded
            assert not isinstance(result.outputs, (tuple, list))
            first_read = tuple(result.outputs)
            second_read = tuple(result.outputs)
            assert [(item.path, item.label) for item in first_read] == [
                ("report.xlsx", "月度报表"),
                ("charts/trend.png", "销售趋势"),
            ]
            assert [(item.path, item.label) for item in second_read] == [
                ("report.xlsx", "月度报表"),
                ("charts/trend.png", "销售趋势"),
            ]
            assert [item.size_bytes for item in first_read] == [10, 9]
            assert all(isinstance(item.source_path, Path) for item in first_read)
            assert not hasattr(first_read[0], "content")

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_nonzero_exit_does_not_publish_manifest_files(tmp_path: Path) -> None:
    """非零退出返回退出码和日志，但不读取为已声明产物。"""
    script = """
import json
import os
from pathlib import Path
import sys

output_dir = Path(json.loads(Path(os.environ["ATHENA_INPUT_PATH"]).read_text())["output_dir"])
(output_dir / "failed.txt").write_text("not delivered", encoding="utf-8")
(output_dir / "manifest.json").write_text(
    '{"outputs": [{"path": "failed.txt", "label": "失败文件"}]}',
    encoding="utf-8",
)
print("stdout failure")
print("stderr failure", file=sys.stderr)
raise SystemExit(7)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert not result.succeeded
            assert result.exit_code == 7
            assert result.error_code is ScriptExecutionErrorCode.EXIT_NONZERO
            assert tuple(result.outputs) == ()
            assert result.stdout == b"stdout failure\n"
            assert result.stderr == b"stderr failure\n"

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_signals_old_process_group_only_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结果产出后的最终上传阶段不再向旧 PGID 发送第二个信号。"""
    script = """
from pathlib import Path
import os

output_dir = Path(os.path.dirname(os.environ["ATHENA_INPUT_PATH"])) / "outputs"
(output_dir / "report.txt").write_text("ok", encoding="utf-8")
(output_dir / "manifest.json").write_text(
    '{"outputs": [{"path": "report.txt", "label": "报告"}]}',
    encoding="utf-8",
)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)
    real_killpg = os.killpg
    kill_calls: list[tuple[int, int]] = []

    def record_killpg(process_group_id: int, signal_number: int) -> None:
        """记录执行器实际发送的进程组信号并执行真实系统调用。"""
        kill_calls.append((process_group_id, signal_number))
        real_killpg(process_group_id, signal_number)

    monkeypatch.setattr(os, "killpg", record_killpg)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.succeeded
            assert [item.path for item in result.outputs] == ["report.txt"]

    asyncio.run(scenario())
    assert len(kill_calls) == 1
    assert kill_calls[0][1] == signal.SIGKILL
    assert not working_directory.exists()


def test_script_execution_cancellation_waits_existing_group_convergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待组退出期间取消会复用原收敛任务，不再发送第二个 SIGKILL。"""
    script = """
from pathlib import Path
import os

output_dir = Path(os.path.dirname(os.environ["ATHENA_INPUT_PATH"])) / "outputs"
(output_dir / "manifest.json").write_text('{"outputs": []}', encoding="utf-8")
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)
    kill_calls: list[tuple[int, int]] = []
    convergence_started = asyncio.Event()

    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        convergence_deadline = loop.time() + 0.2

        def accept_killpg(process_group_id: int, signal_number: int) -> None:
            """模拟信号已成功发送，使测试停在等待组收敛阶段。"""
            kill_calls.append((process_group_id, signal_number))
            if signal_number == signal.SIGKILL:
                convergence_started.set()

        def still_live(process_group_id: int) -> bool:
            """在短暂窗口内报告进程组尚未收敛。"""
            del process_group_id
            return loop.time() < convergence_deadline

        monkeypatch.setattr(os, "killpg", accept_killpg)
        monkeypatch.setattr(
            PythonScriptExecutor,
            "_process_group_has_live_members",
            staticmethod(still_live),
        )

        async def run_execution() -> None:
            executor = PythonScriptExecutor()
            async with executor.execute(request):
                raise AssertionError("cancelled convergence must not yield a result")

        task = asyncio.create_task(run_execution())
        await convergence_started.wait()
        task.cancel()
        with pytest.raises(ScriptExecutionCancelled):
            await asyncio.wait_for(task, timeout=3)
        assert task.cancelled()

    asyncio.run(scenario())
    assert [signal_number for _, signal_number in kill_calls].count(signal.SIGKILL) == 1
    assert not working_directory.exists()


def test_script_execution_process_group_failure_disables_success_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程组收敛失败生成明确错误、空产物且不在上传后重试旧 PGID。"""
    script = """
from pathlib import Path
import os

output_dir = Path(os.path.dirname(os.environ["ATHENA_INPUT_PATH"])) / "outputs"
(output_dir / "report.txt").write_text("must not publish", encoding="utf-8")
(output_dir / "manifest.json").write_text(
    '{"outputs": [{"path": "report.txt", "label": "报告"}]}',
    encoding="utf-8",
)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)
    kill_calls: list[tuple[int, int]] = []

    def reject_killpg(process_group_id: int, signal_number: int) -> None:
        """模拟仅系统拒绝进程组信号，脚本本身仍真实执行。"""
        kill_calls.append((process_group_id, signal_number))
        raise PermissionError("script test denies process-group signal")

    monkeypatch.setattr(os, "killpg", reject_killpg)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert not result.succeeded
            assert result.error_code is ScriptExecutionErrorCode.PROCESS_CLEANUP_FAILED
            assert tuple(result.outputs) == ()
            assert ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED in (
                result.cleanup_error_codes
            )
            diagnostic_path = (
                working_directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME
            )
            diagnostic = ScriptProcessDiagnostic.model_validate_json(
                diagnostic_path.read_bytes()
            )
            assert diagnostic.run_id == "run_skill_execution_test"
            assert diagnostic.tool_call_id == "toolcall_skill_execution_test"
            assert diagnostic.worker_instance_id == "worker_skill_execution_test"
            assert diagnostic.process_id == diagnostic.process_group_id
            assert diagnostic.working_directory == str(working_directory)
            assert diagnostic_path.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())
    assert len(kill_calls) == 1
    assert kill_calls[0][1] == signal.SIGKILL
    assert working_directory.is_dir()
    assert (working_directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME).is_file()


def test_script_execution_cancellation_recovers_live_child_after_group_signal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """组信号失败时直接杀掉仍存活子进程，并在清理时限内回收。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "")
    process_id_path = working_directory / "process.id"
    script = f"""
import os
from pathlib import Path
import time

Path({str(process_id_path)!r}).write_text(str(os.getpid()), encoding="utf-8")
time.sleep(60)
"""
    (working_directory / "scripts" / "run.py").write_text(script, encoding="utf-8")
    request = _request(working_directory, output_directory)

    def reject_killpg(process_group_id: int, signal_number: int) -> None:
        """拒绝所有进程组信号，保留直接子进程存活。"""
        del process_group_id, signal_number
        raise PermissionError("script test denies process-group signal")

    monkeypatch.setattr(os, "killpg", reject_killpg)

    async def run_execution() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request):
            raise AssertionError("cancelled execution must not yield a result")

    async def scenario() -> None:
        task = asyncio.create_task(run_execution())
        await _wait_for_path(process_id_path)
        process_id = int(process_id_path.read_text(encoding="utf-8"))
        task.cancel()
        with pytest.raises(ScriptExecutionCancelled) as error:
            await asyncio.wait_for(task, timeout=3)
        assert task.cancelled()
        assert (
            ScriptCleanupErrorCode.PROCESS_GROUP_CLEANUP_FAILED in error.value.cleanup_error_codes
        )
        assert error.value.process_group_id == process_id
        assert error.value.process_reaped is True
        diagnostic_path = (
            working_directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME
        )
        diagnostic = ScriptProcessDiagnostic.model_validate_json(
            diagnostic_path.read_bytes()
        )
        assert diagnostic.process_id == process_id
        assert diagnostic.process_group_id == process_id
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)

    asyncio.run(scenario())
    assert working_directory.is_dir()
    assert (working_directory / SCRIPT_PROCESS_DIAGNOSTIC_FILENAME).is_file()


@pytest.mark.parametrize(
    ("stat_line", "expected"),
    [
        ("123 (python worker) Z 1 456 456 0 -1 0", ("Z", 456)),
        ("123 (python (worker)) S 1 456 456 0 -1 0", ("S", 456)),
        ("invalid", None),
    ],
)
def test_linux_process_stat_parser_handles_zombie_and_parentheses(
    stat_line: str,
    expected: tuple[str, int] | None,
) -> None:
    """Linux 僵尸状态识别使用 pgrp 字段，命令名中的空格和括号不干扰解析。"""
    assert PythonScriptExecutor._parse_linux_process_stat(stat_line) == expected


@pytest.mark.parametrize(
    ("case_name", "script_tail", "expected_error"),
    [
        (
            "missing-manifest",
            "",
            ScriptExecutionErrorCode.MANIFEST_MISSING,
        ),
        (
            "invalid-json",
            '(output_dir / "manifest.json").write_text("{", encoding="utf-8")',
            ScriptExecutionErrorCode.MANIFEST_INVALID,
        ),
        (
            "path-traversal",
            '(output_dir / "manifest.json").write_text('
            '\'{"outputs": [{"path": "../x", "label": "x"}]}\', encoding="utf-8")',
            ScriptExecutionErrorCode.OUTPUT_PATH_INVALID,
        ),
        (
            "duplicate-path",
            '(output_dir / "report.txt").write_text("x"); '
            '(output_dir / "manifest.json").write_text(\'{"outputs": ['
            '{"path": "report.txt", "label": "a"}, '
            '{"path": "report.txt", "label": "b"}]}\', encoding="utf-8")',
            ScriptExecutionErrorCode.OUTPUT_DUPLICATE,
        ),
        (
            "missing-file",
            '(output_dir / "manifest.json").write_text(\'{"outputs": ['
            '{"path": "missing.txt", "label": "missing"}]}\', encoding="utf-8")',
            ScriptExecutionErrorCode.OUTPUT_MISSING,
        ),
        (
            "symlink-file",
            '(output_dir / "real.txt").write_text("x"); '
            '(output_dir / "link.txt").symlink_to(output_dir / "real.txt"); '
            '(output_dir / "manifest.json").write_text(\'{"outputs": ['
            '{"path": "link.txt", "label": "link"}]}\', encoding="utf-8")',
            ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED,
        ),
        (
            "hardlink-file",
            '(output_dir / "real.txt").write_text("x"); '
            'os.link(output_dir / "real.txt", output_dir / "hard.txt"); '
            '(output_dir / "manifest.json").write_text(\'{"outputs": ['
            '{"path": "hard.txt", "label": "hard"}]}\', encoding="utf-8")',
            ScriptExecutionErrorCode.OUTPUT_LINK_NOT_ALLOWED,
        ),
    ],
)
def test_script_execution_rejects_invalid_or_unsafe_manifests(
    tmp_path: Path,
    case_name: str,
    script_tail: str,
    expected_error: ScriptExecutionErrorCode,
) -> None:
    """缺失、损坏、越界、重复、缺失文件和链接清单整组失败。"""
    del case_name
    script = f"""
import os
from pathlib import Path

output_dir = Path(os.path.dirname(os.environ["ATHENA_INPUT_PATH"])) / "outputs"
{script_tail}
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.exit_code == 0
            assert result.error_code is expected_error
            assert tuple(result.outputs) == ()

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_rejects_massive_continuous_json_escapes(
    tmp_path: Path,
) -> None:
    """连续转义字符在追加后立即受编码长度约束，不产生无界字符串。"""
    escaped_path = "\\\\" * 4_000
    script = f"""
from pathlib import Path
import os

output_dir = Path(os.path.dirname(os.environ["ATHENA_INPUT_PATH"])) / "outputs"
raw_escaped_path = {escaped_path!r}
(output_dir / "manifest.json").write_text(
    '{{"outputs":[{{"path":"' + raw_escaped_path + '","label":"x"}}]}}',
    encoding="utf-8",
)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.exit_code == 0
            assert result.error_code is ScriptExecutionErrorCode.MANIFEST_INVALID
            assert tuple(result.outputs) == ()

    asyncio.run(asyncio.wait_for(scenario(), timeout=3))
    assert not working_directory.exists()


def test_script_execution_drains_both_logs_with_shared_five_mib_limit(
    tmp_path: Path,
) -> None:
    """两路同时超量时持续排空，仅保留合计五 MiB 且不阻塞退出。"""
    script = """
import json
import os
from pathlib import Path
import sys

chunk = b"x" * 65536
for _ in range(48):
    sys.stdout.buffer.write(b"O" * 65536)
    sys.stderr.buffer.write(b"E" * 65536)
sys.stdout.buffer.flush()
sys.stderr.buffer.flush()
output_dir = Path(json.loads(Path(os.environ["ATHENA_INPUT_PATH"]).read_text())["output_dir"])
(output_dir / "manifest.json").write_text('{"outputs": []}', encoding="utf-8")
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.succeeded
            assert result.log_truncated
            assert len(result.stdout) + len(result.stderr) == MAX_SCRIPT_LOG_BYTES
            assert result.stdout
            assert result.stderr

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert not working_directory.exists()


def test_script_execution_kills_same_group_after_parent_exits_first(tmp_path: Path) -> None:
    """主入口先退出后仍按启动时 PGID 清理存活后代并排空继承管道。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "")
    child_pid_path = working_directory / "child.pid"
    main_pid_path = working_directory / "main.pid"
    child_code = (
        f"import os,time; open({str(child_pid_path)!r},'w').write(str(os.getpid())); time.sleep(60)"
    )
    script = f"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

open({str(main_pid_path)!r}, "w").write(str(os.getpid()))
subprocess.Popen([sys.executable, "-c", {child_code!r}])
deadline = time.monotonic() + 3
while not Path({str(child_pid_path)!r}).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not start")
    time.sleep(0.01)
output_dir = Path({str(output_directory)!r})
(output_dir / "manifest.json").write_text('{{"outputs": []}}', encoding="utf-8")
"""
    (working_directory / "scripts" / "run.py").write_text(script, encoding="utf-8")
    request = _request(working_directory, output_directory)

    async def scenario() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            assert result.succeeded
            assert result.process_group_id == int(main_pid_path.read_text(encoding="utf-8"))
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            await _wait_for_process_group_gone(child_pid)

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_cancellation_kills_process_group_and_cleans_directory(
    tmp_path: Path,
) -> None:
    """外部取消真实触发 SIGKILL、直接子进程回收和工作目录删除。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "")
    child_pid_path = working_directory / "child.pid"
    main_pid_path = working_directory / "main.pid"
    group_id_path = working_directory / "group.id"
    child_code = (
        f"import os,time; open({str(child_pid_path)!r},'w').write(str(os.getpid())); time.sleep(60)"
    )
    script = f"""
import os
from pathlib import Path
import subprocess
import sys
import time

open({str(main_pid_path)!r}, "w").write(str(os.getpid()))
open({str(group_id_path)!r}, "w").write(str(os.getpgid(0)))
subprocess.Popen([sys.executable, "-c", {child_code!r}])
deadline = time.monotonic() + 3
while not Path({str(child_pid_path)!r}).exists():
    if time.monotonic() >= deadline:
        raise RuntimeError("child did not start")
    time.sleep(0.01)
time.sleep(60)
"""
    (working_directory / "scripts" / "run.py").write_text(script, encoding="utf-8")
    request = _request(working_directory, output_directory)

    async def run_execution() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request):
            raise AssertionError("cancelled execution must not yield a result")

    async def scenario() -> None:
        task = asyncio.create_task(run_execution())
        await _wait_for_path(child_pid_path)
        main_pid = int(main_pid_path.read_text(encoding="utf-8"))
        child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        process_group_id = int(group_id_path.read_text(encoding="utf-8"))
        assert process_group_id == main_pid
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()
        await _wait_for_process_group_gone(process_group_id)
        assert child_pid != main_pid

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_cancellation_reports_cleanup_failure(
    tmp_path: Path,
) -> None:
    """取消清理失败仍保持 CancelledError 语义并携带可观察清理码。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "")
    ready_path = tmp_path / "ready"
    script = f"""
import os
from pathlib import Path
import time

working_directory = Path(os.environ["ATHENA_INPUT_PATH"]).parent
Path({str(output_directory / "manifest.json")!r}).write_text(
    '{{"outputs": []}}',
    encoding="utf-8",
)
working_directory.chmod(0o500)
Path({str(ready_path)!r}).write_text("ready", encoding="utf-8")
time.sleep(60)
"""
    (working_directory / "scripts" / "run.py").write_text(script, encoding="utf-8")
    request = _request(working_directory, output_directory)

    async def run_execution() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request):
            raise AssertionError("cancelled execution must not yield a result")

    async def scenario() -> None:
        task = asyncio.create_task(run_execution())
        await _wait_for_path(ready_path)
        task.cancel()
        with pytest.raises(ScriptExecutionCancelled) as error:
            await task
        assert task.cancelled()
        assert error.value.cleanup_error_codes == (
            ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED,
        )

    try:
        asyncio.run(scenario())
    finally:
        working_directory.chmod(0o700)
        shutil.rmtree(working_directory, ignore_errors=True)


def test_script_execution_cancels_large_manifest_staging_without_blocking_loop(
    tmp_path: Path,
) -> None:
    """超大空白 manifest 在后台增量扫描，事件循环可运行且取消及时收敛。"""
    working_directory, output_directory = _prepare_workspace(tmp_path, "")
    group_id_path = working_directory / "group.id"
    script = f"""
import os
from pathlib import Path

output_dir = Path(os.environ["ATHENA_INPUT_PATH"]).parent / "outputs"
Path({str(group_id_path)!r}).write_text(str(os.getpgid(0)), encoding="utf-8")
with (output_dir / "manifest.json").open("wb") as stream:
    stream.write(b'{{"outputs":[ ')
    stream.write(b" " * (32 * 1024 * 1024))
    stream.write(b"]}}")
"""
    (working_directory / "scripts" / "run.py").write_text(script, encoding="utf-8")
    request = _request(working_directory, output_directory)

    async def run_execution() -> None:
        executor = PythonScriptExecutor()
        async with executor.execute(request):
            raise AssertionError("cancelled staging must not yield a result")

    async def scenario() -> None:
        task = asyncio.create_task(run_execution())
        await _wait_for_path(working_directory / ".athena-manifest.sqlite3")
        await _wait_for_process_group_gone(int(group_id_path.read_text(encoding="utf-8")))
        ticks = 0

        async def count_ticks() -> None:
            """记录清单暂存期间事件循环仍能推进。"""
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        ticker = asyncio.create_task(count_ticks())
        await asyncio.sleep(0.03)
        assert ticks > 0
        task.cancel()
        ticker.cancel()
        with pytest.raises(ScriptExecutionCancelled):
            await asyncio.wait_for(task, timeout=3)
        assert task.cancelled()
        await asyncio.gather(ticker, return_exceptions=True)

    asyncio.run(scenario())
    assert not working_directory.exists()


def test_script_execution_reports_real_working_directory_cleanup_failure(
    tmp_path: Path,
) -> None:
    """脚本留下的无写权限子目录产生可观察清理错误且不掩盖执行结果。"""
    script = """
import json
import os
from pathlib import Path

working_directory = Path(os.environ["ATHENA_INPUT_PATH"]).parent
locked_directory = working_directory / "locked"
locked_directory.mkdir()
(locked_directory / "contents.txt").write_text("x", encoding="utf-8")
(working_directory / "outputs" / "manifest.json").write_text(
    '{"outputs": []}',
    encoding="utf-8",
)
locked_directory.chmod(0o500)
"""
    working_directory, output_directory = _prepare_workspace(tmp_path, script)
    request = _request(working_directory, output_directory)
    observed_result: ScriptExecutionResult | None = None

    async def scenario() -> None:
        nonlocal observed_result
        executor = PythonScriptExecutor()
        async with executor.execute(request) as result:
            observed_result = result
            assert result.succeeded

    try:
        asyncio.run(scenario())
        assert observed_result is not None
        assert ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED in (
            observed_result.cleanup_error_codes
        )
        assert (
            observed_result.cleanup_error_code
            == ScriptCleanupErrorCode.WORKING_DIRECTORY_CLEANUP_FAILED.value
        )
    finally:
        locked_directory = working_directory / "locked"
        if locked_directory.exists():
            locked_directory.chmod(0o700)
        shutil.rmtree(working_directory, ignore_errors=True)
