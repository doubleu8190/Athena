"""Stable identities used by PostgreSQL memory-forget suppression markers."""

from __future__ import annotations

import hashlib
import json


EPISODE_SUPPRESSION_FINGERPRINT = hashlib.sha256(b"episode-summary").hexdigest()
PROJECT_SUPPRESSION_FINGERPRINT = hashlib.sha256(b"project-space").hexdigest()
PROJECT_TURN_SUPPRESSION_FINGERPRINT = hashlib.sha256(
    b"project-space-source-turn"
).hexdigest()


def source_suppression_lock_id(source_id: str, fingerprint: str) -> int:
    """Build a shared advisory lock key for one suppression identity."""
    serialized = json.dumps(
        ("memory-source-suppression", source_id, fingerprint),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return int.from_bytes(
        hashlib.sha256(serialized.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )


__all__ = [
    "EPISODE_SUPPRESSION_FINGERPRINT",
    "PROJECT_SUPPRESSION_FINGERPRINT",
    "PROJECT_TURN_SUPPRESSION_FINGERPRINT",
    "source_suppression_lock_id",
]
