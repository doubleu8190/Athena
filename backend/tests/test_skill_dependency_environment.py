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
from access_support import make_test_access_policy

import app.skill.dependency as dependency_module
from app.skill.dependency import DependencyChecker
from app.skill.service import SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    Skill,
    SkillPackageError,
)
from domain.skill.model import DependencyCheckItem

CHECKED_AT = datetime(2026, 11, 12, tzinfo=UTC)


def _member_actor() -> AuthContext:
    """构造测试用普通成员认证上下文，用于调用统一三层用户目录接口。"""
    return AuthContext(
        user=User(
            id="usr_member",
            email="member@example.test",
            password_hash="hash",
            display_name="Member",
            status=UserStatus.ACTIVE,
            created_at=CHECKED_AT,
            updated_at=CHECKED_AT,
            role=UserRole.MEMBER,
        ),
        session_id="auth_member",
    )


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

    async def get_readable_user_directory_entries(
        self,
        owner_id: str,
        *,
        snapshot: object = None,
    ) -> tuple[tuple[Skill, SimpleNamespace], ...]:
        """返回候选与对应当前版本二元组，匹配 SkillRepository 正式契约。"""
        del owner_id, snapshot
        return tuple(
            (skill, self.versions[skill.current_version_id])
            for skill in self.skills
            if skill.current_version_id is not None and skill.current_version_id in self.versions
        )

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
    """10 个 Skill 的两次公共目录读取只探测一次，且 GET 路径全程零写库。"""
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
        access_policy=make_test_access_policy(),
    )
    actor = _member_actor()

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...]]:
        first = await service.user_directory(actor)
        second = await service.user_directory(actor)
        return first, second

    first, second = asyncio.run(scenario())

    assert len(first) == len(second) == 10
    assert probe.calls == 1
    assert repository.dependency_writes == []


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
        access_policy=make_test_access_policy(),
    )
    actor = _member_actor()

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...]]:
        first = await service.user_directory(actor)
        clock.advance(61)
        second = await service.user_directory(actor)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == ()
    assert second == (skill,)
    # GET 目录仅做只读实时过滤，恢复可用后不触发任何数据库写回。
    assert repository.dependency_writes == []


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

    check = checker.check((f'target-only-package; implementation_version == "{expected}"',))

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

        async def get_readable_user_directory_entries(
            self,
            owner_id: str,
            *,
            snapshot: object = None,
        ) -> tuple[tuple[Skill, SimpleNamespace], ...]:
            del owner_id, snapshot
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
        access_policy=make_test_access_policy(),
    )
    actor = _member_actor()

    async def scenario() -> tuple[tuple[Skill, ...], tuple[Skill, ...], tuple[Skill, ...]]:
        # 首次读取与 TTL 过期后第二次读取，仅 checked_at 推进、依赖语义未变，不应写库。
        first = await service.user_directory(actor)
        clock.advance(61)
        wall_clock_value[0] = CHECKED_AT + timedelta(minutes=5)
        second = await service.user_directory(actor)
        # 第三次探测版本降级为 1.0，语义变为 UNSATISFIED；GET 仅实时过滤，不触发写库。
        clock.advance(61)
        wall_clock_value[0] = CHECKED_AT + timedelta(minutes=10)
        third = await service.user_directory(actor)
        return first, second, third

    first, second, third = asyncio.run(scenario())

    assert first == (skill,)
    assert second == (skill,)
    assert third == ()
    assert repository.get_version_calls == 0
    assert repository.dependency_writes == []


