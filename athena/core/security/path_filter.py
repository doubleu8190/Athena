"""路径安全过滤器 — 防止路径遍历和注入攻击.

检查规则：
- 拒绝包含 .. 的路径（路径遍历）
- 拒绝绝对路径外访问（限制在工作目录内）
- 拒绝特殊字符（防注入）
- 可配置白名单/黑名单目录
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from athena.utils.logging import get_logger

logger = get_logger(__name__)

# 受保护的系统目录（统一定义，避免多处重复）
_PROTECTED_SYSTEM_DIRS = [
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
]

# 危险路径模式
_DANGEROUS_PATTERNS = [
    r"\.\./",  # 路径遍历
    r"\.\.\\",  # Windows 路径遍历
    r"^/etc/",  # 系统配置
    r"^/proc/",  # 进程信息
    r"^/sys/",  # 系统信息
    r"^~/.ssh",  # SSH 密钥
    r"^~/.aws",  # AWS 凭证
    r"\x00",  # 空字节注入
]

# 默认允许的文件扩展名（可选）
_ALLOWED_EXTENSIONS: set[str] | None = None


class PathSecurityError(Exception):
    """路径安全检查失败."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Path security check failed for '{path}': {reason}")


class PathSecurityFilter:
    """路径安全过滤器.

    使用方式：
        filter = PathSecurityFilter(allowed_dirs=["/workspace", "/tmp"])
        safe_path = filter.validate("/workspace/file.txt")  # OK
        safe_path = filter.validate("/etc/passwd")  # raises PathSecurityError
    """

    def __init__(
        self,
        allowed_dirs: list[str] | None = None,
        blocked_dirs: list[str] | None = None,
        allowed_extensions: set[str] | None = None,
        max_path_length: int = 4096,
        strict_mode: bool = True,
    ) -> None:
        self._allowed_dirs = [Path(d).resolve() for d in (allowed_dirs or [])]
        self._blocked_dirs = [Path(d).resolve() for d in (blocked_dirs or [])]
        self._allowed_extensions = allowed_extensions
        self._max_path_length = max_path_length
        self._strict_mode = strict_mode

    def validate(self, path: str) -> Path:
        """验证路径安全性.

        Args:
            path: 待验证的路径字符串

        Returns:
            解析后的安全 Path 对象

        Raises:
            PathSecurityError: 路径不安全
        """
        # 1. 长度检查
        if len(path) > self._max_path_length:
            raise PathSecurityError(path, f"Path too long ({len(path)} > {self._max_path_length})")

        # 2. 空字节检查
        if "\x00" in path:
            raise PathSecurityError(path, "Null byte detected")

        # 3. 危险模式检查
        for pattern in _DANGEROUS_PATTERNS:
            if re.search(pattern, path, re.IGNORECASE):
                raise PathSecurityError(path, f"Dangerous pattern detected: {pattern}")

        # 4. 解析路径
        try:
            resolved = Path(path).resolve()
        except (ValueError, OSError) as e:
            raise PathSecurityError(path, f"Path resolution failed: {e}")

        # 5. 路径遍历检查（resolve 后检查是否仍在预期目录）
        if self._strict_mode and self._allowed_dirs:
            is_allowed = any(
                str(resolved).startswith(str(allowed_dir))
                for allowed_dir in self._allowed_dirs
            )
            if not is_allowed:
                raise PathSecurityError(
                    path,
                    f"Path outside allowed directories: {[str(d) for d in self._allowed_dirs]}"
                )

        # 6. 黑名单目录检查
        for blocked_dir in self._blocked_dirs:
            if str(resolved).startswith(str(blocked_dir)):
                raise PathSecurityError(path, f"Path in blocked directory: {blocked_dir}")

        # 7. 扩展名检查（可选）
        if self._allowed_extensions is not None:
            ext = resolved.suffix.lower()
            if ext and ext not in self._allowed_extensions:
                raise PathSecurityError(path, f"File extension not allowed: {ext}")

        return resolved

    def validate_for_read(self, path: str) -> Path:
        """验证读操作的路径."""
        return self.validate(path)

    def validate_for_write(self, path: str) -> Path:
        """验证写操作的路径.

        额外检查：
        - 不能写入系统目录
        - 不能覆盖关键文件
        """
        resolved = self.validate(path)

        # 写入保护检查：使用统一常量，避免与 _DANGEROUS_PATTERNS / blocked_dirs 重复定义
        for protected_dir in _PROTECTED_SYSTEM_DIRS:
            if str(resolved).startswith(str(protected_dir)):
                raise PathSecurityError(path, f"Cannot write to protected directory: {protected_dir}")

        return resolved

    def is_safe(self, path: str) -> bool:
        """检查路径是否安全（不抛异常）."""
        try:
            self.validate(path)
            return True
        except PathSecurityError:
            return False


# 默认全局实例
_default_filter: PathSecurityFilter | None = None


def get_path_security_filter() -> PathSecurityFilter:
    """获取默认路径安全过滤器.

    默认使用非严格模式：允许访问任意目录，但拦截危险模式和黑名单目录。
    严格模式（限制在工作目录内）由应用层在需要时显式配置。
    """
    global _default_filter
    if _default_filter is None:
        cwd = os.getcwd()
        _default_filter = PathSecurityFilter(
            allowed_dirs=[cwd],
            blocked_dirs=_PROTECTED_SYSTEM_DIRS,
            strict_mode=False,
        )
    return _default_filter


def set_path_security_filter(filter_instance: PathSecurityFilter) -> None:
    """设置默认路径安全过滤器."""
    global _default_filter
    _default_filter = filter_instance
