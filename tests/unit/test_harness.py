"""Tests for Harness Engine — safety rule evaluation."""

import pytest

from athena.core.harness import HarnessEngine, HarnessAction, HarnessResult, RiskLevel


class TestHarnessBlacklist:
    """Test blacklist rule enforcement."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def test_rm_rf_blocked(self, harness):
        """rm -rf / should be blocked."""
        action = HarnessAction(tool_name="run_script", arguments={"script": "rm -rf /"})
        result = harness._check_blacklist(action, HarnessResult())
        assert result.allowed is False
        assert "rm" in result.reason.lower()

    def test_sudo_blocked(self, harness):
        """sudo should be blocked."""
        action = HarnessAction(tool_name="run_script", arguments={"script": "sudo ls"})
        result = harness._check_blacklist(action, HarnessResult())
        assert result.allowed is False

    def test_safe_command_allowed(self, harness):
        """Safe commands should pass."""
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/test.txt"})
        result = harness._check_blacklist(action, HarnessResult())
        assert result.allowed is True


class TestPathBoundary:
    """Test /workspace/ path boundary enforcement."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def test_workspace_path_allowed(self, harness):
        """Paths inside /workspace/ should be allowed."""
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True

    def test_etc_path_blocked(self, harness):
        """Paths outside /workspace/ should be blocked."""
        action = HarnessAction(tool_name="file_read", arguments={"path": "/etc/passwd"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is False

    def test_path_traversal_blocked(self, harness):
        """Path traversal should be blocked."""
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/../../../etc/passwd"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is False

    def test_relative_path_allowed(self, harness):
        """Relative paths that resolve inside workspace should pass pre-check."""
        # Note: relative paths get anchored to /workspace in filesystem tools
        action = HarnessAction(tool_name="file_read", arguments={"path": "data.txt"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True  # Not absolute, pre-check passes


class TestRiskAssessment:
    """Test risk level determination."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def test_file_read_is_low_risk(self, harness):
        assert harness.get_risk_level("file_read") == RiskLevel.LOW

    def test_file_delete_is_critical(self, harness):
        assert harness.get_risk_level("file_delete") == RiskLevel.CRITICAL

    def test_file_write_is_high(self, harness):
        assert harness.get_risk_level("file_write") == RiskLevel.HIGH

    def test_web_search_is_low(self, harness):
        assert harness.get_risk_level("web_search") == RiskLevel.LOW

    def test_unknown_tool_is_medium(self, harness):
        assert harness.get_risk_level("unknown_tool") == RiskLevel.MEDIUM


class TestCoolingOff:
    """Test cooling-off period defaults."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def test_low_no_cooling_off(self, harness):
        assert harness.get_cooling_off(RiskLevel.LOW) == 0

    def test_high_30_seconds(self, harness):
        assert harness.get_cooling_off(RiskLevel.HIGH) == 30

    def test_critical_60_seconds(self, harness):
        assert harness.get_cooling_off(RiskLevel.CRITICAL) == 60