def test_recheck_version_dependencies_skips_write_when_unchanged_and_updates_on_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依赖结论语义未变仅 checked_at 变化时不触发写库，结论变化时正常落库。"""
    probe = _ProbeRecorder("2.1", "2.1", "1.0")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    wall_clock = [CHECKED_AT]
    initial_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=True,
                installed_version="2.1",
                detail_code=None,
            ),
        ),
        checked_at=CHECKED_AT - timedelta(hours=1),
    )
    version = SimpleNamespace(
        id="skillver_recheck",
        skill_id="skill_recheck",
        dependency_requirements=("demo-package>=2",),
        dependency_check=initial_check,
    )
    repository = _DirectoryRepository((), (version,))
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: wall_clock[0],
        monotonic_clock=_ManualMonotonicClock(),
    )
    service = SkillService(
        cast(Any, repository),
        cast(Any, object()),
        cast(Any, object()),
        dependency_checker=checker,
        clock=lambda: wall_clock[0],
        access_policy=make_test_access_policy(),
    )

    async def scenario() -> None:
        # 连续两次实时复核均命中相同版本 2.1，仅 checked_at 推进，必须零写库。
        v1, c1 = await service.recheck_version_dependencies(cast(Any, version))
        assert c1.status is DependencyCheckStatus.SATISFIED
        assert cast(object, v1) is version
        wall_clock[0] = CHECKED_AT + timedelta(minutes=10)
        v2, c2 = await service.recheck_version_dependencies(cast(Any, version))
        assert c2.status is DependencyCheckStatus.SATISFIED
        assert cast(object, v2) is version
        assert repository.dependency_writes == []

        # 环境变为 1.0 后依赖不满足，应立即写回数据库一次。
        wall_clock[0] = CHECKED_AT + timedelta(minutes=20)
        _v3, c3 = await service.recheck_version_dependencies(cast(Any, version))
        assert c3.status is DependencyCheckStatus.UNSATISFIED
        assert len(repository.dependency_writes) == 1
        assert repository.dependency_writes[0].status is DependencyCheckStatus.UNSATISFIED
        assert repository.dependency_writes[0].items[0].installed_version == "1.0"

    asyncio.run(scenario())
    assert probe.calls == 3


def test_prepare_call_uses_cached_dependency_probe_and_avoids_duplicate_probe_before_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_call 复用缓存探针、execute 执行实时探针并刷新快照，避免单次调用双倍探针。"""
    from app.tool.runtime import SKILL_EXECUTE_DEFINITION, RunToolRuntimeService
    from domain.run.model import Run, RunModelSnapshot, RunStatus, RunUsage
    from domain.skill import (
        PackageResource,
        RunSkillEntry,
        RunSkillSnapshot,
        SkillPackageManifest,
        SkillScope,
        SkillScriptDefinition,
        SkillVersion,
    )
    from domain.tool import AuthorizedRunContext

    probe = _ProbeRecorder("2.1", "2.1", "2.1")
    monkeypatch.setattr(dependency_module, "_probe_target_environment", probe)
    monotonic_clock = _ManualMonotonicClock()
    wall_clock = [CHECKED_AT]
    initial_check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.14.0",
        python_implementation="CPython",
        items=(
            DependencyCheckItem(
                requirement="demo-package>=2",
                package="demo-package",
                satisfied=True,
                installed_version="2.1",
                detail_code=None,
            ),
        ),
        checked_at=CHECKED_AT - timedelta(minutes=5),
    )
    manifest = SkillPackageManifest(
        slug="report-skill",
        name="Report Skill",
        description="Generate report",
        guide="Guide",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=5,
                sha256="a" * 64,
                text=True,
            ),
            PackageResource(
                path="scripts/run.py",
                size_bytes=10,
                sha256="b" * 64,
                text=True,
            ),
        ),
        scripts=(
            SkillScriptDefinition(
                path="scripts/run.py",
                label="Run report",
                purpose="Build monthly report",
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ),
    )
    version = SkillVersion(
        id="skillver_exec_dedup",
        skill_id="skill_exec_dedup",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key="skills/aa/dedup.zip",
        package_sha256="c" * 64,
        dependency_requirements=("demo-package>=2",),
        dependency_check=initial_check,
        created_by="usr_admin",
        created_at=CHECKED_AT,
    )
    repository = _DirectoryRepository((), (cast(Any, version),))
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: wall_clock[0],
        monotonic_clock=monotonic_clock,
    )
    service = SkillService(
        cast(Any, repository),
        cast(Any, object()),
        cast(Any, object()),
        dependency_checker=checker,
        clock=lambda: wall_clock[0],
        access_policy=make_test_access_policy(),
    )

    class _FakeFiles:
        async def get_many(
            self,
            session_id: str,
            owner_id: str,
            file_ids: tuple[str, ...],
        ) -> tuple[object, ...]:
            del session_id, owner_id, file_ids
            return ()

    runtime = RunToolRuntimeService(
        auth=cast(Any, object()),
        sessions=cast(Any, object()),
        mcp_catalog=cast(Any, object()),
        mcp_executor=cast(Any, object()),
        knowledge=None,
        project_knowledge=None,
        skills=service,
        session_files=cast(Any, _FakeFiles()),
        access_policy=make_test_access_policy(),
    )
    snapshot = RunSkillSnapshot(
        skills=(
            RunSkillEntry(
                skill_id=version.skill_id,
                slug="report-skill",
                name="Report Skill",
                description="Generate report",
                version_id=version.id,
                version_label="v1",
                requires_approval=False,
                scope=SkillScope.COMPANY,
            ),
        ),
        selected_skill_ids=(version.skill_id,),
    )
    run = Run(
        id="run_skill_dedup",
        session_id="ses_skill_dedup",
        owner_id="usr_member",
        status=RunStatus.RUNNING,
        trigger_message_id="msg_1",
        context_cutoff_sequence=1,
        retry_of_run_id=None,
        model_config_version_id="mpv_1",
        model_snapshot=RunModelSnapshot(
            id="mpv_1",
            name="Skill Test Model",
            protocol="chat_completions",
            base_url="https://api.example.com/v1",
            model="gpt-4o-mini",
            context_window_tokens=128000,
            tokenizer_encoding="cl100k_base",
            supports_stream=True,
            supports_tools=True,
            supports_parallel_tool_calls=False,
            max_output_tokens=4096,
            request_timeout_seconds=30,
            execution_timeout_seconds=120,
            max_concurrency=4,
        ),
        main_prompt_version_id="prompt_main",
        compression_prompt_version_id="prompt_comp",
        prompt_template_version="run-v2",
        event_sequence=1,
        execution_epoch=1,
        worker_id="worker_1",
        lease_expires_at=CHECKED_AT + timedelta(minutes=1),
        execution_started_at=CHECKED_AT,
        execution_deadline_at=CHECKED_AT + timedelta(minutes=1),
        queued_at=CHECKED_AT,
        first_started_at=CHECKED_AT,
        finished_at=None,
        created_at=CHECKED_AT,
        updated_at=CHECKED_AT,
        cancel_requested_at=None,
        safe_error_code=None,
        usage=RunUsage(),
        skill_snapshot=snapshot,
    )
    auth_ctx = AuthorizedRunContext(
        owner_id="usr_member",
        chat_session_id="ses_skill_dedup",
        project_id=None,
        auth_session_id="auth_1",
    )
    arguments: dict[str, Any] = {
        "skill_id": version.skill_id,
        "script_path": "scripts/run.py",
        "parameters": {},
        "input_file_ids": [],
    }

    async def scenario() -> None:
        # 1. 预热共享环境缓存（模拟目录查询或前一次执行后 60s 内）。
        checker.check_cached(version.dependency_requirements)
        assert probe.calls == 1

        # 2. 一次完整的 prepare_call（走缓存） + execute 目标准备（实时复核并刷新快照）
        #    只应新增 1 次实时探测，而非双倍探测，且依赖未变时零写库。
        prep = await runtime.prepare_call(
            SKILL_EXECUTE_DEFINITION,
            "skill_execute",
            arguments,
            run,
            auth_ctx,
            call_id="tc_1",
        )
        assert prep.outcome is None
        assert probe.calls == 1

        wall_clock[0] = CHECKED_AT + timedelta(seconds=2)
        await service.prepare_script_target(
            snapshot,
            skill_id=version.skill_id,
            script_path="scripts/run.py",
            parameters={},
        )
        assert probe.calls == 2

        # 3. 紧接着第二轮 prepare_call 命中刚由 execute 刷新的缓存，仍然 0 新增探针。
        prep_second = await runtime.prepare_call(
            SKILL_EXECUTE_DEFINITION,
            "skill_execute",
            arguments,
            run,
            auth_ctx,
            call_id="tc_2",
        )
        assert prep_second.outcome is None
        assert probe.calls == 2
        assert repository.dependency_writes == []

    asyncio.run(scenario())


