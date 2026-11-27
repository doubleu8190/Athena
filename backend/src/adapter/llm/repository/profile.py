"""模型配置版本的 PostgreSQL 持久化。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.repository.transaction_guard import verify_active_session_user
from adapter.llm.converter.profile import profile_to_domain
from adapter.llm.model.profile import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from adapter.subagent.model import SubagentModel
from common.date import utc_now
from common.ids import new_id
from common.unset import UNSET
from domain.auth import AuthContext, UserRole
from domain.llm.model.compression_policy import (
    CompressionPolicyInput,
    CompressionPolicyVersion,
)
from domain.llm.model.profile import (
    ModelProfileInput,
    ModelProfileUpdateInput,
    ModelProfileVersion,
    RuntimeModelProfile,
    validate_model_profile_api_key,
    validate_model_profile_fields,
)
from domain.llm.model.profile_errors import (
    ModelProfileAuthorizationError,
    ModelProfileConflictError,
    ModelProfileError,
    ModelProfileNotFoundError,
)
from domain.llm.profile_ports import CompressionPolicyRepository, ModelProfileRepository
from domain.mcp import McpSecretError, McpSecretPurpose
from infrastructure.security.mcp_secrets import McpSecretCipher


class SqlAlchemyModelProfileRepository(ModelProfileRepository, CompressionPolicyRepository):
    """模型版本写入时复核管理员，并在调用时解封数据库凭据。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        secret_cipher: McpSecretCipher | None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        """保存数据库会话工厂和可替换时钟。"""
        self._session_factory = session_factory
        self._secret_cipher = secret_cipher
        self._clock = clock

    async def create(self, actor: AuthContext, value: ModelProfileInput) -> ModelProfileVersion:
        """复核管理员后创建不可变模型版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                if self._secret_cipher is None:
                    raise ModelProfileError("llm_secret_key_missing")
                now = self._clock()
                profile_id = new_id("llmprof")
                # 先完成凭据信封加密再构造非空 ORM 行，确保库内不存在空密文半成品记录。
                try:
                    sealed = self._secret_cipher.seal(
                        profile_id, McpSecretPurpose.LLM_API_KEY, {"api_key": value.api_key}
                    )
                except McpSecretError:
                    raise ModelProfileError("llm_model_credential_invalid") from None
                row = ModelProfileVersionModel(
                    id=profile_id,
                    name=value.name,
                    protocol="chat_completions",
                    base_url=value.base_url,
                    model=value.model,
                    api_key_ciphertext=sealed.ciphertext,
                    api_key_nonce=sealed.nonce,
                    api_key_key_version=sealed.key_version,
                    context_window_tokens=value.context_window_tokens,
                    tokenizer_encoding=value.tokenizer_encoding,
                    supports_stream=value.supports_stream,
                    supports_tools=value.supports_tools,
                    supports_parallel_tool_calls=value.supports_parallel_tool_calls,
                    max_output_tokens=value.max_output_tokens,
                    request_timeout_seconds=value.request_timeout_seconds,
                    execution_timeout_seconds=value.execution_timeout_seconds,
                    max_concurrency=value.max_concurrency,
                    temperature=value.temperature,
                    enabled=True,
                    visible_to_users=value.visible_to_users,
                    scenario_label=value.scenario_label,
                    scenario_description=value.scenario_description,
                    supports_vision=value.supports_vision,
                    deleted_at=None,
                    version=1,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError:
                    raise ModelProfileConflictError() from None
                return profile_to_domain(row)

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]:
        """无锁只读会话内复核管理员身份，按创建顺序读取未软删除且不含秘密的配置。"""
        async with self._session_factory() as session:
            await self._require_admin(session, actor)
            rows = (
                await session.scalars(
                    select(ModelProfileVersionModel)
                    .where(ModelProfileVersionModel.deleted_at.is_(None))
                    .order_by(
                        ModelProfileVersionModel.created_at,
                        ModelProfileVersionModel.id,
                    )
                )
            ).all()
            return tuple(profile_to_domain(row) for row in rows)

    async def list_with_default(
        self, actor: AuthContext
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        """同一无锁只读会话内复核管理员并读取未软删除配置列表与当前默认版本标识。"""
        async with self._session_factory() as session:
            await self._require_admin(session, actor)
            rows = (
                await session.scalars(
                    select(ModelProfileVersionModel)
                    .where(ModelProfileVersionModel.deleted_at.is_(None))
                    .order_by(
                        ModelProfileVersionModel.created_at,
                        ModelProfileVersionModel.id,
                    )
                )
            ).all()
            default_id = await session.scalar(
                select(DefaultModelProfileModel.profile_id)
                .join(
                    ModelProfileVersionModel,
                    ModelProfileVersionModel.id == DefaultModelProfileModel.profile_id,
                )
                .where(
                    DefaultModelProfileModel.singleton_id == 1,
                    ModelProfileVersionModel.deleted_at.is_(None),
                )
            )
            return tuple(profile_to_domain(row) for row in rows), default_id

    async def list_user_visible_with_default(
        self,
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        """面向普通用户读取可对话、已启用、对用户可见且未软删除的模型目录及默认模型标识。"""
        async with self._session_factory() as session:
            # 与 Run 提交校验保持一致：仅暴露支持流式与工具调用的启用可见模型，
            # 避免前端选出不可提交的模型。
            rows = (
                await session.scalars(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.enabled.is_(True),
                        ModelProfileVersionModel.visible_to_users.is_(True),
                        ModelProfileVersionModel.deleted_at.is_(None),
                        ModelProfileVersionModel.supports_stream.is_(True),
                        ModelProfileVersionModel.supports_tools.is_(True),
                    )
                    .order_by(
                        ModelProfileVersionModel.created_at,
                        ModelProfileVersionModel.id,
                    )
                )
            ).all()
            default_id = await session.scalar(
                select(DefaultModelProfileModel.profile_id).where(
                    DefaultModelProfileModel.singleton_id == 1
                )
            )
            items = tuple(profile_to_domain(row) for row in rows)
            visible_ids = {item.id for item in items}
            # 若默认模型当前不可见、已停用、已软删除或不满足对话能力/凭据前置条件，
            # 则不向普通用户暴露该默认 ID。
            effective_default_id = default_id if default_id in visible_ids else None
            return items, effective_default_id

    async def set_enabled(
        self, actor: AuthContext, profile_id: str, enabled: bool
    ) -> ModelProfileVersion:
        """复核管理员并以单条原子 UPDATE 切换单个未软删除版本的可用状态。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                # 单条原子 UPDATE ... RETURNING 避免先 SELECT FOR UPDATE 再 UPDATE 的额外往返，
                # 同时与并发读取该行的 FOR SHARE 事务在行锁层面保持互斥。
                now = self._clock()
                row = await session.scalar(
                    update(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == profile_id,
                        ModelProfileVersionModel.deleted_at.is_(None),
                    )
                    .values(
                        enabled=enabled,
                        version=ModelProfileVersionModel.version + 1,
                        updated_at=now,
                    )
                    .returning(ModelProfileVersionModel)
                    .execution_options(synchronize_session=False, populate_existing=True)
                )
                if row is None:
                    raise ModelProfileNotFoundError()
                return profile_to_domain(row)

    async def update(
        self,
        actor: AuthContext,
        profile_id: str,
        patch: ModelProfileUpdateInput,
    ) -> ModelProfileVersion:
        """在行锁事务内局部编辑模型配置，严格按约定保留或重加密凭据并复核全部不变量。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                row = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == profile_id,
                        ModelProfileVersionModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelProfileNotFoundError("model_profile_not_found")
                if patch.expected_version is not None and row.version != patch.expected_version:
                    raise ModelProfileConflictError("model_profile_version_conflict")

                # 将局部补丁与锁内当前行合并，统一复核所有领域不变量（含输出窗口 < 上下文窗口）。
                merged = validate_model_profile_fields(
                    name=row.name if patch.name is UNSET else patch.name,
                    base_url=row.base_url if patch.base_url is UNSET else patch.base_url,
                    model=row.model if patch.model is UNSET else patch.model,
                    context_window_tokens=(
                        row.context_window_tokens
                        if patch.context_window_tokens is UNSET
                        else patch.context_window_tokens
                    ),
                    tokenizer_encoding=(
                        row.tokenizer_encoding
                        if patch.tokenizer_encoding is UNSET
                        else patch.tokenizer_encoding
                    ),
                    supports_stream=(
                        bool(row.supports_stream)
                        if patch.supports_stream is UNSET
                        else patch.supports_stream
                    ),
                    supports_tools=(
                        bool(row.supports_tools)
                        if patch.supports_tools is UNSET
                        else patch.supports_tools
                    ),
                    supports_parallel_tool_calls=(
                        bool(row.supports_parallel_tool_calls)
                        if patch.supports_parallel_tool_calls is UNSET
                        else patch.supports_parallel_tool_calls
                    ),
                    max_output_tokens=(
                        row.max_output_tokens
                        if patch.max_output_tokens is UNSET
                        else patch.max_output_tokens
                    ),
                    request_timeout_seconds=(
                        row.request_timeout_seconds
                        if patch.request_timeout_seconds is UNSET
                        else patch.request_timeout_seconds
                    ),
                    execution_timeout_seconds=(
                        row.execution_timeout_seconds
                        if patch.execution_timeout_seconds is UNSET
                        else patch.execution_timeout_seconds
                    ),
                    max_concurrency=(
                        row.max_concurrency
                        if patch.max_concurrency is UNSET
                        else patch.max_concurrency
                    ),
                    temperature=(
                        row.temperature if patch.temperature is UNSET else patch.temperature
                    ),
                    enabled=bool(row.enabled) if patch.enabled is UNSET else patch.enabled,
                    visible_to_users=(
                        bool(row.visible_to_users)
                        if patch.visible_to_users is UNSET
                        else patch.visible_to_users
                    ),
                    scenario_label=(
                        row.scenario_label
                        if patch.scenario_label is UNSET
                        else patch.scenario_label
                    ),
                    scenario_description=(
                        row.scenario_description
                        if patch.scenario_description is UNSET
                        else patch.scenario_description
                    ),
                    supports_vision=(
                        bool(row.supports_vision)
                        if patch.supports_vision is UNSET
                        else patch.supports_vision
                    ),
                    require_output_strictly_less_than_context=True,
                )

                # 活跃引用保护（保持与 delete() 一致的锁序：先 ModelProfile，再默认/压缩策略指针）：
                # 1) 禁止通过局部编辑将当前默认模型降级为禁用或不支持流式/工具调用，
                #    否则会令所有普通用户提交默认对话立即不可用；
                if not merged.enabled or not merged.supports_stream or not merged.supports_tools:
                    default_row = await session.scalar(
                        select(DefaultModelProfileModel)
                        .where(DefaultModelProfileModel.singleton_id == 1)
                        .with_for_update()
                    )
                    if default_row is not None and default_row.profile_id == profile_id:
                        raise ModelProfileConflictError("model_profile_in_use_as_default")
                # 2) 禁止通过局部编辑禁用当前上下文压缩策略引用的模型，避免后台压缩任务失效。
                if not merged.enabled:
                    current_policy = await session.scalar(
                        select(CurrentCompressionPolicyModel)
                        .where(CurrentCompressionPolicyModel.singleton_id == 1)
                        .with_for_update()
                    )
                    if current_policy is not None:
                        compression_profile_id = await session.scalar(
                            select(
                                CompressionPolicyVersionModel.compression_model_profile_id
                            ).where(
                                CompressionPolicyVersionModel.id == current_policy.policy_version_id
                            )
                        )
                        if compression_profile_id == profile_id:
                            raise ModelProfileConflictError(
                                "model_profile_in_use_by_compression_policy"
                            )

                # 密钥保留边界：未传 api_key 或显式传 None 时严禁改动原加密凭据；
                # 仅当传入非空字符串时重新校验并加密替换。
                if patch.api_key is not UNSET and patch.api_key is not None:
                    validate_model_profile_api_key(patch.api_key)
                    if self._secret_cipher is None:
                        raise ModelProfileError("llm_secret_key_missing")
                    try:
                        sealed = self._secret_cipher.seal(
                            row.id,
                            McpSecretPurpose.LLM_API_KEY,
                            {"api_key": patch.api_key},
                        )
                    except McpSecretError:
                        raise ModelProfileError("llm_model_credential_invalid") from None
                    row.api_key_ciphertext = sealed.ciphertext
                    row.api_key_nonce = sealed.nonce
                    row.api_key_key_version = sealed.key_version

                row.name = merged.name
                row.base_url = merged.base_url
                row.model = merged.model
                row.context_window_tokens = merged.context_window_tokens
                row.tokenizer_encoding = merged.tokenizer_encoding
                row.supports_stream = merged.supports_stream
                row.supports_tools = merged.supports_tools
                row.supports_parallel_tool_calls = merged.supports_parallel_tool_calls
                row.max_output_tokens = merged.max_output_tokens
                row.request_timeout_seconds = merged.request_timeout_seconds
                row.execution_timeout_seconds = merged.execution_timeout_seconds
                row.max_concurrency = merged.max_concurrency
                row.temperature = merged.temperature
                row.enabled = merged.enabled
                row.visible_to_users = merged.visible_to_users
                row.scenario_label = merged.scenario_label
                row.scenario_description = merged.scenario_description
                row.supports_vision = merged.supports_vision
                row.version += 1
                row.updated_at = self._clock()
                await session.flush()
                return profile_to_domain(row)

    async def delete(self, actor: AuthContext, profile_id: str) -> None:
        """按统一锁序复核强引用保护后执行模型配置软删除，绝不物理删行以保持历史 Run 外键完好。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                # 锁序约定：先锁模型配置行，再锁默认指针 / 当前压缩策略指针 / 活跃子智能体，
                # 与 Run 提交锁序（ModelProfile -> DefaultModelProfile）严格一致以防死锁。
                row = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == profile_id,
                        ModelProfileVersionModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if row is None:
                    raise ModelProfileNotFoundError("model_profile_not_found")

                # 强引用保护 1：禁止删除当前默认模型。
                default_row = await session.scalar(
                    select(DefaultModelProfileModel)
                    .where(DefaultModelProfileModel.singleton_id == 1)
                    .with_for_update()
                )
                if default_row is not None and default_row.profile_id == profile_id:
                    raise ModelProfileConflictError("model_profile_in_use_as_default")

                # 强引用保护 2：禁止删除当前上下文压缩策略引用的模型。
                current_policy = await session.scalar(
                    select(CurrentCompressionPolicyModel)
                    .where(CurrentCompressionPolicyModel.singleton_id == 1)
                    .with_for_update()
                )
                if current_policy is not None:
                    compression_profile_id = await session.scalar(
                        select(CompressionPolicyVersionModel.compression_model_profile_id).where(
                            CompressionPolicyVersionModel.id == current_policy.policy_version_id
                        )
                    )
                    if compression_profile_id == profile_id:
                        raise ModelProfileConflictError(
                            "model_profile_in_use_by_compression_policy"
                        )

                # 强引用保护 3：禁止删除仍被未软删除子智能体显式绑定的模型。
                bound_subagent = await session.scalar(
                    select(SubagentModel.id)
                    .where(
                        SubagentModel.model_profile_id == profile_id,
                        SubagentModel.deleted_at.is_(None),
                    )
                    .limit(1)
                    .with_for_update()
                )
                if bound_subagent is not None:
                    raise ModelProfileConflictError("model_profile_in_use_by_subagent")

                now = self._clock()
                row.deleted_at = now
                row.enabled = False
                row.updated_at = now
                row.version += 1
                await session.flush()

    async def set_default(self, actor: AuthContext, profile_id: str) -> ModelProfileVersion:
        """复核管理员并将默认指向切换为启用且未软删除的版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                row = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == profile_id,
                        ModelProfileVersionModel.deleted_at.is_(None),
                    )
                    .with_for_update()
                )
                if (
                    row is None
                    or not row.enabled
                    or not row.supports_stream
                    or not row.supports_tools
                    or self._runtime(row) is None
                ):
                    raise ModelProfileNotFoundError()
                statement = pg_insert(DefaultModelProfileModel).values(
                    singleton_id=1, profile_id=profile_id
                )
                statement = statement.on_conflict_do_update(
                    index_elements=[DefaultModelProfileModel.singleton_id],
                    set_={"profile_id": profile_id},
                )
                await session.execute(statement)
                await session.flush()
                return profile_to_domain(row)

    async def get_default_runtime(self) -> RuntimeModelProfile | None:
        """读取当前启用且未软删除的默认版本，并在调用时解封数据库凭据。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelProfileVersionModel)
                .join(
                    DefaultModelProfileModel,
                    DefaultModelProfileModel.profile_id == ModelProfileVersionModel.id,
                )
                .where(
                    DefaultModelProfileModel.singleton_id == 1,
                    ModelProfileVersionModel.deleted_at.is_(None),
                )
            )
            runtime = self._runtime(row)
            if (
                runtime is None
                or not runtime.version.supports_stream
                or not runtime.version.supports_tools
            ):
                return None
            return runtime

    async def get_current_compression_policy(self) -> CompressionPolicyVersion | None:
        """读取当前压缩策略版本；未配置时返回 None。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(CompressionPolicyVersionModel)
                .join(
                    CurrentCompressionPolicyModel,
                    CurrentCompressionPolicyModel.policy_version_id
                    == CompressionPolicyVersionModel.id,
                )
                .where(CurrentCompressionPolicyModel.singleton_id == 1)
            )
            return None if row is None else _compression_policy_to_domain(row)

    async def get_compression_policy(self, policy_id: str) -> CompressionPolicyVersion | None:
        """读取 Run 已固定的新格式压缩策略快照。"""
        async with self._session_factory() as session:
            row = await session.get(CompressionPolicyVersionModel, policy_id)
            return None if row is None else _compression_policy_to_domain(row)

    async def set_compression_policy(
        self, actor: AuthContext, value: CompressionPolicyInput
    ) -> CompressionPolicyVersion:
        """创建不可变压缩策略版本并切换当前指向。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                # 仅校验压缩模型启用且未软删除状态，使用 FOR SHARE 允许并发策略提交，仍与禁用互斥。
                profile = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == value.compression_model_profile_id,
                        ModelProfileVersionModel.enabled.is_(True),
                        ModelProfileVersionModel.deleted_at.is_(None),
                    )
                    .with_for_update(read=True)
                )
                if profile is None or self._runtime(profile) is None:
                    raise ModelProfileNotFoundError()
                now = self._clock()
                row = CompressionPolicyVersionModel(
                    id=new_id("cmpol"),
                    compression_model_profile_id=profile.id,
                    trigger_tokens=value.trigger_tokens,
                    recent_ratio=value.recent_ratio,
                    recent_token_cap=value.recent_token_cap,
                    summary_ratio=value.summary_ratio,
                    summary_token_cap=value.summary_token_cap,
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                await session.merge(
                    CurrentCompressionPolicyModel(singleton_id=1, policy_version_id=row.id)
                )
                await session.flush()
                return _compression_policy_to_domain(row)

    async def get_default_id(self, actor: AuthContext) -> str | None:
        """无锁只读会话内复核管理员身份并返回当前未软删除的默认版本标识。"""
        async with self._session_factory() as session:
            await self._require_admin(session, actor)
            return await session.scalar(
                select(DefaultModelProfileModel.profile_id)
                .join(
                    ModelProfileVersionModel,
                    ModelProfileVersionModel.id == DefaultModelProfileModel.profile_id,
                )
                .where(
                    DefaultModelProfileModel.singleton_id == 1,
                    ModelProfileVersionModel.deleted_at.is_(None),
                )
            )

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
        """读取指定启用且未软删除版本并解封数据库凭据。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelProfileVersionModel).where(
                    ModelProfileVersionModel.id == profile_id,
                    ModelProfileVersionModel.enabled.is_(True),
                    ModelProfileVersionModel.deleted_at.is_(None),
                )
            )
            return self._runtime(row)

    def _runtime(self, row: ModelProfileVersionModel | None) -> RuntimeModelProfile | None:
        if row is None or not row.enabled or row.deleted_at is not None:
            return None
        if self._secret_cipher is None:
            return None
        try:
            secrets = self._secret_cipher.open(
                row.id,
                McpSecretPurpose.LLM_API_KEY,
                row.api_key_ciphertext,
                row.api_key_nonce,
                row.api_key_key_version,
            )
        except McpSecretError:
            return None
        api_key = secrets.get("api_key")
        if not api_key:
            return None
        return RuntimeModelProfile(version=profile_to_domain(row), api_key=api_key)

    async def _require_admin(self, session: AsyncSession, actor: AuthContext) -> None:
        """以单条无锁 JOIN 复核当前超管身份与有效登录会话（只读与写事务统一复用）。"""
        user = await verify_active_session_user(
            session,
            actor.user.id,
            actor.session_id,
            clock=self._clock,
        )
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise ModelProfileAuthorizationError()


def _compression_policy_to_domain(
    row: CompressionPolicyVersionModel,
) -> CompressionPolicyVersion:
    """将压缩策略版本 ORM 行映射为领域对象。"""
    return CompressionPolicyVersion(
        id=row.id,
        compression_model_profile_id=row.compression_model_profile_id,
        trigger_tokens=row.trigger_tokens,
        recent_ratio=row.recent_ratio,
        recent_token_cap=row.recent_token_cap,
        summary_ratio=row.summary_ratio,
        summary_token_cap=row.summary_token_cap,
        created_at=row.created_at,
    )


__all__ = ["SqlAlchemyModelProfileRepository"]
