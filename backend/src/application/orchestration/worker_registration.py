"""Worker 契约校验与注册持久化应用用例。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace

import rfc8785
from pydantic import JsonValue

from domain.orchestration.ports import WorkerOrchestrationRepository
from domain.orchestration.worker_registry import WorkerInstancePrincipalMismatch
from domain.orchestration.worker_entities import (
    WorkerRegistrationIntent,
    WorkerRegistrationReceipt,
)
from .worker_contracts import WorkerRegistrationRequest
from .worker_protocol_validation import validate_worker_request
from .worker_protocol_models import WorkerRegistrationResponseModel


class WorkerRegistrationForbidden(PermissionError):
    """认证主体试图接管其他 Worker 已占用的实例键。"""


@dataclass(frozen=True, slots=True)
class WorkerRegistrationOutcome:
    """保留幂等重放标记但不扩展 S1 注册响应 body。"""

    response: WorkerRegistrationResponseModel
    replayed: bool


class WorkerRegistrationService:
    """把已认证且通过 Schema 校验的注册请求交给注册中心。"""

    def __init__(self, repository: WorkerOrchestrationRepository) -> None:
        """初始化注册用例。

        参数：
        - `repository`（WorkerOrchestrationRepository）：以事务保存能力版本、实例和注册回执的仓储端口。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        self._repository = repository

    async def register(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        service_principal: str,
    ) -> WorkerRegistrationResponseModel:
        """登记 Worker 实例并稳定复用不可变能力版本。

        参数：
        - `raw_body`（Mapping[str, JsonValue]）：原始 JSON 对象；在 Pydantic 归一化前计算 JCS 幂等摘要。
        - `service_principal`（str）：认证边界提供的服务主体；约束：去除空白后非空。

        返回：符合 v1 Schema 的持久化注册响应。

        异常：空主体、幂等冲突或 PostgreSQL 错误原样传播。
        """
        outcome = await self.register_with_replay(
            raw_body, service_principal=service_principal
        )
        return outcome.response

    async def register_with_replay(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        service_principal: str,
    ) -> WorkerRegistrationOutcome:
        """登记 Worker 并保留内部状态码选择所需的重放标记。

        参数：
        - `raw_body`（Mapping[str, JsonValue]）：原始 JSON 注册请求体。
        - `service_principal`（str）：HTTP Bearer 身份绑定的认证主体。

        返回：协议响应模型及不写入响应 body 的 replayed 标记。

        异常：schema、身份冲突和持久化错误原样传播。
        """
        intent = self._registration_intent(raw_body, service_principal)
        try:
            receipt = await self._repository.register_worker(intent)
        except WorkerInstancePrincipalMismatch as exc:
            raise WorkerRegistrationForbidden(
                "worker_instance_principal_mismatch"
            ) from exc
        return self._outcome(receipt)

    async def find_replay(
        self,
        raw_body: Mapping[str, JsonValue],
        *,
        service_principal: str,
    ) -> WorkerRegistrationOutcome | None:
        """只读查询已有注册请求，供接口在 DNS 检查前处理幂等重放。

        参数：
        - `raw_body`（Mapping[str, JsonValue]）：通过协议边界校验的原始注册 JSON。
        - `service_principal`（str）：Bearer 凭据绑定的服务主体。

        返回：请求不存在时为 `None`；摘要匹配时返回原注册回执。

        异常：WorkerIdempotencyConflict 表示同一主体和请求 ID 已绑定其他内容；协议错误原样传播。
        """
        intent = self._registration_intent(raw_body, service_principal)
        receipt = await self._repository.find_registration_receipt(
            service_principal,
            intent.registration_request_id,
            intent.request_hash or "",
        )
        return None if receipt is None else self._outcome(receipt)

    @staticmethod
    def _registration_intent(
        raw_body: Mapping[str, JsonValue], service_principal: str
    ) -> WorkerRegistrationIntent:
        """保留原始 JSON 语义并生成供仓储幂等比较的注册命令。"""
        raw_document = dict(raw_body)
        request_hash = hashlib.sha256(rfc8785.dumps(raw_document)).hexdigest()
        validate_worker_request("RegistrationRequest", raw_document)
        request = WorkerRegistrationRequest.model_validate(raw_document)
        return replace(
            request.to_domain_intent(service_principal),
            request_hash=request_hash,
        )

    @staticmethod
    def _outcome(receipt: WorkerRegistrationReceipt) -> WorkerRegistrationOutcome:
        """将持久注册回执转换为稳定的 S1 响应模型。"""
        response = WorkerRegistrationResponseModel.model_validate(
            {
                "worker_instance_id": receipt.instance_id,
                "worker_instance_key": receipt.instance_key,
                "contract_id": receipt.contract_id,
                "contract_revision": receipt.contract_revision,
                "status": "inactive",
                "registered_at": receipt.registered_at,
            }
        )
        return WorkerRegistrationOutcome(response=response, replayed=receipt.replayed)


__all__ = [
    "WorkerRegistrationForbidden",
    "WorkerRegistrationOutcome",
    "WorkerRegistrationService",
]
