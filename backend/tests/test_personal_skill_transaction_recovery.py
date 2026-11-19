"""个人 Skill 在提交结果未知时不得误删可能已被引用的对象。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.skill.service import SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    RunSkillEntry,
    RunSkillSnapshot,
    Skill,
    SkillImportRequestResult,
    SkillNotFoundError,
    SkillPackageManifest,
    SkillScope,
    SkillScriptDefinition,
    SkillVersion,
)

NOW = datetime(2026, 11, 19, tzinfo=UTC)


class _Objects:
    """记录个人包写入和清理。"""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content_type
        self.values[key] = content

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.values.pop(key, None)


class _AuthReader:
    """个人流程不读取管理员身份。"""

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del user_id, session_id, now
        return None


class _Repository:
    """模拟提交响应丢失、取消和已知事务回滚。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.replay: SkillImportRequestResult | None = None
        self.find_calls = 0

    async def find_personal_request_result(
        self,
        actor: AuthContext,
        **kwargs: object,
    ) -> SkillImportRequestResult | None:
        del actor, kwargs
        self.find_calls += 1
        return self.replay

    async def create_personal_skill(
        self,
        **kwargs: object,
    ) -> SkillImportRequestResult:
        if isinstance(self.error, RuntimeError):
            self.replay = _records(
                str(kwargs["skill_id"]),
                str(kwargs["version_id"]),
                str(kwargs["object_key"]),
            )
        raise self.error


class _PublishRepository:
    """模拟个人发布已提交后响应丢失，但当前指针已前进到新版本。"""

    def __init__(self, replay_error: Exception | None = None) -> None:
        self.find_calls = 0
        self.replay: SkillImportRequestResult | None = None
        self.replay_error = replay_error
        self.request_object_key = ""

    async def find_personal_request_result(
        self,
        actor: AuthContext,
        **kwargs: object,
    ) -> SkillImportRequestResult | None:
        del actor, kwargs
        self.find_calls += 1
        if self.find_calls > 1 and self.replay_error is not None:
            raise self.replay_error
        return None if self.find_calls == 1 else self.replay

    async def get_personal_skill(
        self,
        owner_id: str,
        skill_id: str,
    ) -> Skill | None:
        del owner_id, skill_id
        return _records("skill_1", "skillver_previous", "skills/aa/previous.zip").skill

    async def publish_personal_version(
        self,
        skill_id: str,
        **kwargs: object,
    ) -> SkillImportRequestResult:
        self.request_object_key = str(kwargs["object_key"])
        request = _records(
            skill_id,
            str(kwargs["version_id"]),
            self.request_object_key,
        )
        current = _records(
            skill_id,
            "skillver_current",
            "skills/aa/current.zip",
        )
        self.replay = SkillImportRequestResult(
            skill=replace(current.skill, revision=3),
            version=current.version,
            request_version=request.version,
        )
        raise RuntimeError("database commit response lost")


def test_committed_personal_response_failure_replays_without_deleting() -> None:
    """数据库可能已提交时，重放命中原对象且保留存储。"""
    repository = _Repository(RuntimeError("response lost"))
    objects = _Objects()
    service = _service(repository, objects)

    details = asyncio.run(
        service.create_personal(
            _actor(),
            name="周报",
            description="整理事实",
            guide="正文",
            idempotency_key="create-key",
        )
    )

    assert details.skill.id == "skill_1"
    assert details.versions[0].id == "skillver_2"
    assert objects.deleted == []
    assert len(objects.values) == 1


def test_personal_unknown_commit_replay_keeps_original_request_object() -> None:
    """当前指针已前进时，恢复分支仍按原始请求版本判断对象是否被引用。"""
    repository = _PublishRepository()
    objects = _Objects()
    service = SkillService(
        cast(Any, repository),
        cast(Any, objects),
        _AuthReader(),
        id_factory=lambda _: "skillver_prepared",
        clock=lambda: NOW,
    )

    details = asyncio.run(
        service.publish_personal(
            _actor(),
            "skill_1",
            name="周报",
            description="整理事实",
            guide="正文 v2",
            expected_revision=1,
            idempotency_key="publish-current",
        )
    )

    assert details.versions[0].id == "skillver_current"
    assert repository.request_object_key in objects.values
    assert repository.request_object_key not in objects.deleted


def test_personal_unknown_commit_replay_propagates_deleted_not_found() -> None:
    """恢复查询发现 Skill 已删除时返回 404 语义，并保留待安全回收对象。"""
    repository = _PublishRepository(SkillNotFoundError())
    objects = _Objects()
    service = SkillService(
        cast(Any, repository),
        cast(Any, objects),
        _AuthReader(),
        id_factory=lambda _: "skillver_prepared",
        clock=lambda: NOW,
    )

    with pytest.raises(SkillNotFoundError):
        asyncio.run(
            service.publish_personal(
                _actor(),
                "skill_1",
                name="周报",
                description="整理事实",
                guide="正文 v2",
                expected_revision=1,
                idempotency_key="publish-deleted",
            )
        )

    assert repository.request_object_key in objects.values
    assert repository.request_object_key not in objects.deleted


