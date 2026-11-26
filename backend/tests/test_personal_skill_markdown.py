"""个人 Skill 的单文件 Markdown 协议与纯库不可变清单测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.skill.personal import (
    PersonalSkillError,
    build_markdown_manifest,
    parse_personal_markdown,
    validate_personal_content,
)
from domain.skill import MAX_GUIDE_CHARACTERS, MAX_PERSONAL_MARKDOWN_BYTES


def test_parse_plain_markdown_keeps_body_and_empty_metadata(tmp_path: Path) -> None:
    """无 frontmatter 的任意大小写 Markdown 直接作为正文。"""
    path = tmp_path / "weekly.MD"
    path.write_text("# 周报\n\n只使用已提供的事实。", encoding="utf-8")

    draft = parse_personal_markdown("weekly.MD", path)

    assert draft.name == ""
    assert draft.description == ""
    assert draft.guide == "# 周报\n\n只使用已提供的事实。"


@pytest.mark.parametrize(
    "metadata",
    ("", "普通正文\n", "- 列表正文\n"),
    ids=("empty", "scalar", "sequence"),
)
def test_parse_complete_horizontal_rule_body_keeps_full_text(
    tmp_path: Path,
    metadata: str,
) -> None:
    """完整分隔线之间不是 YAML 映射时，整段 Markdown 仍作为正文。"""
    text = f"---\n{metadata}---\n后续正文\n"
    path = tmp_path / "horizontal-rule.md"
    path.write_text(text, encoding="utf-8")

    draft = parse_personal_markdown("horizontal-rule.md", path)

    assert draft.name == ""
    assert draft.description == ""
    assert draft.guide == text


def test_parse_unclosed_leading_horizontal_rule_keeps_full_text(tmp_path: Path) -> None:
    """开头分隔线未完整闭合时，不按 frontmatter 删除任何正文。"""
    text = "---\n\n没有闭合分隔线的正文\n"
    path = tmp_path / "unclosed.md"
    path.write_text(text, encoding="utf-8")

    draft = parse_personal_markdown("unclosed.md", path)

    assert draft.name == ""
    assert draft.description == ""
    assert draft.guide == text


def test_parse_frontmatter_accepts_slug_and_removes_metadata(tmp_path: Path) -> None:
    """frontmatter 只读取允许字段，slug 兼容但不参与个人身份。"""
    path = tmp_path / "skill.md"
    path.write_text(
        "---\n"
        "name: 周报整理\n"
        "description: 按事实整理周报\n"
        "slug: legacy-slug\n"
        "---\n"
        "正文\n",
        encoding="utf-8",
    )

    draft = parse_personal_markdown("skill.md", path)

    assert draft.name == "周报整理"
    assert draft.description == "按事实整理周报"
    assert draft.guide == "正文\n"


@pytest.mark.parametrize(
    "field",
    ("scripts", "dependencies", "unknown"),
)
def test_parse_rejects_unsupported_frontmatter_fields(tmp_path: Path, field: str) -> None:
    """脚本、依赖及其他未知声明即使为空也必须拒绝。"""
    path = tmp_path / "skill.md"
    path.write_text(
        f"---\nname: demo\n{field}: []\n---\n正文",
        encoding="utf-8",
    )

    with pytest.raises(PersonalSkillError, match="skill_markdown_field_unsupported"):
        parse_personal_markdown("skill.md", path)


def test_parse_rejects_wrong_extension_encoding_and_size(tmp_path: Path) -> None:
    """扩展名、UTF-8 和 1 MiB 字节上限均在解析入口校验。"""
    wrong_extension = tmp_path / "skill.py"
    wrong_extension.write_text("正文", encoding="utf-8")
    with pytest.raises(PersonalSkillError, match="skill_markdown_filename_invalid"):
        parse_personal_markdown("skill.py", wrong_extension)

    invalid_encoding = tmp_path / "invalid.md"
    invalid_encoding.write_bytes(b"\xff")
    with pytest.raises(PersonalSkillError, match="skill_markdown_encoding_invalid"):
        parse_personal_markdown("invalid.md", invalid_encoding)

    oversized = tmp_path / "oversized.md"
    oversized.write_bytes(b"a" * (MAX_PERSONAL_MARKDOWN_BYTES + 1))
    with pytest.raises(PersonalSkillError, match="skill_markdown_too_large"):
        parse_personal_markdown("oversized.md", oversized)


def test_structured_content_limits_and_package_manifest() -> None:
    """在线字段执行正文上限，纯 Markdown 清单只含 SKILL.md 且直接计算正文 UTF-8 SHA-256。"""
    draft = validate_personal_content("名称", "描述", "业务正文")
    manifest, package_sha256 = build_markdown_manifest("personal-demo", draft)

    expected_sha256 = hashlib.sha256("业务正文".encode()).hexdigest()
    assert manifest.dependencies == ()
    assert manifest.scripts == ()
    assert manifest.guide == "业务正文"
    assert [item.path for item in manifest.files] == ["SKILL.md"]
    assert manifest.files[0].sha256 == expected_sha256
    assert manifest.files[0].size_bytes == len("业务正文".encode())
    assert package_sha256 == expected_sha256

    with pytest.raises(PersonalSkillError, match="skill_guide_invalid"):
        validate_personal_content("名称", "描述", "x" * (MAX_GUIDE_CHARACTERS + 1))
