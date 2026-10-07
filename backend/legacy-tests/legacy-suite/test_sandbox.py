from pathlib import Path

import pytest

from athena.config.settings import Settings
from athena.core.sandbox.workspace import WorkspaceError, WorkspaceManager
from athena.infrastructure.sandbox.docker_runner import DockerRunner


def test_workspace_manager_rejects_escape(tmp_path: Path):
    manager = WorkspaceManager(tmp_path / "sandboxes")
    workspace = manager.create("session", "run")

    assert manager.resolve_cwd(workspace, None) == "/workspace"
    assert manager.resolve_cwd(workspace, "materials") == "/workspace/materials"
    with pytest.raises(WorkspaceError):
        manager.resolve_cwd(workspace, "../outside")
    with pytest.raises(WorkspaceError):
        manager.resolve_cwd(workspace, "/etc")


def test_docker_runner_builds_restricted_mcp_command(tmp_path: Path):
    settings = Settings(_env_file=None, sandbox_workspace_root=str(tmp_path))
    runner = DockerRunner(settings)
    spec = runner.build_mcp_process(
        server_name="web/search",
        image="registry.example/mcp@sha256:abc",
        command=["python", "/opt/server.py"],
        args=["--stdio"],
        workspace=tmp_path,
    )

    assert spec.command == "docker"
    assert "-i" in spec.args
    assert "--read-only" in spec.args
    assert "--cap-drop" in spec.args
    assert "ALL" in spec.args
    assert "--network" in spec.args
    assert spec.args[spec.args.index("--network") + 1] == "none"
    assert "--privileged" not in spec.args
    assert "--network=host" not in spec.args
    assert "registry.example/mcp@sha256:abc" in spec.args
    assert "python" in spec.args
    assert "--env" not in spec.args


def test_docker_runner_rejects_non_none_network_policy(tmp_path: Path):
    settings = Settings(_env_file=None, sandbox_workspace_root=str(tmp_path))
    runner = DockerRunner(settings)
    with pytest.raises(Exception, match="unsupported network policy"):
        runner.build_mcp_process(
            server_name="web",
            image="registry.example/mcp@sha256:abc",
            command=["python"],
            args=[],
            workspace=tmp_path,
            network_policy="allowlist",
        )
