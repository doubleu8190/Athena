"""OAuth2 / authentication helpers for MCP client connections."""

from __future__ import annotations

from cryptography.fernet import Fernet
import os

# Key for AES-256-GCM token encryption. In production, this should be
# injected via environment variable.
_ENCRYPTION_KEY = os.environ.get(
    "ATHENA_ENCRYPTION_KEY",
    Fernet.generate_key().decode() if not os.environ.get("ATHENA_ENCRYPTION_KEY") else "",
)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_ENCRYPTION_KEY.encode() if isinstance(_ENCRYPTION_KEY, str) else _ENCRYPTION_KEY)
    return _fernet


def encrypt_token(token: str) -> str:
    """Encrypt an OAuth2 access token for persistent storage."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a stored OAuth2 access token."""
    return _get_fernet().decrypt(encrypted_token.encode()).decode()
