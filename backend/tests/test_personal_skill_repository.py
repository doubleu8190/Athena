"""个人 Skill 的 SQLite 事务、归属、版本和 Run 快照链路测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from adapter.auth.model import AuthSessionModel, UserModel
from adapter.base import Base
from adapter.skill.model.skill import SkillModel, SkillVersionModel
from adapter.skill.repository import SqlAlchemySkillRepository, build_run_skill_snapshot
from app.skill.service import SkillService
from domain.auth import AuthContext, User, UserRole, UserStatus
from domain.skill import (
    DependencyCheck,
    DependencyCheckStatus,
    PackageResource,
    SkillConflictError,
    SkillNotFoundError,
    SkillPackageManifest,
    SkillScope,
)


# 使用相对当前 UTC 时间作为测试基准时钟，避免硬编码历史日期导致认证会话随时间老化过期。
def _current_utc() -> datetime:
    """返回相对当前系统时间的 UTC 时间戳，保证测试会话永不过期。"""
    return datetime.now(UTC)


NOW = _current_utc()


class _MemoryObjects:
    """只实现个人 Markdown 发布所需的不可变对象操作。"""

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
    """为服务构造器提供不会参与个人流程的认证读取。"""

    async def get_current_session_user(
        self,
        user_id: str,
        session_id: str,
        now: datetime,
    ) -> User | None:
        del user_id, session_id, now
        return None


def test_personal_skill_owner_crud_idempotency_and_snapshot_history(
    tmp_path: Path,
) -> None:
    """双用户同内容隔离、幂等发布、CAS、删除和历史快照在真实 ORM 上收敛。"""
    asyncio.run(_run_personal_skill_scenario(tmp_path))


def test_admin_package_hash_is_non_unique_but_request_key_remains_idempotent(
    tmp_path: Path,
) -> None:
    """放开内容哈希后，不同请求键可重复导入，同键仍返回原版本。"""
    asyncio.run(_run_admin_package_hash_scenario(tmp_path))


def test_personal_replay_returns_current_identity_without_appending(
    tmp_path: Path,
) -> None:
    """旧请求键在后续发布或启停后重放，返回当前详情且不追加版本。"""
    asyncio.run(_run_personal_replay_current_scenario(tmp_path))


async def _run_personal_skill_scenario(tmp_path: Path) -> None:
    """执行完整异步场景，便于单一测试内保持清晰的事务边界。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'skills.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=cast(
                        Any,
                        [
                            UserModel.__table__,
                            AuthSessionModel.__table__,
                            SkillModel.__table__,
                            SkillVersionModel.__table__,
                        ],
                    ),
                )
            )
        actor_a, actor_b = await _seed_users(factory)
        repository = SqlAlchemySkillRepository(factory)
        sequence = count(1)
        service = SkillService(
            repository,
            cast(Any, _MemoryObjects()),
            _AuthReader(),
            id_factory=lambda prefix: f"{prefix}_{next(sequence)}",
            clock=lambda: NOW,
        )

        created_a = await service.create_personal(
            actor_a,
            name="周报 A",
            description="整理事实",
            guide="只整理用户提供的事实。",
            idempotency_key="create-a",
        )
        created_b = await service.create_personal(
            actor_b,
            name="周报 B",
            description="整理事实",
            guide="只整理用户提供的事实。",
            idempotency_key="create-b",
        )
        assert created_a.skill.scope is SkillScope.PERSONAL
        assert created_a.skill.owner_id == actor_a.user.id
        assert created_b.skill.owner_id == actor_b.user.id
        assert created_a.versions[0].package_sha256 == created_b.versions[0].package_sha256

        await _insert_public_skill(factory)
        public_entries = await repository.get_public_directory_entries()
        assert [(s.id, v.id) for s, v in public_entries] == [
            ("skill_public", "skillver_public")
        ]
        user_entries = await repository.get_user_directory_entries(actor_a.user.id)
        assert {(s.id, v.id) for s, v in user_entries} == {
            ("skill_public", "skillver_public"),
            (created_a.skill.id, created_a.versions[0].id),
        }
        assert [item.id for item in await service.list_personal(actor_a)] == [
            created_a.skill.id
        ]
        assert await repository.get_personal_skill(actor_b.user.id, created_a.skill.id) is None
        with pytest.raises(SkillNotFoundError):
            await service.get_personal(actor_b, created_a.skill.id)

        async with factory() as session:
            snapshot = await build_run_skill_snapshot(
                session,
                (),
                owner_id=actor_a.user.id,
            )
        by_scope = {(item.slug, item.scope) for item in snapshot.skills}
        assert ("public-shared", SkillScope.PUBLIC) in by_scope
        assert (created_a.skill.slug, SkillScope.PERSONAL) in by_scope
        assert (created_b.skill.slug, SkillScope.PERSONAL) not in by_scope
        with pytest.raises(SkillConflictError, match="skill_selection_unavailable"):
            async with factory() as session:
                await build_run_skill_snapshot(
                    session,
                    (created_b.skill.id,),
                    owner_id=actor_a.user.id,
                )

        published = await service.publish_personal(
            actor_a,
            created_a.skill.id,
            name="周报 A v2",
            description="整理并校验事实",
            guide="先校验事实，再整理周报。",
            expected_revision=created_a.skill.revision,
            idempotency_key="publish-a",
        )
        replayed = await service.publish_personal(
            actor_a,
            created_a.skill.id,
            name="周报 A v2",
            description="整理并校验事实",
            guide="先校验事实，再整理周报。",
            expected_revision=created_a.skill.revision,
            idempotency_key="publish-a",
        )
        assert replayed.versions[0].id == published.versions[0].id
        assert published.skill.revision == 2
        assert published.skill.enabled is True
        with pytest.raises(SkillConflictError, match="idempotency_conflict"):
            await service.publish_personal(
                actor_a,
                created_a.skill.id,
                name="冲突内容",
                description="冲突内容",
                guide="冲突正文",
                expected_revision=1,
                idempotency_key="publish-a",
            )
        with pytest.raises(SkillConflictError, match="skill_revision_conflict"):
            await service.publish_personal(
                actor_a,
                created_a.skill.id,
                name="旧修订",
                description="旧修订",
                guide="旧修订正文",
                expected_revision=1,
                idempotency_key="publish-stale",
            )

        async with factory() as session:
            historical_snapshot = await build_run_skill_snapshot(
                session,
                (created_a.skill.id,),
                owner_id=actor_a.user.id,
            )
            version_count = await session.scalar(
                select(func.count(SkillVersionModel.id)).where(
                    SkillVersionModel.skill_id == created_a.skill.id
                )
            )
        assert version_count == 2

        disabled = await service.set_personal_enabled(
            actor_a,
            created_a.skill.id,
            enabled=False,
            expected_revision=2,
        )
        assert disabled.skill.enabled is False
        assert disabled.skill.revision == 3
        assert created_a.skill.id not in {
            item.id for item in await service.user_directory(actor_a)
        }
        enabled = await service.set_personal_enabled(
            actor_a,
            created_a.skill.id,
            enabled=True,
            expected_revision=3,
        )
        assert enabled.skill.enabled is True
        assert enabled.skill.revision == 4

        await service.delete_personal(
            actor_a,
            created_a.skill.id,
            expected_revision=4,
        )
        await service.delete_personal(
            actor_a,
            created_a.skill.id,
            expected_revision=4,
        )
        with pytest.raises(SkillNotFoundError):
            await service.create_personal(
                actor_a,
                name="周报 A",
                description="整理事实",
                guide="只整理用户提供的事实。",
                idempotency_key="create-a",
            )
        assert await repository.get_personal_skill(actor_a.user.id, created_a.skill.id) is None
        resolved, old_version = await service.resolve_run_entry(
            historical_snapshot,
            created_a.skill.id,
        )
        assert resolved.version_id == published.versions[0].id
        assert old_version.id == published.versions[0].id
    finally:
        await engine.dispose()


