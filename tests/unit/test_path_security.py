"""路径安全过滤器单元测试."""

import pytest
from pathlib import Path

from athena.core.security.path_filter import (
    PathSecurityError,
    PathSecurityFilter,
)


class TestPathSecurityFilter:
    """路径安全过滤器测试."""

    def setup_method(self):
        self.filter = PathSecurityFilter(
            allowed_dirs=["/workspace"],
            strict_mode=True,
        )

    def test_valid_path(self):
        result = self.filter.validate("/workspace/file.txt")
        assert isinstance(result, Path)

    def test_path_traversal_rejected(self):
        with pytest.raises(PathSecurityError, match="Dangerous pattern"):
            self.filter.validate("/workspace/../etc/passwd")

    def test_null_byte_rejected(self):
        with pytest.raises(PathSecurityError, match="Null byte"):
            self.filter.validate("/workspace/file\x00.txt")

    def test_long_path_rejected(self):
        long_path = "/workspace/" + "a" * 5000
        with pytest.raises(PathSecurityError, match="Path too long"):
            self.filter.validate(long_path)

    def test_blocked_dir_rejected(self):
        filter_with_blocks = PathSecurityFilter(
            allowed_dirs=["/workspace"],
            blocked_dirs=["/var"],
            strict_mode=False,
        )
        with pytest.raises(PathSecurityError, match="blocked directory"):
            filter_with_blocks.validate("/var/log/test.log")

    def test_write_to_protected_dir_rejected(self):
        filter_loose = PathSecurityFilter(
            allowed_dirs=["/workspace"],
            strict_mode=False,
        )
        with pytest.raises(PathSecurityError, match="protected directory"):
            filter_loose.validate_for_write("/usr/bin/testfile")

    def test_is_safe_returns_false(self):
        assert self.filter.is_safe("/workspace/file.txt") is True
        assert self.filter.is_safe("/workspace/../etc/passwd") is False

    def test_extension_filter(self):
        filter_with_ext = PathSecurityFilter(
            allowed_dirs=["/workspace"],
            allowed_extensions={".txt", ".py"},
            strict_mode=False,
        )
        # 允许的扩展名
        result = filter_with_ext.validate("/workspace/file.txt")
        assert isinstance(result, Path)

        # 不允许的扩展名
        with pytest.raises(PathSecurityError, match="extension not allowed"):
            filter_with_ext.validate("/workspace/file.exe")

    def test_non_strict_mode_allows_outside_dirs(self):
        filter_loose = PathSecurityFilter(
            allowed_dirs=["/workspace"],
            strict_mode=False,
        )
        # 非严格模式下允许访问目录外
        result = filter_loose.validate("/tmp/file.txt")
        assert isinstance(result, Path)
