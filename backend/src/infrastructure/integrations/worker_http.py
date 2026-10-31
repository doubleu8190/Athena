"""连接已登记 Worker 时执行地址限制并固定 DNS 解析结果。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import socket
import ssl
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import SplitResult, urlsplit

import httpx
import rfc8785
from jsonschema.exceptions import ValidationError

from application.orchestration.worker_protocol_validation import validate_worker_request
from domain.orchestration.worker_entities import WorkerDispatchStart
from domain.orchestration.worker_registry import (
    WorkerHealthProbeResult,
    WorkerHealthProbeTarget,
)


class WorkerOriginRejected(ValueError):
    """Worker 地址未通过 origin 允许列表或 IP 网络策略。"""


class WorkerTaskResponseInvalid(ValueError):
    """Worker 已响应，但响应体无法作为合法协议 JSON 使用。"""


@dataclass(frozen=True, slots=True)
class WorkerTaskPostResponse:
    """保留安全解码后的 Worker HTTP 状态和 JSON 响应。"""

    status_code: int
    body: dict[str, object]
    retry_after_seconds: float | None


class PinnedWorkerHttpClient:
    """安全探测 Worker：限制 origin/IP 并将连接固定到单次解析结果。"""

    def __init__(
        self,
        *,
        allowed_origins: Iterable[str],
        allowed_networks: Iterable[str],
        orchestrator_token: str,
        ca_file: str = "",
        timeout_seconds: float = 3.0,
    ) -> None:
        """准备 origin、网络、TLS 和超时策略。

        参数：
        - `allowed_origins`（Iterable[str]）：Worker HTTPS origin 精确允许列表。
        - `allowed_networks`（Iterable[str]）：显式允许的 IP 网络；仅这些网段可以访问非全局地址。
        - `orchestrator_token`（str）：发往 Worker 的 Athena 服务 Bearer token。
        - `ca_file`（str）：自定义 TLS CA bundle 路径；为空时使用系统 CA。
        - `timeout_seconds`（float）：单次 HTTP 健康探测的超时秒数，必须大于零。

        返回：无。

        异常：ValueError 表示 origin、CIDR、token 或超时配置无效。
        """
        if timeout_seconds <= 0:
            raise ValueError("worker_health_timeout_invalid")
        if not orchestrator_token:
            raise ValueError("worker_orchestrator_token_required")
        self._allowed_origins = frozenset(
            _normalize_origin(origin) for origin in allowed_origins
        )
        if not self._allowed_origins:
            raise ValueError("worker_allowed_origins_required")
        try:
            self._allowed_networks = tuple(
                ipaddress.ip_network(network, strict=False) for network in allowed_networks
            )
        except ValueError as exc:
            raise ValueError("worker_allowed_network_invalid") from exc
        self._orchestrator_token = orchestrator_token
        self._ssl_context = ssl.create_default_context(cafile=ca_file or None)
        self._timeout_seconds = timeout_seconds

    async def validate_registration_origin(self, base_url: str) -> None:
        """确认注册 URL 在精确 origin 列表和当前网络策略内。

        参数：
        - `base_url`（str）：注册请求声明的 HTTPS origin。

        返回：通过 origin 和全部 DNS 地址检查时返回 `None`。

        异常：WorkerOriginRejected 表示地址不在允许列表、解析失败或含未授权 IP。
        """
        try:
            parsed = _parse_https_origin(base_url)
            origin = _normalize_origin(base_url)
        except ValueError as exc:
            raise WorkerOriginRejected("worker_https_origin_invalid") from exc
        if origin not in self._allowed_origins:
            raise WorkerOriginRejected("worker_origin_not_allowlisted")
        if not await self._resolve_and_check(parsed.hostname, parsed.port or 443):
            raise WorkerOriginRejected("worker_origin_dns_resolution_failed")

    async def health_check(
        self, target: WorkerHealthProbeTarget
    ) -> WorkerHealthProbeResult | None:
        """探测一次 Worker 健康状态，拒绝重定向和 DNS 重绑定连接。

        参数：
        - `target`（WorkerHealthProbeTarget）：注册表提供的固定实例键和 base_url 快照。

        返回：健康响应通过 S1 Schema 且回显实例键匹配时的结果，否则返回 `None`。

        异常：WorkerOriginRejected 表示地址不在允许范围；网络与响应协议错误返回 `None`。
        """
        try:
            async with asyncio.timeout(self._timeout_seconds):
                parsed = _parse_https_origin(target.base_url)
                if _normalize_origin(target.base_url) not in self._allowed_origins:
                    raise WorkerOriginRejected("worker_origin_not_allowlisted")
                resolved_port = parsed.port or 443
                addresses = await self._resolve_and_check(parsed.hostname, resolved_port)
                if not addresses:
                    return None

                # 同一 DNS 响应中的所有 IP 都要通过策略；不从混合公网/私网应答中
                # 挑一个允许地址，避免攻击者借另一条记录隐藏内网解析结果。
                response_json: dict[str, object] | None = None
                original_host = parsed.hostname.encode("idna").decode("ascii")
                for address in addresses:
                    pinned_url = httpx.URL(
                        scheme="https",
                        host=address,
                        port=resolved_port,
                        path="/worker-api/v1/health",
                    )
                    host_header = _authority(original_host, resolved_port)
                    try:
                        async with httpx.AsyncClient(
                            verify=self._ssl_context,
                            timeout=self._timeout_seconds,
                            follow_redirects=False,
                            trust_env=False,
                        ) as client:
                            async with client.stream(
                                "GET",
                                pinned_url,
                                headers={
                                    "Authorization": f"Bearer {self._orchestrator_token}",
                                    "X-Athena-Protocol-Version": "1.0",
                                    "Host": host_header,
                                    "Accept": "application/json",
                                },
                                extensions={"sni_hostname": original_host},
                            ) as response:
                                if response.status_code != 200:
                                    continue
                                content_length = response.headers.get("content-length")
                                if content_length is not None and int(content_length) > 65_536:
                                    continue
                                raw = bytearray()
                                async for chunk in response.aiter_bytes():
                                    raw.extend(chunk)
                                    if len(raw) > 65_536:
                                        raw.clear()
                                        break
                                if not raw or len(raw) > 65_536:
                                    continue
                                parsed_body = json.loads(
                                    raw.decode("utf-8", errors="strict"),
                                    object_pairs_hook=_unique_object,
                                    parse_constant=_reject_non_finite,
                                )
                                if not isinstance(parsed_body, dict):
                                    continue
                                rfc8785.dumps(parsed_body)
                                validate_worker_request("HealthResponse", parsed_body)
                                response_json = parsed_body
                                break
                    except (
                        httpx.HTTPError,
                        UnicodeDecodeError,
                        json.JSONDecodeError,
                        ValueError,
                        ValidationError,
                        rfc8785.CanonicalizationError,
                    ):
                        continue
        except TimeoutError:
            return None
        if response_json is None:
            return None

        response_key = response_json.get("worker_instance_key")
        if response_key != target.instance_key:
            # 调用方将该情况记作路由身份错误，不读取响应中的其它路由信息。
            return WorkerHealthProbeResult(
                instance_key=str(response_key or ""),
                status="identity_mismatch",
                accepting_tasks=False,
                checked_at=datetime.now(timezone.utc),
            )
        status = response_json.get("status")
        accepting = response_json.get("accepting_tasks")
        if status not in {"ready", "not_ready"} or not isinstance(accepting, bool):
            return None
        return WorkerHealthProbeResult(
            instance_key=target.instance_key,
            status=status,
            accepting_tasks=accepting,
            checked_at=datetime.now(timezone.utc),
        )

    async def send_task(
        self,
        target: WorkerDispatchStart,
        *,
        max_response_bytes: int = 65_536,
        timeout_seconds: float | None = None,
    ) -> WorkerTaskPostResponse:
        """通过已固定 DNS 地址发送不可变 TaskCommand 并读取有界 JSON 响应。

        参数：
        - `target`（WorkerDispatchStart）：数据库已经冻结的执行、命令和注册地址快照。
        - `max_response_bytes`（int）：HTTP 响应体字节上限，必须为正数。

        返回：HTTP 状态码及无重复键的 JSON 对象；业务状态由 Dispatcher 按协议目录裁决。

        异常：WorkerOriginRejected 表示目标地址越权；HTTPError、ValueError 表示网络、响应或协议失败。
        """
        request_timeout = (
            self._timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        if max_response_bytes < 1 or request_timeout <= 0:
            raise ValueError("worker_response_limit_invalid")
        try:
            async with asyncio.timeout(request_timeout):
                return await self._send_task(target, max_response_bytes=max_response_bytes)
        except TimeoutError as exc:
            raise httpx.TimeoutException("worker_task_request_timeout") from exc

    async def _send_task(
        self, target: WorkerDispatchStart, *, max_response_bytes: int
    ) -> WorkerTaskPostResponse:
        """在调用方的总超时范围内完成固定地址解析与单次 POST。"""
        try:
            parsed = _parse_https_origin(target.base_url)
            if _normalize_origin(target.base_url) not in self._allowed_origins:
                raise WorkerOriginRejected("worker_origin_not_allowlisted")
            port = parsed.port or 443
            addresses = await self._resolve_and_check(parsed.hostname, port)
            if not addresses:
                raise httpx.ConnectError("worker_dns_resolution_failed")
            host = parsed.hostname.encode("idna").decode("ascii")
            body = rfc8785.dumps(target.command)
            response: WorkerTaskPostResponse | None = None
            for address in addresses:
                pinned_url = httpx.URL(
                    scheme="https",
                    host=address,
                    port=port,
                    path="/worker-api/v1/tasks",
                )
                async with httpx.AsyncClient(
                    verify=self._ssl_context,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        pinned_url,
                        content=body,
                        headers={
                            "Authorization": f"Bearer {self._orchestrator_token}",
                            "X-Athena-Protocol-Version": "1.0",
                            "Host": _authority(host, port),
                            "Accept": "application/json",
                            "Content-Type": "application/json; charset=utf-8",
                            "Idempotency-Key": target.command_id,
                        },
                        extensions={"sni_hostname": host},
                    ) as result:
                        try:
                            length = result.headers.get("content-length")
                            if length is not None and int(length) > max_response_bytes:
                                raise ValueError("worker_response_too_large")
                            raw = bytearray()
                            async for chunk in result.aiter_bytes():
                                raw.extend(chunk)
                                if len(raw) > max_response_bytes:
                                    raise ValueError("worker_response_too_large")
                            parsed_body = json.loads(
                                raw.decode("utf-8", errors="strict"),
                                object_pairs_hook=_unique_object,
                                parse_constant=_reject_non_finite,
                            )
                            if not isinstance(parsed_body, dict):
                                raise ValueError("worker_response_not_object")
                            rfc8785.dumps(parsed_body)
                        except (
                            UnicodeDecodeError,
                            json.JSONDecodeError,
                            ValueError,
                            rfc8785.CanonicalizationError,
                        ) as exc:
                            raise WorkerTaskResponseInvalid(
                                "worker_response_json_invalid"
                            ) from exc
                        retry_after = _retry_after_seconds(
                            result.headers.get("retry-after")
                        )
                        response = WorkerTaskPostResponse(
                            result.status_code, parsed_body, retry_after
                        )
                        break
            if response is None:
                raise httpx.ConnectError("worker_addresses_unreachable")
            return response
        except (WorkerOriginRejected, WorkerTaskResponseInvalid, httpx.HTTPError):
            raise

    async def _resolve_and_check(self, hostname: str, port: int) -> tuple[str, ...]:
        """解析所有 A/AAAA 结果，并验证每个地址可被当前策略访问。

        参数：
        - `hostname`（str）：经 HTTPS URL 解析得到的 Worker 主机名。
        - `port`（int）：目标 Worker HTTPS 端口。

        返回：通过策略且去重后的 IP 字符串元组。

        异常：WorkerOriginRejected 表示 DNS 返回了未授权的网络地址。
        """
        loop = asyncio.get_running_loop()
        try:
            records = await asyncio.wait_for(
                loop.getaddrinfo(
                    hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
                ),
                timeout=self._timeout_seconds,
            )
        except (OSError, asyncio.TimeoutError):
            return ()
        addresses = tuple(
            dict.fromkeys(
                str(ipaddress.ip_address(record[4][0]))
                for record in records
                if record[0] in {socket.AF_INET, socket.AF_INET6}
            )
        )
        if not addresses:
            return ()
        for address_text in addresses:
            address = ipaddress.ip_address(address_text)
            globally_routable = address.is_global
            explicitly_allowed = any(address in network for network in self._allowed_networks)
            if not globally_routable and not explicitly_allowed:
                raise WorkerOriginRejected("worker_origin_resolves_to_private_address")
        return addresses


def _parse_https_origin(base_url: str) -> SplitResult:
    """将注册地址解析为无用户信息、路径、查询或片段的 HTTPS origin。

    参数：
    - `base_url`（str）：Worker 注册时给出的服务 origin。

    返回：标准库 SplitResult；端口省略时返回 HTTPS 默认端口。

    异常：ValueError 表示 URL 不是安全 HTTPS origin。
    """
    try:
        parsed = urlsplit(base_url)
        port = parsed.port or 443
    except ValueError as exc:
        raise ValueError("worker_https_origin_invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or not 1 <= port <= 65535
    ):
        raise ValueError("worker_https_origin_invalid")
    return parsed._replace(netloc=parsed.netloc, path="", query="", fragment="")


def _normalize_origin(origin: str) -> str:
    """以小写主机名和规范 HTTPS 端口规范化 origin 允许项。

    参数：
    - `origin`（str）：配置中的 HTTPS 地址。

    返回：含显式端口（默认 443）的规范 origin。

    异常：ValueError 表示配置项不是 origin。
    """
    parsed = _parse_https_origin(origin)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port or 443
    return f"https://{host.lower()}:{port}"


def _authority(host: str, port: int) -> str:
    """生成 DNS/IP URL 共用的正确 HTTP Host authority。

    参数：
    - `host`（str）：原注册主机名或 IPv6 地址。
    - `port`（int）：HTTP TLS 目标端口，范围为 1–65535。

    返回：省略默认 443 端口，并为 IPv6 地址加方括号的 Host 值。

    异常：ValueError 表示主机或端口无效。
    """
    if not host or not 1 <= port <= 65535:
        raise ValueError("worker_http_authority_invalid")
    value = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return value if port == 443 else f"{value}:{port}"


def _unique_object(pairs: Iterable[tuple[str, object]]) -> dict[str, object]:
    """拒绝健康响应中的重复 JSON 键。

    参数：
    - `pairs`（Iterable[tuple[str, object]]）：JSON 解码器提供的键值对。

    返回：没有重复键的 JSON 对象。

    异常：ValueError 表示同一对象中出现重复键。
    """
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate_json_key")
        document[key] = value
    return document


def _reject_non_finite(value: str) -> None:
    """拒绝非标准 JSON 数值。

    参数：
    - `value`（str）：JSON 解析器读取到的数值字面值。

    返回：无。

    异常：ValueError 表示数值不属于 RFC 8259 JSON。
    """
    raise ValueError(f"non-finite_json_number:{value}")


def _retry_after_seconds(value: str | None) -> float | None:
    """解析非负有限秒数形式的 Retry-After；忽略日期或畸形值。"""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


__all__ = [
    "PinnedWorkerHttpClient",
    "WorkerOriginRejected",
    "WorkerTaskPostResponse",
    "WorkerTaskResponseInvalid",
]
