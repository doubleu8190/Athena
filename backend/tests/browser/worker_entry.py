"""以正常 bootstrap RunWorker 启动浏览器 E2E Worker。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from importlib import import_module
from pathlib import Path
from typing import Any, cast

from browser.database_scope import install_search_path


async def _run(schema: str, ready_file: Path) -> None:
    """安装测试 schema 后调用未修改的生产 Worker 生命周期。"""
    install_search_path(schema)
    worker_entry = cast(Any, import_module("run_worker"))

    original_build = worker_entry.build_run_worker_dependencies

    def build_with_ready(*args: Any, **kwargs: Any) -> None:
        original_build(*args, **kwargs)
        ready_file.write_text("ready\n", encoding="utf-8")
        ready_file.chmod(0o600)

    worker_entry.build_run_worker_dependencies = build_with_ready
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, stop_event.set)
    await worker_entry.run_worker_process(stop_event=stop_event)


def main() -> None:
    """解析入口参数并运行 asyncio Worker。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--schema", required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run(args.schema, args.ready_file))


if __name__ == "__main__":
    main()
