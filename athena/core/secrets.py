"""Secrets helper for Docker Compose/Secrets and environment variables.

Reads secrets from files mounted at /run/secrets/<name> (Docker Compose Secrets),
falling back to environment variables for backward compatibility and local dev.

Usage:
    from athena.core.secrets import get_secret

    token = get_secret("TELEGRAM_BOT_TOKEN")
    api_key = get_secret("DEEPSEEK_API_KEY", default="")
"""

from __future__ import annotations

import os
from pathlib import Path

# Docker Compose mounts secret files under /run/secrets/<secret_name>
_SECRETS_DIR = Path("/run/secrets")


def get_secret(env_var: str, default: str = "") -> str:
    """Read a secret, preferring Docker Compose Secrets file over env var.

    Docker Compose Secrets are mounted at /run/secrets/<lowercase(env_var)>.
    If the secret file exists, its content is read and whitespace-stripped.
    Otherwise, falls back to the environment variable.

    Args:
        env_var: Environment variable name (e.g. "TELEGRAM_BOT_TOKEN").
                 The corresponding secret file is the lowercase version.
        default: Default value if neither secret file nor env var exists.

    Returns:
        The secret value, or ``default`` if not found.
    """
    secret_name = env_var.lower()
    secret_path = _SECRETS_DIR / secret_name

    if secret_path.is_file():
        try:
            return secret_path.read_text().strip()
        except OSError:
            pass  # Fall through to env var

    return os.environ.get(env_var, default)
