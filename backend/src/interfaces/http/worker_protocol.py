"""Worker HTTP 接口共用的身份、JSON 和错误处理。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from jsonschema.exceptions import ValidationError
from pydantic import JsonValue
import rfc8785

from application.orchestration.worker_protocol_validation import validate_worker_request


class WorkerProtocolError(Exception):
    """带有 v1 错误码和处置指令的 HTTP 协议错误。"""

    def __init__(
        self, code: str, status: int, directive: str, message: str
    ) -> None:
        """保存将返回给远端服务的稳定错误信息。

        参数：
        - `code`（str）：协议错误码。
        - `status`（int）：HTTP 状态码。
        - `directive`（str）：Worker 应采取的处置类型。
        - `message`（str）：不包含凭据的简短说明。

        返回：无。

        异常：此构造函数不主动抛出业务异常。
        """
        super().__init__(message)
        self.code = code
        self.status = status
        self.directive = directive
        self.message = message


class WorkerProtocolHttp:
    """验证 Worker 服务凭据和 v1 HTTP JSON 边界。"""

    def __init__(
        self,
        service_tokens: dict[str, str],
        max_json_body_bytes: int = 1_048_576,
    ) -> None:
        """创建不可变身份映射和请求大小限制。

        参数：
        - `service_tokens`（dict[str, str]）：服务主体到 Bearer token 的映射；主体和 token 均须非空。
        - `max_json_body_bytes`（int）：JSON 请求最大字节数；范围为 1024–104857600。

        返回：无。

        异常：ValueError 表示凭据映射无效或请求大小超出协议配置范围。
        """
        if not 1024 <= max_json_body_bytes <= 104_857_600:
            raise ValueError("worker_json_body_limit_out_of_range")
        if any(not principal.strip() or not token for principal, token in service_tokens.items()):
            raise ValueError("worker_service_token_mapping_invalid")
        self._token_to_principal = {token: principal for principal, token in service_tokens.items()}
        if len(self._token_to_principal) != len(service_tokens):
            raise ValueError("worker_service_tokens_must_be_unique")
        self._max_json_body_bytes = max_json_body_bytes

    def authenticate(self, request: Request) -> str:
        """验证服务 Bearer token 与协议版本请求头。

        参数：
        - `request`（Request）：当前 Worker 协议 HTTP 请求。

        返回：凭据所绑定的服务主体名称。

        异常：WorkerProtocolError 表示身份失败或协议版本不受支持。
        """
        authorization_values = request.headers.getlist("authorization")
        version_values = request.headers.getlist("x-athena-protocol-version")
        if len(authorization_values) > 1 or len(version_values) > 1:
            raise WorkerProtocolError(
                "MALFORMED_REQUEST", 400, "operator_action", "protocol headers must appear once"
            )
        if not authorization_values:
            raise WorkerProtocolError(
                "AUTHENTICATION_FAILED", 401, "operator_action", "service authentication failed"
            )
        authorization = authorization_values[0]
        scheme, separator, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not token:
            raise WorkerProtocolError(
                "AUTHENTICATION_FAILED", 401, "operator_action", "service authentication failed"
            )
        principal = self._token_to_principal.get(token)
        if principal is None:
            raise WorkerProtocolError(
                "AUTHENTICATION_FAILED", 401, "operator_action", "service authentication failed"
            )
        if not version_values:
            raise WorkerProtocolError(
                "PROTOCOL_VERSION_UNSUPPORTED",
                426,
                "operator_action",
                "protocol version is unsupported",
            )
        if version_values[0] != "1.0":
            raise WorkerProtocolError(
                "PROTOCOL_VERSION_UNSUPPORTED",
                426,
                "operator_action",
                "protocol version is unsupported",
            )
        return principal

    async def read_json(
        self,
        request: Request,
        *,
        schema_name: str,
        id_field: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, JsonValue]:
        """读取并严格校验 v1 JSON 请求体及路径、幂等键绑定。

        参数：
        - `request`（Request）：当前 HTTP 请求。
        - `schema_name`（str）：共享协议 JSON Schema 中请求 DTO 的 `$defs` 名称。
        - `id_field`（str | None）：必须匹配 Idempotency-Key 的 UUID 字段；无幂等键的请求传 None。
        - `execution_id`（str | None）：路径中的 Execution ID；提供时须与请求体字段相同。

        返回：通过格式、重复键、Schema 及身份字段校验的 JSON 对象。

        异常：WorkerProtocolError 表示请求头、大小、编码、JSON、Schema 或绑定规则无效。
        """
        content_types = request.headers.getlist("content-type")
        if len(content_types) != 1:
            raise WorkerProtocolError("MALFORMED_REQUEST", 400, "operator_action", "Content-Type is required")
        media_type, _, charset = content_types[0].partition(";")
        if (
            media_type.strip().lower() != "application/json"
            or not charset
            or charset.strip().lower() != "charset=utf-8"
        ):
            raise WorkerProtocolError(
                "UNSUPPORTED_MEDIA_TYPE", 415, "operator_action", "Content-Type must be application/json; charset=utf-8"
            )
        if request.headers.getlist("content-length"):
            try:
                declared_size = int(request.headers["content-length"])
            except (ValueError, KeyError) as exc:
                raise WorkerProtocolError("MALFORMED_REQUEST", 400, "operator_action", "invalid Content-Length") from exc
            if declared_size < 0 or declared_size > self._max_json_body_bytes:
                raise WorkerProtocolError("PAYLOAD_TOO_LARGE", 413, "operator_action", "JSON body is too large")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > self._max_json_body_bytes:
                raise WorkerProtocolError("PAYLOAD_TOO_LARGE", 413, "operator_action", "JSON body is too large")
        try:
            text = raw.decode("utf-8", errors="strict")
            body = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_finite,
            )
        except WorkerProtocolError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkerProtocolError("MALFORMED_REQUEST", 400, "operator_action", "body must be valid UTF-8 JSON") from exc
        if not isinstance(body, dict):
            raise WorkerProtocolError("MALFORMED_REQUEST", 400, "operator_action", "body must be a JSON object")
        try:
            rfc8785.dumps(body)
        except rfc8785.CanonicalizationError as exc:
            raise WorkerProtocolError(
                "MALFORMED_REQUEST", 400, "operator_action", "body cannot be represented by RFC 8785 JSON"
            ) from exc
        try:
            validate_worker_request(schema_name, body)
        except ValidationError as exc:
            raise WorkerProtocolError("VALIDATION_ERROR", 422, "operator_action", "body does not match the Worker v1 schema") from exc
        if id_field is not None:
            expected_id = body.get(id_field)
            self.require_idempotency(request, expected_id if isinstance(expected_id, str) else "")
        if execution_id is not None and body.get("execution_id") != execution_id:
            raise WorkerProtocolError("MALFORMED_REQUEST", 400, "operator_action", "path and body execution IDs differ")
        return body

    @staticmethod
    def require_idempotency(request: Request, expected_id: str) -> None:
        """检查唯一的 Idempotency-Key 与请求 JSON 字段一致。

        参数：
        - `request`（Request）：当前 HTTP 请求。
        - `expected_id`（str）：请求体或资源路径中的稳定操作 ID。

        返回：一致时返回 `None`。

        异常：WorkerProtocolError 表示 Header 缺失、重复或绑定不一致。
        """
        values = request.headers.getlist("idempotency-key")
        if len(values) != 1 or values[0] != expected_id:
            raise WorkerProtocolError(
                "MALFORMED_REQUEST", 400, "operator_action", "Idempotency-Key must match the request identity"
            )

    @staticmethod
    def validate_path_identifier(identifier: str) -> None:
        """验证路径中的不透明 S1 Identifier 格式。

        参数：
        - `identifier`（str）：Run/Plan/Task/Execution 等路径身份。

        返回：格式正确时返回 `None`。

        异常：WorkerProtocolError 表示路径字段不符合 S1 Identifier。
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,254}", identifier):
            raise WorkerProtocolError(
                "MALFORMED_REQUEST", 400, "operator_action", "path identifier is invalid"
            )


