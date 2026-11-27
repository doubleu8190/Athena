from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _write_test_project(root: Path) -> None:
    scripts = root / "scripts"
    docker = root / "docker"
    secrets = root / "secrets"
    scripts.mkdir()
    docker.mkdir()
    secrets.mkdir(mode=0o700)
    shutil.copy2(PROJECT_ROOT / "scripts" / "load-test-env.sh", scripts)
    shutil.copy2(PROJECT_ROOT / "scripts" / "test-docker.sh", scripts)
    (root / "compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "compose.test.yml").write_text("services: {}\n", encoding="utf-8")
    (root / ".env").write_text("ROOT_ONLY=from-root-dotenv\n", encoding="utf-8")
    (root / ".env.test").write_text(
        "POSTGRES_TEST_DB=from-generated-env\n", encoding="utf-8"
    )
    (docker / "test.env").write_text(
        "\n".join(
            (
                "PORT=18080",
                "POSTGRES_DB='athena dev@primary'",
                "POSTGRES_TEST_DB='athena test@primary'",
                "POSTGRES_PORT=15432",
                "REDIS_PORT=16379",
                "MINIO_API_PORT=19000",
                "MINIO_CONSOLE_PORT=19001",
                "MINIO_DEV_BUCKET=tenant-storage-dev",
                "MINIO_TEST_BUCKET=tenant-storage-test",
                "ATHENA_COOKIE_SECURE=false",
                "ATHENA_STORAGE_SECURE=false",
                "ATHENA_TEST_MINIO_SECURE=false",
                "ATHENA_INSTALL_KNOWLEDGE=false",
                "ATHENA_EMBEDDING_BASE_URL=http://host.docker.internal:11434/v1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    secret_values = {
        "postgres_user": "db-user@example.test",
        "postgres_password": "p@ss:word/with?#&=\"quotes\"",
        "minio_root_user": "minio-user@example.test",
        "minio_root_password": "m:in/io@secret#2026",
        "mcp_secret_key": "mcp+/key==",
    }
    for name, value in secret_values.items():
        path = secrets / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)


@pytest.mark.parametrize("shell_name", ("bash", "zsh"))
def test_container_environment_encodes_credentials_and_uses_test_names(
    tmp_path: Path, shell_name: str
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _write_test_project(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "ATHENA_TEST_ENV_TARGET": "container",
            "EXPECTED_DATABASE_NAME": "athena test@primary",
            "EXPECTED_DATABASE_USER": "db-user@example.test",
            "EXPECTED_DATABASE_PASSWORD": "p@ss:word/with?#&=\"quotes\"",
            "POSTGRES_TEST_DB": "host-shell-database",
            "MINIO_TEST_BUCKET": "host-shell-bucket",
        }
    )
    command = """
set -euo pipefail
source "$1"
python3 - <<'PY'
import os
from urllib.parse import unquote, urlsplit

parsed = urlsplit(os.environ["ATHENA_DATABASE_URL"])
assert parsed.hostname == "host.docker.internal"
assert parsed.path == "/" + __import__("urllib.parse").parse.quote(
    os.environ["EXPECTED_DATABASE_NAME"], safe=""
)
assert unquote(parsed.username or "") == os.environ["EXPECTED_DATABASE_USER"]
assert unquote(parsed.password or "") == os.environ["EXPECTED_DATABASE_PASSWORD"]
assert os.environ["ATHENA_STORAGE_BUCKET"] == "tenant-storage-test"
assert os.environ["ATHENA_TEST_MINIO_BUCKET"] == "tenant-storage-test"
assert os.environ["ATHENA_STORAGE_ENDPOINT"] == "host.docker.internal:19000"
assert os.environ["ATHENA_STORAGE_SECURE"] == "false"
PY
"""
    result = subprocess.run(
        [shell, "-c", command, "load-test-env", str(tmp_path / "scripts/load-test-env.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("shell_name", ("bash", "zsh"))
@pytest.mark.parametrize(
    ("target", "expected_app_database"),
    (
        ("host", "athena dev@primary"),
        ("container", "athena_test"),
    ),
)
def test_missing_database_fields_ignore_shell_and_use_target_defaults(
    tmp_path: Path, shell_name: str, target: str, expected_app_database: str
) -> None:
    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} is not installed")
    _write_test_project(tmp_path)
    config_path = tmp_path / "docker/test.env"
    config_path.write_text(
        "\n".join(
            line
            for line in config_path.read_text(encoding="utf-8").splitlines()
            if not line.startswith(("POSTGRES_PORT=", "POSTGRES_TEST_DB="))
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "ATHENA_TEST_ENV_TARGET": target,
            "EXPECTED_APP_DATABASE": expected_app_database,
            "POSTGRES_PORT": "6543",
            "POSTGRES_TEST_DB": "host-shell-database",
        }
    )
    command = """
set -euo pipefail
source "$1"
python3 - <<'PY'
import os
from urllib.parse import quote, urlsplit

app = urlsplit(os.environ["ATHENA_DATABASE_URL"])
test = urlsplit(os.environ["ATHENA_TEST_DATABASE_URL"])
assert app.port == 5432
assert test.port == 5432
assert app.path == "/" + quote(os.environ["EXPECTED_APP_DATABASE"], safe="")
assert test.path == "/athena_test"
assert os.environ["POSTGRES_PORT"] == "5432"
assert os.environ["POSTGRES_TEST_DB"] == "athena_test"
PY
"""
    result = subprocess.run(
        [shell, "-c", command, "load-test-env", str(tmp_path / "scripts/load-test-env.sh")],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_docker_entrypoint_sanitizes_shell_values_and_probes_mapped_port(
    tmp_path: Path,
) -> None:
    _write_test_project(tmp_path)
    command_log = tmp_path / "docker-command.log"
    curl_log = tmp_path / "curl.log"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker_stub = bin_directory / "docker"
    docker_stub.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1" == "info" || "$1" == "image" || "$1" == "build" ]]; then
  exit 0
fi
printf 'ARGV=%s\\n' "$*" >> {str(command_log)!r}
for key in \
  PORT POSTGRES_TEST_DB MINIO_TEST_BUCKET ATHENA_DATABASE_URL \
  ATHENA_STORAGE_BUCKET ATHENA_STORAGE_SECURE ATHENA_EMBEDDING_BASE_URL ROOT_ONLY; do
  printf 'ENV_%s=%s\\n' "$key" "${{!key-}}" >> {str(command_log)!r}
done
case "$*" in
  *"port frontend 80"*) printf '127.0.0.1:19080\\n' ;;
  *" ps "*|*" ps"*) printf 'Up (healthy)\\n' ;;
esac
""",
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)
    curl_stub = bin_directory / "curl"
    curl_stub.write_text(
        f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> {str(curl_log)!r}
""",
        encoding="utf-8",
    )
    curl_stub.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_directory}:{environment['PATH']}",
            "HOME": str(tmp_path),
            "PORT": "9999",
            "POSTGRES_PORT": "6543",
            "POSTGRES_TEST_DB": "host-shell-database",
            "MINIO_TEST_BUCKET": "host-shell-bucket",
            "ATHENA_DATABASE_URL": "postgresql+asyncpg://host@127.0.0.1/wrong",
        }
    )
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/test-docker.sh"), "up"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    command_text = command_log.read_text(encoding="utf-8")
    assert "ENV_PORT=18080" in command_text
    assert "ENV_POSTGRES_TEST_DB=athena test@primary" in command_text
    assert "ENV_MINIO_TEST_BUCKET=tenant-storage-test" in command_text
    assert "ENV_ATHENA_STORAGE_BUCKET=tenant-storage-test" in command_text
    assert "ENV_ATHENA_STORAGE_SECURE=false" in command_text
    assert "ENV_ATHENA_EMBEDDING_BASE_URL=http://host.docker.internal:11434/v1" in command_text
    encoded_database = quote("athena test@primary", safe="")
    encoded_user = quote("db-user@example.test", safe="")
    encoded_password = quote('p@ss:word/with?#&="quotes"', safe="")
    assert (
        "ENV_ATHENA_DATABASE_URL="
        f"postgresql+asyncpg://{encoded_user}:{encoded_password}"
        f"@host.docker.internal:15432/{encoded_database}"
    ) in command_text
    assert "ENV_ROOT_ONLY=" in command_text
    assert "ENV_ROOT_ONLY=from-root-dotenv" not in command_text
    assert "from-generated-env" not in command_text
    assert "--env-file /dev/null" in command_text
    assert "-f compose.yml -f docker/compose.test-mcp.override.yml" in command_text
    assert curl_log.read_text(encoding="utf-8").strip().endswith(
        "http://127.0.0.1:19080/health"
    )


