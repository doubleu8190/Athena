"""企业测试数据套件会话级夹具：按需幂等生成 datasets/enterprise_hroa/generated 样本资产。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from datasets.enterprise_hroa.builders.build_assets import (
    DATASET_ROOT,
    GENERATED_DIR,
    build_all_assets,
)

_SOURCE_DIRS: tuple[Path, ...] = (
    DATASET_ROOT / "sources",
    DATASET_ROOT / "skills",
    DATASET_ROOT / "builders",
)


def _latest_source_mtime(source_dirs: tuple[Path, ...] = _SOURCE_DIRS) -> float:
    """扫描源文档、技能源码与构建脚本目录的最新修改时间戳（忽略 __pycache__）。"""
    latest = 0.0
    for directory in source_dirs:
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if "__pycache__" in path.parts or path.suffix == ".pyc" or not path.is_file():
                continue
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                continue
    return latest


def _is_generated_assets_complete(
    output_root: Path = GENERATED_DIR,
    *,
    source_dirs: tuple[Path, ...] = _SOURCE_DIRS,
) -> bool:
    """校验 checksums.json 完整性、文件大小磁盘一致性及相对源码的新鲜度。"""
    checksums_path = output_root / "checksums.json"
    if not checksums_path.is_file():
        return False
    try:
        checksums_mtime = checksums_path.stat().st_mtime
        payload: Any = json.loads(checksums_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    files = payload.get("files")
    file_count = payload.get("file_count")
    if (
        not isinstance(files, dict)
        or not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count <= 0
        or len(files) != file_count
    ):
        return False
    # 源码、技能包或生成器有更新时自动触发重建，避免复用过期 generated/ 产物。
    if _latest_source_mtime(source_dirs) > checksums_mtime:
        return False
    for relpath, meta in files.items():
        if not isinstance(relpath, str) or not isinstance(meta, dict):
            return False
        expected_size = meta.get("size_bytes")
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            return False
        target = output_root / relpath
        try:
            if not target.is_file() or target.stat().st_size != expected_size:
                return False
        except OSError:
            return False
    return True


@pytest.fixture(scope="session", autouse=True)
def ensure_enterprise_hroa_generated_assets() -> None:
    """在企业测试套件启动前确保全部确定性多格式测试样本就绪，缺失或过期时自动重建。"""
    # 若 generated/ 被清理、文件字节漂移或源码已更新，自动原地重建确定性产物。
    if not _is_generated_assets_complete(GENERATED_DIR):
        build_all_assets(GENERATED_DIR)
