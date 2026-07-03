"""Tests for Harness Engine — safety rule evaluation."""

import pytest

from athena.core.harness import HarnessEngine, HarnessAction, HarnessResult
from athena.models.harness_rule import RuleType


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
        assert "recursive" in result.reason.lower()

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
    """Test path_permission rule enforcement (replaced old /workspace boundary)."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def test_no_rules_allows_all(self, harness):
        """With zero path_permission rules, all paths are unrestricted."""
        action = HarnessAction(tool_name="file_read", arguments={"path": "/etc/passwd"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True

    def test_no_rules_allows_write(self, harness):
        """With zero rules, writes are also unrestricted."""
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True

    def test_file_search_allowed_no_rules(self, harness):
        """file_search is checked like file_read."""
        action = HarnessAction(tool_name="file_search", arguments={"path": "/workspace/"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True

    def test_non_filesystem_tool_ignored(self, harness):
        """Non-filesystem tools bypass path_permission check."""
        # Even with rules loaded, non-filesystem tools are not checked
        harness._rules = [{
            "rule_id": "test-rule",
            "rule_type": RuleType.PATH_PERMISSION,
            "name": "block-all",
            "config": {"path": "/**", "permissions": "r"},
            "priority": 100,
            "revision": 1,
        }]
        action = HarnessAction(tool_name="adb_shell", arguments={"path": "/system/bin/ls"})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True

    def test_empty_path_ignored(self, harness):
        """Actions with no path argument bypass the check."""
        harness._rules = [{
            "rule_id": "test-rule",
            "rule_type": RuleType.PATH_PERMISSION,
            "name": "block-all",
            "config": {"path": "/**", "permissions": ""},
            "priority": 100,
            "revision": 1,
        }]
        action = HarnessAction(tool_name="file_read", arguments={})
        result = harness._check_path_boundary(action, HarnessResult())
        assert result.allowed is True


class TestPathPermissions:
    """Test path_permission rule matching and enforcement."""

    @pytest.fixture
    def harness(self, test_config):
        return HarnessEngine(test_config)

    def _load_rules(self, harness, rules):
        """Helper: load test rules into harness in-memory store."""
        harness._rules = [
            {
                "rule_id": r["id"],
                "rule_type": RuleType.PATH_PERMISSION,
                "name": r["name"],
                "config": {"path": r["path"], "permissions": r["perms"]},
                "priority": r.get("priority", 100),
                "revision": 1,
            }
            for r in rules
        ]

    # ── Permission enforcement ──────────────────────────────────────

    def test_r_allows_read(self, harness):
        """r permission allows file_read."""
        self._load_rules(harness, [{"id": "r1", "name": "read-only", "path": "/workspace/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True

    def test_r_blocks_write(self, harness):
        """r permission blocks file_write."""
        self._load_rules(harness, [{"id": "r1", "name": "read-only", "path": "/workspace/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is False
        assert "read-only" in result.reason
        assert result.blocked_by_rule == "path_permission"

    def test_r_blocks_delete(self, harness):
        """r permission blocks file_delete."""
        self._load_rules(harness, [{"id": "r1", "name": "read-only", "path": "/workspace/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_delete", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is False

    def test_r_allows_file_search(self, harness):
        """r permission allows file_search (treated as read)."""
        self._load_rules(harness, [{"id": "r1", "name": "read-only", "path": "/workspace/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_search", arguments={"path": "/workspace/"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True

    def test_w_allows_write(self, harness):
        """w permission allows file_write."""
        self._load_rules(harness, [{"id": "w1", "name": "write-only", "path": "/workspace/**", "perms": "w"}])
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True

    def test_w_blocks_read(self, harness):
        """w permission blocks file_read."""
        self._load_rules(harness, [{"id": "w1", "name": "write-only", "path": "/workspace/**", "perms": "w"}])
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is False

    def test_w_allows_delete(self, harness):
        """w permission allows file_delete (destructive write)."""
        self._load_rules(harness, [{"id": "w1", "name": "write-only", "path": "/workspace/**", "perms": "w"}])
        action = HarnessAction(tool_name="file_delete", arguments={"path": "/workspace/data.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True

    def test_rw_allows_all(self, harness):
        """rw permission allows both read and write."""
        self._load_rules(harness, [{"id": "rw1", "name": "full-access", "path": "/workspace/**", "perms": "rw"}])
        for tool in ["file_read", "file_write", "file_delete", "file_search"]:
            action = HarnessAction(tool_name=tool, arguments={"path": "/workspace/data.txt"})
            result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
            assert result.allowed is True, f"{tool} should be allowed with rw"

    # ── Priority / first-match-wins ─────────────────────────────────

    def test_first_match_wins(self, harness):
        """Higher-priority rule matches first and wins."""
        self._load_rules(harness, [
            {"id": "p100", "name": "read-only", "path": "/workspace/**", "perms": "r", "priority": 100},
            {"id": "p50", "name": "rw-area", "path": "/workspace/data/**", "perms": "rw", "priority": 50},
        ])
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/data/x.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        # P100 matches first and only allows r → blocked
        assert result.allowed is False
        assert "read-only" in result.reason

    def test_prioritized_specific_rule(self, harness):
        """More-specific rule with higher priority can override general rule."""
        self._load_rules(harness, [
            {"id": "p100", "name": "rw-area", "path": "/workspace/data/**", "perms": "rw", "priority": 100},
            {"id": "p10", "name": "read-only", "path": "/workspace/**", "perms": "r", "priority": 10},
        ])
        # file_read on a non-data path → P100 doesn't match, P10 matches with r
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/other/file.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True  # P10 allows read

    # ── Unmatched paths ────────────────────────────────────────────

    def test_unmatched_path_allowed(self, harness):
        """Path not matching any rule is unrestricted."""
        self._load_rules(harness, [{"id": "r1", "name": "restricted", "path": "/workspace/data/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_write", arguments={"path": "/workspace/temp/out.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True  # unmatched → unrestricted

    # ── Path normalization ──────────────────────────────────────────

    def test_dot_dot_normalized(self, harness):
        """Path with .. is normalized before matching."""
        self._load_rules(harness, [{"id": "r1", "name": "restricted", "path": "/workspace/temp/**", "perms": "r"}])
        # /workspace/temp/../temp/file.txt → /workspace/temp/file.txt
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace/temp/../temp/file.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True  # normalized path matches /workspace/temp/**

    def test_double_slash_normalized(self, harness):
        """Double slashes are normalized."""
        self._load_rules(harness, [{"id": "r1", "name": "restricted", "path": "/workspace/data/**", "perms": "r"}])
        action = HarnessAction(tool_name="file_read", arguments={"path": "/workspace//data/file.txt"})
        result = harness._check_path_permissions(action, HarnessResult(), action.arguments["path"])
        assert result.allowed is True


class TestGlobMatch:
    """Test _path_matches_glob static method."""

    def test_exact_match(self):
        assert HarnessEngine._path_matches_glob("/workspace/data.txt", "/workspace/data.txt") is True

    def test_exact_mismatch(self):
        assert HarnessEngine._path_matches_glob("/workspace/data.txt", "/workspace/other.txt") is False

    def test_star_single_segment(self):
        """* matches within a single path segment."""
        assert HarnessEngine._path_matches_glob("/workspace/data.txt", "/workspace/*.txt") is True
        assert HarnessEngine._path_matches_glob("/workspace/data.log", "/workspace/*.txt") is False

    def test_star_no_cross_directory(self):
        """* does NOT match across directory boundaries."""
        assert HarnessEngine._path_matches_glob("/workspace/sub/data.txt", "/workspace/*.txt") is False

    def test_double_star_recursive(self):
        """** matches any depth including zero."""
        assert HarnessEngine._path_matches_glob("/workspace/data.txt", "/workspace/**") is True
        assert HarnessEngine._path_matches_glob("/workspace/a/b/c/d.txt", "/workspace/**") is True
        assert HarnessEngine._path_matches_glob("/workspace/", "/workspace/**") is True

    def test_double_star_matches_base_dir(self):
        """Pattern ending with /** matches the base directory itself."""
        assert HarnessEngine._path_matches_glob("/workspace/data", "/workspace/data/**") is True
        assert HarnessEngine._path_matches_glob("/workspace/data/", "/workspace/data/**") is True
        assert HarnessEngine._path_matches_glob("/workspace/data/sub", "/workspace/data/**") is True

    def test_question_mark(self):
        """? matches a single character."""
        assert HarnessEngine._path_matches_glob("/workspace/a.txt", "/workspace/?.txt") is True
        assert HarnessEngine._path_matches_glob("/workspace/ab.txt", "/workspace/?.txt") is False

    def test_regex_chars_escaped(self):
        """Regex metacharacters in path are handled literally."""
        assert HarnessEngine._path_matches_glob("/workspace/file[1].txt", "/workspace/file[1].txt") is True

    def test_pattern_in_middle(self):
        """Glob pattern with ** in the middle."""
        assert HarnessEngine._path_matches_glob("/workspace/a/b/data.txt", "/workspace/**/data.txt") is True

    def test_complex_pattern(self):
        """Complex pattern with both * and **."""
        assert HarnessEngine._path_matches_glob("/workspace/projects/my-app/src/main.py", "/workspace/projects/*/src/**") is True
        assert HarnessEngine._path_matches_glob("/workspace/projects/other/main.py", "/workspace/projects/*/src/**") is False
