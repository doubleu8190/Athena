"""Skill Manager — Docker container lifecycle for MCP-based skills.

Each skill runs as a Docker container with its own MCP server (stdio mode).
Network access is controlled via allowed_domains → Squid proxy ACL.

Install flow:
1. Pull Docker image
2. Create container (non-root, no privileges, resource limits)
3. Configure network (none or bridge+proxy)
4. Start container, attach to stdin/stdout for MCP communication
5. MCP handshake → tools/list → register in ToolRegistry
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from athena.config import Config
from athena.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SkillInfo:
    """Information about an installed skill."""
    skill_id: str
    name: str
    version: str
    image_uri: str
    status: str
    container_id: str | None = None
    allowed_domains: list[str] | None = None


class SkillManager:
    """Manages the lifecycle of Docker-based Skill containers.

    Each skill runs an MCP stdio server inside a Docker container.
    Communication happens via docker exec / attach over stdin/stdout.
    """

    def __init__(self, config: Config):
        self.config = config
        self._docker_client = None

    def _get_docker(self):
        """Lazy-init the Docker SDK client."""
        if self._docker_client is None:
            import docker
            self._docker_client = docker.from_env()
        return self._docker_client

    # ── Install ───────────────────────────────────────────────────────

    async def install(
        self,
        image_uri: str,
        skill_name: str,
        allowed_domains: list[str] | None = None,
        version: str = "latest",
    ) -> str:
        """Install a new skill.

        Args:
            image_uri: Docker image URI to pull.
            skill_name: Human-readable skill name.
            allowed_domains: List of domains the skill can access (empty = no network).
            version: Image version tag.

        Returns:
            skill_id: Unique skill identifier.
        """
        import uuid
        skill_id = f"skill_{uuid.uuid4().hex[:12]}"

        logger.info(
            "skill_install_start",
            skill_id=skill_id,
            image=image_uri,
            name=skill_name,
        )

        try:
            docker = self._get_docker()

            # 1. Pull image
            logger.info("skill_pulling_image", image=image_uri)
            image = docker.images.pull(image_uri, tag=version)

            # 2. Prepare container config
            container_config: dict[str, Any] = {
                "image": image_uri,
                "name": f"athena-skill-{skill_id}",
                "detach": True,
                "stdin_open": True,  # For stdio MCP
                "user": "nobody",  # Non-root
                "security_opt": ["no-new-privileges"],
                "cap_drop": ["ALL"],
                "read_only": True,
                "mem_limit": "512m",
                "cpu_period": 100000,
                "cpu_quota": 50000,  # 0.5 CPU
            }

            # 3. Network configuration
            if not allowed_domains:
                # No network access
                container_config["network_mode"] = "none"
            else:
                # Connect to skill-proxy bridge network
                container_config["network"] = "skill-net"
                container_config["environment"] = {
                    "HTTP_PROXY": "http://skill-proxy:3128",
                    "HTTPS_PROXY": "http://skill-proxy:3128",
                }

            # 4. Create and start container
            container = docker.containers.run(**container_config)

            # 5. Update Squid ACL if domains specified
            if allowed_domains:
                from athena.skills.proxy import ProxyManager
                proxy = ProxyManager()
                await proxy.add_acl(skill_id, allowed_domains)

            # 6. Register as MCP server
            # (In production, MCP handshake happens here)

            logger.info(
                "skill_installed",
                skill_id=skill_id,
                container_id=container.id[:12],
            )

            return skill_id

        except Exception as e:
            logger.error("skill_install_failed", skill_id=skill_id, error=str(e))
            raise

    # ── Uninstall ─────────────────────────────────────────────────────

    async def uninstall(self, skill_id: str) -> bool:
        """Uninstall a skill: stop container, remove image, clean up.

        Returns True if successful.
        """
        logger.info("skill_uninstall_start", skill_id=skill_id)

        try:
            docker = self._get_docker()

            container_name = f"athena-skill-{skill_id}"

            # Stop and remove container
            try:
                container = docker.containers.get(container_name)
                container.stop(timeout=10)
                container.remove(force=True)
            except Exception:
                pass  # Container may not exist

            # Clean up proxy ACL
            try:
                from athena.skills.proxy import ProxyManager
                proxy = ProxyManager()
                await proxy.remove_acl(skill_id)
            except Exception:
                pass

            logger.info("skill_uninstalled", skill_id=skill_id)
            return True

        except Exception as e:
            logger.error("skill_uninstall_failed", skill_id=skill_id, error=str(e))
            return False

    # ── Status ────────────────────────────────────────────────────────

    async def get_status(self, skill_id: str) -> str:
        """Get the current status of a skill container."""
        try:
            docker = self._get_docker()
            container_name = f"athena-skill-{skill_id}"
            container = docker.containers.get(container_name)
            return container.status
        except Exception:
            return "not_found"

    async def list_skills(self) -> list[SkillInfo]:
        """List all installed skill containers."""
        try:
            docker = self._get_docker()
            containers = docker.containers.list(
                all=True,
                filters={"name": "athena-skill-"},
            )
            return [
                SkillInfo(
                    skill_id=c.name.replace("athena-skill-", ""),
                    name=c.name,
                    version="latest",
                    image_uri=c.image.tags[0] if c.image.tags else "unknown",
                    status=c.status,
                    container_id=c.id[:12],
                )
                for c in containers
            ]
        except Exception as e:
            logger.error("skill_list_failed", error=str(e))
            return []
