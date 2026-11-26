"""个人 Markdown Skill 纯库事务恢复、零 MinIO 触达与脚本隔离测试。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from app.skill.execution_service import SkillExecutionService
from app.skill.service import SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.file import FileStorageError
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
from domain.tool import ToolExecutionContext, ToolOutcomeStatus

NOW = datetime(2026, 11, 19, tzinfo=UTC)


class _StrictUnusedObjects:
    """断言纯 Markdown 读写路径 0 次触达对象存储（任何调用立即报错）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def put_bytes(self, key: str, content: bytes, *, content_type: str) -> None:
        del content, content_type
        self.calls.append(f"put_bytes:{key}")
        raise FileStorageError("file_storage_unavailable")

    async def get_bytes(self, key: str, *, maximum_bytes: int) -> bytes:
        del maximum_bytes
        self.calls.append(f"get_bytes:{key}")
        raise FileStorageError("file_storage_unavailable")

    async def delete(self, key: str) -> None:
        self.calls.append(f"delete:{key}")
        raise FileStorageError("file_storage_unavailable")


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
    """模拟纯库提交响应丢失、取消与 object_key=None 写入校验。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.replay: SkillImportRequestResult | None = None
        self.find_calls = 0
        self.received_object_keys: list[object] = []

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
        self.received_object_keys.append(kwargs["object_key"])
        if isinstance(self.error, RuntimeError):
            self.replay = _records(
                str(kwargs["skill_id"]),
                str(kwargs["version_id"]),
                cast(str | None, kwargs["object_key"]),
            )
        raise self.error


class _PublishRepository:
    """模拟个人发布已提交后响应丢失，但当前指针已前进到新版本。"""

    def __init__(self, replay_error: Exception | None = None) -> None:
        self.find_calls = 0
        self.replay: SkillImportRequestResult | None = None
        self.replay_error = replay_error
        self.received_object_keys: list[object] = []

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
        return _records("skill_1", "skillver_previous", None).skill

    async def publish_personal_version(
        self,
        skill_id: str,
        **kwargs: object,
    ) -> SkillImportRequestResult:
        self.received_object_keys.append(kwargs["object_key"])
        request = _records(
            skill_id,
            str(kwargs["version_id"]),
            cast(str | None, kwargs["object_key"]),
        )
        current = _records(skill_id, "skillver_current", None)
        self.replay = SkillImportRequestResult(
            skill=replace(current.skill, revision=3),
            version=current.version,
            request_version=request.version,
        )
        raise RuntimeError("database commit response lost")


def test_committed_personal_response_failure_replays_without_touching_object_store() -> None:
    """数据库提交响应丢失时纯库重放命中，object_key 为 None 且 0 次触达 MinIO。"""
    repository = _Repository(RuntimeError("response lost"))
    objects = _StrictUnusedObjects()
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
    assert details.versions[0].object_key is None
    assert repository.received_object_keys == [None]
    assert objects.calls == []


def test_personal_unknown_commit_replay_works_with_no_object_store() -> None:
    """object_store=None 且当前指针已前进时，恢复重放仍返回最新有效详情。"""
    repository = _PublishRepository()
    service = SkillService(
        cast(Any, repository),
        None,
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
    assert details.versions[0].object_key is None
    assert repository.received_object_keys == [None]


def test_personal_unknown_commit_replay_propagates_deleted_not_found() -> None:
    """恢复查询发现 Skill 已删除时返回 404 语义，且全程不触达对象存储。"""
    repository = _PublishRepository(SkillNotFoundError())
    objects = _StrictUnusedObjects()
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

    assert repository.received_object_keys == [None]
    assert objects.calls == []


def test_cancelled_personal_commit_propagates_without_touching_object_store() -> None:
    """取消纯库写入直接传播 CancelledError，且全程 0 次触达对象存储。"""
    repository = _Repository(asyncio.CancelledError())
    objects = _StrictUnusedObjects()
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

    assert repository.received_object_keys == [None]
    assert objects.calls == []


def test_markdown_read_resource_reads_from_db_without_minio_and_rejects_other_paths() -> None:
    """object_key=None 时读取 SKILL.md 直出 DB 正文切片，其他路径抛 SkillNotFoundError。"""
    guide = "# 指南\n中文😀内容"
    guide_bytes = guide.encode("utf-8")
    manifest = SkillPackageManifest(
        slug="md-direct-read",
        name="纯文本技能",
        description="零 MinIO 读取测试",
        guide=guide,
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=len(guide_bytes),
                sha256="a" * 64,
                text=True,
            ),
            # 模拟历史或异常记录中意外存在第二个文件声明，验证 object_key=None 时仍拒绝。
            PackageResource(
                path="references/extra.txt",
                size_bytes=4,
                sha256="b" * 64,
                text=True,
            ),
        ),
    )
    version = SkillVersion(
        id="skillver_md",
        skill_id="skill_md",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=None,
        package_sha256="c" * 64,
        dependency_requirements=(),
        dependency_check=DependencyCheck(
            status=DependencyCheckStatus.SATISFIED,
            python_version="not-checked",
            python_implementation="markdown-only",
            items=(),
            checked_at=NOW,
        ),
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
                scope=SkillScope.COMPANY,
            ),
        ),
        selected_skill_ids=(version.skill_id,),
    )

    class _VersionRepo:
        async def get_version(self, version_id: str) -> SkillVersion | None:
            return version if version_id == version.id else None

    objects = _StrictUnusedObjects()
    # 同时验证 object_store 不可用或为 None 两种情况均正常读取 SKILL.md。
    for store in (cast(Any, objects), None):
        service = SkillService(cast(Any, _VersionRepo()), store, _AuthReader(), clock=lambda: NOW)
        offset = 0
        chunks: list[str] = []
        while True:
            sliced = asyncio.run(
                service.read_resource(
                    snapshot,
                    skill_id=version.skill_id,
                    path="SKILL.md",
                    offset=offset,
                    limit_bytes=5,
                )
            )
            chunks.append(sliced.content)
            offset = sliced.next_offset
            if sliced.eof:
                break
        assert "".join(chunks) == guide
        with pytest.raises(SkillNotFoundError, match="skill_resource_not_found"):
            asyncio.run(
                service.read_resource(
                    snapshot,
                    skill_id=version.skill_id,
                    path="references/extra.txt",
                    offset=0,
                    limit_bytes=64,
                )
            )
    assert objects.calls == []


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
        object_key=None,
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
        None,
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


def test_execution_service_rejects_null_object_key_version() -> None:
    """即使异常数据导致 object_key=None 进入脚本执行，也必须安全返回 skill_version_unavailable。"""
    script = SkillScriptDefinition(
        path="scripts/run.py",
        label="运行",
        purpose="防御测试",
        parameters={"type": "object"},
    )
    manifest = SkillPackageManifest(
        slug="null-key-script",
        name="异常无包版本",
        description="测试防御",
        guide="正文",
        scripts=(script,),
        files=(
            PackageResource(path="SKILL.md", size_bytes=6, sha256="a" * 64, text=True),
            PackageResource(path="scripts/run.py", size_bytes=1, sha256="b" * 64, text=True),
        ),
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="3.12",
        python_implementation="cpython",
        items=(),
        checked_at=NOW,
    )
    version = SkillVersion(
        id="skillver_null_key",
        skill_id="skill_null_key",
        ordinal=1,
        label="v1",
        manifest=manifest,
        object_key=None,
        package_sha256="c" * 64,
        dependency_requirements=(),
        dependency_check=check,
        created_by="usr_owner",
        created_at=NOW,
    )
    entry = RunSkillEntry(
        skill_id=version.skill_id,
        slug=manifest.slug,
        name=manifest.name,
        description=manifest.description,
        version_id=version.id,
        version_label="v1",
        requires_approval=False,
        scope=SkillScope.COMPANY,
    )

    class _SkillsStub:
        async def prepare_script_target(self, snapshot: RunSkillSnapshot, **kwargs: Any) -> Any:
            del snapshot, kwargs
            return entry, version, script, check

    class _UnusedFiles:
        async def get_many(self, *_args: Any) -> tuple[object, ...]:
            raise AssertionError("object_key=None 防御应在拉取输入文件前立即拦截")

    service = SkillExecutionService(
        skills=cast(Any, _SkillsStub()),
        files=cast(Any, _UnusedFiles()),
        repository=cast(Any, object()),
        object_store=cast(Any, _StrictUnusedObjects()),
        runner=cast(Any, object()),
        clock=lambda: NOW,
    )
    context = ToolExecutionContext(
        owner_id="usr_owner",
        chat_session_id="ses_1",
        project_id=None,
        run_id="run_1",
        execution_epoch=1,
        auth_session_id="auth_owner",
        deadline_at=NOW + timedelta(minutes=2),
        tool_call_record_id="call_1",
        skill_snapshot=RunSkillSnapshot(skills=(entry,), selected_skill_ids=(entry.skill_id,)),
    )
    result = asyncio.run(
        service.execute(
            {
                "skill_id": version.skill_id,
                "script_path": script.path,
                "parameters": {},
                "input_file_ids": [],
            },
            context,
        )
    )
    assert result.outcome.status is ToolOutcomeStatus.ERROR
    assert result.outcome.code == "skill_version_unavailable"


def _service(repository: _Repository, objects: _StrictUnusedObjects) -> SkillService:
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
    object_key: str | None,
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
            role=UserRole.MEMBER,
        ),
        session_id="auth_owner",
    )
