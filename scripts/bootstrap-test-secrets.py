#!/usr/bin/env python3
"""将忽略文件中的本地测试凭据迁移到权限受限的 secret 文件。"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "docker" / "test.env"
CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "docker" / "test.env.example"
SECRETS_DIRECTORY = PROJECT_ROOT / "secrets"
SENSITIVE_KEYS = {
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "ATHENA_DATABASE_URL",
    "ATHENA_TEST_DATABASE_URL",
    "ATHENA_STORAGE_ACCESS_KEY",
    "ATHENA_STORAGE_SECRET_KEY",
    "ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY",
    "ATHENA_KNOWLEDGE_MINIO_SECRET_KEY",
    "ATHENA_FILE_MINIO_ACCESS_KEY",
    "ATHENA_FILE_MINIO_SECRET_KEY",
    "ATHENA_TEST_MINIO_ACCESS_KEY",
    "ATHENA_TEST_MINIO_SECRET_KEY",
    "ATHENA_MCP_SECRET_KEY",
}
DERIVED_CONFIG_KEYS = {
    "ATHENA_STORAGE_ENDPOINT",
    "ATHENA_STORAGE_BUCKET",
    "ATHENA_KNOWLEDGE_MINIO_ENDPOINT",
    "ATHENA_FILE_MINIO_ENDPOINT",
    "ATHENA_TEST_MINIO_ENDPOINT",
    "ATHENA_KNOWLEDGE_MINIO_BUCKET",
    "ATHENA_FILE_MINIO_BUCKET",
    "ATHENA_TEST_MINIO_BUCKET",
}
SECRET_NAMES = (
    "postgres_user",
    "postgres_password",
    "minio_root_user",
    "minio_root_password",
    "mcp_secret_key",
)
DATABASE_URL_KEYS = ("ATHENA_DATABASE_URL", "ATHENA_TEST_DATABASE_URL")


def _parse_env_file(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 配置。

    参数：path 为忽略跟踪的本地配置文件路径。
    返回：键值映射；注释和空行不进入结果。
    异常：文件无法读取时由 pathlib 原样抛出异常。
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _generated_secret(name: str) -> str:
    """为未配置项创建合适长度的本地凭据。

    参数：name 是 secret 文件名，用于区分用户名、密码和 MCP 加密密钥。
    返回：不带换行符的凭据文本。
    异常：不主动抛出业务异常。
    """
    if name == "postgres_user":
        return "athena_dev"
    if name == "minio_root_user":
        return "athena_minio"
    if name == "mcp_secret_key":
        return base64.b64encode(secrets.token_bytes(32)).decode("ascii")
    return secrets.token_urlsafe(32)


def _read_existing_secret(name: str) -> str | None:
    """读取并检查已存在的 secret，不创建或修改文件。

    参数：name 是 `secrets/` 中的凭据文件名。
    返回：文件内容；文件不存在时返回 None。
    异常：文件类型、内容或权限不安全时抛出 ValueError，读取失败时抛出 OSError。
    """
    path = SECRETS_DIRECTORY / name
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError(f"secret 文件无效：{path.relative_to(PROJECT_ROOT)}")
    value = path.read_text(encoding="utf-8")
    if not value:
        raise ValueError(f"secret 文件为空：{path.relative_to(PROJECT_ROOT)}")
    if path.stat().st_mode & 0o077:
        raise ValueError(f"secret 文件权限过宽，请执行 chmod 600：{path.relative_to(PROJECT_ROOT)}")
    return value


def _parse_database_credentials(config_values: dict[str, str]) -> list[tuple[str, str]]:
    """解析旧连接 URL 中的 PostgreSQL 用户名和密码。

    参数：config_values 是本地配置文件中的键值映射。
    返回：每个有效数据库 URL 对应的用户名和密码。
    异常：URL 格式无效或缺少用户名/密码时抛出 ValueError。
    """
    credentials: list[tuple[str, str]] = []
    for key in DATABASE_URL_KEYS:
        raw_url = config_values.get(key)
        if not raw_url:
            continue
        try:
            parsed_url = urlsplit(raw_url)
            username = parsed_url.username
            password = parsed_url.password
        except ValueError as error:
            raise ValueError(f"{key} 格式无效，未迁移或清理旧配置") from error
        if not parsed_url.scheme.startswith("postgresql") or username is None or password is None:
            raise ValueError(f"{key} 必须包含 PostgreSQL 用户名和密码，未清理旧配置")
        credentials.append((unquote(username), unquote(password)))
    return credentials


def _has_existing_data() -> bool:
    """判断 Compose bind mount 目录中是否已有数据。

    参数：无。
    返回：任一数据库、Redis 或 MinIO 数据目录非空时返回 True。
    异常：目录无法检查时由 pathlib 原样抛出 OSError。
    """
    for name in ("postgres", "redis", "minio"):
        data_path = PROJECT_ROOT / "data" / name
        if data_path.is_dir() and next(data_path.iterdir(), None) is not None:
            return True
    return False


def _resolve_credential(label: str, candidates: list[str], generated_name: str) -> str:
    """确认多个旧凭据来源一致，或为全新环境生成凭据。

    参数：label 是错误提示中的凭据用途；candidates 是已有凭据候选值；
    generated_name 用于选择全新环境的默认值。
    返回：一致的旧值或新生成的值。
    异常：已有来源互相冲突时抛出 ValueError，错误中不包含凭据内容。
    """
    distinct_values = set(candidates)
    if len(distinct_values) > 1:
        raise ValueError(f"{label} 的旧配置与现有 secret 不一致，未写入或清理任何文件")
    return next(iter(distinct_values)) if distinct_values else _generated_secret(generated_name)


def _present_values(*values: str | None) -> list[str]:
    """移除不存在的凭据候选值。

    参数：values 是可选凭据来源。
    返回：按输入顺序保留的非空凭据值列表。
    异常：不主动抛出业务异常。
    """
    return [value for value in values if value]


def _write_secret_if_missing(path: Path, value: str) -> None:
    """只创建尚不存在的 secret 文件，并限制权限为当前用户可读写。

    参数：path 是 secret 目标；value 是预检确认后需要落盘的凭据。
    返回：无。
    异常：目标文件已存在但为空或权限过宽时抛出 ValueError，其他 I/O 错误原样抛出。
    """
    if path.exists():
        if not path.is_file():
            raise ValueError(f"secret 文件无效：{path.relative_to(PROJECT_ROOT)}")
        existing_value = path.read_text(encoding="utf-8")
        if not existing_value:
            raise ValueError(f"secret 文件为空：{path.relative_to(PROJECT_ROOT)}")
        if path.stat().st_mode & 0o077:
            relative_path = path.relative_to(PROJECT_ROOT)
            raise ValueError(f"secret 文件权限过宽，请执行 chmod 600：{relative_path}")
        return

    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as secret_file:
        secret_file.write(value)


def _remove_migrated_config_lines(path: Path) -> None:
    """清除已迁移凭据与应由 helper 派生的旧配置项。

    参数：path 是本地 `docker/test.env` 路径。
    返回：无；非敏感项及注释保持原样。
    异常：原子替换失败时由 pathlib/os 原样抛出异常。
    """
    retained_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("=", maxsplit=1)[0].strip()
        not in SENSITIVE_KEYS | DERIVED_CONFIG_KEYS
    ]
    temporary_path = path.with_suffix(".env.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        config_file.write("\n".join(retained_lines) + "\n")
    os.replace(temporary_path, path)
    path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    """预检所有来源后迁移凭据并初始化缺失的本地 secret。

    参数：argv 是可选命令行参数列表；仅全新本地环境可显式传入 `--fresh`。
    返回：成功时返回 0。
    异常：配置冲突、已有数据缺少旧凭据或文件权限无效时报告错误并返回 1。
    """
    try:
        parser = argparse.ArgumentParser(description="迁移 Docker 测试依赖的本地 secret")
        parser.add_argument(
            "--fresh",
            action="store_true",
            help="仅用于没有部署数据的新环境，允许生成缺失凭据",
        )
        arguments = parser.parse_args(argv)
        config_exists = CONFIG_PATH.exists()
        source_config = CONFIG_PATH if config_exists else CONFIG_TEMPLATE_PATH
        if not source_config.is_file():
            raise ValueError("未找到 docker/test.env 或其模板；未创建任何文件")
        legacy_values = _parse_env_file(source_config)
        existing = {name: _read_existing_secret(name) for name in SECRET_NAMES}
        database_credentials = _parse_database_credentials(legacy_values)

        database_users = [username for username, _ in database_credentials]
        database_passwords = [password for _, password in database_credentials]
        available_old_values = {
            "PostgreSQL 用户名": (
                existing["postgres_user"]
                or legacy_values.get("POSTGRES_USER")
                or (database_users[0] if database_users else None)
            ),
            "PostgreSQL 密码": (
                existing["postgres_password"]
                or legacy_values.get("POSTGRES_PASSWORD")
                or (database_passwords[0] if database_passwords else None)
            ),
            "MinIO 用户名": existing["minio_root_user"] or legacy_values.get("MINIO_ROOT_USER"),
            "MinIO 密码": (
                existing["minio_root_password"] or legacy_values.get("MINIO_ROOT_PASSWORD")
            ),
            "MCP 加密密钥": (
                existing["mcp_secret_key"] or legacy_values.get("ATHENA_MCP_SECRET_KEY")
            ),
        }
        missing_values = [label for label, value in available_old_values.items() if not value]
        if missing_values:
            missing = "、".join(missing_values)
            if not arguments.fresh:
                raise ValueError(f"缺少旧凭据：{missing}；仅全新环境可使用 --fresh 生成")
            if _has_existing_data():
                raise ValueError(f"data/ 中已有部署数据但缺少旧凭据：{missing}；请恢复原 secret")

        # 预检所有可清理的来源，发现冲突时不写 secret、不改配置文件。
        postgres_user = _resolve_credential(
            "PostgreSQL 用户名",
            _present_values(
                existing["postgres_user"], legacy_values.get("POSTGRES_USER"), *database_users
            ),
            "postgres_user",
        )
        postgres_password = _resolve_credential(
            "PostgreSQL 密码",
            _present_values(
                existing["postgres_password"],
                legacy_values.get("POSTGRES_PASSWORD"),
                *database_passwords,
            ),
            "postgres_password",
        )
        minio_user = _resolve_credential(
            "MinIO 用户名",
            _present_values(existing["minio_root_user"], legacy_values.get("MINIO_ROOT_USER")),
            "minio_root_user",
        )
        minio_password = _resolve_credential(
            "MinIO 密码",
            _present_values(
                existing["minio_root_password"],
                legacy_values.get("MINIO_ROOT_PASSWORD"),
            ),
            "minio_root_password",
        )
        mcp_secret_key = _resolve_credential(
            "MCP 加密密钥",
            _present_values(
                existing["mcp_secret_key"], legacy_values.get("ATHENA_MCP_SECRET_KEY")
            ),
            "mcp_secret_key",
        )
        for key, expected in (
            ("ATHENA_STORAGE_ACCESS_KEY", minio_user),
            ("ATHENA_KNOWLEDGE_MINIO_ACCESS_KEY", minio_user),
            ("ATHENA_FILE_MINIO_ACCESS_KEY", minio_user),
            ("ATHENA_TEST_MINIO_ACCESS_KEY", minio_user),
            ("ATHENA_STORAGE_SECRET_KEY", minio_password),
            ("ATHENA_KNOWLEDGE_MINIO_SECRET_KEY", minio_password),
            ("ATHENA_FILE_MINIO_SECRET_KEY", minio_password),
            ("ATHENA_TEST_MINIO_SECRET_KEY", minio_password),
        ):
            configured = legacy_values.get(key)
            if configured and configured != expected:
                raise ValueError(f"{key} 与 MinIO root secret 不一致，未写入或清理任何文件")
        for app_key, compose_key, default_bucket in (
            (
                "ATHENA_STORAGE_BUCKET",
                "MINIO_DEV_BUCKET",
                "athena-storage-dev",
            ),
            (
                "ATHENA_KNOWLEDGE_MINIO_BUCKET",
                "MINIO_DEV_BUCKET",
                "athena-storage-dev",
            ),
            (
                "ATHENA_TEST_MINIO_BUCKET",
                "MINIO_TEST_BUCKET",
                "athena-storage-test",
            ),
        ):
            configured_bucket = legacy_values.get(app_key)
            compose_bucket = legacy_values.get(compose_key) or default_bucket
            if configured_bucket and configured_bucket != compose_bucket:
                raise ValueError(
                    f"{app_key} 与 {compose_key} 不一致；请统一本地桶名后重试，旧配置已保留"
                )
        # MinIO 会拒绝长度不足的 root 凭据；在写入或清理旧配置前先验证。
        if len(minio_user) < 3:
            raise ValueError("MinIO root 用户名至少需要 3 个字符")
        if len(minio_password) < 8:
            raise ValueError("MinIO root 密码至少需要 8 个字符；请更新旧配置后重试")

        secret_values = {
            "postgres_user": postgres_user,
            "postgres_password": postgres_password,
            "minio_root_user": minio_user,
            "minio_root_password": minio_password,
            "mcp_secret_key": mcp_secret_key,
        }

        # 所有冲突和权限错误已在上方检查，通过后才创建配置、目录和凭据文件。
        if not config_exists:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CONFIG_TEMPLATE_PATH, CONFIG_PATH)
            CONFIG_PATH.chmod(0o600)
        SECRETS_DIRECTORY.mkdir(mode=0o700, parents=True, exist_ok=True)
        SECRETS_DIRECTORY.chmod(0o700)
        for name, value in secret_values.items():
            _write_secret_if_missing(SECRETS_DIRECTORY / name, value)
        _remove_migrated_config_lines(CONFIG_PATH)
    except (OSError, ValueError) as error:
        print(f"本地 secret 初始化失败：{error}", file=sys.stderr)
        return 1

    print("本地 secret 文件已就绪；凭据值未输出。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
