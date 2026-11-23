"""目标 Python 环境的 Skill 依赖探测边界测试。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import app.skill.dependency as dependency_module
from app.skill.dependency import DependencyChecker
from app.skill.service import SkillService
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    Skill,
    SkillPackageError,
)
from domain.skill.model import DependencyCheckItem

CHECKED_AT = datetime(2026, 11, 12, tzinfo=UTC)


class _ManualMonotonicClock:
    """提供可推进且线程安全的单调时钟。"""

    def __init__(self) -> None:
        """从零点创建时钟。"""
        self._lock = threading.Lock()
        self._value = 0.0

    def __call__(self) -> float:
        """返回当前测试时间。"""
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        """推进测试时间。"""
        with self._lock:
            self._value += seconds


class _ProbeRecorder:
    """记录目标环境探测次数，并按顺序返回版本或异常。"""

    def __init__(self, *outcomes: str | Exception) -> None:
        """保存探测结果序列。"""
        self._outcomes = outcomes
        self._lock = threading.Lock()
        self.calls = 0

    def __call__(
        self,
        python_executable: Path,
        *,
        timeout_seconds: float,
    ) -> dependency_module._TargetEnvironment:
        """返回固定测试环境或抛出预设异常。"""
        del python_executable, timeout_seconds
        with self._lock:
            outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
            self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return dependency_module._TargetEnvironment(
            python_version="3.14.0",
            python_implementation="CPython",
            marker_environment={
                "implementation_name": "cpython",
                "implementation_version": "3.14.0",
                "os_name": "posix",
                "platform_machine": "arm64",
                "platform_release": "test",
                "platform_system": "Test",
                "platform_version": "test",
                "python_full_version": "3.14.0",
                "platform_python_implementation": "CPython",
                "python_version": "3.14",
                "sys_platform": "test",
            },
            distributions={"demo-package": (outcome,)},
        )


class _DirectoryRepository:
    """为公共目录缓存接线测试提供实时版本读取和检查写回。"""

    def __init__(self, skills: tuple[Skill, ...], versions: tuple[SimpleNamespace, ...]) -> None:
        """保存目录项和当前版本替身。"""
        self.skills = skills
        self.versions = {cast(str, version.id): version for version in versions}
        self.dependency_writes: list[DependencyCheck] = []

    async def get_public_directory(self) -> tuple[Skill, ...]:
        """返回全部候选，版本可用性由服务逐项读取。"""
        return self.skills

    async def get_version(self, version_id: str) -> SimpleNamespace | None:
        """返回数据库中的实时版本替身。"""
        return self.versions.get(version_id)

    async def record_dependency_check(
        self,
        version_id: str,
        dependency_check: DependencyCheck,
    ) -> SimpleNamespace | None:
        """记录检查写回并更新实时版本。"""
        version = self.versions.get(version_id)
        if version is None:
            return None
        version.dependency_check = dependency_check
        self.dependency_writes.append(dependency_check)
        return version


@dataclass(frozen=True, slots=True)
class _TargetVenv:
    """保存测试用真实虚拟环境及其 Python 入口。"""

    python_executable: Path
    site_packages: Path


def _venv_python(venv_directory: Path) -> Path:
    """返回当前操作系统中的虚拟环境 Python 入口。"""
    if os.name == "nt":
        return venv_directory / "Scripts" / "python.exe"
    return venv_directory / "bin" / "python"


def _create_target_venv(path: Path, *, distribution_version: str) -> _TargetVenv:
    """创建无 pip 的真实虚拟环境并手工写入一个发行包元数据目录。"""
    subprocess.run(
        (sys.executable, "-m", "venv", "--without-pip", str(path)),
        check=True,
        timeout=60,
    )
    python_executable = _venv_python(path)
    completed = subprocess.run(
        (
            str(python_executable),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ),
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    site_packages = Path(completed.stdout.strip())
    dist_info = site_packages / "Target_Only_Package-1.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        "\n".join(
            (
                "Metadata-Version: 2.4",
                "Name: Target-Only_Package",
                f"Version: {distribution_version}",
                "",
            )
        ),
        encoding="utf-8",
    )
    return _TargetVenv(
        python_executable=python_executable,
        site_packages=site_packages,
    )


@pytest.fixture(scope="module")
def target_venvs(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[_TargetVenv, _TargetVenv]:
    """创建仅发行包版本不同的两个真实目标解释器环境。"""
    root = tmp_path_factory.mktemp("skill-dependency-venvs")
    return (
        _create_target_venv(root / "target-a", distribution_version="2.1"),
        _create_target_venv(root / "target-b", distribution_version="3.0"),
    )


def test_check_cached_coalesces_cold_requests_and_reuses_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 checker 在 TTL 内对多个 Skill 和并发 GET 只探测一次。"""
    probe = _ProbeRecorder("2.1")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    clock = _ManualMonotonicClock()
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=clock,
    )

    for _ in range(10):
        check = checker.check_cached(("demo-package>=2",))
        assert check.checked_at == CHECKED_AT
    assert probe.calls == 1

    barrier = threading.Barrier(8)

    def run_cached() -> DependencyCheck:
        barrier.wait()
        return checker.check_cached(("demo-package>=2",))

    with ThreadPoolExecutor(max_workers=8) as executor:
        checks = list(executor.map(lambda _: run_cached(), range(8)))

    assert probe.calls == 1
    assert all(check.checked_at == CHECKED_AT for check in checks)


