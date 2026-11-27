"""Skill ZIP、参数 Schema 与 PEP 508 依赖边界测试。"""

from __future__ import annotations

import stat
import sys
import warnings
import zipfile
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import pytest

from app.skill.dependency import DependencyChecker
from app.skill.package import SkillPackageValidator, validate_parameter_schema
from domain.skill import DependencyCheckStatus, SkillPackageError

CHECKED_AT = datetime(2026, 11, 12, tzinfo=UTC)


def _markdown(
    *,
    slug: str = "monthly-report",
    dependencies: str = "dependencies: []",
    parameters: str = """
      type: object
      additionalProperties: false
      properties:
        period:
          type: string
      required: [period]
""",
) -> bytes:
    """构造最小合法 SKILL.md 元数据与正文。"""
    content = f"""
name: 月度报告
slug: {slug}
description: 生成月度报表。
{dependencies}
scripts:
  - path: scripts/build.py
    label: 生成报表
    purpose: 按月份生成交付文件。
    parameters: {parameters}
---
按登记参数生成月度报表。
""".strip()
    return ("---\n" + content + "\n").encode()


def _zip(
    path: Path,
    *,
    markdown: bytes | None = None,
    extra_files: tuple[tuple[str, bytes, int], ...] = (),
) -> Path:
    """把资源和脚本写入磁盘 ZIP。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", markdown or _markdown())
        archive.writestr("scripts/build.py", "print('ok')\n")
        for name, content, mode in extra_files:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return path


def test_validator_accepts_permission_only_entries_and_hashes_actual_content(
    tmp_path: Path,
) -> None:
    package = _zip(
        tmp_path / "valid.zip",
        extra_files=(("references/example.txt", "示例\n".encode(), 0o600),),
    )

    validated = SkillPackageValidator().validate(package)

    assert validated.manifest.slug == "monthly-report"
    assert validated.manifest.guide.rstrip() == "按登记参数生成月度报表。"
    assert [item.path for item in validated.manifest.files] == [
        "SKILL.md",
        "scripts/build.py",
        "references/example.txt",
    ]
    reference = validated.manifest.files[-1]
    assert reference.text is True
    assert reference.size_bytes == len("示例\n".encode())
    assert len(reference.sha256) == 64


@pytest.mark.parametrize(
    ("name", "content", "mode", "code"),
    [
        (
            "../escape.txt",
            b"escape",
            stat.S_IFREG | 0o600,
            "skill_zip_path_invalid",
        ),
        (
            "references/link.txt",
            b"target.txt",
            stat.S_IFLNK | 0o777,
            "skill_zip_special_file",
        ),
        (
            "other/not-allowed.txt",
            b"outside",
            stat.S_IFREG | 0o600,
            "skill_zip_path_invalid",
        ),
    ],
)
def test_validator_rejects_escape_links_and_unknown_roots(
    tmp_path: Path,
    name: str,
    content: bytes,
    mode: int,
    code: str,
) -> None:
    package = _zip(
        tmp_path / "invalid.zip",
        extra_files=((name, content, mode),),
    )

    with pytest.raises(SkillPackageError, match=code) as error:
        SkillPackageValidator().validate(package)

    assert error.value.member_path == name


def test_validator_rejects_casefold_duplicate_and_excessive_ratio(
    tmp_path: Path,
) -> None:
    duplicate = _zip(
        tmp_path / "duplicate.zip",
        extra_files=(
            ("references/Report.txt", b"one", stat.S_IFREG | 0o600),
            ("references/report.txt", b"two", stat.S_IFREG | 0o600),
        ),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_duplicate") as error:
        SkillPackageValidator().validate(duplicate)

    assert error.value.member_path == "references/report.txt"

    compressible = tmp_path / "compressible.zip"
    with zipfile.ZipFile(compressible, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", _markdown())
        archive.writestr("scripts/build.py", "print('ok')\n")
        archive.writestr("references/data.txt", b"a" * 256_000)
    with pytest.raises(SkillPackageError, match="skill_zip_ratio_exceeded"):
        SkillPackageValidator(maximum_ratio=1.01).validate(compressible)


def test_validator_ignores_only_explicit_packaging_metadata(tmp_path: Path) -> None:
    """忽略打包元数据，同时保留目录内 README、requirements 和其他隐藏文件。"""
    mode = stat.S_IFREG | 0o600
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:")
        package = _zip(
            tmp_path / "metadata.zip",
            extra_files=(
                ("__MACOSX/._build.py", b"mac", mode),
                ("scripts/__MACOSX/._script.py", b"mac", mode),
                ("scripts/.DS_Store", b"store-1", mode),
                ("scripts/.DS_Store", b"store-2", mode),
                ("references/nested/.DS_Store", b"store", mode),
                ("README.MD", b"root readme", mode),
                ("Requirements.TXT", b"root requirements", mode),
                ("scripts/README.md", b"directory readme", mode),
                ("references/requirements.txt", b"directory requirements", mode),
                ("references/.env", b"TOKEN=test", mode),
                ("scripts/.other", b"not ignored", mode),
            ),
        )

    validated = SkillPackageValidator().validate(package)
    paths = [item.path for item in validated.manifest.files]

    assert paths == [
        "SKILL.md",
        "scripts/build.py",
        "scripts/README.md",
        "references/requirements.txt",
        "references/.env",
        "scripts/.other",
    ]
    assert validated.uncompressed_size_bytes > sum(
        item.size_bytes for item in validated.manifest.files
    )


def test_ignored_metadata_paths_still_receive_raw_path_security_checks(
    tmp_path: Path,
) -> None:
    """忽略规则不能绕过原始路径穿越和特殊文件检查。"""
    traversal = _zip(
        tmp_path / "traversal.zip",
        extra_files=(("../README.md", b"escape", stat.S_IFREG | 0o600),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(traversal)
    assert error.value.member_path == "../README.md"

    link = _zip(
        tmp_path / "ignored-link.zip",
        extra_files=(("scripts/__MACOSX/link", b"target", stat.S_IFLNK | 0o777),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_special_file") as error:
        SkillPackageValidator().validate(link)
    assert error.value.member_path == "scripts/__MACOSX/link"

    empty_segment = _zip(
        tmp_path / "empty-segment.zip",
        extra_files=(("scripts//bad.py", b"bad", stat.S_IFREG | 0o600),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(empty_segment)
    assert error.value.member_path == "scripts//bad.py"


def test_member_path_detail_escapes_controls_and_truncates(tmp_path: Path) -> None:
    """成员路径详情可转义控制字符并限制长度，不影响原错误字符串。"""
    unicode_path = _zip(
        tmp_path / "unicode.zip",
        extra_files=(("附件/报告.txt", b"bad", stat.S_IFREG | 0o600),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(unicode_path)
    assert error.value.member_path == "附件/报告.txt"

    control = _zip(
        tmp_path / "control.zip",
        extra_files=(("scripts/bad\x01.py", b"bad", stat.S_IFREG | 0o600),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(control)
    assert str(error.value) == "skill_zip_path_invalid"
    assert error.value.member_path == "scripts/bad\\x01.py"

    overlong = _zip(
        tmp_path / "overlong.zip",
        extra_files=((f"other/{'a' * 600}.txt", b"bad", stat.S_IFREG | 0o600),),
    )
    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(overlong)
    assert error.value.member_path is not None
    assert len(error.value.member_path) <= 240
    assert error.value.member_path.endswith("...")


def test_ignored_metadata_still_consumes_member_count_budget(tmp_path: Path) -> None:
    """重复忽略项不报重复，但仍计入归档实体数量预算。"""
    metadata = ("scripts/.DS_Store", b"x", stat.S_IFREG | 0o600)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:")
        package = _zip(tmp_path / "count.zip", extra_files=(metadata,) * 499)

    with pytest.raises(
        SkillPackageError,
        match="skill_package_file_count_exceeded",
    ) as error:
        SkillPackageValidator().validate(package)

    assert error.value.member_path == "scripts/.DS_Store"


def test_macosx_component_is_case_sensitive(tmp_path: Path) -> None:
    """仅精确 __MACOSX 分量是元数据目录，其他大小写仍按普通非法根处理。"""
    package = _zip(
        tmp_path / "case.zip",
        extra_files=(("__macosx/._build.py", b"wrong case", stat.S_IFREG | 0o600),),
    )

    with pytest.raises(SkillPackageError, match="skill_zip_path_invalid") as error:
        SkillPackageValidator().validate(package)
    assert error.value.member_path == "__macosx/._build.py"


@pytest.mark.parametrize(
    ("requirements", "code"),
    [
        (("not a requirement !",), "skill_dependency_invalid"),
        (("demo @ https://example.test/demo.whl",), "skill_dependency_url_unsupported"),
        (("demo[feature]>=1",), "skill_dependency_extras_unsupported"),
    ],
)
def test_dependency_checker_rejects_unsupported_requirement_forms(
    requirements: tuple[str, ...],
    code: str,
) -> None:
    checker = DependencyChecker(clock=lambda: CHECKED_AT)

    with pytest.raises(SkillPackageError, match=code):
        checker.validate_requirements(requirements)


def test_dependency_checker_uses_target_distribution_and_markers() -> None:
    """目标进程探测会读取真实发行包，而非 API 进程内 monkeypatch 的元数据。"""
    checker = DependencyChecker(
        python_executable=sys.executable,
        clock=lambda: CHECKED_AT,
    )

    check = checker.check(
        (
            "packaging>=0",
            "impossible-bandit==1; python_version < '1'",
        )
    )

    assert check.status is DependencyCheckStatus.SATISFIED
    assert [item.package for item in check.items] == [
        "packaging",
        "impossible-bandit",
    ]
    assert check.items[0].installed_version == metadata.version("packaging")
    assert check.items[1].marker_applies is False


def test_parameter_schema_accepts_standard_composition_and_local_refs() -> None:
    """根省略 type 时按 object，并允许标准组合、本地 ref 和 boolean 子 schema。"""
    schema: dict[str, object] = {
        "$defs": {
            "common": {
                "type": "object",
                "properties": {"code": {"type": "string", "pattern": "^[A-Z]{2}$"}},
                "required": ["code"],
            },
            "open": True,
            "closed": False,
        },
        "allOf": [{"$ref": "#/$defs/common"}],
        "anyOf": [
            {"properties": {"mode": {"const": "read"}}},
            {"properties": {"mode": {"const": "write"}}},
        ],
        "properties": {
            "mode": {"type": "string"},
            "optional": {"$ref": "#/$defs/open"},
            "blocked": {"$ref": "#/$defs/closed"},
        },
        "required": ["mode"],
    }

    assert validate_parameter_schema(schema, {"code": "AB", "mode": "read"}) is None
    assert "code" in (validate_parameter_schema(schema, {"code": "bad", "mode": "other"}) or "")
    assert validate_parameter_schema(schema, []) is not None
    assert "blocked" in (
        validate_parameter_schema(
            schema,
            {"code": "AB", "mode": "read", "blocked": True},
        )
        or ""
    )


def test_parameter_schema_format_is_annotation_by_default() -> None:
    """未显式配置 format checker 时，format 只作为标准注解。"""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"email": {"type": "string", "format": "email"}},
        "required": ["email"],
    }

    assert validate_parameter_schema(schema, {"email": "not-an-email"}) is None


def test_parameter_schema_rejects_explicit_non_object_root() -> None:
    """根 Schema 显式声明 array 时拒绝，不用默认 object 覆盖。"""
    with pytest.raises(SkillPackageError, match="skill_parameter_schema_invalid"):
        validate_parameter_schema({"type": "array"}, [])


def test_parameter_schema_rejects_invalid_schema() -> None:
    """Draft 2020-12 结构非法时在参数验证前稳定失败。"""
    with pytest.raises(SkillPackageError, match="skill_parameter_schema_invalid"):
        validate_parameter_schema({"type": "object", "properties": []}, {})


def test_parameter_schema_external_ref_fails_without_network() -> None:
    """空 Registry 不允许外部引用，异常不包含目标 URL。"""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "value": {"$ref": "https://example.invalid/private-schema.json"},
        },
    }

    with pytest.raises(
        SkillPackageError,
        match="skill_parameter_schema_reference_unresolved",
    ) as error:
        validate_parameter_schema(schema, {"value": 1})

    assert "example.invalid" not in str(error.value)


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), object()],
)
def test_parameter_schema_rejects_nan_infinity_and_non_json_serializable(
    bad_value: object,
) -> None:
    """NaN、Infinity 与不可 JSON 序列化值必须在序列化阶段转为 skill_parameter_schema_invalid。"""
    schema: dict[str, object] = {
        "type": "object",
        "properties": {"value": {"default": bad_value}},
    }

    with pytest.raises(SkillPackageError, match="skill_parameter_schema_invalid"):
        validate_parameter_schema(schema, {})
