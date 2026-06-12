"""Tests for configuration loading."""

import os
import tempfile
from pathlib import Path

import pytest
import yaml


class TestConfigLoading:
    """Test Config.load() from YAML files."""

    def test_default_config_loads(self):
        """Config should load with defaults when no files exist."""
        from athena.config import Config
        config = Config.load()
        assert config.system.max_fallback_depth == 1
        assert config.system.session_idle_timeout_minutes == 30

    def test_system_config_from_file(self):
        """System config should be loaded from athena.yaml."""
        import athena.config as cfg_mod
        orig_path = cfg_mod.DEFAULT_SYSTEM_CONFIG_PATH

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "system": {"max_fallback_depth": 3, "session_idle_timeout_minutes": 15},
                "harness": {"circuit_breaker_threshold": 5},
            }, f)
            temp_path = Path(f.name)

        try:
            cfg_mod.DEFAULT_SYSTEM_CONFIG_PATH = temp_path
            from athena.config import Config, set_config
            set_config(None)
            config = Config.load()
            assert config.system.max_fallback_depth == 3
            assert config.system.session_idle_timeout_minutes == 15
            assert config.system.circuit_breaker_threshold == 5
        finally:
            cfg_mod.DEFAULT_SYSTEM_CONFIG_PATH = orig_path
            temp_path.unlink(missing_ok=True)

    def test_llm_config_loads(self):
        """LLM config should load provider info."""
        import athena.config as cfg_mod
        orig_path = cfg_mod.DEFAULT_LLM_CONFIG_PATH

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump({
                "default_provider": "openai",
                "fallback_chain": ["anthropic"],
                "providers": {
                    "openai": {"api_key_env": "OPENAI_KEY", "model": "gpt-4o", "max_tokens": 4096},
                    "anthropic": {"api_key_env": "ANTHROPIC_KEY", "model": "claude-sonnet-4-6"},
                },
            }, f)
            temp_path = Path(f.name)

        try:
            cfg_mod.DEFAULT_LLM_CONFIG_PATH = temp_path
            from athena.config import Config, set_config
            set_config(None)
            config = Config.load()
            assert config.llm.default_provider == "openai"
            assert "anthropic" in config.llm.fallback_chain
            assert "openai" in config.llm.providers
        finally:
            cfg_mod.DEFAULT_LLM_CONFIG_PATH = orig_path
            temp_path.unlink(missing_ok=True)

    def test_env_var_override(self):
        """Environment variables should override defaults."""
        from athena.config import Config, set_config
        set_config(None)

        os.environ["ADMIN_API_KEY"] = "test-key-123"
        os.environ["LOG_LEVEL"] = "DEBUG"
        try:
            config = Config.load()
            assert config.admin_api_key == "test-key-123"
            assert config.log_level == "DEBUG"
        finally:
            del os.environ["ADMIN_API_KEY"]
            del os.environ["LOG_LEVEL"]