def test_production_dockerfile_excludes_mock_mcp_servers_and_mounts_readonly() -> None:
    """生产后端镜像不得打包 mcp_servers/，合并后单 Dockerfile 直接基于 python:3.12-slim。"""
    assert not (PROJECT_ROOT / "docker" / "Dockerfile.backend-base").exists()
    dockerfile_text = (PROJECT_ROOT / "docker" / "Dockerfile.backend").read_text(encoding="utf-8")
    assert "FROM python:3.12-slim" in dockerfile_text
    assert "ARG INSTALL_KNOWLEDGE=false" in dockerfile_text
    assert "COPY mcp_servers/" not in dockerfile_text

    compose_text = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")
    assert 'INSTALL_KNOWLEDGE: "${ATHENA_INSTALL_KNOWLEDGE:-false}"' in compose_text

    override_path = PROJECT_ROOT / "docker" / "compose.test-mcp.override.yml"
    assert override_path.is_file()
    override_text = override_path.read_text(encoding="utf-8")
    assert "backend-api:" in override_text
    assert "backend-worker:" in override_text
    assert "./mcp_servers:/app/mcp_servers:ro" in override_text


@pytest.mark.parametrize(
    "embedding_line",
    (
        "ATHENA_EMBEDDING_BASE_URL=",
        "ATHENA_EMBEDDING_BASE_URL='   '",
        None,
    ),
)
def test_docker_entrypoint_fails_fast_when_embedding_base_url_missing(
    tmp_path: Path,
    embedding_line: str | None,
) -> None:
    """ATHENA_EMBEDDING_BASE_URL 缺失或仅含空白时，test-docker.sh up 必须在拉起容器前快速报错。"""
    _write_test_project(tmp_path)
    config_path = tmp_path / "docker/test.env"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("ATHENA_EMBEDDING_BASE_URL=")
    ]
    if embedding_line is not None:
        lines.append(embedding_line)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    command_log = tmp_path / "docker-command.log"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker_stub = bin_directory / "docker"
    docker_stub.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1" == "info" ]]; then
  exit 0
