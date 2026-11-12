"""使用目标 Python 环境校验 Skill 的 PEP 508 依赖。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, ValidationError

from common.ids import now_utc
from domain.skill.model import (
    DependencyCheck,
    DependencyCheckItem,
    DependencyCheckStatus,
    SkillPackageError,
)

_DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0
_DEFAULT_ENVIRONMENT_CACHE_TTL_SECONDS = 60.0
_PROBE_SCRIPT = r"""
import importlib.metadata
import json
import os
import platform
import sys

def format_full_version(version):
    value = f"{version.major}.{version.minor}.{version.micro}"
    if version.releaselevel != "final":
        value += version.releaselevel[0] + str(version.serial)
    return value


distributions = []
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    version = distribution.version
    if name is None or not name.strip():
        continue
    distributions.append({"name": name, "version": version})

payload = {
    "python_version": platform.python_version(),
    "python_implementation": platform.python_implementation(),
    "marker_environment": {
        "implementation_name": sys.implementation.name,
        "implementation_version": format_full_version(sys.implementation.version),
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
    "distributions": distributions,
}
print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
"""


class _ProbeDistribution(BaseModel):
    """目标解释器中的一个发行包名称及版本。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str
    version: str


class _ProbeMarkerEnvironment(BaseModel):
    """固定 probe 脚本必须返回的全部 PEP 508 marker 字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    implementation_name: str
    implementation_version: str
    os_name: str
    platform_machine: str
    platform_release: str
    platform_system: str
    platform_version: str
    python_full_version: str
    platform_python_implementation: str
    python_version: str
    sys_platform: str


class _ProbePayload(BaseModel):
    """固定 stdlib probe 输出的纯 JSON 协议。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    python_version: str
    python_implementation: str
    marker_environment: _ProbeMarkerEnvironment
    distributions: list[_ProbeDistribution]


@dataclass(frozen=True, slots=True)
class _TargetEnvironment:
    """保存一次目标解释器探测得到的检查输入。"""

    python_version: str
    python_implementation: str
    marker_environment: Mapping[str, str]
    distributions: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _CachedEnvironment:
    """保存共享环境快照、探测时间和单调时钟起点。"""

    environment: _TargetEnvironment
    checked_at: datetime
    monotonic_at: float


def _resolve_python_executable(python_executable: str | Path | None) -> Path:
    """解析并验证目标 Python 可执行文件。

    参数:
        python_executable: 显式解释器路径；为空时优先读取
            ``ATHENA_SKILL_PYTHON_EXECUTABLE``，最后使用当前解释器。

    返回:
        可启动的绝对解释器路径。

    异常:
        路径为空、相对、不是普通文件或不可执行时抛出
        ``SkillPackageError("skill_dependency_python_invalid")``。
    """
    value = (
        python_executable
        if python_executable is not None
        else os.environ.get("ATHENA_SKILL_PYTHON_EXECUTABLE", sys.executable)
    )
    executable = Path(value)
    try:
        valid = (
            executable.is_absolute()
            and executable.is_file()
            and os.access(executable, os.X_OK)
        )
    except OSError as error:
        raise SkillPackageError("skill_dependency_python_invalid") from error
    if not valid:
        raise SkillPackageError("skill_dependency_python_invalid")
    return executable


def _probe_target_environment(
    python_executable: Path,
    *,
    timeout_seconds: float,
) -> _TargetEnvironment:
    """用固定标准库程序读取目标环境的指纹、marker 和发行包元数据。

    参数:
        python_executable: 已验证的目标解释器绝对路径。
        timeout_seconds: 单次环境探测的最长等待秒数。

    返回:
        经结构和类型校验的目标环境快照。

    异常:
        进程启动、超时、非零退出或 JSON 协议不合法时抛出
        ``SkillPackageError("skill_dependency_environment_unavailable")``。
    """
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
        payload = _ProbePayload.model_validate_json(completed.stdout)
    except (UnicodeError, ValidationError) as error:
        raise SkillPackageError("skill_dependency_environment_unavailable") from error

    distributions: dict[str, list[str]] = {}
    for item in payload.distributions:
        package = canonicalize_name(item.name)
        distributions.setdefault(package, []).append(item.version)

    return _TargetEnvironment(
        python_version=payload.python_version,
        python_implementation=payload.python_implementation,
        marker_environment=payload.marker_environment.model_dump(),
        distributions={
            package: tuple(versions) for package, versions in distributions.items()
        },
    )


def environment_fingerprint(
    python_executable: str | Path | None = None,
    *,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """探测目标解释器并返回不包含绝对路径的安全指纹。

    参数:
        python_executable: 目标解释器；为空时遵循构造函数相同的默认规则。
        timeout_seconds: 单次探测的最长等待秒数。

    返回:
        ``(python_version, python_implementation)`` 形式的实际目标指纹。

    异常:
        目标路径非法或探测不可用时抛出 ``SkillPackageError``。
    """
    if timeout_seconds <= 0:
        raise SkillPackageError("skill_dependency_probe_configuration_invalid")
    executable = _resolve_python_executable(python_executable)
    snapshot = _probe_target_environment(
        executable,
        timeout_seconds=timeout_seconds,
    )
    return snapshot.python_version, snapshot.python_implementation


class DependencyChecker:
    """按目标脚本解释器检查 PEP 508 依赖和 marker。"""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        clock: Callable[[], datetime] = now_utc,
        monotonic_clock: Callable[[], float] = time.monotonic,
        environment_cache_ttl_seconds: float = _DEFAULT_ENVIRONMENT_CACHE_TTL_SECONDS,
        probe_timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> None:
        """保存目标解释器和带超时的探测配置。

        参数:
            python_executable: 目标解释器；为空时遵循环境变量及当前解释器默认规则。
            clock: 生成检查时间时使用的业务时钟。
            monotonic_clock: 判断环境快照年龄时使用的单调时钟。
            environment_cache_ttl_seconds: 公共目录可复用环境快照的秒数。
            probe_timeout_seconds: 环境探测子进程的最长等待秒数。

        返回:
            无返回值。

        异常:
            解释器路径非法或探测限额不是正数时抛出 ``SkillPackageError``。
        """
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
        """导入阶段拒绝非法格式、直接 URL、extras 和空需求。

        参数:
            requirements: Skill 声明的 PEP 508 需求文本。

        返回:
            按原顺序规范化后的需求文本。

        异常:
            数量超限或声明不受支持时抛出带稳定错误码的 ``SkillPackageError``。
        """
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
        """在目标脚本解释器中检查 marker、发行包名与版本范围。

        该方法会同步启动带超时的子进程，异步调用方应在线程中执行以避免阻塞事件循环。

        参数:
            requirements: Skill 声明的 PEP 508 需求文本。
            checked_at: 调用方固定的检查时间；为空时使用构造时业务时钟。

        返回:
            带目标 Python 指纹及逐项结果的依赖检查记录。

        异常:
            声明不受支持、目标解释器非法或环境探测不可用时抛出
            ``SkillPackageError``；不会回退到 API/Worker 进程环境。
        """
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
        """使用 TTL 内的共享环境快照检查公共目录依赖。

        参数:
            requirements: Skill 声明的 PEP 508 需求文本。

        返回:
            使用实际探测时间且可安全持久化的依赖检查记录。

        异常:
            声明不受支持或环境探测失败时抛出 ``SkillPackageError``；探测失败会
            立即失效已有快照，不回退过期环境或 API/Worker 进程环境。
        """
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
        """在锁内获取有效快照或执行一次并替换共享快照。

        参数:
            use_cached: 为 ``True`` 时允许 TTL 内的公共目录快照命中。
            checked_at: 强制探测时使用的固定业务时间；为空时在探测成功后读取时钟。

        返回:
            目标环境以及该环境实际对应的检查时间。

        异常:
            探测或时钟失败时清除共享快照并继续抛出；不会返回旧环境。
        """
        with self._environment_lock:
            if use_cached:
                snapshot = self._environment_snapshot
                if snapshot is not None:
                    age = self._monotonic_clock() - snapshot.monotonic_at
                    if 0 <= age < self._environment_cache_ttl_seconds:
                        return snapshot.environment, snapshot.checked_at
            try:
                environment = _probe_target_environment(
                    self._python_executable,
                    timeout_seconds=self._probe_timeout_seconds,
                )
                environment_checked_at = checked_at or self._clock()
            except Exception:
                # 任何一次显式 fresh 失败都使已知结果不可信，后续 GET 必须重新探测。
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
        """用已取得的目标环境计算一次 Skill 依赖结果。

        参数:
            normalized_requirements: 已通过格式校验并规范化的需求文本。
            environment: 本次检查共享的目标环境快照。
            checked_at: 该环境实际探测对应的检查时间。

        返回:
            带稳定探测时间和逐项结果的依赖检查记录。

        异常:
            需求不受支持时抛出 ``SkillPackageError``。
        """
        items: list[DependencyCheckItem] = []
        for value in normalized_requirements:
            requirement = Requirement(value)
            # marker 字段全部取自目标解释器，不能混入 API 进程环境。
            applies = (
                True
                if requirement.marker is None
                else bool(
                    requirement.marker.evaluate(
                        environment=environment.marker_environment
                    )
                )
            )
            if not applies:
                items.append(
                    DependencyCheckItem(
                        requirement=value,
                        package=canonicalize_name(requirement.name),
                        satisfied=True,
                        marker_applies=False,
                    )
                )
                continue
            package = canonicalize_name(requirement.name)
            versions = environment.distributions.get(package, ())
            satisfying = [
                version
                for version in versions
                if _version_satisfies(requirement, version)
            ]
            items.append(
                DependencyCheckItem(
                    requirement=value,
                    package=package,
                    satisfied=bool(satisfying),
                    installed_version=(
                        satisfying[0]
                        if satisfying
                        else (versions[0] if versions else None)
                    ),
                    detail_code=(
                        None
                        if satisfying
                        else (
                            "dependency_version_mismatch"
                            if versions
                            else "dependency_missing"
                        )
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
    """判断目标环境中的一个发行包版本是否满足 PEP 440 约束。

    参数:
        requirement: 已解析的 PEP 508 需求。
        version: 目标发行包报告的实际版本文本。

    返回:
        ``True`` 表示版本合法且处于约束范围内，否则返回 ``False``。

    异常:
        无业务异常；发行包元数据中的非法版本按不满足处理。
    """
    try:
        parsed_version = Version(version)
    except InvalidVersion:
        return False
    return requirement.specifier.contains(parsed_version, prereleases=True)


__all__ = ["DependencyChecker", "environment_fingerprint"]
