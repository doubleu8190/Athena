"""Docker 沙箱管理器 — 使用 docker-py 实现完整沙箱隔离.

特性：
- 容器作用域管理（session/agent/shared）
- 路径白名单（防路径遍历）
- bind mount 文件输入
- exec_run 命令执行
- 容器生命周期管理 + 定期清理

project_memory 约束：
- 文件输入通过 bind mount，严格路径过滤
- exec_run 准确获取输出
- 基于作用域映射的容器生命周期管理
"""

from __future__ import annotations

import asyncio
import io
import tarfile
from pathlib import Path
from typing import Any, TYPE_CHECKING

from athena.config.settings import Settings
from athena.utils.logging import get_logger

if TYPE_CHECKING:
    import docker

logger = get_logger(__name__)


class PathSecurityFilter:
    """路径安全过滤器 - 防路径遍历与白名单校验."""

    def __init__(self, allowed_paths: list[str] | None = None) -> None:
        """初始化当前对象。

        参数：
            allowed_paths (list[str] | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._allowed_paths = [str(Path(p).resolve()) for p in (allowed_paths or [])]

    def configure(self, paths: list[str]) -> None:
        """配置运行参数。

        参数：
            paths (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._allowed_paths = [str(Path(p).resolve()) for p in paths]

    def validate(self, file_path: str) -> bool:
        """验证路径是否安全."""
        if ".." in file_path:
            return False
        try:
            resolved = str(Path(file_path).expanduser().resolve())
        except (OSError, ValueError):
            return False
        if not self._allowed_paths:
            return True  # 未配置白名单时放行（开发模式）
        for allowed in self._allowed_paths:
            if resolved == allowed or resolved.startswith(allowed + "/"):
                return True
        return False

    @property
    def allowed_paths(self) -> list[str]:
        """执行“allowed paths”操作。

        返回值：
            list[str]: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return list(self._allowed_paths)


class DockerSandboxManager:
    """Docker 沙箱管理器."""

    SCOPE_SESSION = "session"
    SCOPE_AGENT = "agent"
    SCOPE_SHARED = "shared"

    def __init__(
        self,
        image: str = "athena-sandbox:latest",
        network_disabled: bool = True,
        memory_limit: str = "256m",
        cpu_limit: float = 0.5,
        workspace_path: str = "/workspace",
        allowed_paths: list[str] | None = None,
        docker_client: docker.DockerClient | None = None,
    ) -> None:
        """初始化当前对象。

        参数：
            image (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            network_disabled (bool): 输入参数；其类型和取值约束由方法签名及实现定义。
            memory_limit (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            cpu_limit (float): 输入参数；其类型和取值约束由方法签名及实现定义。
            workspace_path (str): 输入参数；其类型和取值约束由方法签名及实现定义。
            allowed_paths (list[str] | None): 输入参数；其类型和取值约束由方法签名及实现定义。
            docker_client (docker.DockerClient | None): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._client = docker_client
        self._image = image
        self._network_disabled = network_disabled
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._workspace_path = workspace_path
        self._containers: dict[str, str] = {}  # 作用域键 scope_key → 容器 ID container_id
        self._path_filter = PathSecurityFilter(allowed_paths)
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "DockerSandboxManager":
        """从配置创建实例."""
        return cls(
            image=settings.sandbox_image,
            network_disabled=settings.sandbox_network_disabled,
            memory_limit=settings.sandbox_memory_limit,
            cpu_limit=settings.sandbox_cpu_limit,
            workspace_path=settings.sandbox_workspace_path,
            allowed_paths=settings.allowed_paths_list,
        )

    def configure_allowed_paths(self, paths: list[str]) -> None:
        """执行“configure allowed paths”操作。

        参数：
            paths (list[str]): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._path_filter.configure(paths)

    def _get_client(self) -> docker.DockerClient:
        """执行“get client”操作。

        返回值：
            docker.DockerClient: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if self._client is None:
            import docker
            self._client = docker.from_env()
        return self._client

    def _get_scope_key(
        self, scope: str, session_id: str, run_id: str | None = None
    ) -> str:
        """执行“get scope key”操作。

        参数：
            scope (str): 检索范围或权限范围。
            session_id (str): 会话唯一标识。
            run_id (str | None): 运行唯一标识。

        返回值：
            str: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if scope == self.SCOPE_SESSION:
            return f"session_{session_id}"
        if scope == self.SCOPE_AGENT:
            return f"agent_{session_id}_{run_id or 'default'}"
        return "shared_default"

    def _validate_path(self, file_path: str) -> bool:
        """执行“validate path”操作。

        参数：
            file_path (str): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            bool: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        return self._path_filter.validate(file_path)

    def _ensure_container(
        self,
        scope: str,
        session_id: str,
        run_id: str | None = None,
        bind_mounts: dict[str, str] | None = None,
    ) -> docker.models.containers.Container:
        """确保指定作用域的容器存在且运行."""
        scope_key = self._get_scope_key(scope, session_id, run_id)
        client = self._get_client()

        if scope_key in self._containers:
            container_id = self._containers[scope_key]
            try:
                container = client.containers.get(container_id)
                if container.status != "running":
                    container.start()
                return container
            except Exception as e:
                # 容器不存在，移除并创建新的
                logger.warning("sandbox_existing_container_unavailable", scope_key=scope_key, container_id=container_id, error=str(e))
                self._containers.pop(scope_key, None)

        # 构建容器配置
        volumes: dict[str, dict[str, str]] = {}
        if bind_mounts:
            for host_path, container_path in bind_mounts.items():
                if self._validate_path(host_path):
                    volumes[host_path] = {"bind": container_path, "mode": "ro"}

        container = client.containers.run(
            image=self._image,
            detach=True,
            network_disabled=self._network_disabled,
            mem_limit=self._memory_limit,
            cpus=self._cpu_limit,
            working_dir=self._workspace_path,
            volumes=volumes,
            command=["sleep", "infinity"],  # 保持容器运行
        )
        self._containers[scope_key] = container.id
        logger.info(
            "sandbox_container_created",
            scope_key=scope_key,
            container_id=container.id,
        )
        return container

    async def execute_command(
        self,
        command: str,
        session_id: str,
        run_id: str | None = None,
        scope: str = SCOPE_SESSION,
        input_files: list[dict[str, str]] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        """在沙箱中执行命令.

        参数：
            command: 要执行的命令
            session_id: 会话 ID
            run_id: 运行 ID（可选）
            scope: 容器作用域
            input_files: 输入文件 [{"host_path": ..., "container_path": ...}]
            timeout: 超时时间（秒）

        返回值：
            {"exit_code": int, "stdout": str, "stderr": str}
        """
        async with self._lock:
            bind_mounts: dict[str, str] = {}
            if input_files:
                for spec in input_files:
                    host_path = spec["host_path"]
                    container_path = spec.get(
                        "container_path",
                        f"{self._workspace_path}/{Path(host_path).name}",
                    )
                    if self._validate_path(host_path):
                        bind_mounts[host_path] = container_path
                    else:
                        logger.warning("sandbox_path_blocked", path=host_path)

            container = self._ensure_container(scope, session_id, run_id, bind_mounts)

        def _exec() -> dict[str, Any]:
            """执行“exec”操作。

            返回值：
                dict[str, Any]: 操作结果；具体语义由调用场景决定。

            异常：
                Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
            """
            result = container.exec_run(
                cmd=["sh", "-c", command],
                demux=True,
                workdir=self._workspace_path,
            )
            stdout_bytes, stderr_bytes = (
                result.output if isinstance(result.output, tuple) else (result.output, b"")
            )
            return {
                "exit_code": result.exit_code,
                "stdout": stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else "",
                "stderr": stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else "",
            }

        try:
            return await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _exec),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"命令执行超时（{timeout}s）",
            }

    async def fetch_file(
        self,
        container_path: str,
        session_id: str,
        run_id: str | None = None,
        scope: str = SCOPE_SESSION,
    ) -> bytes | None:
        """从沙箱容器获取文件内容（project_memory 约束：使用 get_archive + tarfile）."""
        async with self._lock:
            scope_key = self._get_scope_key(scope, session_id, run_id)
            container_id = self._containers.get(scope_key)
            if not container_id:
                return None
            client = self._get_client()
            container = client.containers.get(container_id)

        def _fetch() -> bytes | None:
            """执行“fetch”操作。

            返回值：
                bytes | None: 操作结果；具体语义由调用场景决定。

            异常：
                Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
            """
            try:
                stream, _ = container.get_archive(container_path)
                buffer = b"".join(stream)
                with tarfile.open(fileobj=io.BytesIO(buffer), mode="r") as tf:
                    member = tf.getmembers()[0]
                    f = tf.extractfile(member)
                    return f.read() if f else None
            except Exception as e:
                logger.error("sandbox_fetch_failed", path=container_path, error=str(e))
                return None

        return await asyncio.get_event_loop().run_in_executor(None, _fetch)

    def cleanup_container(
        self,
        scope: str,
        session_id: str,
        run_id: str | None = None,
        force: bool = False,
    ) -> None:
        """清理指定作用域的容器."""
        scope_key = self._get_scope_key(scope, session_id, run_id)
        container_id = self._containers.pop(scope_key, None)
        if not container_id:
            return
        try:
            client = self._get_client()
            container = client.containers.get(container_id)
            container.remove(force=force)
            logger.info("sandbox_container_removed", scope_key=scope_key)
        except Exception as e:
            logger.warning("sandbox_cleanup_failed", error=str(e))

    def cleanup_session_containers(self, session_id: str) -> None:
        """清理指定会话的所有容器."""
        keys_to_remove = [k for k in self._containers if session_id in k]
        for key in keys_to_remove:
            container_id = self._containers.pop(key)
            try:
                client = self._get_client()
                container = client.containers.get(container_id)
                container.remove(force=True)
            except Exception as e:
                logger.warning("sandbox_session_container_cleanup_failed", session_id=session_id, container_id=container_id, error=str(e))
        if keys_to_remove:
            logger.info(
                "sandbox_session_cleaned",
                session_id=session_id,
                count=len(keys_to_remove),
            )

    def cleanup_all(self) -> None:
        """清理所有容器."""
        for scope_key in list(self._containers.keys()):
            container_id = self._containers.pop(scope_key)
            try:
                client = self._get_client()
                container = client.containers.get(container_id)
                container.remove(force=True)
            except Exception as e:
                logger.warning("sandbox_container_cleanup_failed", scope_key=scope_key, container_id=container_id, error=str(e))
        logger.info("sandbox_all_containers_cleaned")


class SandboxReconstructionChecker:
    """沙箱容器定期重建检查器（project_memory 约束：定时检查 + 手动重建）."""

    def __init__(
        self,
        manager: DockerSandboxManager,
        check_interval: int = 300,
    ) -> None:
        """初始化当前对象。

        参数：
            manager (DockerSandboxManager): 输入参数；其类型和取值约束由方法签名及实现定义。
            check_interval (int): 输入参数；其类型和取值约束由方法签名及实现定义。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._manager = manager
        self._check_interval = check_interval
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """启动服务。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止服务。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        """执行“loop”操作。

        返回值：
            None: 操作结果；具体语义由调用场景决定。

        异常：
            Exception: 底层校验、存储、网络或服务调用失败且未被当前方法处理时抛出。
        """
        while self._running:
            try:
                await asyncio.sleep(self._check_interval)
                # 简单实现：仅记录日志，实际可检查容器健康状态
                logger.debug("sandbox_reconstruction_check")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("sandbox_check_error", error=str(e))
