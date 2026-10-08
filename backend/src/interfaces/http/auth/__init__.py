"""HTTP cookie authentication owned by the target interface layer."""

from .middleware import AuthenticationMiddleware
from .options import AuthOptions
from .routes import build_auth_router, is_authenticated

__all__ = ["AuthOptions", "AuthenticationMiddleware", "build_auth_router", "is_authenticated"]
