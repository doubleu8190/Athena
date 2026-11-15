"""进程共享的严格环境变量解析。"""

from __future__ import annotations

import ipaddress
import os
import re


def env_flag(name: str, *, default: bool) -> bool:
    """解析严格布尔环境变量，拒绝含糊配置。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name}_invalid")


def env_int(name: str, *, default: int, minimum: int) -> int:
    """解析带下界的环境整数，拒绝 bool 语义和含糊文本。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError as error:
        raise ValueError(f"{name}_invalid") from error
    if parsed < minimum:
        raise ValueError(f"{name}_invalid")
    return parsed


def env_csv_set(name: str) -> frozenset[str]:
    """解析逗号分隔的部署白名单，拒绝空项和非法主机/变量名。"""
    raw = os.getenv(name, "")
    if not raw.strip():
        return frozenset()
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ValueError(f"{name}_invalid")
    if name == "ATHENA_MCP_ALLOWED_PRIVATE_HOSTS":
        normalized_hosts: set[str] = set()
        try:
            for part in parts:
                try:
                    host = ipaddress.ip_address(part).compressed
                except ValueError:
                    ascii_host = part.encode("idna").decode("ascii").lower()
                    labels = ascii_host.split(".")
                    if len(ascii_host) > 253 or any(
                        not label
                        or len(label) > 63
                        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
                        for label in labels
                    ):
                        raise ValueError from None
                    host = ascii_host
                normalized_hosts.add(host)
        except (UnicodeError, ValueError):
            raise ValueError(f"{name}_invalid") from None
        return frozenset(normalized_hosts)
    else:
        valid = all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts)
    if not valid:
        raise ValueError(f"{name}_invalid")
    return frozenset(parts)


def memory_enabled_from_env() -> bool:
    """统一记忆开关：同时兼容 ATHENA_MEMORY_ENABLED 与旧 ATHENA_MEMORY_LIFECYCLE_ENABLED。"""
    return env_flag("ATHENA_MEMORY_ENABLED", default=False) or env_flag(
        "ATHENA_MEMORY_LIFECYCLE_ENABLED", default=False
    )


__all__ = ["env_csv_set", "env_flag", "env_int", "memory_enabled_from_env"]