fi
printf 'ARGV=%s\\n' "$*" >> {str(command_log)!r}
exit 0
""",
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_directory}:{environment['PATH']}",
            "HOME": str(tmp_path),
            # 即使宿主 shell 残留 ATHENA_EMBEDDING_BASE_URL，也必须以 docker/test.env 为准拒绝启动。
            "ATHENA_EMBEDDING_BASE_URL": "http://host-leak.invalid/v1",
        }
    )
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/test-docker.sh"), "up"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "ATHENA_EMBEDDING_BASE_URL" in result.stderr
    assert "docker/test.env" in result.stderr
    assert not command_log.exists()


@pytest.mark.parametrize(
    ("install_flag", "expected_env_value"),
    (
        ("false", "false"),
        ("true", "true"),
    ),
)
def test_docker_up_passes_athena_install_knowledge_to_compose_build(
    tmp_path: Path,
    install_flag: str,
    expected_env_value: str,
) -> None:
    """test-docker.sh up 直接通过 docker compose build 透传 ATHENA_INSTALL_KNOWLEDGE。"""
    _write_test_project(tmp_path)
    config_path = tmp_path / "docker/test.env"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("ATHENA_INSTALL_KNOWLEDGE=")
    ]
    lines.append(f"ATHENA_INSTALL_KNOWLEDGE={install_flag}")
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    build_log = tmp_path / "docker-build.log"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker_stub = bin_directory / "docker"
    docker_stub.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1" == "info" ]]; then
  exit 0
fi
printf 'ARGV=%s ENV_INSTALL_KNOWLEDGE=%s\\n' \
  "$*" "${{ATHENA_INSTALL_KNOWLEDGE-}}" >> {str(build_log)!r}
case "$*" in
  *"port frontend 80"*) printf '127.0.0.1:19080\\n' ;;
  *" ps "*|*" ps"*) printf 'Up (healthy)\\n' ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)
    curl_stub = bin_directory / "curl"
    curl_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    curl_stub.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_directory}:{environment['PATH']}",
            "HOME": str(tmp_path),
            "ATHENA_INSTALL_KNOWLEDGE": "wrong-host-value",
        }
    )
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/test-docker.sh"), "up"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    build_text = build_log.read_text(encoding="utf-8")
    assert f"ENV_INSTALL_KNOWLEDGE={expected_env_value}" in build_text
    assert "Dockerfile.backend-base" not in build_text


def test_docker_up_unsets_host_athena_install_knowledge_when_omitted_in_test_env(
    tmp_path: Path,
) -> None:
    """docker/test.env 省略 ATHENA_INSTALL_KNOWLEDGE 时，宿主 shell 残留值不得泄漏到构建参数。"""
    _write_test_project(tmp_path)
    config_path = tmp_path / "docker/test.env"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("ATHENA_INSTALL_KNOWLEDGE=")
    ]
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    build_log = tmp_path / "docker-build.log"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    docker_stub = bin_directory / "docker"
    docker_stub.write_text(
        f"""#!/usr/bin/env bash
set -eu
if [[ "$1" == "info" ]]; then
  exit 0
fi
printf 'ARGV=%s ENV_INSTALL_KNOWLEDGE=%s\\n' \
  "$*" "${{ATHENA_INSTALL_KNOWLEDGE-}}" >> {str(build_log)!r}
case "$*" in
  *"port frontend 80"*) printf '127.0.0.1:19080\\n' ;;
  *" ps "*|*" ps"*) printf 'Up (healthy)\\n' ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    docker_stub.chmod(0o755)
    curl_stub = bin_directory / "curl"
    curl_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    curl_stub.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{bin_directory}:{environment['PATH']}",
            "HOME": str(tmp_path),
            "ATHENA_INSTALL_KNOWLEDGE": "true",
        }
    )
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/test-docker.sh"), "up"],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    build_text = build_log.read_text(encoding="utf-8")
    assert "ENV_INSTALL_KNOWLEDGE=true" not in build_text
    assert "ENV_INSTALL_KNOWLEDGE=\n" in build_text
    assert "INSTALL_KNOWLEDGE=false" in result.stdout
