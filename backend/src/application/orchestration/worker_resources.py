"""签发执行授权并协调 Worker 资源的存取和完整性校验。"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from domain.orchestration.worker_resource_entities import (
    WorkerExecutionResourceContext,
    WorkerExecutionResourceScope,
    WorkerResourceReference,
)
from domain.orchestration.worker_entities import WorkerDispatchStart

from .worker_execution_grants import (
    WorkerExecutionGrantClaims,
    WorkerExecutionGrantSigner,
)


@dataclass(frozen=True, slots=True)
class WorkerResourceWriteResult:
    """描述已持久写入或幂等重放的资源。"""

    reference: WorkerResourceReference
    disposition: Literal["created", "replayed"]


@dataclass(frozen=True, slots=True)
class WorkerResourceDownload:
    """描述已授权下载的资源和服务端文件位置。"""

    reference: WorkerResourceReference
    path: Path


class WorkerResourceRepository(Protocol):
    """为授权服务提供执行身份查询和原子资源持久化。"""

    async def load_execution_resource_context(
        self, execution_id: str
    ) -> WorkerExecutionResourceContext: ...

    async def load_execution_resource_context_for_dispatch(
        self, execution_id: str
    ) -> WorkerExecutionResourceContext: ...

    async def resolve_read_resource_ids(
        self, *, run_id: str, candidate_ids: tuple[str, ...]
    ) -> tuple[str, ...]: ...

    async def validate_current_execution(
        self, claims: WorkerExecutionGrantClaims
    ) -> WorkerExecutionResourceScope | None: ...

    async def store_resource(
        self,
        *,
        scope: WorkerExecutionResourceScope,
        resource_id: str,
        media_type: str,
        expected_sha256: str,
        file_name: str | None,
        chunks: AsyncIterable[bytes],
        max_bytes: int,
    ) -> WorkerResourceWriteResult: ...

    async def load_resource(
        self,
        *,
        scope: WorkerExecutionResourceScope,
        resource_id: str,
    ) -> WorkerResourceDownload | None: ...


class WorkerResourceService:
    """签发受限授权并验证跨进程资源请求。"""

    def __init__(
        self,
        repository: WorkerResourceRepository,
        signer: WorkerExecutionGrantSigner,
        *,
        max_bytes: int,
    ) -> None:
        """初始化资源用例。

        参数：
        - `repository`（WorkerResourceRepository）：校验执行身份和持久化资源的应用端口。
        - `signer`（WorkerExecutionGrantSigner）：只由 Athena 持有签名密钥的授权签名器。
        - `max_bytes`（int）：单资源最大字节数，必须为正整数。

        返回：无。

        异常：ValueError 表示资源大小上限无效。
        """
        if max_bytes < 1:
            raise ValueError("worker_resource_max_bytes_invalid")
        self._repository = repository
        self._signer = signer
        self._max_bytes = max_bytes

    @property
    def max_bytes(self) -> int:
        """返回该服务对单资源流设置的字节上限。"""
        return self._max_bytes

    async def issue_execution_grant(self, execution_id: str) -> str:
        """为当前分配的 Execution 签发绑定到其冻结输入和截止时间的授权。

        参数：
        - `execution_id`（str）：Athena 已分配并正在执行的远程尝试。

        返回：Worker 可转交资源接口的不透明签名 token。

        异常：ValueError 表示执行不活跃或输入引用了其他 Run 的资源。
        """
        context = await self._repository.load_execution_resource_context(execution_id)
        candidate_ids = tuple(sorted(_resource_ids(context.task_input)))
        readable_ids = await self._repository.resolve_read_resource_ids(
            run_id=context.scope.run_id,
            candidate_ids=candidate_ids,
        )
        if context.scope.expires_at is None:
            raise ValueError("worker_execution_grant_expiry_missing")
        return self._signer.issue(
            replace(context.scope, read_resource_ids=readable_ids)
        )

    async def prepare_execution_grant_context(
        self, execution_id: str
    ) -> WorkerExecutionResourceContext:
        """在执行计时开始前异步准备签发授权所需的只读资源范围。

        参数：
        - `execution_id`（str）：已分配实例和容量、尚未首次发送的 Execution。

        返回：绑定当前执行身份、冻结输入和已验证可读资源 ID 的上下文；此上下文不含授权期限，不能直接签发。

        异常：ValueError 表示执行或资源范围已失效；数据库错误原样传播。
        """
        context = await self._repository.load_execution_resource_context_for_dispatch(
            execution_id
        )
        candidate_ids = tuple(sorted(_resource_ids(context.task_input)))
        readable_ids = await self._repository.resolve_read_resource_ids(
            run_id=context.scope.run_id,
            candidate_ids=candidate_ids,
        )
        return WorkerExecutionResourceContext(
            scope=replace(
                context.scope,
                read_resource_ids=readable_ids,
                expires_at=None,
            ),
            task_input=context.task_input,
        )

    def issue_prepared_execution_grant(
        self,
        context: WorkerExecutionResourceContext,
        start: WorkerDispatchStart,
    ) -> str:
        """使用最终数据库 deadline 同步签发已准备的执行授权。

        参数：
        - `context`（WorkerExecutionResourceContext）：前置阶段收集的身份快照及可读资源范围。
        - `start`（WorkerDispatchStart）：最终发送事务锁定并设置 deadline 后返回的执行快照。

        返回：有效期与最终 Execution deadline 完全一致的授权 token。

        异常：ValueError 表示快照不匹配、deadline 缺失或授权签名失败。
        """
        scope = context.scope
        if (
            start.deadline_at is None
            or scope.execution_id != start.execution_id
            or scope.worker_instance_id != start.instance_id
            or scope.worker_instance_key != start.instance_key
            or scope.service_principal != start.service_principal
        ):
            raise ValueError("worker_execution_grant_context_mismatch")
        if start.started_at is None:
            raise ValueError("worker_execution_grant_started_at_missing")
        return self._signer.issue(
            replace(scope, expires_at=start.deadline_at),
            now=start.started_at,
        )

    async def put_resource(
        self,
        *,
        service_principal: str,
        grant_token: str,
        resource_id: str,
        media_type: str,
        expected_sha256: str,
        file_name: str | None,
        chunks: AsyncIterable[bytes],
    ) -> WorkerResourceWriteResult:
        """验证服务身份、授权范围与内容摘要后不可变地保存资源。

        参数：
        - `service_principal`（str）：Bearer 凭据映射出的 Worker 服务主体。
        - `grant_token`（str）：Athena 为该执行签名的写入授权。
        - `resource_id`（str）：S1 UUID 格式资源身份。
        - `media_type`（str）：原始媒体类型；最多 255 个字符。
        - `expected_sha256`（str）：Content-SHA256 声明的 64 位小写摘要。
        - `file_name`（str | None）：可选展示名称；最多 255 字符且只作为元数据。
        - `chunks`（AsyncIterable[bytes]）：服务器流式读取的原始字节。

        返回：首次创建或同 ID 同摘要重放的资源引用。

        异常：ValueError 表示授权、内容、媒体字段或大小不合法；仓储会报告 ID 冲突。
        """
        claims = self._verify_current_claims(service_principal, grant_token)
        if str(UUID(resource_id)) != resource_id.lower():
            raise ValueError("worker_resource_id_invalid")
        if not media_type or len(media_type) > 255:
            raise ValueError("worker_resource_media_type_invalid")
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise ValueError("worker_resource_digest_invalid")
        if file_name is not None and len(file_name) > 255:
            raise ValueError("worker_resource_file_name_invalid")
        # 授权范围仅写当前 Execution；客户端不能替资源选择其他归属。
        scope = await self._repository.validate_current_execution(claims)
        if scope is None:
            raise ValueError("worker_execution_grant_scope_revoked")
        return await self._repository.store_resource(
            scope=scope,
            resource_id=resource_id.lower(),
            media_type=media_type,
            expected_sha256=expected_sha256,
            file_name=file_name,
            chunks=chunks,
            max_bytes=self._max_bytes,
        )

    async def get_resource(
        self,
        *,
        service_principal: str,
        grant_token: str,
        resource_id: str,
    ) -> WorkerResourceDownload:
        """仅在当前 Execution 和授权读资源范围内返回资源文件。

        参数：
        - `service_principal`（str）：Bearer 凭据映射出的 Worker 服务主体。
        - `grant_token`（str）：Athena 为该执行签名的资源读取授权。
        - `resource_id`（str）：S1 UUID 格式资源身份。

        返回：可流式发送的资源文件及协议元数据。

        异常：ValueError 表示身份、签名或当前执行授权无效。
        """
        if str(UUID(resource_id)) != resource_id.lower():
            raise ValueError("worker_resource_id_invalid")
        claims = self._verify_current_claims(service_principal, grant_token)
        scope = await self._repository.validate_current_execution(claims)
        if scope is None:
            raise ValueError("worker_execution_grant_scope_revoked")
        download = await self._repository.load_resource(
            scope=scope,
            resource_id=resource_id.lower(),
        )
        if download is None:
            raise ValueError("worker_resource_forbidden_or_unknown")
        return download

    def _verify_current_claims(
        self, service_principal: str, grant_token: str
    ) -> WorkerExecutionGrantClaims:
        """在查询数据库前验证签名、时间和 Bearer 主体绑定。"""
        claims = self._signer.verify(grant_token)
        if claims.service_principal != service_principal:
            raise ValueError("worker_execution_grant_principal_mismatch")
        return claims


def _resource_ids(value: object) -> set[str]:
    """只收集冻结 JSON 输入中 resource_id 键下的合法 UUID 字符串。"""
    result: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "resource_id" and isinstance(nested, str):
                try:
                    resource_id = str(UUID(nested)).lower()
                except ValueError:
                    continue
                result.add(resource_id)
            else:
                result.update(_resource_ids(nested))
    elif isinstance(value, list):
        for item in value:
            result.update(_resource_ids(item))
    return result


__all__ = [
    "WorkerResourceDownload",
    "WorkerResourceRepository",
    "WorkerResourceService",
    "WorkerResourceWriteResult",
]
