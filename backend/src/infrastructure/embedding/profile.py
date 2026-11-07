"""Stable identifier for an embedding model and preprocessing contract."""

from __future__ import annotations

import hashlib
import json


def embedding_profile_id(model: str, revision: str) -> str:
    """Build the shared profile key used by API writes and worker inference."""
    profile = json.dumps(
        {
            "model": model,
            "revision": revision,
            "dimensions": 1024,
            "normalize_embeddings": False,
            "preprocessing": "sentence-transformers-default-v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"bge-{hashlib.sha256(profile.encode()).hexdigest()[:32]}"
