"""通用 HTTP(S) Base URL 与控制字符安全校验工具。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def has_control_characters(value: str) -> bool:
    """检查字符串是否包含 ASCII 控制字符（防止 Header 注入或日志污染）。"""
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def validate_http_base_url(
    url: str,
    *,
    allow_http: bool = True,
    allow_query: bool = False,
) -> None:
    """严格校验 HTTP(S) Base URL，拒绝控制字符、空白、反斜杠、凭据片段与非法主机/端口。"""
    if not url or has_control_characters(url) or any(ch.isspace() for ch in url) or "\\" in url:
        raise ValueError("http_base_url_invalid")

    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("http_base_url_invalid") from None

    allowed_schemes = {"http", "https"} if allow_http else {"https"}
    if (
        parsed.scheme.lower() not in allowed_schemes
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or "@" in parsed.netloc
        or (not allow_query and (bool(parsed.query) or "?" in url))
        or bool(parsed.fragment)
        or "#" in url
    ):
        raise ValueError("http_base_url_invalid")

    # 精确校验端口形态：拒绝悬空冒号、方括号后非法后缀及越界端口号（仅允许 1..65535）。
    is_bracketed_ipv6 = parsed.netloc.startswith("[")
    trailing = parsed.netloc.rsplit("]", 1)[-1] if is_bracketed_ipv6 else parsed.netloc
    if is_bracketed_ipv6 and trailing and not trailing.startswith(":"):
        raise ValueError("http_base_url_invalid")
    if (":" in trailing and port is None) or (port is not None and not 1 <= port <= 65535):
        raise ValueError("http_base_url_invalid")

    # 主机部分允许合法 IPv6（须带方括号且不含 zone id）、IPv4 或符合 RFC 1123 的域名标签。
    if is_bracketed_ipv6:
        if "%" in host:
            raise ValueError("http_base_url_invalid")
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError("http_base_url_invalid") from None
        return

    try:
        parsed_ip = ipaddress.ip_address(host)
    except ValueError:
        if host.replace(".", "").isdigit() and "." in host:
            raise ValueError("http_base_url_invalid") from None
    else:
        if isinstance(parsed_ip, ipaddress.IPv4Address):
            return
        raise ValueError("http_base_url_invalid")

    if len(host) > 253 or any(_HOST_LABEL.fullmatch(label) is None for label in host.split(".")):
        raise ValueError("http_base_url_invalid")


__all__ = [
    "has_control_characters",
    "validate_http_base_url",
]