def test_cancelled_personal_commit_retains_object_for_recovery() -> None:
    """取消可能发生在提交后，不能同步删除对象。"""
    repository = _Repository(asyncio.CancelledError())
    objects = _Objects()
    service = _service(repository, objects)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            service.create_personal(
                _actor(),
                name="周报",
                description="整理事实",
                guide="正文",
                idempotency_key="create-key",
            )
        )

    assert objects.deleted == []
    assert len(objects.values) == 1


def test_personal_skill_script_target_is_always_rejected() -> None:
    """即使持久化个人清单损坏，也不能进入现有脚本执行准备链路。"""
    script = SkillScriptDefinition(
        path="scripts/run.py",
        label="运行",
        purpose="测试伪造目标",
        parameters={"type": "object"},
    )
    manifest = SkillPackageManifest(
        slug="personal-script",
        name="个人脚本",
        description="不应执行",
        guide="正文",
        scripts=(script,),
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=6,
                sha256="a" * 64,
                text=True,
            ),
            PackageResource(
                path="scripts/run.py",
                size_bytes=1,
                sha256="c" * 64,
                text=True,
            ),
        ),
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="not-checked",
        python_implementation="markdown-only",
        items=(),
        checked_at=NOW,
    )
    version = SkillVersion(
        id="skillver_script",
        skill_id="skill_script",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key="skills/aa/script.zip",
        package_sha256="d" * 64,
        dependency_requirements=(),
        dependency_check=check,
        created_by="usr_owner",
        created_at=NOW,
    )
    snapshot = RunSkillSnapshot(
        skills=(
            RunSkillEntry(
                skill_id=version.skill_id,
                slug=manifest.slug,
                name=manifest.name,
                description=manifest.description,
                version_id=version.id,
                version_label=version.label,
                requires_approval=False,
                scope=SkillScope.PERSONAL,
            ),
        ),
        selected_skill_ids=(version.skill_id,),
    )

    class _VersionRepository:
        async def get_version(self, version_id: str) -> SkillVersion | None:
            return version if version_id == version.id else None

    service = SkillService(
        cast(Any, _VersionRepository()),
        cast(Any, _Objects()),
        _AuthReader(),
        clock=lambda: NOW,
    )
    with pytest.raises(SkillNotFoundError, match="skill_script_not_registered"):
        asyncio.run(
            service.prepare_script_target(
                snapshot,
                skill_id=version.skill_id,
                script_path=script.path,
                parameters={},
            )
        )


def _service(repository: _Repository, objects: _Objects) -> SkillService:
    """构造只走个人流程的最小服务。"""
    values = iter(("skill_1", "skillver_2"))
    return SkillService(
        cast(Any, repository),
        cast(Any, objects),
        _AuthReader(),
        id_factory=lambda prefix: next(values),
        clock=lambda: NOW,
    )


def _records(
    skill_id: str,
    version_id: str,
    object_key: str,
) -> SkillImportRequestResult:
    """构造与仓储已提交对象一致的领域结果。"""
    manifest = SkillPackageManifest(
        slug="personal-demo",
        name="周报",
        description="整理事实",
        guide="正文",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=6,
                sha256="a" * 64,
                text=True,
            ),
        ),
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="not-checked",
        python_implementation="markdown-only",
        items=(),
        checked_at=NOW,
    )
    version = SkillVersion(
        id=version_id,
        skill_id=skill_id,
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=object_key,
        package_sha256="b" * 64,
        dependency_requirements=(),
        dependency_check=check,
        created_by="usr_owner",
        created_at=NOW,
    )
    skill = Skill(
        id=skill_id,
        slug=manifest.slug,
        current_version_id=version.id,
        enabled=True,
        script_requires_approval=False,
        revision=1,
        created_by="usr_owner",
        created_at=NOW,
        updated_at=NOW,
        name=manifest.name,
        description=manifest.description,
        current_version_label="v1",
        dependency_check=check,
        scope=SkillScope.PERSONAL,
        owner_id="usr_owner",
    )
    return SkillImportRequestResult(skill=skill, version=version)


def _actor() -> AuthContext:
    """返回个人 Skill 所有者认证上下文。"""
    return AuthContext(
        user=User(
            id="usr_owner",
            email="owner@example.test",
            password_hash="unused",
            display_name=None,
            status=UserStatus.ACTIVE,
            created_at=NOW,
            updated_at=NOW,
            role=UserRole.USER,
        ),
        session_id="auth_owner",
    )
