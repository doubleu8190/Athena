"""纯 Markdown Skill 的单文件协议校验与不可变清单构造。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml  # type: ignore[import-untyped]

from domain.skill import (
    MAX_GUIDE_CHARACTERS,
    MAX_PERSONAL_MARKDOWN_BYTES,
    MAX_SKILL_DESCRIPTION_CHARACTERS,
    MAX_SKILL_NAME_CHARACTERS,
    MAX_SKILL_STARTER_PROMPT_CHARACTERS,
    PackageResource,
    SkillPackageError,
    SkillPackageManifest,
)

_ALLOWED_METADATA_FIELDS = frozenset({"name", "description", "slug"})
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ALL_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f]")
_SKILL_SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_MAX_VERSION_LABEL_CHARACTERS = 64
_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<metadata>.*?)(?:\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


class PersonalSkillError(ValueError):
    """个人 Markdown 或结构化内容未通过稳定业务校验。"""

    def __init__(self, code: str, *, field: str | None = None) -> None:
        """保存可供前端投影的安全错误码及可选表单字段。"""
        super().__init__(code)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class PersonalSkillDraft:
    """解析后进入编辑表单的名称、描述和 Markdown 正文。"""

    name: str
    description: str
    guide: str


def parse_personal_markdown(filename: str, path: Path) -> PersonalSkillDraft:
    """解析单个 UTF-8 Markdown，并允许 frontmatter 缺省名称和描述。"""
    if PurePosixPath(filename.replace("\\", "/")).suffix.casefold() != ".md":
        raise PersonalSkillError("skill_markdown_filename_invalid", field="file")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise PersonalSkillError("skill_markdown_unreadable", field="file") from error
    if size > MAX_PERSONAL_MARKDOWN_BYTES:
        raise PersonalSkillError("skill_markdown_too_large", field="file")
    if size == 0:
        raise PersonalSkillError("skill_markdown_empty", field="file")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise PersonalSkillError("skill_markdown_unreadable", field="file") from error
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise PersonalSkillError("skill_markdown_encoding_invalid", field="file") from error
    _validate_markdown_text(text)

    name = ""
    description = ""
    guide = text
    if text.startswith("---"):
        matched = _FRONTMATTER.match(text)
        if matched is not None:
            metadata = _load_metadata(matched.group("metadata"))
            if metadata is not None:
                # 只有完整闭合且中间确为 YAML 映射时才消费分隔线，避免把水平线正文误删。
                name = _metadata_text(metadata, "name", MAX_SKILL_NAME_CHARACTERS)
                description = _metadata_text(
                    metadata,
                    "description",
                    MAX_SKILL_DESCRIPTION_CHARACTERS,
                )
                guide = text[matched.end() :]
    _validate_guide(guide)
    return PersonalSkillDraft(name=name, description=description, guide=guide)


def validate_personal_content(name: str, description: str, guide: str) -> PersonalSkillDraft:
    """校验在线创建或新版本的三个字段，并返回规范化表单值。"""
    if not isinstance(name, str):
        raise PersonalSkillError("skill_name_invalid", field="name")
    if not isinstance(description, str):
        raise PersonalSkillError("skill_description_invalid", field="description")
    if not isinstance(guide, str):
        raise PersonalSkillError("skill_guide_invalid", field="guide")
    normalized_name = name.strip()
    normalized_description = description.strip()
    if (
        not normalized_name
        or len(normalized_name) > MAX_SKILL_NAME_CHARACTERS
        or _CONTROL_CHARACTER.search(normalized_name)
    ):
        raise PersonalSkillError("skill_name_invalid", field="name")
    if (
        not normalized_description
        or len(normalized_description) > MAX_SKILL_DESCRIPTION_CHARACTERS
        or _CONTROL_CHARACTER.search(normalized_description)
    ):
        raise PersonalSkillError("skill_description_invalid", field="description")
    _validate_guide(guide)
    return PersonalSkillDraft(
        name=normalized_name,
        description=normalized_description,
        guide=guide,
    )


def build_markdown_manifest(
    slug: str,
    draft: PersonalSkillDraft,
) -> tuple[SkillPackageManifest, str]:
    """将已校验 Markdown 正文构造为纯数据库持久化的清单与 UTF-8 SHA-256 摘要（零 ZIP/MinIO）。"""
    guide_bytes = draft.guide.encode("utf-8")
    guide_sha256 = hashlib.sha256(guide_bytes).hexdigest()
    manifest = SkillPackageManifest(
        slug=slug,
        name=draft.name,
        description=draft.description,
        dependencies=(),
        guide=draft.guide,
        scripts=(),
        files=(
            PackageResource(
                path="SKILL.md",
                size_bytes=len(guide_bytes),
                sha256=guide_sha256,
                text=True,
            ),
        ),
    )
    return manifest, guide_sha256


def validate_starter_prompt(value: str | None) -> str | None:
    """校验首页快捷应用起始引导语；空值或纯空白归一为 None，超长或含非法控制字符拒绝。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkillPackageError("skill_starter_prompt_invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > MAX_SKILL_STARTER_PROMPT_CHARACTERS or _CONTROL_CHARACTER.search(
        normalized
    ):
        raise SkillPackageError("skill_starter_prompt_invalid")
    return normalized


