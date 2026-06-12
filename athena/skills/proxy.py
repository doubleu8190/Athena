"""Squid proxy ACL management for skill network isolation.

Manages per-skill domain whitelists via dynamic Squid ACL files.
Each skill gets its own ACL file at /etc/squid/acl/<skill_id>.conf.
"""

from __future__ import annotations

import os
from pathlib import Path

from athena.logging_config import get_logger

logger = get_logger(__name__)

ACL_DIR = Path("/etc/squid/acl")
PROXY_CONTAINER_NAME = "skill-proxy"


class ProxyManager:
    """Manages Squid proxy ACL rules for skill containers.

    Each skill's allowed_domains are written as Squid ACL config snippets.
    After changes, squid -k reconfigure is called for hot-reload.
    """

    async def add_acl(self, skill_id: str, domains: list[str]) -> None:
        """Add ACL rules for a skill's allowed domains.

        Args:
            skill_id: Skill identifier.
            domains: List of allowed domain names (e.g. ["api.example.com", "*.github.com"]).
        """
        acl_file = ACL_DIR / f"{skill_id}.conf"
        ACL_DIR.mkdir(parents=True, exist_ok=True)

        lines = [f"# ACL rules for skill: {skill_id}"]
        for domain in domains:
            lines.append(f"acl {skill_id}_domains dstdomain {domain}")
        lines.append(f"http_access allow {skill_id}_domains")
        lines.append("")

        acl_file.write_text("\n".join(lines))
        logger.info(
            "proxy_acl_added",
            skill_id=skill_id,
            domains=domains,
        )

        await self._reconfigure()

    async def remove_acl(self, skill_id: str) -> None:
        """Remove ACL rules for a skill."""
        acl_file = ACL_DIR / f"{skill_id}.conf"
        if acl_file.exists():
            acl_file.unlink()
            logger.info("proxy_acl_removed", skill_id=skill_id)
            await self._reconfigure()

    async def _reconfigure(self) -> None:
        """Tell Squid to reload its configuration."""
        try:
            import docker
            client = docker.from_env()
            try:
                container = client.containers.get(PROXY_CONTAINER_NAME)
                container.exec_run("squid -k reconfigure")
                logger.debug("proxy_reconfigured")
            except Exception:
                # Proxy container may not exist
                pass
        except Exception as e:
            logger.warning("proxy_reconfigure_failed", error=str(e))
