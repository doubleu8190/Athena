"""Worker 注册输入中稳定 JSON 结构的应用层校验模型。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

import jsonschema
from jsonschema import Draft202012Validator
from pydantic import AnyUrl, BaseModel, ConfigDict, Field, JsonValue, field_validator

from domain.orchestration.worker_entities import WorkerRegistrationIntent


class WorkerJsonSchema(BaseModel):
    """Worker 输入或输出对象的 JSON Schema 定义。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_uri: str | None = Field(default=None, alias="$schema")
    schema_id: str | None = Field(default=None, alias="$id")
    title: str | None = None
    description: str | None = None
    type: Literal["object"]
    properties: dict[str, JsonValue] = Field(default_factory=dict)
    required: tuple[str, ...] = ()
    additional_properties: bool | dict[str, JsonValue] = Field(
        default=True, alias="additionalProperties"
    )

    @field_validator("required")
    @classmethod
    def unique_required_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝重复的必填字段名称。

        参数：
        - `value`（tuple[str, ...]）：Schema 的必填字段；约束：名称不得重复。

        返回：去重检查后的原始字段顺序。

        异常：
        - ValueError：字段名称重复时抛出。
        """
        if len(value) != len(set(value)):
            raise ValueError("worker_schema_required_fields_duplicate")
        return value


class WorkerCapabilityContract(BaseModel):
    """注册中心保存的四项 Worker 能力契约。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    can_do: tuple[str, ...] = Field(min_length=1)
    cannot_do: tuple[str, ...] = Field(min_length=1)
    input_schema: WorkerJsonSchema
    output_schema: WorkerJsonSchema

    @field_validator("can_do", "cannot_do")
    @classmethod
    def non_empty_descriptions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """要求能力说明至少含一个非空项目。

        参数：
        - `value`（tuple[str, ...]）：能力或限制说明；约束：每项去除空白后非空。

        返回：原顺序的能力说明。

        异常：
        - ValueError：说明项为空时抛出。
        """
        normalized = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("worker_capability_description_empty")
        return normalized

    @field_validator("input_schema", "output_schema")
    @classmethod
    def valid_json_schema(cls, value: WorkerJsonSchema) -> WorkerJsonSchema:
        """在应用边界校验 Draft 2020-12 Schema 自身有效。

        参数：
        - `value`（WorkerJsonSchema）：Worker 输入或输出对象的 Schema；约束：根类型须为 object。

        返回：已通过标准元 Schema 校验的输入对象。

        异常：
        - ValueError：Schema 定义无效时抛出。
        """
        try:
            Draft202012Validator.check_schema(
                value.model_dump(mode="json", by_alias=True, exclude_none=True)
            )
        except jsonschema.SchemaError as exc:
            raise ValueError("worker_json_schema_invalid") from exc
        return value


class WorkerRegistrationRequest(BaseModel):
    """符合 Worker Protocol v1 的注册请求体。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1.0"]
    registration_request_id: UUID
    worker_instance_key: UUID
    base_url: AnyUrl
    execution_timeout_seconds: int = Field(ge=1, le=604800)
    max_concurrency: int = Field(ge=1, le=256)
    capability: WorkerCapabilityContract

    @field_validator("base_url")
    @classmethod
    def require_https(cls, value: AnyUrl) -> AnyUrl:
        """只接受加密传输的 Worker 注册地址。

        参数：
        - `value`（AnyUrl）：Worker 服务根地址；约束：Scheme 必须为 HTTPS 且不可含路径、查询或片段。

        返回：通过地址约束的 URL。

        异常：
        - ValueError：地址不符合 Worker v1 的 HTTPS 服务根地址形式。
        """
        if value.scheme != "https" or value.path not in {"", "/"} or value.query or value.fragment:
            raise ValueError("worker_base_url_must_be_https_origin")
        return value

    def to_domain_intent(self, service_principal: str) -> WorkerRegistrationIntent:
        """将已认证注册请求转换为纯领域输入。

        参数：
        - `service_principal`（str）：认证中间件确认的 Worker 服务主体；约束：去除空白后非空。

        返回：供注册仓储持久化的 `WorkerRegistrationIntent`。

        异常：
        - ValueError：服务主体为空时抛出。
        """
        if not service_principal.strip():
            raise ValueError("worker_service_principal_required")
        schemas = self.capability.model_dump(mode="json", by_alias=True, exclude_none=True)
        return WorkerRegistrationIntent(
            registration_request_id=str(self.registration_request_id),
            service_principal=service_principal,
            instance_key=str(self.worker_instance_key),
            base_url=str(self.base_url).rstrip("/"),
            execution_timeout_seconds=self.execution_timeout_seconds,
            max_concurrency=self.max_concurrency,
            can_do=self.capability.can_do,
            cannot_do=self.capability.cannot_do,
            input_schema=schemas["input_schema"],
            output_schema=schemas["output_schema"],
        )


__all__ = [
    "WorkerCapabilityContract",
    "WorkerJsonSchema",
    "WorkerRegistrationRequest",
]