def test_check_cached_concurrent_expiry_runs_one_fresh_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TTL 过期后的并发 miss 只由第一个请求刷新共享快照。"""
    probe = _ProbeRecorder("2.1", "3.0")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    clock = _ManualMonotonicClock()
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=clock,
    )
    checker.check_cached(("demo-package>=2",))
    clock.advance(61)
    barrier = threading.Barrier(8)

    def run_cached() -> DependencyCheck:
        barrier.wait()
        return checker.check_cached(("demo-package>=2",))

    with ThreadPoolExecutor(max_workers=8) as executor:
        checks = list(executor.map(lambda _: run_cached(), range(8)))

    assert probe.calls == 2
    assert {check.items[0].installed_version for check in checks} == {"3.0"}


def test_cached_environment_is_isolated_per_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不同 checker 各自探测和持有快照，不跨实例混用环境结果。"""
    probe = _ProbeRecorder("2.1", "3.0")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    first = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=_ManualMonotonicClock(),
    )
    second = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=_ManualMonotonicClock(),
    )

    first_check = first.check_cached(("demo-package>=2",))
    second_check = second.check_cached(("demo-package>=2",))

    assert first_check.items[0].installed_version == "2.1"
    assert second_check.items[0].installed_version == "3.0"
    assert probe.calls == 2