async def _run_personal_replay_current_scenario(tmp_path: Path) -> None:
    """验证个人旧请求重放跟随当前指针，但不改变历史版本或新增版本。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'replay-skills.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=cast(
                        Any,
                        [
                            UserModel.__table__,
                            AuthSessionModel.__table__,
                            SkillModel.__table__,
                            SkillVersionModel.__table__,
                        ],
                    ),
                )
            )
        actor, _ = await _seed_users(factory)
        repository = SqlAlchemySkillRepository(factory)
        objects = _MemoryObjects()
        sequence = count(1)
        service = SkillService(
            repository,
            cast(Any, objects),
            _AuthReader(),
            id_factory=lambda prefix: f"{prefix}_{next(sequence)}",
            clock=lambda: NOW,
        )

        created = await service.create_personal(
            actor,
            name="名称 v1",
            description="描述 v1",
            guide="正文 v1",
            idempotency_key="create-current",
        )
        await service.publish_personal(
            actor,
            created.skill.id,
            name="名称 v2",
            description="描述 v2",
            guide="正文 v2",
            expected_revision=1,
            idempotency_key="publish-v2",
        )
        current = await service.publish_personal(
            actor,
            created.skill.id,
            name="名称 v3",
            description="描述 v3",
            guide="正文 v3",
            expected_revision=2,
            idempotency_key="publish-v3",
        )

        replayed_v2 = await service.publish_personal(
            actor,
            created.skill.id,
            name="名称 v2",
            description="描述 v2",
            guide="正文 v2",
            expected_revision=1,
            idempotency_key="publish-v2",
        )
        assert replayed_v2.skill.revision == 3
        assert replayed_v2.skill.current_version_id == current.versions[0].id
        assert replayed_v2.versions[0].id == current.versions[0].id
        assert replayed_v2.versions[0].manifest.name == "名称 v3"
        assert replayed_v2.versions[0].manifest.guide == "正文 v3"
        assert replayed_v2.skill.name == "名称 v3"
        assert replayed_v2.skill.current_version_label == "v3"

        disabled = await service.set_personal_enabled(
            actor,
            created.skill.id,
            enabled=False,
            expected_revision=3,
        )
        assert disabled.skill.revision == 4
        replayed_after_toggle = await service.publish_personal(
            actor,
            created.skill.id,
            name="名称 v2",
            description="描述 v2",
            guide="正文 v2",
            expected_revision=1,
            idempotency_key="publish-v2",
        )
        assert replayed_after_toggle.skill.revision == 4
        assert replayed_after_toggle.skill.enabled is False
        assert replayed_after_toggle.skill.current_version_id == current.versions[0].id
        assert replayed_after_toggle.versions[0].id == current.versions[0].id

        async with factory() as session:
            version_count = await session.scalar(
                select(func.count(SkillVersionModel.id)).where(
                    SkillVersionModel.skill_id == created.skill.id
                )
            )
        assert version_count == 3
        assert len(objects.values) == 3
    finally:
        await engine.dispose()


async def _seed_users(
    factory: async_sessionmaker[Any],
) -> tuple[AuthContext, AuthContext]:
    """写入两个有效用户及登录会话并返回认证上下文。"""
    values = (
        ("usr_a", "a@example.test", "auth_a"),
        ("usr_b", "b@example.test", "auth_b"),
    )
    async with factory() as session:
        async with session.begin():
            for user_id, email, session_id in values:
                session.add(
                    UserModel(
                        id=user_id,
                        email=email,
                        password_hash="unused",
                        display_name=None,
                        status=UserStatus.ACTIVE.value,
                        role=UserRole.USER.value,
                        created_at=NOW,
                        updated_at=NOW,
                        deleted_time=None,
                    )
                )
                session.add(
                    AuthSessionModel(
                        id=session_id,
                        user_id=user_id,
                        token_hash=(user_id[-1] * 64),
                        expires_at=NOW + timedelta(days=1),
                        revoked_time=None,
                        created_at=NOW,
                    )
                )
    contexts: list[AuthContext] = []
    for user_id, email, session_id in values:
        contexts.append(
            AuthContext(
                user=User(
                    id=user_id,
                    email=email,
                    password_hash="unused",
                    display_name=None,
                    status=UserStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                    role=UserRole.USER,
                ),
                session_id=session_id,
            )
        )
    return contexts[0], contexts[1]


async def _insert_public_skill(factory: async_sessionmaker[Any]) -> None:
    """直接写入一个启用公共 Skill，用于验证目录合并和同名来源。"""
    manifest = SkillPackageManifest(
        slug="public-shared",
        name="公共周报",
        description="公共说明",
        guide="公共指南",
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=12,
                sha256="a" * 64,
                text=True,
            ),
        ),
    )
    check = DependencyCheck(
        status=DependencyCheckStatus.SATISFIED,
        python_version="not-checked",
        python_implementation="test",
        items=(),
        checked_at=NOW,
    )
    async with factory() as session:
        async with session.begin():
            skill = SkillModel(
                id="skill_public",
                slug=manifest.slug,
                scope=SkillScope.PUBLIC.value,
                owner_id=None,
                deleted_at=None,
                current_version_id=None,
                enabled=True,
                script_requires_approval=False,
                revision=1,
                created_by=None,
                created_at=NOW,
                updated_at=NOW,
            )
            session.add(skill)
            await session.flush()
            version = SkillVersionModel(
                id="skillver_public",
                skill_id=skill.id,
                ordinal=1,
                label="v1",
                guide=manifest.guide,
                manifest=manifest.model_dump(mode="json"),
                object_key="skills/aa/public.zip",
                package_sha256="b" * 64,
                dependency_requirements=[],
                dependency_check=check.model_dump(mode="json"),
                created_by=None,
                request_operation="import",
                idempotency_key="public-import",
                request_fingerprint="c" * 64,
                request_target_skill_id=None,
                created_at=NOW,
            )
            session.add(version)
            await session.flush()
            skill.current_version_id = version.id


async def _run_admin_package_hash_scenario(tmp_path: Path) -> None:
    """验证公共版本内容哈希不再作为跨请求去重约束。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin-skills.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync,
                    tables=cast(
                        Any,
                        [
                            UserModel.__table__,
                            AuthSessionModel.__table__,
                            SkillModel.__table__,
                            SkillVersionModel.__table__,
                        ],
                    ),
                )
            )
        admin = await _seed_admin(factory)
        repository = SqlAlchemySkillRepository(factory)
        manifest = SkillPackageManifest(
            slug="public-repeat",
            name="重复包",
            description="同一内容可独立发布",
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
            python_implementation="test",
            items=(),
            checked_at=NOW,
        )
        skill, _ = await repository.create_skill(
            skill_id="skill_public_repeat",
            version_id="skillver_public_v1",
            label="v1",
            manifest=manifest,
            object_key="skills/aa/public-v1.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="import-key",
            request_fingerprint="1" * 64,
            created_at=NOW,
        )
        import_replay_skill, import_replay_version = await repository.create_skill(
            skill_id="skill_public_unused",
            version_id="skillver_public_unused",
            label="v1",
            manifest=manifest,
            object_key="skills/aa/public-unused.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="import-key",
            request_fingerprint="1" * 64,
            created_at=NOW,
        )
        assert import_replay_skill.id == skill.id
        assert import_replay_version.id == "skillver_public_v1"
        with pytest.raises(SkillConflictError, match="skill_slug_exists"):
            await repository.create_skill(
                skill_id="skill_public_duplicate",
                version_id="skillver_public_duplicate",
                label="v1",
                manifest=manifest,
                object_key="skills/aa/public-duplicate.zip",
                package_sha256="a" * 64,
                dependency_check=check,
                actor=admin,
                idempotency_key="import-key-two",
                request_fingerprint="4" * 64,
                created_at=NOW,
            )
        second_manifest = manifest.model_copy(update={"slug": "public-repeat-two"})
        independent_skill, _ = await repository.create_skill(
            skill_id="skill_public_independent",
            version_id="skillver_public_independent",
            label="v1",
            manifest=second_manifest,
            object_key="skills/aa/public-independent.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="import-key-independent",
            request_fingerprint="5" * 64,
            created_at=NOW,
        )
        assert independent_skill.id == "skill_public_independent"
        second = await repository.add_version(
            skill.id,
            version_id="skillver_public_v2",
            label="v2",
            manifest=manifest,
            object_key="skills/aa/public-v2.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="version-two",
            request_fingerprint="2" * 64,
            created_at=NOW,
        )
        third = await repository.add_version(
            skill.id,
            version_id="skillver_public_v3",
            label="v3",
            manifest=manifest,
            object_key="skills/aa/public-v3.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="version-three",
            request_fingerprint="3" * 64,
            created_at=NOW,
        )
        replay = await repository.add_version(
            skill.id,
            version_id="skillver_unused",
            label="v2",
            manifest=manifest,
            object_key="skills/aa/unused.zip",
            package_sha256="a" * 64,
            dependency_check=check,
            actor=admin,
            idempotency_key="version-two",
            request_fingerprint="2" * 64,
            created_at=NOW,
        )
        assert second is not None and third is not None
        assert replay is not None and replay.id == second.id
        async with factory() as session:
            total_for_skill = await session.scalar(
                select(func.count(SkillVersionModel.id)).where(
                    SkillVersionModel.skill_id == skill.id
                )
            )
            total_for_hash = await session.scalar(
                select(func.count(SkillVersionModel.id)).where(
                    SkillVersionModel.package_sha256 == "a" * 64
                )
            )
        assert total_for_skill == 3
        assert total_for_hash == 4
    finally:
        await engine.dispose()


async def _seed_admin(factory: async_sessionmaker[Any]) -> AuthContext:
    """写入一个管理员及其有效登录会话。"""
    user = User(
        id="usr_admin",
        email="admin@example.test",
        password_hash="unused",
        display_name=None,
        status=UserStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
        role=UserRole.ADMIN,
    )
    async with factory() as session:
        async with session.begin():
            session.add(
                UserModel(
                    id=user.id,
                    email=user.email,
                    password_hash=user.password_hash,
                    display_name=None,
                    status=user.status.value,
                    role=user.role.value,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_time=None,
                )
            )
            session.add(
                AuthSessionModel(
                    id="auth_admin",
                    user_id=user.id,
                    token_hash="d" * 64,
                    expires_at=NOW + timedelta(days=1),
                    revoked_time=None,
                    created_at=NOW,
                )
            )
    return AuthContext(user=user, session_id="auth_admin")
