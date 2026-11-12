"""管理员 Skill ZIP 的流式边界校验与参数 Schema 验证。"""

from __future__ import annotations

import hashlib
import re
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from referencing import Registry
from referencing.exceptions import Unresolvable

from domain.skill import (
    MAX_GUIDE_CHARACTERS,
    MAX_PACKAGE_FILE_BYTES,
    MAX_PACKAGE_FILES,
    MAX_PACKAGE_UNCOMPRESSED_BYTES,
    MAX_PACKAGE_ZIP_BYTES,
    PackageResource,
    SkillPackageError,
    SkillPackageManifest,
    SkillScriptDefinition,
)

_ALLOWED_ROOTS = {"scripts", "templates", "references"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ROOT_IGNORED_METADATA = {"readme.md", "requirements.txt"}
_SAFE_MEMBER_PATH_CHARACTERS = 240


@dataclass(frozen=True, slots=True)
class ValidatedSkillPackage:
    """通过 ZIP 与元数据校验、尚未写入数据库的不可变包。"""

    manifest: SkillPackageManifest
    package_sha256: str
    zip_size_bytes: int
    uncompressed_size_bytes: int


class SkillPackageValidator:
    """在读取流中执行路径、链接、数量、体积和压缩比限制。"""

    def __init__(
        self,
        *,
        maximum_ratio: float = 200.0,
    ) -> None:
        """保存可配置的整体压缩比阈值。"""
        if maximum_ratio < 1:
            raise ValueError("skill_zip_ratio_invalid")
        self._maximum_ratio = maximum_ratio

    def validate(self, path: Path) -> ValidatedSkillPackage:
        """读取 ZIP 一次并返回实际字节计数生成的 typed 清单。"""
        try:
            zip_size = path.stat().st_size
        except OSError as error:
            raise SkillPackageError("skill_zip_unreadable") from error
        if zip_size <= 0 or zip_size > MAX_PACKAGE_ZIP_BYTES:
            raise SkillPackageError("skill_zip_too_large")
        package_hash = hashlib.sha256()
        try:
            with path.open("rb") as package:
                while chunk := package.read(128 * 1024):
                    package_hash.update(chunk)
            with zipfile.ZipFile(path) as archive:
                return self._validate_archive(archive, zip_size, package_hash.hexdigest())
        except SkillPackageError:
            raise
        except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError) as error:
            raise SkillPackageError("skill_zip_invalid") from error

    def _validate_archive(
        self,
        archive: zipfile.ZipFile,
        zip_size: int,
        package_sha256: str,
    ) -> ValidatedSkillPackage:
        infos = archive.infolist()
        files: list[tuple[zipfile.ZipInfo, str]] = []
        normalized_files: set[str] = set()
        entity_count = 0
        total_uncompressed = 0
        total_compressed = 0
        for info in infos:
            path, segments = _validated_member_path(info)
            if info.flag_bits & 0x1:
                raise _member_error("skill_zip_encrypted", path)
            mode = (info.external_attr >> 16) & 0xFFFF
            # 常见工具只写权限位而不写 Unix 文件类型位；只有显式类型位存在时
            # 才据其拒绝链接和设备项，避免把普通 ZIP 误判成特殊文件。
            file_type = stat.S_IFMT(mode)
            if file_type and not (
                (info.is_dir() and stat.S_ISDIR(mode))
                or (not info.is_dir() and stat.S_ISREG(mode))
            ):
                raise _member_error("skill_zip_special_file", path)
            if info.is_dir():
                continue

            entity_count += 1
            if entity_count > MAX_PACKAGE_FILES:
                raise _member_error("skill_package_file_count_exceeded", path)
            if info.file_size > MAX_PACKAGE_FILE_BYTES:
                raise _member_error("skill_file_too_large", path)
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                raise _member_error("skill_package_too_large", path)
            if (
                info.file_size > 0
                and (info.compress_size == 0 or info.file_size / info.compress_size > 1000)
            ):
                raise _member_error("skill_zip_ratio_exceeded", path)
            total_compressed += max(1, info.compress_size)

            normalized = unicodedata.normalize("NFC", path).casefold()
            if _is_ignored_metadata(segments):
                # 忽略项不展开，也不进入资源重复或 manifest 状态。
                continue
            if normalized in normalized_files:
                raise _member_error("skill_zip_path_duplicate", path)
            normalized_files.add(normalized)
            _validate_manifest_path(path, segments)
            files.append((info, path))
        if not files:
            raise SkillPackageError("skill_entry_missing")
        if total_uncompressed / total_compressed > self._maximum_ratio:
            raise SkillPackageError("skill_zip_ratio_exceeded")
        if not any(path == "SKILL.md" for _, path in files):
            raise SkillPackageError("skill_entry_missing")
        guide_info = next(info for info, path in files if path == "SKILL.md")
        guide_bytes = _read_bounded(archive, guide_info)
        try:
            metadata, guide = _parse_skill_markdown(guide_bytes)
            scripts = _parse_scripts(metadata.get("scripts"))
            dependencies = _parse_dependencies(metadata.get("dependencies"))
            name = _required_text(metadata, "name", 120)
            description = _required_text(metadata, "description", 1000)
            slug = _required_text(metadata, "slug", 64)
        except SkillPackageError as error:
            if error.member_path is not None:
                raise
            raise SkillPackageError(error.code, member_path="SKILL.md") from error
        resources: list[PackageResource] = []
        counted = 0
        for info, path in files:
            content_hash = hashlib.sha256()
            size = 0
            try:
                with archive.open(info, "r") as member:
                    while chunk := member.read(128 * 1024):
                        size += len(chunk)
                        counted += len(chunk)
                        if size > MAX_PACKAGE_FILE_BYTES:
                            raise _member_error("skill_file_too_large", path)
                        if counted > MAX_PACKAGE_UNCOMPRESSED_BYTES:
                            raise _member_error("skill_package_too_large", path)
                        content_hash.update(chunk)
            except (OSError, RuntimeError, zipfile.BadZipFile) as error:
                raise _member_error("skill_zip_invalid", path) from error
            if size != info.file_size:
                raise _member_error("skill_zip_size_mismatch", path)
            resources.append(
                PackageResource(
                    path=path,
                    size_bytes=size,
                    sha256=content_hash.hexdigest(),
                    text=_is_text_resource(path),
                )
            )
        manifest = SkillPackageManifest(
            slug=slug,
            name=name,
            description=description,
            dependencies=dependencies,
            guide=guide,
            scripts=scripts,
            files=tuple(resources),
        )
        return ValidatedSkillPackage(
            manifest=manifest,
            package_sha256=package_sha256,
            zip_size_bytes=zip_size,
            uncompressed_size_bytes=total_uncompressed,
        )