def test_fresh_probe_updates_snapshot_and_failure_invalidates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式复核更新共享快照，失败时即使 TTL 未到也不得继续命中旧值。"""
    probe = _ProbeRecorder(
        "2.1",
        "3.0",
        SkillPackageError("skill_dependency_environment_unavailable"),
        "4.0",
    )
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=_ManualMonotonicClock(),
    )

    assert checker.check_cached(("demo-package>=2",)).items[0].installed_version == "2.1"
    refreshed = checker.check(("demo-package>=2",), checked_at=CHECKED_AT)
    assert refreshed.items[0].installed_version == "3.0"
    assert checker.check_cached(("demo-package>=2",)).items[0].installed_version == "3.0"
    assert probe.calls == 2

    with pytest.raises(SkillPackageError, match="skill_dependency_environment_unavailable"):
        checker.check(("demo-package>=2",), checked_at=CHECKED_AT)

    recovered = checker.check_cached(("demo-package>=2",))
    assert recovered.items[0].installed_version == "4.0"
    assert probe.calls == 4


def test_expired_probe_failure_never_returns_stale_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """过期探测失败后下一次 GET 会重新尝试，不返回过期环境。"""
    probe = _ProbeRecorder(
        "2.1",
        SkillPackageError("skill_dependency_environment_unavailable"),
        "3.0",
    )
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    clock = _ManualMonotonicClock()
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=clock,
    )
    checker.check_cached(("demo-package>=2",))
    clock.advance(61)

    with pytest.raises(SkillPackageError, match="skill_dependency_environment_unavailable"):
        checker.check_cached(("demo-package>=2",))

    recovered = checker.check_cached(("demo-package>=2",))
    assert recovered.items[0].installed_version == "3.0"
    assert probe.calls == 3


def test_public_directory_reuses_probe_timestamp_without_repeated_db_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 个 Skill 的两次公共目录读取只探测一次，重复 GET 不重复写检查。"""
    probe = _ProbeRecorder("2.1")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    old_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=True,
                installed_version="1.0",
            ),
        ),
        checked_at=CHECKED_AT - timedelta(days=1),
    )
    versions = tuple(
        SimpleNamespace(
            id=f"skillver_{index}",
            skill_id=f"skill_{index}",
            dependency_requirements=("demo-package>=2",),
            dependency_check=old_check,
        )
        for index in range(10)
    )
    skills = tuple(
        Skill(
            id=version.skill_id,
            slug=f"skill-{index}",
            current_version_id=version.id,
            enabled=True,
            script_requires_approval=False,
            revision=1,
            created_by="usr_admin",
            created_at=CHECKED_AT,
            updated_at=CHECKED_AT,
            name=f"Skill {index}",
            description="cache test",
        )
        for index, version in enumerate(versions)
    )
    repository = _DirectoryRepository(skills, versions)
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=_ManualMonotonicClock(),
    )
    service = SkillService(
        cast(Any, repository),
        cast(Any, object()),
        cast(Any, object()),
        dependency_checker=checker,
        clock=lambda: CHECKED_AT,
    )

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...]]:
        first = await service.public_directory()
        second = await service.public_directory()
        return first, second

    first, second = asyncio.run(scenario())

    assert len(first) == len(second) == 10
    assert probe.calls == 1
    assert len(repository.dependency_writes) == 10
    assert {write.checked_at for write in repository.dependency_writes} == {CHECKED_AT}


