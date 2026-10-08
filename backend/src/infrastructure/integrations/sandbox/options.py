"""Docker sandbox adapter options."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxOptions:
    docker_binary: str = "docker"
    default_network: str = "none"
    max_output_bytes: int = 1_000_000
    memory_mb: int = 512
    cpu_limit: float = 1.0


__all__ = ["SandboxOptions"]