def validate_parameter_schema(schema: dict[str, Any], value: Any) -> str | None:
    """用 Draft 2020-12 验证一次调用参数，根 Schema 保持对象语义。"""
    effective_schema = _checked_parameter_schema(schema)
    try:
        errors = sorted(
            Draft202012Validator(
                effective_schema,
                registry=Registry(),
            ).iter_errors(value),
            key=lambda item: list(item.absolute_path),
        )
    except Unresolvable as error:
        raise SkillPackageError("skill_parameter_schema_reference_unresolved") from error
    if not errors:
        return None
    validation_error = errors[0]
    path = ".".join(str(item) for item in validation_error.absolute_path)
    return (
        f"{path}: {validation_error.message}"
        if path
        else validation_error.message
    )


def _checked_parameter_schema(schema: Any) -> dict[str, Any]:
    """校验标准 Schema 结构，并为省略 type 的根补对象语义副本。"""
    if not isinstance(schema, dict):
        raise SkillPackageError("skill_parameter_schema_invalid")
    if schema.get("type", "object") != "object":
        raise SkillPackageError("skill_parameter_schema_invalid")
    effective_schema = schema if "type" in schema else {**schema, "type": "object"}
    try:
        Draft202012Validator.check_schema(effective_schema)
    except SchemaError as error:
        raise SkillPackageError("skill_parameter_schema_invalid") from error
    return effective_schema