def test_public_directory_restores_previously_unsatisfied_skill_after_probe_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库中的旧 unsatisfied 结论不能永久阻止恢复后的 Skill 再次公开。"""
    probe = _ProbeRecorder("1.0", "2.1")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    clock = _ManualMonotonicClock()
    old_check = DependencyCheck(
        status=DependencyCheckStatus.UNSATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=False,
                installed_version="1.0",
                detail_code="dependency_version_mismatch",
            ),
        ),
        checked_at=CHECKED_AT - timedelta(days=1),
    )
    version = SimpleNamespace(
        id="skillver_recovery",
        skill_id="skill_recovery",
        dependency_requirements=("demo-package>=2",),
        dependency_check=old_check,
    )
    skill = Skill(
        id="skill_recovery",
        slug="skill-recovery",
        current_version_id=version.id,
        enabled=True,
        script_requires_approval=False,
        revision=1,
        created_by="usr_admin",
        created_at=CHECKED_AT,
        updated_at=CHECKED_AT,
        name="Recovered Skill",
        description="dependency recovery",
    )
    repository = _DirectoryRepository((skill,), (version,))
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
        monotonic_clock=clock,
    )
    service = SkillService(
        cast(Any, repository),
        cast(Any, object()),
        cast(Any, object()),
        dependency_checker=checker,
        clock=lambda: CHECKED_AT,
    )

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...]]:
        first = await service.public_directory()
        clock.advance(61)
        second = await service.public_directory()
        return first, second

    first, second = asyncio.run(scenario())

    assert first == ()
    assert second == (skill,)
    # 首次探测结果与旧持久化结论实质语义一致（仅 checked_at 推进），不再重复写库；
    # 依赖恢复为 SATISFIED 后立即写回一次。
    assert [write.status for write in repository.dependency_writes] == [
        DependencyCheckStatus.SATISFIED,
    ]


def test_check_uses_target_distributions_markers_and_python_fingerprint(
    target_venvs: tuple[_TargetVenv, _TargetVenv],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证默认环境变量选择目标解释器，且不会混用 API 进程依赖。"""
    target_a, target_b = target_venvs
    monkeypatch.setenv(
        "ATHENA_SKILL_PYTHON_EXECUTABLE",
        str(target_a.python_executable),
    )
    first_checker = DependencyChecker(clock=lambda: CHECKED_AT)
    second_checker = DependencyChecker(
        python_executable=target_b.python_executable,
        clock=lambda: CHECKED_AT,
    )
    requirements = (
        "target-only-package>=2,<3",
        "target-only-package==99; python_version < '1'",
    )

    first_check = first_checker.check(requirements)
    second_check = second_checker.check(requirements)
    identity = json.loads(
        subprocess.run(
            (
                str(target_a.python_executable),
                "-I",
                "-c",
                (
                    "import json, platform; print(json.dumps({"
                    "'version': platform.python_version(), "
                    "'implementation': platform.python_implementation()}))"
                ),
            ),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    )

    assert first_check.status is DependencyCheckStatus.SATISFIED
    assert first_check.python_version == identity["version"]
    assert first_check.python_implementation == identity["implementation"]
    assert first_check.items[0].package == "target-only-package"
    assert first_check.items[0].installed_version == "2.1"
    assert first_check.items[1].marker_applies is False
    assert first_check.items[1].satisfied is True

    assert second_check.status is DependencyCheckStatus.UNSATISFIED
    assert second_check.items[0].installed_version == "3.0"
    assert second_check.items[0].detail_code == "dependency_version_mismatch"


def test_probe_implementation_version_matches_packaging_format(
    target_venvs: tuple[_TargetVenv, _TargetVenv],
) -> None:
    """目标 marker 使用 packaging 等价的 releaselevel 版本格式。"""
    target_a, _ = target_venvs
    implementation = sys.implementation.version
    expected = f"{implementation.major}.{implementation.minor}.{implementation.micro}"
    if implementation.releaselevel != "final":
        expected += implementation.releaselevel[0] + str(implementation.serial)
    checker = DependencyChecker(
        python_executable=target_a.python_executable,
        clock=lambda: CHECKED_AT,
    )

    check = checker.check(
        (f'target-only-package; implementation_version == "{expected}"',)
    )

    assert check.status is DependencyCheckStatus.SATISFIED


def test_check_reports_missing_distribution_in_target_environment(
    target_venvs: tuple[_TargetVenv, _TargetVenv],
) -> None:
    """验证目标环境缺少发行包时返回明确的不满足结果。"""
    target_a, _ = target_venvs
    checker = DependencyChecker(
        python_executable=target_a.python_executable,
        clock=lambda: CHECKED_AT,
    )

    check = checker.check(("not-installed-package==1",))

    assert check.status is DependencyCheckStatus.UNSATISFIED
    assert check.items[0].detail_code == "dependency_missing"
    assert check.items[0].installed_version is None


def test_checker_rejects_invalid_python_path(tmp_path: Path) -> None:
    """验证无效目标解释器不会退回当前进程环境。"""
    with pytest.raises(SkillPackageError, match="skill_dependency_python_invalid"):
        DependencyChecker(python_executable=tmp_path / "missing-python")


def test_check_fails_closed_when_target_probe_exits_nonzero(tmp_path: Path) -> None:
    """验证可执行但无法完成协议探测的目标进程返回环境不可用。"""
    failing_python = tmp_path / "failing-python"
    failing_python.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
    failing_python.chmod(0o755)
    checker = DependencyChecker(
        python_executable=failing_python,
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(
        SkillPackageError,
        match="skill_dependency_environment_unavailable",
    ):
        checker.check(("any-package==1",))


def test_check_fails_closed_when_target_probe_times_out(tmp_path: Path) -> None:
    """目标探测超过短超时时间时稳定失败。"""
    slow_python = tmp_path / "slow-python"
    slow_python.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    slow_python.chmod(0o755)
    checker = DependencyChecker(
        python_executable=slow_python,
        clock=lambda: CHECKED_AT,
        probe_timeout_seconds=0.01,
    )

    with pytest.raises(
        SkillPackageError,
        match="skill_dependency_environment_unavailable",
    ):
        checker.check(("any-package==1",))


@pytest.mark.parametrize(
    ("name", "body"),
    (
        ("invalid-json-python", "printf '{\"python_version\":'"),
        ("invalid-utf8-python", "printf '\\377'"),
    ),
)
def test_check_fails_closed_when_target_probe_returns_invalid_output(
    tmp_path: Path,
    name: str,
    body: str,
) -> None:
    """可信固定脚本返回非法 JSON 或编码时按环境不可用失败。"""
    invalid_python = tmp_path / name
    invalid_python.write_text(
        f"#!/bin/sh\n{body}\n",
        encoding="utf-8",
    )
    invalid_python.chmod(0o755)
    checker = DependencyChecker(
        python_executable=invalid_python,
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(
        SkillPackageError,
        match="skill_dependency_environment_unavailable",
    ):
        checker.check(("any-package==1",))


def test_directory_uses_joined_entries_and_skips_noop_writes_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目录读取复用单次 JOIN 避免 N+1，且 TTL 过期后语义未变时零写库。"""
    clock = _ManualMonotonicClock()
    wall_clock_value = [CHECKED_AT]
    probe = _ProbeRecorder("2.0", "2.0", "1.0")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)

    initial_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=True,
                installed_version="2.0",
                detail_code=None,
            ),
        ),
        checked_at=CHECKED_AT - timedelta(hours=2),
    )
    version = SimpleNamespace(
        id="skillver_joined",
        skill_id="skill_joined",
        dependency_requirements=("demo-package>=2",),
        dependency_check=initial_check,
    )
    skill = Skill(
        id="skill_joined",
        slug="skill-joined",
        current_version_id=version.id,
        enabled=True,
        script_requires_approval=False,
        revision=1,
        created_by="usr_admin",
        created_at=CHECKED_AT,
        updated_at=CHECKED_AT,
        name="Joined Skill",
        description="joined directory check",
    )

    class _JoinedDirectoryRepository(_DirectoryRepository):
        def __init__(self) -> None:
            super().__init__((skill,), (version,))
            self.get_version_calls = 0

        async def get_public_directory_entries(
            self,
        ) -> tuple[tuple[Skill, SimpleNamespace], ...]:
            return ((skill, version),)

        async def get_user_directory_entries(
            self,
            owner_id: str,
        ) -> tuple[tuple[Skill, SimpleNamespace], ...]:
            del owner_id
            return ((skill, version),)

        async def get_version(self, version_id: str) -> SimpleNamespace | None:
            self.get_version_calls += 1
            return await super().get_version(version_id)

    repository = _JoinedDirectoryRepository()
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: wall_clock_value[0],
        monotonic_clock=clock,
    )
    service = SkillService(
        cast(Any, repository),
        cast(Any, object()),
        cast(Any, object()),
        dependency_checker=checker,
        clock=lambda: wall_clock_value[0],
    )
    actor = SimpleNamespace(user=SimpleNamespace(id="usr_member"))

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...], tuple[Skill, ...]]:
        # 首次读取与 TTL 过期后第二次读取，仅 checked_at 推进、依赖语义未变，不应写库。
        first = await service.public_directory()
        clock.advance(61)
        wall_clock_value[0] = CHECKED_AT + timedelta(minutes=5)
        second = await service.user_directory(cast(Any, actor))
        # 第三次探测版本降级为 1.0，语义变为 UNSATISFIED，此时应落库一次。
        clock.advance(61)
        wall_clock_value[0] = CHECKED_AT + timedelta(minutes=10)
        third = await service.public_directory()
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first == (skill,)
    assert second == (skill,)
    assert third == ()
    assert repository.get_version_calls == 0
    assert [write.status for write in repository.dependency_writes] == [
        DependencyCheckStatus.UNSATISFIED,
    ]