def install_worker_protocol_handlers(app: FastAPI) -> None:
    """为 Athena FastAPI 应用注册 Worker v1 错误响应转换。

    参数：
    - `app`（FastAPI）：包含 Worker 协议路由的应用实例。

    返回：无。

    异常：此函数不主动抛出业务异常。
    """
    @app.exception_handler(WorkerProtocolError)
    async def worker_protocol_error_handler(
        request: Request, error: WorkerProtocolError
    ) -> JSONResponse:
        """将协议错误序列化为共享 ErrorResponse。

        参数：
        - `request`（Request）：触发错误的请求。
        - `error`（WorkerProtocolError）：稳定错误码、HTTP 状态和处置建议。

        返回：符合 Worker v1 ErrorResponse 的 JSON 响应。

        异常：此处理函数不主动抛出业务异常。
        """
        return JSONResponse(
            status_code=error.status,
            content={
                "protocol_version": "1.0",
                "code": error.code,
                "message": error.message,
                "directive": error.directive,
                "request_id": uuid4().hex,
            },
        )


def _unique_object(pairs: Iterable[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    """拒绝 JSON 对象（含嵌套对象）中的重复属性名。

    参数：
    - `pairs`（Iterable[tuple[str, JsonValue]]）：解码器提供的对象键值对。

    返回：不含重复键的 JSON 对象。

    异常：ValueError 表示同一对象中出现重复键。
    """
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    """拒绝 JSON 标准之外的 NaN 与 Infinity 数值。

    参数：
    - `value`（str）：解析器识别的非有限数字字面值。

    返回：无。

    异常：ValueError 表示输入使用了非标准 JSON 数值。
    """
    raise ValueError(f"non-finite JSON number: {value}")


__all__ = ["WorkerProtocolError", "WorkerProtocolHttp", "install_worker_protocol_handlers"]
