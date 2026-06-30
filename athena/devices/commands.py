"""Device command definitions.

Standardized command interface for host and Android devices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeviceCommand:
    """A command to be executed on a device."""
    id: str
    method: str  # 'run_script', 'screenshot', 'simulate_keystroke', etc.
    params: dict[str, Any] = field(default_factory=dict)
