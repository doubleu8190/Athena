"""使用目标 Python 环境校验 Skill 的 PEP 508 依赖。"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from common.date import utc_now
from domain.skill.model import (
    DependencyCheck,
    DependencyCheckItem,
    DependencyCheckStatus,
    SkillPackageError,
)

_DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0
_DEFAULT_ENVIRONMENT_CACHE_TTL_SECONDS = 60.0
_MARKER_ENV_KEYS = frozenset(
    {
        "implementation_name",
        "implementation_version",
        "os_name",
        "platform_machine",
        "platform_release",
        "platform_system",
        "platform_version",
        "python_full_version",
        "platform_python_implementation",
        "python_version",
        "sys_platform",
    }
)
_PROBE_SCRIPT = r"""
import importlib.metadata, json, os, platform, sys
v = sys.implementation.version
suffix = "" if v.releaselevel == "final" else v.releaselevel[0] + str(v.serial)
iv = f"{v.major}.{v.minor}.{v.micro}{suffix}"
dists = [
    {"name": n, "version": d.version}
    for d in importlib.metadata.distributions()
    if (n := d.metadata.get("Name")) and n.strip() and isinstance(d.version, str)
]
print(json.dumps({
    "python_version": platform.python_version(),
    "python_implementation": platform.python_implementation(),
    "marker_environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": iv,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": platform.python_version(),
        "platform_python_implementation": platform.python_implementation(),
        "python_version": ".".join(platform.python_version_tuple()[:2]),
        "sys_platform": sys.platform,
    },
    "distributions": dists,
}, ensure_ascii=True, separators=(",", ":")))
"""


@dataclass(frozen=True, slots=True)
class _TargetEnvironment:
    """保存一次目标解释器探测得到的检查输入。"""

    python_version: str
    python_implementation: str
    marker_environment: Mapping[str, str]
    distributions: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _CachedEnvironment:
    """保存共享环境快照、探测业务时间与单调时钟起点。"""

    environment: _TargetEnvironment
    checked_at: datetime
    monotonic_at: float


def _resolve_python_executable(python_executable: str | Path | None) -> Path:
    """解析并验证目标 Python 绝对可执行路径。"""
    value = (
        python_executable
        if python_executable is not None
        else os.environ.get("ATHENA_SKILL_PYTHON_EXECUTABLE", sys.executable)
    )
    executable = Path(value)
    try:
        valid = executable.is_absolute() and executable.is_file() and os.access(executable, os.X_OK)
    except OSError as error:
        raise SkillPackageError("skill_dependency_python_invalid") from error
    if not valid:
        raise SkillPackageError("skill_dependency_python_invalid")
    return executable


def _is_current_interpreter(python_executable: Path) -> bool:
    """按词法绝对路径判断是否为当前解释器，严禁 resolve() 以免解引用外部 venv 符号链接。"""
    return Path(os.path.abspath(python_executable)) == Path(os.path.abspath(sys.executable))


def _probe_current_environment() -> _TargetEnvironment:
    """目标解释器与当前进程一致时直接进程内读取，零子进程开销。"""
    try:
        distributions: dict[str, list[str]] = {}
        for distribution in importlib.metadata.distributions():
            name = distribution.metadata.get("Name")
            version = distribution.version
            if not isinstance(name, str) or not name.strip() or not isinstance(version, str):
                continue
            distributions.setdefault(canonicalize_name(name), []).append(version)
        marker_environment = {key: str(value) for key, value in default_environment().items()}
        return _TargetEnvironment(
            python_version=platform.python_version(),
            python_implementation=platform.python_implementation(),
            marker_environment=marker_environment,
            distributions={pkg: tuple(vers) for pkg, vers in distributions.items()},
        )
    except (OSError, UnicodeError) as error:
        raise SkillPackageError("skill_dependency_environment_unavailable") from error


def _parse_probe_payload(payload: object) -> _TargetEnvironment:
    """轻量校验子进程 JSON 输出协议并归一化发行包名。"""
    if not isinstance(payload, dict):
        raise SkillPackageError("skill_dependency_environment_unavailable")
    python_version = payload.get("python_version")
    python_implementation = payload.get("python_implementation")
    marker_raw = payload.get("marker_environment")
    raw_distributions = payload.get("distributions")
    if (
        not isinstance(python_version, str)
        or not isinstance(python_implementation, str)
        or not isinstance(marker_raw, dict)
        or set(marker_raw) != _MARKER_ENV_KEYS
        or not all(isinstance(v, str) for v in marker_raw.values())
        or not isinstance(raw_distributions, list)
    ):
        raise SkillPackageError("skill_dependency_environment_unavailable")

    distributions: dict[str, list[str]] = {}
    for item in raw_distributions:
        if not isinstance(item, dict):
            raise SkillPackageError("skill_dependency_environment_unavailable")
        name = item.get("name")
        version = item.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise SkillPackageError("skill_dependency_environment_unavailable")
        distributions.setdefault(canonicalize_name(name), []).append(version)

    return _TargetEnvironment(
        python_version=python_version,
        python_implementation=python_implementation,
        marker_environment={str(k): str(v) for k, v in marker_raw.items()},
        distributions={pkg: tuple(vers) for pkg, vers in distributions.items()},
    )


def _probe_target_environment(
    python_executable: Path,
    *,
    timeout_seconds: float,
) -> _TargetEnvironment:
    """读取目标环境指纹、marker 与发行包；当前解释器走进程内快路径，外部 venv 走子进程探测。"""
    if _is_current_interpreter(python_executable):
        return _probe_current_environment()
    try:
        completed = subprocess.run(
            (str(python_executable), "-I", "-c", _PROBE_SCRIPT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise SkillPackageError("skill_dependency_environment_unavailable") from error
    if completed.returncode != 0:
        raise SkillPackageError("skill_dependency_environment_unavailable")
    try:
        payload = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SkillPackageError("skill_dependency_environment_unavailable") from error
    return _parse_probe_payload(payload)


class DependencyChecker:
    """按目标脚本解释器检查 PEP 508 依赖与 marker，带 TTL 环境快照缓存。"""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        clock: Callable[[], datetime] = utc_now,
        monotonic_clock: Callable[[], float] = time.monotonic,
        environment_cache_ttl_seconds: float = _DEFAULT_ENVIRONMENT_CACHE_TTL_SECONDS,
        probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        if environment_cache_ttl_seconds <= 0 or probe_timeout_seconds <= 0:
            raise SkillPackageError("skill_dependency_probe_configuration_invalid")
        self._python_executable = _resolve_python_executable(python_executable)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._environment_cache_ttl_seconds = environment_cache_ttl_seconds
        self._probe_timeout_seconds = probe_timeout_seconds
        self._environment_lock = threading.Lock()
        self._environment_snapshot: _CachedEnvironment | None = None

    def validate_requirements(self, requirements: Sequence[str]) -> tuple[str, ...]:
        """导入阶段校验并规范化 PEP 508 需求，拒绝空值、超量、直接 URL 与 extras。"""
        if len(requirements) > 256:
            raise SkillPackageError("skill_dependencies_too_many")
        normalized: list[str] = []
        for value in requirements:
            if not isinstance(value, str) or not value.strip():
                raise SkillPackageError("skill_dependency_invalid")
            try:
                requirement = Requirement(value)
            except InvalidRequirement as error:
                raise SkillPackageError("skill_dependency_invalid") from error
            if requirement.url is not None:
                raise SkillPackageError("skill_dependency_url_unsupported")
            if requirement.extras:
                raise SkillPackageError("skill_dependency_extras_unsupported")
            normalized.append(str(requirement))
        return tuple(normalized)

    def check(
        self,
        requirements: Sequence[str],
        *,
        checked_at: datetime | None = None,
    ) -> DependencyCheck:
        """实时探测目标环境并刷新共享快照，用于导入、配置及执行前复核。"""
        normalized = self.validate_requirements(requirements)
        environment, environment_checked_at = self._environment_for_check(
            use_cached=False,
            checked_at=checked_at,
        )
        return self._check_with_environment(
            normalized,
            environment,
            checked_at=checked_at or environment_checked_at,
        )

    def check_cached(self, requirements: Sequence[str]) -> DependencyCheck:
        """优先命中 TTL 内共享环境快照，用于目录只读过滤与调用预检。"""
        normalized = self.validate_requirements(requirements)
        environment, environment_checked_at = self._environment_for_check(
            use_cached=True,
            checked_at=None,
        )
        return self._check_with_environment(
            normalized,
            environment,
            checked_at=environment_checked_at,
        )

    def _environment_for_check(
        self,
        *,
        use_cached: bool,
        checked_at: datetime | None,
    ) -> tuple[_TargetEnvironment, datetime]:
        """在互斥锁内复用有效缓存或执行新探测；探测失败立即失效旧快照。"""
        with self._environment_lock:
            if use_cached and self._environment_snapshot is not None:
                age = self._monotonic_clock() - self._environment_snapshot.monotonic_at
                if 0 <= age < self._environment_cache_ttl_seconds:
                    return (
                        self._environment_snapshot.environment,
                        self._environment_snapshot.checked_at,
                    )
            try:
                environment = _probe_target_environment(
                    self._python_executable,
                    timeout_seconds=self._probe_timeout_seconds,
                )
                environment_checked_at = checked_at or self._clock()
            except Exception:
                # 探测失败立即清空缓存，避免后续请求继续命中过期环境。
                self._environment_snapshot = None
                raise
            self._environment_snapshot = _CachedEnvironment(
                environment=environment,
                checked_at=environment_checked_at,
                monotonic_at=self._monotonic_clock(),
            )
            return environment, environment_checked_at

    def _check_with_environment(
        self,
        normalized_requirements: Sequence[str],
        environment: _TargetEnvironment,
        *,
        checked_at: datetime,
    ) -> DependencyCheck:
        """基于已探测目标环境逐项评估 marker、发行包存在性与 PEP 440 版本范围。"""
        items: list[DependencyCheckItem] = []
        for value in normalized_requirements:
            requirement = Requirement(value)
            applies = (
                True
                if requirement.marker is None
                else bool(requirement.marker.evaluate(environment=environment.marker_environment))
            )
            package = canonicalize_name(requirement.name)
            if not applies:
                items.append(
                    DependencyCheckItem(
                        requirement=value,
                        package=package,
                        satisfied=True,
                        marker_applies=False,
                    )
                )
                continue
            versions = environment.distributions.get(package, ())
            satisfying = [ver for ver in versions if _version_satisfies(requirement, ver)]
            items.append(
                DependencyCheckItem(
                    requirement=value,
                    package=package,
                    satisfied=bool(satisfying),
                    installed_version=(
                        satisfying[0] if satisfying else (versions[0] if versions else None)
                    ),
                    detail_code=(
                        None
                        if satisfying
                        else ("dependency_version_mismatch" if versions else "dependency_missing")
                    ),
                )
            )
        status = (
            DependencyCheckStatus.SATISFIED
            if all(item.satisfied for item in items)
            else DependencyCheckStatus.UNSATISFIED
        )
        return DependencyCheck(
            status=status,
            python_version=environment.python_version,
            python_implementation=environment.python_implementation,
            items=tuple(items),
            checked_at=checked_at,
        )


def _version_satisfies(requirement: Requirement, version: str) -> bool:
    """判断单个发行包版本是否满足 PEP 440 约束，非法版本按不满足处理。"""
    try:
        parsed_version = Version(version)
    except InvalidVersion:
        return False
    return requirement.specifier.contains(parsed_version, prereleases=True)


__all__ = ["DependencyChecker"]