def _validated_member_path(info: zipfile.ZipInfo) -> tuple[str, tuple[str, ...]]:
    """只使用原始成员名做未归一化路径安全检查。"""
    raw = info.orig_filename
    if not raw or "\\" in raw or _CONTROL.search(raw):
        raise _member_error("skill_zip_path_invalid", raw)
    path = raw[:-1] if raw.endswith("/") else raw
    segments = tuple(path.split("/"))
    if (
        not path
        or path.startswith("/")
        or (len(path) >= 2 and path[1] == ":")
        or any(part in {"", ".", ".."} for part in segments)
    ):
        raise _member_error("skill_zip_path_invalid", raw)
    if len(raw) > 512:
        raise _member_error("skill_zip_path_invalid", raw)
    return path, segments


def _validate_manifest_path(path: str, segments: tuple[str, ...]) -> None:
    """正常资源只允许根 SKILL.md 或声明的资源目录。"""
    if path != "SKILL.md" and segments[0] not in _ALLOWED_ROOTS:
        raise _member_error("skill_zip_path_invalid", path)


def _is_ignored_metadata(segments: tuple[str, ...]) -> bool:
    """仅忽略明确的三类打包元数据，不扩大隐藏文件范围。"""
    if "__MACOSX" in segments or segments[-1] == ".DS_Store":
        return True
    return len(segments) == 1 and segments[0].casefold() in _ROOT_IGNORED_METADATA


def _member_error(code: str, raw_member_path: str) -> SkillPackageError:
    """创建只携带安全包内路径的成员级错误。"""
    return SkillPackageError(code, member_path=_safe_member_path(raw_member_path))


def _safe_member_path(raw_member_path: str) -> str | None:
    """转义控制字符并限制详情长度，原始 ZIP 名称不直接外露。"""
    if not raw_member_path:
        return None
    escaped = "".join(
        character
        if character.isprintable()
        else character.encode("unicode_escape", errors="backslashreplace").decode("ascii")
        for character in raw_member_path
    )
    if len(escaped) > _SAFE_MEMBER_PATH_CHARACTERS:
        return escaped[: _SAFE_MEMBER_PATH_CHARACTERS - 3] + "..."
    return escaped


def _read_bounded(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    maximum = min(MAX_PACKAGE_FILE_BYTES, (MAX_GUIDE_CHARACTERS * 4) + 64 * 1024)
    if info.file_size > maximum:
        raise _member_error("skill_guide_too_large", info.orig_filename)
    try:
        with archive.open(info, "r") as member:
            content = member.read(maximum + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise _member_error("skill_zip_invalid", info.orig_filename) from error
    if len(content) > maximum:
        raise _member_error("skill_guide_too_large", info.orig_filename)
    return content


def _parse_skill_markdown(content: bytes) -> tuple[dict[str, Any], str]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SkillPackageError("skill_guide_encoding_invalid") from error
    if not text.startswith("---\n"):
        raise SkillPackageError("skill_metadata_missing")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillPackageError("skill_metadata_invalid")
    raw_metadata = text[4:end]
    guide = text[end + 5 :]
    if not guide.strip() or len(guide) > MAX_GUIDE_CHARACTERS:
        raise SkillPackageError("skill_guide_invalid")
    try:
        metadata = yaml.safe_load(raw_metadata)
    except yaml.YAMLError as error:
        raise SkillPackageError("skill_metadata_invalid") from error
    if not isinstance(metadata, dict):
        raise SkillPackageError("skill_metadata_invalid")
    return metadata, guide


def _required_text(metadata: dict[str, Any], key: str, maximum: int) -> str:
    value = metadata.get(key)
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _CONTROL.search(value)
    ):
        raise SkillPackageError("skill_metadata_invalid")
    return value


def _parse_dependencies(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillPackageError("skill_dependency_invalid")
    return tuple(value)


def _parse_scripts(value: Any) -> tuple[SkillScriptDefinition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SkillPackageError("skill_scripts_invalid")
    scripts: list[SkillScriptDefinition] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "label", "purpose", "parameters"}:
            raise SkillPackageError("skill_scripts_invalid")
        _checked_parameter_schema(item["parameters"])
        try:
            scripts.append(SkillScriptDefinition.model_validate(item))
        except ValueError as error:
            raise SkillPackageError("skill_scripts_invalid") from error
    return tuple(scripts)


def _is_text_resource(path: str) -> bool:
    return path.lower().endswith((".txt", ".md", ".csv", ".json", ".log", ".yaml", ".yml"))


__all__ = [
    "SkillPackageValidator",
    "ValidatedSkillPackage",
    "validate_parameter_schema",
]
