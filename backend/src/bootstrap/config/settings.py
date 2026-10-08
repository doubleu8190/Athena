"""Backward-compatible import path for process configuration.

New code should import from :mod:`configuration.settings`.  This shim keeps
existing scripts and deployments working during the package migration.
"""

from __future__ import annotations

from configuration.settings import LLMProviderConfig, LLMRetrySettings, Settings, get_settings

__all__ = ["LLMProviderConfig", "LLMRetrySettings", "Settings", "get_settings"]
