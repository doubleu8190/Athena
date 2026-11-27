"""启动真实 API、Worker、provider、Redis、PostgreSQL schema 和 MinIO bucket。"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_hex
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from minio import Minio
from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from adapter.auth.model import UserModel
from adapter.llm.model import DefaultModelProfileModel, ModelProfileVersionModel
from browser.contract import (
    AccountManifest,
    BrowserStateManifest,
    InputManifest,
    ProviderManifest,
    ResourcesManifest,
    ScenarioManifest,
    SkillManifest,
    write_json_evidence,
    write_state_manifest,
)
from domain.mcp import McpSecretPurpose
from infrastructure.security import Argon2PasswordHasher
from infrastructure.security.mcp_secrets import McpSecretCipher

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_PYTHON = _REPOSITORY_ROOT / ".venv" / "bin" / "python"
_ALEMBIC_INI = _REPOSITORY_ROOT / "backend" / "alembic.ini"
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0YQAAAAASUVORK5CYII="
)
_E2E_SCRIPT_START_MARKER = ".e2e-script-started"


class _HarnessTerminated(Exception):
    """收到受控终止信号后进入 finally 清理本次已拥有的资源。"""


def _reserve_port(preferred: int, *, excluded: set[int] | None = None) -> int:
    """优先使用固定端口；被占用时从高位选择空闲端口。"""
    unavailable = excluded or set()
    candidates = [preferred, *(18_000 + index for index in range(100))]
    for port in candidates:
        if port in unavailable:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("e2e_port_unavailable")


def _runtime_environment() -> dict[str, str]:
    """验证 test-only 依赖环境并返回不输出秘密的子进程环境。"""
    required = (
        "ATHENA_TEST_DATABASE_URL",
        "ATHENA_MCP_SECRET_KEY",
        "ATHENA_STORAGE_ENDPOINT",
        "ATHENA_STORAGE_ACCESS_KEY",
        "ATHENA_STORAGE_SECRET_KEY",
    )
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise RuntimeError("e2e_environment_missing:" + ",".join(missing))
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        (
            str(_REPOSITORY_ROOT / "backend" / "src"),
            str(_REPOSITORY_ROOT / "backend" / "tests"),
        )
    )
    return env


async def _create_schema(
    database_url: str,
    schema: str,
    *,
    before_created: Callable[[], None] | None = None,
    on_created: Callable[[], None] | None = None,
    fail_dispose: bool = False,
) -> None:
    """只创建并迁移本次随机 schema。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        # 提交成功后立即登记 ownership，连接 dispose 失败也不能跳过登记。
        if before_created is not None:
            before_created()
        if on_created is not None:
            on_created()
    finally:
        await engine.dispose()
        if fail_dispose:
            raise RuntimeError("e2e_injected_schema_dispose_failure")
    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["connect_args"] = {"server_settings": {"search_path": f"{schema},public"}}
    config.attributes["version_table_schema"] = schema
    await asyncio.to_thread(command.upgrade, config, "head")


async def _drop_schema(database_url: str, schema: str) -> None:
    """只删除 harness 自己创建的 schema，并确认清理后确实不存在。"""
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        async with engine.connect() as connection:
            exists = await connection.scalar(
                text("SELECT to_regnamespace(:schema) IS NOT NULL"),
                {"schema": schema},
            )
            if exists:
                raise RuntimeError("e2e_schema_still_exists")
    finally:
        await engine.dispose()