def validate_optional_admin_slug(value: str | None) -> str | None:
    """校验管理端在线 Markdown 可选 slug；未提供或纯空白返回 None 交由服务端派生。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkillPackageError("skill_slug_invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if _SKILL_SLUG_PATTERN.fullmatch(normalized) is None:
        raise SkillPackageError("skill_slug_invalid")
    return normalized


def build_admin_markdown_slug(skill_id: str) -> str:
    """由稳定 skill_id 自动生成严格符合 slug 规范的不碰撞标识（md-<小写字母数字>）。"""
    tail = skill_id.removeprefix("skill_")
    cleaned = re.sub(r"[^a-z0-9]", "", tail.casefold())
    digest = hashlib.sha256(skill_id.encode("utf-8")).hexdigest()
    if not cleaned:
        return f"md-{digest[:32]}"
    if len(cleaned) <= 61:
        return f"md-{cleaned}"
    return f"md-{cleaned[-48:]}-{digest[:12]}"


def validate_version_label(value: str | None) -> str | None:
    """校验在线 Markdown 版本标签；未提供或空白返回 None，否则须 <= 64 字符且无控制字符。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SkillPackageError("skill_version_label_invalid")
    if _ALL_CONTROL_CHARACTER.search(value):
        raise SkillPackageError("skill_version_label_invalid")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > _MAX_VERSION_LABEL_CHARACTERS:
        raise SkillPackageError("skill_version_label_invalid")
    return normalized


def _validate_markdown_text(text: str) -> None:
    """拒绝 Markdown 文件中的非法控制字符。"""
    if _CONTROL_CHARACTER.search(text):
        raise PersonalSkillError("skill_markdown_control_character", field="file")


def _load_metadata(raw_metadata: str) -> dict[str, Any] | None:
    """安全读取 YAML frontmatter；非映射返回空值以保留原正文。"""
    try:
        value = yaml.safe_load(raw_metadata)
    except yaml.YAMLError as error:
        raise PersonalSkillError("skill_markdown_metadata_invalid", field="file") from error
    if not isinstance(value, dict):
        return None
    metadata = {str(key): item for key, item in value.items()}
    unsupported = sorted(set(metadata) - _ALLOWED_METADATA_FIELDS)
    if unsupported:
        raise PersonalSkillError(
            "skill_markdown_field_unsupported",
            field=unsupported[0],
        )
    slug = metadata.get("slug")
    if slug is not None and not isinstance(slug, str):
        raise PersonalSkillError("skill_markdown_slug_invalid", field="slug")
    return metadata


def _metadata_text(metadata: dict[str, Any], field: str, maximum: int) -> str:
    """读取可选文本元数据；未提供或空白时保留为空串。"""
    value = metadata.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PersonalSkillError(f"skill_markdown_{field}_invalid", field=field)
    normalized = value.strip()
    if len(normalized) > maximum or _CONTROL_CHARACTER.search(normalized):
        raise PersonalSkillError(f"skill_markdown_{field}_invalid", field=field)
    return normalized


def _validate_guide(guide: str) -> None:
    """要求正文非空、字符数有界且不含非法控制字符。"""
    if not guide.strip() or len(guide) > MAX_GUIDE_CHARACTERS:
        raise PersonalSkillError("skill_guide_invalid", field="guide")
    if _CONTROL_CHARACTER.search(guide):
        raise PersonalSkillError("skill_guide_control_character", field="guide")


__all__ = [
    "MAX_PERSONAL_MARKDOWN_BYTES",
    "PersonalSkillDraft",
    "PersonalSkillError",
    "build_admin_markdown_slug",
    "build_markdown_manifest",
    "parse_personal_markdown",
    "validate_optional_admin_slug",
    "validate_personal_content",
    "validate_starter_prompt",
    "validate_version_label",
]