def test_current_interpreter_probe_runs_in_process_without_subprocess(
    target_venvs: tuple[_TargetVenv, _TargetVenv],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前解释器走进程内探测（零 subprocess.run），外部 venv 符号链接仍走子进程。"""
    import platform

    real_run = subprocess.run
    subprocess_calls: list[object] = []

    def forbid_subprocess(*args: Any, **kwargs: Any) -> Any:
        subprocess_calls.append((args, kwargs))
        raise AssertionError("current interpreter must not spawn subprocess")

    monkeypatch.setattr(subprocess, "run", forbid_subprocess)
    current_checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
    )
    check = current_checker.check(("packaging>=20", "nonexistent-pkg-athena-test==1.0"))

    assert subprocess_calls == []
    assert check.python_version == platform.python_version()
    assert check.python_implementation == platform.python_implementation()
    assert check.status is DependencyCheckStatus.UNSATISFIED
    assert check.items[0].package == "packaging"
    assert check.items[0].satisfied is True
    assert check.items[0].installed_version is not None
    assert check.items[1].satisfied is False
    assert check.items[1].detail_code == "dependency_missing"

    # 外部 venv 的 python 入口通常是指向同一基础解释器的 symlink；
    # 词法路径与 sys.executable 不同，必须仍走子进程探测而非误判为当前进程。
    target_a, _ = target_venvs
    recorded_external_calls = 0

    def record_subprocess(*args: Any, **kwargs: Any) -> Any:
        nonlocal recorded_external_calls
        recorded_external_calls += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record_subprocess)
    external_checker = DependencyChecker(
        python_executable=target_a.python_executable,
        clock=lambda: CHECKED_AT,
    )
    external_check = external_checker.check(("target-only-package>=2,<3",))

    assert recorded_external_calls == 1
    assert external_check.status is DependencyCheckStatus.SATISFIED
    assert external_check.items[0].installed_version == "2.1"


def test_current_interpreter_probe_maps_metadata_os_error_to_environment_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进程内元数据读取发生 OSError 时与子进程路径一致映射为环境不可用。"""
    import importlib.metadata

    def raise_os_error() -> Any:
        raise OSError("dist-info unreadable")

    monkeypatch.setattr(importlib.metadata, "distributions", raise_os_error)
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
    )

    with pytest.raises(SkillPackageError, match="skill_dependency_environment_unavailable"):
        checker.check(("packaging>=20",))