async def _seed_accounts_and_model(
    database_url: str,
    schema: str,
    *,
    account_password: str,
    provider_base_url: str,
    model_source: str,
    dev_database_url: str | None,
) -> None:
    """使用真实密码哈希创建登录账号，并向隔离 schema 写入模型配置。"""
    engine = create_async_engine(
        database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    hasher = Argon2PasswordHasher()
    password_hash = await asyncio.to_thread(hasher.hash_password, account_password)
    try:
        async with factory() as session, session.begin():
            session.add_all(
                (
                    UserModel(
                        id="usr_e2e_admin",
                        email="e2e-admin@example.com",
                        password_hash=password_hash,
                        display_name="E2E Admin",
                        status="active",
                        role="super_admin",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    ),
                    UserModel(
                        id="usr_e2e_ordinary",
                        email="e2e-ordinary@example.com",
                        password_hash=password_hash,
                        display_name="E2E User",
                        status="active",
                        role="member",
                        created_at=now,
                        updated_at=now,
                        deleted_time=None,
                    ),
                )
            )
            if model_source == "scripted":
                cipher = McpSecretCipher.from_environment()
                profile_id = "mdl_e2e_scripted"
                sealed = cipher.seal(
                    profile_id,
                    McpSecretPurpose.LLM_API_KEY,
                    {"api_key": f"e2e-{token_hex(16)}"},
                )
                session.add(
                    ModelProfileVersionModel(
                        id=profile_id,
                        name="Browser E2E Scripted Provider",
                        protocol="chat_completions",
                        base_url=provider_base_url,
                        model="e2e-scripted",
                        api_key_ciphertext=sealed.ciphertext,
                        api_key_nonce=sealed.nonce,
                        api_key_key_version=sealed.key_version,
                        context_window_tokens=32_768,
                        tokenizer_encoding="o200k_base",
                        supports_stream=True,
                        supports_tools=True,
                        supports_parallel_tool_calls=False,
                        max_output_tokens=4096,
                        request_timeout_seconds=60,
                        execution_timeout_seconds=120,
                        max_concurrency=2,
                        temperature=0,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            elif model_source == "configured-default":
                if not dev_database_url:
                    raise RuntimeError("e2e_dev_database_url_missing")
                profile_id = await _copy_configured_default(
                    session,
                    dev_database_url=dev_database_url,
                )
            else:
                raise ValueError("e2e_model_source_invalid")
            # 默认指针必须在模型版本落库后插入，避免 SQLAlchemy 无关系图时颠倒顺序。
            await session.flush()
            session.add(DefaultModelProfileModel(singleton_id=1, profile_id=profile_id))
    finally:
        await engine.dispose()


async def _copy_configured_default(
    target_session: Any,
    *,
    dev_database_url: str,
) -> str:
    """从 dev public 只读克隆默认连接字段，密文仅在隔离 schema 内使用。"""
    dev_engine = create_async_engine(
        dev_database_url,
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": "public"}},
    )
    try:
        async with async_sessionmaker(dev_engine, expire_on_commit=False)() as session:
            source = await session.scalar(
                select(ModelProfileVersionModel)
                .join(
                    DefaultModelProfileModel,
                    DefaultModelProfileModel.profile_id == ModelProfileVersionModel.id,
                )
                .where(DefaultModelProfileModel.singleton_id == 1)
            )
            if source is None or not source.enabled or source.api_key_ciphertext is None:
                raise RuntimeError("e2e_configured_default_unavailable")
            target_session.add(_configured_default_clone(source))
            return source.id
    finally:
        await dev_engine.dispose()


def _configured_default_clone(
    source: ModelProfileVersionModel,
) -> ModelProfileVersionModel:
    """复制真实模型连接与请求参数，只收缩本次测试的预算、超时和并发。"""
    now = datetime.now(UTC)
    return ModelProfileVersionModel(
        id=source.id,
        name=f"Browser E2E Clone {source.name}"[:120],
        protocol=source.protocol,
        base_url=source.base_url,
        model=source.model,
        api_key_ciphertext=source.api_key_ciphertext,
        api_key_nonce=source.api_key_nonce,
        api_key_key_version=source.api_key_key_version,
        context_window_tokens=source.context_window_tokens,
        tokenizer_encoding=source.tokenizer_encoding,
        supports_stream=source.supports_stream,
        supports_tools=source.supports_tools,
        supports_parallel_tool_calls=source.supports_parallel_tool_calls,
        max_output_tokens=min(source.max_output_tokens, 4096),
        request_timeout_seconds=min(source.request_timeout_seconds, 60),
        execution_timeout_seconds=min(source.execution_timeout_seconds, 120),
        max_concurrency=min(source.max_concurrency, 2),
        # 温度属于供应商兼容参数，测试克隆不得擅自覆盖，否则可能触发真实端点 400。
        temperature=source.temperature,
        enabled=True,
        created_at=now,
        updated_at=now,
    )


def _create_minio_bucket(
    bucket: str,
    *,
    on_created: Callable[[Minio], None] | None = None,
) -> Minio:
    """创建本次独占测试 bucket，不列举或删除共享 bucket。"""
    client = Minio(
        os.environ["ATHENA_STORAGE_ENDPOINT"],
        access_key=os.environ["ATHENA_STORAGE_ACCESS_KEY"],
        secret_key=os.environ["ATHENA_STORAGE_SECRET_KEY"],
        secure=os.getenv("ATHENA_STORAGE_SECURE", "false").lower() == "true",
    )
    if client.bucket_exists(bucket):
        raise RuntimeError("e2e_minio_bucket_collision")
    client.make_bucket(bucket)
    if on_created is not None:
        on_created(client)
    return client


def _cleanup_minio_bucket(client: Minio, bucket: str) -> bool:
    """只清空并删除本次随机 bucket。"""
    try:
        objects = list(client.list_objects(bucket, recursive=True))
        for item in objects:
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
        return True
    except Exception:
        return False


def _start_redis(
    container_name: str,
    port: int,
    log_path: Path,
    *,
    before_started: Callable[[], None] | None = None,
    on_started: Callable[[], None] | None = None,
    check_terminated: Callable[[], None] | None = None,
) -> None:
    """启动无持久化的独立 Redis，并在 readiness 边界优先响应终止信号。"""
    with log_path.open("w", encoding="utf-8") as log:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--detach",
                "--name",
                container_name,
                "--label",
                "athena.e2e=true",
                "--publish",
                f"127.0.0.1:{port}:6379",
                "redis:7.4.7-alpine",
                "redis-server",
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    if before_started is not None:
        before_started()
    # docker run 成功即创建容器；readiness 失败时 cleanup 仍必须删除它。
    if on_started is not None:
        on_started()
    deadline = time.monotonic() + 20
    client = Redis(host="127.0.0.1", port=port, socket_timeout=0.5)
    try:
        while time.monotonic() < deadline:
            if check_terminated is not None:
                check_terminated()
            try:
                ready = client.ping()
            except Exception:
                ready = False
            if ready:
                if check_terminated is not None:
                    check_terminated()
                return
            time.sleep(0.1)
        if check_terminated is not None:
            check_terminated()
        raise RuntimeError("e2e_redis_not_ready")
    finally:
        client.close()


def _stop_redis(container_name: str) -> bool:
    completed = subprocess.run(
        ["docker", "rm", "--force", container_name],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _skill_markdown(*, version: int) -> bytes:
    """生成合法 Skill manifest，两个版本保持同一 slug。"""
    return f"""---
name: E2E Monthly Report
slug: e2e-monthly-report
description: Browser E2E monthly report with deterministic Excel and PNG artifacts.
dependencies:
  - openpyxl>=3.1
scripts:
  - path: scripts/build_report.py
    label: E2E 月度报表
    purpose: Read the selected CSV and produce deterministic Excel and PNG outputs.
    parameters:
      type: object
      additionalProperties: false
      properties:
        mode:
          type: string
          title: 执行模式
          enum: [normal, slow]
        period:
          type: string
          title: 报表月份
          const: "2026-10"
      required: [mode, period]
---
E2E guide version {version}. Load this skill, read the resource, inspect the uploaded
CSV, then execute the registered script.
""".encode()


def _skill_script(*, version: int) -> bytes:
    """生成真实子进程脚本；慢模式用于浏览器取消场景。"""
    suffix = "" if version == 1 else "-v2"
    return f'''\
import csv
import json
import os
import time
from pathlib import Path

from openpyxl import Workbook

request = json.loads(Path(os.environ["ATHENA_INPUT_PATH"]).read_text(encoding="utf-8"))
parameters = request["parameters"]
if parameters["mode"] == "slow":
    # test-only marker 位于本次 call 的独立工作目录，供浏览器确认脚本已进入慢执行阶段。
    Path("{_E2E_SCRIPT_START_MARKER}").write_text(
        f"pid={{os.getpid()}}cwd={{Path.cwd().name}}\\n",
        encoding="utf-8",
    )
    time.sleep(120)
inputs = list(request["input_files"].values())
if len(inputs) != 1:
    raise SystemExit("one input file required")
with Path(inputs[0]).open(encoding="utf-8", newline="") as source:
    rows = list(csv.DictReader(source))
if not rows or rows[0].get("month") != "2026-10":
    raise SystemExit("period marker missing")
output = Path(request["output_dir"])
workbook = Workbook()
sheet = workbook.active
sheet.title = "Monthly Report{version}"
sheet.append(["month", "total"])
sheet.append([rows[0]["month"], int(rows[0]["total"])])
report_name = "e2e-report{suffix}.xlsx"
png_name = "e2e-trend{suffix}.png"
summary_name = "e2e-summary{suffix}.txt"
workbook.save(output / report_name)
(output / png_name).write_bytes(__import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z0YQAAAAASUVORK5CYII="
))
(output / summary_name).write_text(
    "E2E_REUSE_SOURCE month=2026-10 total=42 version={version}\\n",
    encoding="utf-8",
)
(output / "manifest.json").write_text(json.dumps({{"outputs": [
    {{"path": report_name, "label": "E2E Excel"}},
    {{"path": png_name, "label": "E2E PNG"}},
    {{"path": summary_name, "label": "E2E Summary Text"}},
]}}), encoding="utf-8")
'''.encode()


def _write_skill_zip(path: Path, *, version: int) -> None:
    """写入真实管理员 Skill ZIP 和文本资源。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", _skill_markdown(version=version))
        archive.writestr("scripts/build_report.py", _skill_script(version=version))
        archive.writestr(
            "references/e2e-instructions.txt",
            f"E2E reference version {version}: verify the uploaded CSV before executing.\n",
        )


def _write_fixtures(state_directory: Path) -> tuple[SkillManifest, InputManifest]:
    """写入技能、版本更新包和固定 CSV。"""
    v1 = state_directory / "e2e-monthly-report-v1.zip"
    v2 = state_directory / "e2e-monthly-report-v2.zip"
    csv_path = state_directory / "e2e-november-input.csv"
    _write_skill_zip(v1, version=1)
    _write_skill_zip(v2, version=2)
    csv_path.write_text("month,total\n2026-10,42\n", encoding="utf-8")
    return (
        SkillManifest(
            zip_path=str(v1),
            version_two_zip_path=str(v2),
            slug="e2e-monthly-report",
            name="E2E Monthly Report",
            guide_marker="E2E guide version 1.",
            script_path="scripts/build_report.py",
        ),
        InputManifest(
            csv_path=str(csv_path),
            filename=csv_path.name,
            expected_text="2026-10,42",
        ),
    )


def _start_process(
    command: list[str],
    *,
    env: dict[str, str],
    log_path: Path,
    before_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.Popen[bytes]:
    """以独立进程组启动资源并持久化脱敏标准输出。"""
    log = log_path.open("wb")
    process = subprocess.Popen(
        command,
        cwd=_REPOSITORY_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._athena_log = log  # type: ignore[attr-defined]
    if before_started is not None:
        before_started(process)
    # 子进程一旦创建就登记所有权，后续等待或信号失败也能进入 finally 清理。
    if on_started is not None:
        on_started(process)
    return process


def _stop_process(process: subprocess.Popen[bytes]) -> bool:
    """只终止本 harness 创建的进程组。"""
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    log = getattr(process, "_athena_log", None)
    if log is not None:
        log.close()
        Path(log.name).chmod(0o600)
    return process.poll() is not None


def _stop_managed_processes(
    processes: list[tuple[str, subprocess.Popen[bytes]]],
) -> tuple[list[dict[str, object]], list[str], int]:
    """逐个停止真实创建的进程，并为每个 role/pid 保存独立结果。"""
    evidence: list[dict[str, object]] = []
    errors: list[str] = []
    for role, process in processes:
        try:
            stopped = _stop_process(process)
        except Exception as error:
            stopped = False
            errors.append(f"process:{role}:{type(error).__name__}")
        evidence.append(
            {
                "role": role,
                "pid": process.pid,
                "stopped": stopped,
                "returncode": process.poll(),
            }
        )
    return evidence, errors, sum(bool(item["stopped"]) for item in evidence)


def _wait_ready(
    *,
    api_url: str,
    provider_url: str,
    worker_ready: Path,
    processes: tuple[subprocess.Popen[bytes], ...],
    check_terminated: Callable[[], None] | None = None,
) -> None:
    """等待三项 readiness，并让待处理终止信号始终优先于就绪返回。"""
    deadline = time.monotonic() + 90
    api_ready = provider_ready = False
    while time.monotonic() < deadline:
        if check_terminated is not None:
            check_terminated()
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"e2e_process_exited:{process.pid}")
        if not provider_ready:
            try:
                provider_ready = httpx.get(f"{provider_url}/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                pass
        if not api_ready:
            try:
                api_ready = httpx.get(f"{api_url}/health", timeout=1).status_code == 200
            except httpx.HTTPError:
                pass
        if check_terminated is not None:
            check_terminated()
        if api_ready and provider_ready and worker_ready.exists():
            return
        time.sleep(0.2)
    if check_terminated is not None:
        check_terminated()
    raise RuntimeError(
        "e2e_stack_not_ready:"
        f"api={int(api_ready)},provider={int(provider_ready)},worker={int(worker_ready.exists())}"
    )


def _login_smoke(api_url: str, frontend_origin: str, password: str) -> None:
    """通过真实登录接口验证账号密码、Cookie 和数据库会话。"""
    with httpx.Client(base_url=api_url, timeout=10) as client:
        for email in ("e2e-admin@example.com", "e2e-ordinary@example.com"):
            response = client.post(
                "/api/v1/auth/login",
                headers={"Origin": frontend_origin},
                json={"email": email, "password": password, "remember_me": False},
            )
            if response.status_code != 200:
                try:
                    detail = str(response.json().get("detail", "unknown"))
                except (ValueError, AttributeError):
                    detail = "unknown"
                raise RuntimeError(f"e2e_login_failed:{email}:{response.status_code}:{detail}")
            me = client.get("/api/v1/auth/me")
            if me.status_code != 200:
                raise RuntimeError(f"e2e_auth_cookie_failed:{me.status_code}")


def _manifest(
    *,
    state_directory: Path,
    frontend_port: int,
    api_port: int,
    provider_port: int,
    account_password: str,
    skill: SkillManifest,
    input_manifest: InputManifest,
    model_source: str,
    schema: str,
    bucket: str,
    redis_container: str,
) -> BrowserStateManifest:
    """构造前端口可直接消费的 state.json。"""
    frontend_base = f"http://127.0.0.1:{frontend_port}"
    api_base = f"http://127.0.0.1:{api_port}"
    scenarios = {
        "normal": ScenarioManifest(
            name="normal-approval-two-artifacts",
            prompt="E2E_NORMAL 请使用已选择的月度报告技能生成 Excel 和趋势图。",
            approval_decision="approve",
            expected_terminal_status="completed",
            expected_artifacts=(
                "e2e-report.xlsx",
                "e2e-trend.png",
                "e2e-summary.txt",
            ),
        ),
        "reuse": ScenarioManifest(
            name="reuse-selected-artifact-without-skill-execution",
            prompt="E2E_REUSE 请读取我当前选择的上一轮文本产物并总结，不再执行技能。",
            approval_decision="none",
            expected_terminal_status="completed",
            expected_artifacts=(),
        ),
        "approval_reject": ScenarioManifest(
            name="approval-reject-no-output",
            prompt="E2E_REJECT 请生成报表；审批时我会拒绝。",
            approval_decision="reject",
            expected_terminal_status="completed",
            expected_artifacts=(),
        ),
        "cancel_slow": ScenarioManifest(
            name="cancel-during-slow-script",
            prompt="E2E_CANCEL 请生成报表，并在慢任务运行期间允许我取消。",
            approval_decision="approve",
            expected_terminal_status="cancelled",
            expected_artifacts=(),
        ),
        "post_delivery_failed": ScenarioManifest(
            name="post-delivery-stream-failure",
            prompt=(
                "E2E_POST_DELIVERY_FAILED 请使用已选择的月度报告技能正常生成并交付报表，"
                "然后模拟流式失败。"
            ),
            approval_decision="approve",
            expected_terminal_status="failed",
            expected_artifacts=(
                "e2e-report.xlsx",
                "e2e-trend.png",
                "e2e-summary.txt",
            ),
        ),
        "post_delivery_cancelled": ScenarioManifest(
            name="post-delivery-stream-cancelled",
            prompt=(
                "E2E_POST_DELIVERY_CANCELLED 请使用已选择的月度报告技能正常生成并交付报表，"
                "然后等待我停止。"
            ),
            approval_decision="approve",
            expected_terminal_status="cancelled",
            expected_artifacts=(
                "e2e-report.xlsx",
                "e2e-trend.png",
                "e2e-summary.txt",
            ),
        ),
        "version_update_pin": ScenarioManifest(
            name="old-run-keeps-v1-after-v2-update",
            prompt="E2E_VERSION_PIN 创建后先更新到 v2，再批准旧 Run。",
            approval_decision="approve",
            expected_terminal_status="completed",
            expected_artifacts=(
                "e2e-report.xlsx",
                "e2e-trend.png",
                "e2e-summary.txt",
            ),
        ),
        "disable_pin": ScenarioManifest(
            name="old-run-keeps-v1-after-disable",
            prompt="E2E_DISABLE_PIN 创建后停用技能，再批准旧 Run。",
            approval_decision="approve",
            expected_terminal_status="completed",
            expected_artifacts=(
                "e2e-report.xlsx",
                "e2e-trend.png",
                "e2e-summary.txt",
            ),
        ),
    }
    return BrowserStateManifest(
        state_file=str(state_directory / "state.json"),
        frontend_base_url=frontend_base,
        api_base_url=api_base,
        health_url=f"{api_base}/health",
        accounts={
            "admin": AccountManifest(
                email="e2e-admin@example.com",
                password=account_password,
                display_name="E2E Admin",
            ),
            "ordinary": AccountManifest(
                email="e2e-ordinary@example.com",
                password=account_password,
                display_name="E2E User",
            ),
        },
        skill=skill,
        input=input_manifest,
        provider=ProviderManifest(
            mode=model_source,
            base_url=(
                f"http://127.0.0.1:{provider_port}/v1" if model_source == "scripted" else None
            ),
            # 仅选择 configured-default 不等于已经完成真实模型调用；浏览器验收后才能更新证据。
            real_llm_verified=False,
            streaming=True,
            tools=True,
            note=(
                "Local scripted ChatCompletions provider; not a real external LLM."
                if model_source == "scripted"
                else "Read-only cloned configured default; controlled low-call verification."
            ),
        ),
        scenarios=scenarios,
        resources=ResourcesManifest(
            database_schema=schema,
            minio_bucket=bucket,
            redis_container=redis_container,
            skill_temp_root=str(state_directory / "skill-temp"),
        ),
        stop_request_path=str(state_directory / "stop.request"),
        ready_at=datetime.now(UTC).isoformat(),
    )


def main() -> None:
    """启动隔离栈，写 manifest 并保持到显式停止请求。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", default=token_hex(4))
    parser.add_argument("--api-port", type=int, default=8011)
    parser.add_argument("--provider-port", type=int, default=8012)
    parser.add_argument("--frontend-port", type=int, default=4183)
    parser.add_argument(
        "--model-source",
        choices=("scripted", "configured-default"),
        default="scripted",
    )
    parser.add_argument(
        "--fault-after",
        choices=(
            "schema-create",
            "schema-dispose",
            "redis-start",
            "provider-start",
            "api-start",
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--signal-window",
        choices=("schema-commit", "redis-create", "process-start"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--smoke-only", action="store_true")
    args = parser.parse_args()
    run_id = args.id
    if not run_id.replace("-", "").replace("_", "").isalnum():
        raise ValueError("e2e_id_invalid")
    state_directory = Path(f"/private/tmp/athena-skills-browser-{run_id}")
    if state_directory.exists():
        raise RuntimeError("e2e_state_directory_exists")
    state_directory.mkdir(mode=0o700)
    (state_directory / "harness.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (state_directory / "harness.pid").chmod(0o600)
    state_file = state_directory / "state.json"
    os.environ["ATHENA_E2E_STATE_FILE"] = str(state_file)
    env = _runtime_environment()
    env["ATHENA_E2E_STATE_FILE"] = str(state_file)
    api_port = _reserve_port(args.api_port)
    provider_port = _reserve_port(args.provider_port, excluded={api_port})
    frontend_port = args.frontend_port
    schema = f"athena_v2_browser_{run_id.replace('-', '_')}"[:63]
    bucket = f"athena-e2e-{run_id.lower().replace('_', '-')}"[:63]
    redis_container = f"athena-e2e-redis-{run_id.lower().replace('_', '-')}"[:63]
    redis_port = _reserve_port(
        16_379,
        excluded={api_port, provider_port},
    )
    account_password = f"E2e-{token_hex(12)}!"
    skill, input_manifest = _write_fixtures(state_directory)
    minio_client: Minio | None = None
    schema_created = False
    redis_started = False
    managed_processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    manifest_written = False
    termination_signal: int | None = None

    def terminate(signum: int, _frame: object) -> None:
        nonlocal termination_signal
        termination_signal = signum

    def raise_if_terminated() -> None:
        if termination_signal is not None:
            raise _HarnessTerminated()

    previous_handlers = {
        signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    for signum in previous_handlers:
        signal.signal(signum, terminate)

    def mark_schema_created() -> None:
        nonlocal schema_created
        schema_created = True
        if args.fault_after == "schema-create":
            raise RuntimeError("e2e_injected_schema_create_failure")
        raise_if_terminated()

    def mark_redis_started() -> None:
        nonlocal redis_started
        redis_started = True
        if args.fault_after == "redis-start":
            raise RuntimeError("e2e_injected_redis_start_failure")
        raise_if_terminated()

    def record_process(role: str, process: subprocess.Popen[bytes]) -> None:
        managed_processes.append((role, process))
        if args.fault_after == f"{role}-start":
            raise RuntimeError(f"e2e_injected_{role}_start_failure")
        raise_if_terminated()

    def pause_started_process(process: subprocess.Popen[bytes]) -> None:
        del process
        pause_signal_window("process-start")

    def mark_minio_created(client: Minio) -> None:
        nonlocal minio_client
        minio_client = client

    def pause_signal_window(window: str) -> None:
        if args.signal_window != window:
            return
        ready_path = state_directory / f"{window}.ready"
        ready_path.write_text("ready\n", encoding="utf-8")
        ready_path.chmod(0o600)
        time.sleep(30)

    try:
        asyncio.run(
            _create_schema(
                env["ATHENA_TEST_DATABASE_URL"],
                schema,
                before_created=lambda: pause_signal_window("schema-commit"),
                on_created=mark_schema_created,
                fail_dispose=args.fault_after == "schema-dispose",
            )
        )
        raise_if_terminated()
        asyncio.run(
            _seed_accounts_and_model(
                env["ATHENA_TEST_DATABASE_URL"],
                schema,
                account_password=account_password,
                provider_base_url=f"http://127.0.0.1:{provider_port}/v1",
                model_source=args.model_source,
                dev_database_url=env.get("ATHENA_DATABASE_URL"),
            )
        )
        raise_if_terminated()
        _create_minio_bucket(bucket, on_created=mark_minio_created)
        raise_if_terminated()
        _start_redis(
            redis_container,
            redis_port,
            state_directory / "redis.log",
            before_started=lambda: pause_signal_window("redis-create"),
            on_started=mark_redis_started,
            check_terminated=raise_if_terminated,
        )
        raise_if_terminated()
        provider_ready = state_directory / "provider.ready"
        _start_process(
            [
                str(_PYTHON),
                "-m",
                "browser.provider_server",
                "--port",
                str(provider_port),
                "--audit-file",
                str(state_directory / "provider-events.jsonl"),
                "--ready-file",
                str(provider_ready),
            ],
            env=env,
            log_path=state_directory / "provider.log",
            before_started=pause_started_process,
            on_started=lambda process: record_process("provider", process),
        )
        runtime_env = env.copy()
        runtime_env.update(
            {
                "ATHENA_DATABASE_URL": env["ATHENA_TEST_DATABASE_URL"],
                "ATHENA_REDIS_URL": f"redis://127.0.0.1:{redis_port}/0",
                "ATHENA_PUBLIC_ORIGIN": f"http://127.0.0.1:{frontend_port}",
                "ATHENA_COOKIE_SECURE": "false",
                # 知识库与文件能力已默认无条件装配，指向本机可控 provider 的 Embeddings 端点。
                "ATHENA_EMBEDDING_BASE_URL": f"http://127.0.0.1:{provider_port}/v1",
                "ATHENA_STORAGE_BUCKET": bucket,
                "ATHENA_SKILL_TEMP_ROOT": str(state_directory / "skill-temp"),
                "ATHENA_SKILL_PYTHON_EXECUTABLE": str(_PYTHON),
                "ATHENA_RUN_WORKER_CONCURRENCY": "1",
                "ATHENA_RUN_EXECUTION_SECONDS": "120",
                "ATHENA_RUN_LEASE_SECONDS": "30",
                "ATHENA_RUN_HEARTBEAT_SECONDS": "5",
                "ATHENA_RUN_LLM_REQUEST_LIMIT": "12",
                "ATHENA_RUN_TOOL_TIMEOUT_SECONDS": "60",
                "ATHENA_E2E_PROBE_PASSWORD": account_password,
                "ATHENA_E2E_AUTH_TRACE_FILE": str(state_directory / "auth-trace.log"),
            }
        )
        _start_process(
            [
                str(_PYTHON),
                "-m",
                "browser.api_entry",
                "--schema",
                schema,
                "--port",
                str(api_port),
                "--probe-file",
                str(state_directory / "api-db-probe.txt"),
            ],
            env=runtime_env,
            log_path=state_directory / "api.log",
            on_started=lambda process: record_process("api", process),
        )
        worker_ready = state_directory / "worker.ready"
        _start_process(
            [
                str(_PYTHON),
                "-m",
                "browser.worker_entry",
                "--schema",
                schema,
                "--ready-file",
                str(worker_ready),
            ],
            env=runtime_env,
            log_path=state_directory / "worker.log",
            on_started=lambda process: record_process("worker", process),
        )
        _wait_ready(
            api_url=f"http://127.0.0.1:{api_port}",
            provider_url=f"http://127.0.0.1:{provider_port}",
            worker_ready=worker_ready,
            processes=tuple(process for _, process in managed_processes),
            check_terminated=raise_if_terminated,
        )
        raise_if_terminated()
        _login_smoke(
            f"http://127.0.0.1:{api_port}",
            f"http://127.0.0.1:{frontend_port}",
            account_password,
        )
        raise_if_terminated()
        manifest = _manifest(
            state_directory=state_directory,
            frontend_port=frontend_port,
            api_port=api_port,
            provider_port=provider_port,
            account_password=account_password,
            skill=skill,
            input_manifest=input_manifest,
            model_source=args.model_source,
            schema=schema,
            bucket=bucket,
            redis_container=redis_container,
        )
        raise_if_terminated()
        write_state_manifest(state_file, manifest)
        manifest_written = True
        print(
            json.dumps(
                {
                    "stateFile": str(state_file),
                    "apiBaseURL": manifest.api_base_url,
                    "frontendBaseURL": manifest.frontend_base_url,
                    "provider": args.model_source,
                    "schema": schema,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.smoke_only:
            return
        stop_path = Path(manifest.stop_request_path)
        while not stop_path.exists():
            if termination_signal is not None:
                raise_if_terminated()
            for role, process in managed_processes:
                if process.poll() is not None:
                    raise RuntimeError(f"e2e_process_exited:{role}:{process.pid}")
            time.sleep(0.5)
    except _HarnessTerminated:
        pass
    finally:
        for signum, previous_handler in previous_handlers.items():
            signal.signal(signum, previous_handler)
        cleanup_errors: list[str] = []
        (
            process_cleanup,
            process_errors,
            processes_stopped,
        ) = _stop_managed_processes(managed_processes)
        cleanup_errors.extend(process_errors)
        redis_container_removed = False
        if redis_started:
            try:
                redis_container_removed = _stop_redis(redis_container)
            except Exception as error:
                redis_container_removed = False
                cleanup_errors.append(f"redis:{type(error).__name__}")
            if not redis_container_removed:
                cleanup_errors.append("redis:remove_failed")
        minio_bucket_created = minio_client is not None
        bucket_removed = False
        if minio_client is not None:
            try:
                bucket_removed = _cleanup_minio_bucket(minio_client, bucket)
            except Exception as error:
                bucket_removed = False
                cleanup_errors.append(f"minio:{type(error).__name__}")
            if not bucket_removed:
                cleanup_errors.append("minio:remove_failed")
        # 只有实际执行并确认 DROP 成功后才记为已删除；未创建时单独由“无需删除”判定清理完成。
        schema_dropped = False
        if schema_created:
            try:
                asyncio.run(_drop_schema(env["ATHENA_TEST_DATABASE_URL"], schema))
                schema_dropped = True
            except Exception as error:
                cleanup_errors.append(f"schema:{type(error).__name__}")
        if processes_stopped != len(managed_processes):
            cleanup_errors.append("process:cleanup_failed")
        cleanup_complete = (
            (not schema_created or schema_dropped)
            and (not minio_bucket_created or bucket_removed)
            and (not redis_started or redis_container_removed)
            and processes_stopped == len(managed_processes)
            and not cleanup_errors
        )
        write_json_evidence(
            state_directory / "cleanup.json",
            {
                "status": "complete" if cleanup_complete else "incomplete",
                "schema_created": schema_created,
                "schema_dropped": schema_dropped,
                "minio_bucket_created": minio_bucket_created,
                "bucket_removed": bucket_removed,
                "redis_started": redis_started,
                "redis_container_removed": redis_container_removed,
                "manifest_written": manifest_written,
                "processes_stopped": processes_stopped,
                "processes_expected": len(managed_processes),
                "process_cleanup": process_cleanup,
                "termination_signal": termination_signal,
                "errors": cleanup_errors,
            },
        )


if __name__ == "__main__":
    main()
