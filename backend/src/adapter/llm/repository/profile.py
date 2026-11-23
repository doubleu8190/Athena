"""模型配置版本的 PostgreSQL 持久化。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adapter.auth.repository.transaction_guard import SqlAlchemyAuthTransactionGuard
from adapter.llm.converter.profile import profile_to_domain
from adapter.llm.model.profile import (
    CompressionPolicyVersionModel,
    CurrentCompressionPolicyModel,
    DefaultModelProfileModel,
    ModelProfileVersionModel,
)
from common.ids import new_id, now_utc
from domain.auth import AuthContext, UserRole
from domain.llm.model.compression_policy import (
    CompressionPolicyInput,
    CompressionPolicyVersion,
)
from domain.llm.model.profile import ModelProfileInput, ModelProfileVersion, RuntimeModelProfile
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
        clock: Callable[[], datetime] = now_utc,
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
                row = ModelProfileVersionModel(
                    id=new_id("llmprof"),
                    name=value.name,
                    protocol="chat_completions",
                    base_url=value.base_url,
                    model=value.model,
                    api_key_ciphertext=None,
                    api_key_nonce=None,
                    api_key_key_version=None,
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
                    created_at=self._clock(),
                )
                try:
                    sealed = self._secret_cipher.seal(
                        row.id, McpSecretPurpose.LLM_API_KEY, {"api_key": value.api_key}
                    )
                except McpSecretError:
                    raise ModelProfileError("llm_model_credential_invalid") from None
                row.api_key_ciphertext = sealed.ciphertext
                row.api_key_nonce = sealed.nonce
                row.api_key_key_version = sealed.key_version
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError:
                    raise ModelProfileConflictError() from None
                return profile_to_domain(row)

    async def list(self, actor: AuthContext) -> tuple[ModelProfileVersion, ...]:
        """复核管理员后按创建顺序读取不含秘密的配置。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                rows = (
                    await session.scalars(
                        select(ModelProfileVersionModel).order_by(
                            ModelProfileVersionModel.created_at,
                            ModelProfileVersionModel.id,
                        )
                    )
                ).all()
                return tuple(profile_to_domain(row) for row in rows)

    async def list_with_default(
        self, actor: AuthContext
    ) -> tuple[tuple[ModelProfileVersion, ...], str | None]:
        """同一事务内复核管理员并读取配置列表与当前默认版本标识，避免两次往返。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                rows = (
                    await session.scalars(
                        select(ModelProfileVersionModel).order_by(
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
                return tuple(profile_to_domain(row) for row in rows), default_id

    async def set_enabled(
        self, actor: AuthContext, profile_id: str, enabled: bool
    ) -> ModelProfileVersion:
        """复核管理员并以单条原子 UPDATE 切换单个版本的可用状态。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                # 单条原子 UPDATE ... RETURNING 避免先 SELECT FOR UPDATE 再 UPDATE 的额外往返，
                # 同时与并发读取该行的 FOR SHARE 事务在行锁层面保持互斥。
                row = await session.scalar(
                    update(ModelProfileVersionModel)
                    .where(ModelProfileVersionModel.id == profile_id)
                    .values(enabled=enabled)
                    .returning(ModelProfileVersionModel)
                    .execution_options(synchronize_session=False, populate_existing=True)
                )
                if row is None:
                    raise ModelProfileNotFoundError()
                return profile_to_domain(row)

    async def set_default(self, actor: AuthContext, profile_id: str) -> ModelProfileVersion:
        """复核管理员并将默认指向切换为启用版本。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                row = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(ModelProfileVersionModel.id == profile_id)
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
        """读取当前启用默认版本，并在调用时解封数据库凭据。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelProfileVersionModel)
                .join(
                    DefaultModelProfileModel,
                    DefaultModelProfileModel.profile_id == ModelProfileVersionModel.id,
                )
                .where(DefaultModelProfileModel.singleton_id == 1)
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
                # 仅校验压缩模型启用状态，使用 FOR SHARE 允许并发策略提交，仍与禁用互斥。
                profile = await session.scalar(
                    select(ModelProfileVersionModel)
                    .where(
                        ModelProfileVersionModel.id == value.compression_model_profile_id,
                        ModelProfileVersionModel.enabled.is_(True),
                    )
                    .with_for_update(read=True)
                )
                if profile is None or self._runtime(profile) is None:
                    raise ModelProfileNotFoundError()
                now = self._clock()
                row = CompressionPolicyVersionModel(
                    id=new_id("cmpol"),
                    compression_model_profile_id=profile.id,
                    # 历史列只用于滚动升级兼容，新实例只读取绝对 Token 阈值。
                    trigger_ratio=0.8,
                    trigger_tokens=value.trigger_tokens,
                    recent_ratio=value.recent_ratio,
                    recent_token_cap=value.recent_token_cap,
                    summary_ratio=value.summary_ratio,
                    summary_token_cap=value.summary_token_cap,
                    prompt_template_version="context-summary-v1",
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                await session.merge(
                    CurrentCompressionPolicyModel(singleton_id=1, policy_version_id=row.id)
                )
                await session.flush()
                result = _compression_policy_to_domain(row)
                if result is None:
                    raise RuntimeError("llm_compression_policy_projection_invalid")
                return result

    async def get_default_id(self, actor: AuthContext) -> str | None:
        """复核管理员后返回当前默认版本标识。"""
        async with self._session_factory() as session:
            async with session.begin():
                await self._require_admin(session, actor)
                return await session.scalar(
                    select(DefaultModelProfileModel.profile_id).where(
                        DefaultModelProfileModel.singleton_id == 1
                    )
                )

    async def get_runtime(self, profile_id: str) -> RuntimeModelProfile | None:
        """读取指定启用版本并解封数据库凭据。"""
        async with self._session_factory() as session:
            row = await session.scalar(
                select(ModelProfileVersionModel).where(
                    ModelProfileVersionModel.id == profile_id,
                    ModelProfileVersionModel.enabled.is_(True),
                )
            )
            return self._runtime(row)

    def _runtime(self, row: ModelProfileVersionModel | None) -> RuntimeModelProfile | None:
        if row is None or not row.enabled:
            return None
        if (
            self._secret_cipher is None
            or row.api_key_ciphertext is None
            or row.api_key_nonce is None
            or row.api_key_key_version is None
        ):
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
        """在写事务中锁定并复核当前管理员身份。"""
        guard = SqlAlchemyAuthTransactionGuard(session, clock=self._clock)
        user = await guard.lock_current_session_user(actor.user.id, actor.session_id)
        if user is None or user.role is not UserRole.SUPER_ADMIN:
            raise ModelProfileAuthorizationError()


def _compression_policy_to_domain(
    row: CompressionPolicyVersionModel,
) -> CompressionPolicyVersion | None:
    """只投影完整的新格式策略，旧浮点策略安全视为未配置。"""
    if row.trigger_tokens is None or row.recent_token_cap is None or row.summary_token_cap is None:
        return None
    return CompressionPolicyVersion(
        id=row.id,
        compression_model_profile_id=row.compression_model_profile_id,
        trigger_tokens=row.trigger_tokens,
        recent_ratio=row.recent_ratio,
        recent_token_cap=row.recent_token_cap,
        summary_ratio=row.summary_ratio,
        summary_token_cap=row.summary_token_cap,
        prompt_template_version=row.prompt_template_version,
        created_at=row.created_at,
    )


__all__ = ["SqlAlchemyModelProfileRepository"]
