"""运行 S8 隔离 PostgreSQL/HTTPS Worker 验收。

脚本只接受负载大小参数；schema 始终由测试随机生成并在结束时清理，避免误删用户指定的数据库对象。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

from sqlalchemy.engine import make_url

from configuration.settings import Settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Athena S8 isolated acceptance")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.tasks <= 1000 or not 1 <= args.concurrency <= 64:
        parser.error("tasks must be 1..1000 and concurrency must be 1..64")
    environment = os.environ.copy()
    environment["ATHENA_S8_TASKS"] = str(args.tasks)
    environment["ATHENA_S8_CONCURRENCY"] = str(args.concurrency)
    database_url = environment.get("ATHENA_TEST_DATABASE_URL") or Settings().postgres_url
    if make_url(database_url).host not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("S8 acceptance requires a loopback PostgreSQL test database")
    environment["ATHENA_TEST_DATABASE_URL"] = database_url
    with tempfile.NamedTemporaryFile(
        prefix="athena_s8_", suffix=".xml", delete=False
    ) as report_file:
        report_path = report_file.name
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/integration/test_worker_acceptance_s8.py",
        "-q",
        "--tb=short",
        "-o",
        "junit_family=legacy",
        f"--junitxml={report_path}",
    ]
    try:
        completed = subprocess.run(command, env=environment, check=False)
        if completed.returncode == 0:
            _print_measured_properties(report_path)
        return completed.returncode
    finally:
        try:
            os.unlink(report_path)
        except FileNotFoundError:
            pass


def _print_measured_properties(path: str) -> None:
    """只打印验收白名单指标，避免把 JUnit 或环境信息原样输出。"""
    allowed = {
        "s8.tasks",
        "s8.concurrency",
        "s8.elapsed_seconds",
        "s8.tasks_per_second",
        "s8.task_http_requests",
        "s8.result_http_requests",
        "s8.result_receipts",
        "s8.terminal_backlog",
        "s8.sql_statements",
        "s8.transactions",
        "s8.connection_checkouts",
        "s8.max_checked_out_connections",
        "s8.scanner_batch_size",
        "s8.scanner_initial_due",
        "s8.scanner_final_due",
        "s8.scanner_handled",
        "s8.scanner_batches",
        "s8.scanner_elapsed_seconds",
        "s8.scanner_tasks_per_second",
        "s8.scanner_repeat_handled",
    }
    root = ET.parse(path).getroot()
    values: dict[str, str] = {}
    for property_node in root.findall(".//property"):
        name = property_node.attrib.get("name")
        value = property_node.attrib.get("value")
        if name in allowed and value is not None:
            values[name] = value
    print("S8 measured metrics:")
    for name in sorted(values):
        print(f"{name}={values[name]}")


if __name__ == "__main__":
    raise SystemExit(main())
