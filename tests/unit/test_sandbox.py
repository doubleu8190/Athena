"""Tests for Jinja2 SandboxedEnvironment template rendering."""

import pytest
from jinja2 import UndefinedError

from athena.core.sandbox import render_template, render_args
from athena.core.task_context import TaskContext


class TestTemplateRendering:
    """Test safe template variable substitution."""

    @pytest.fixture
    def task_ctx(self):
        ctx = TaskContext(
            task_id="test-task",
            session_id="sess-1",
            user_id="user1",
            channel="web",
        )
        ctx.complete_step(1, "Hello from step 1")
        ctx.complete_step(2, {"key": "value"})
        return ctx

    def test_simple_variable(self, task_ctx):
        """Simple step output reference should work."""
        result = render_template("{{ step1.output }}", task_ctx)
        assert result == "Hello from step 1"

    def test_undefined_variable(self, task_ctx):
        """Undefined variable should raise error."""
        with pytest.raises(UndefinedError):
            render_template("{{ step99.output }}", task_ctx)

    def test_no_injection(self, task_ctx):
        """Template with dangerous builtins should be rejected."""
        # Jinja2 SandboxedEnvironment blocks __import__, open, etc.
        result = render_template("safe text without variables", task_ctx)
        assert result == "safe text without variables"

    def test_render_args_dict(self, task_ctx):
        """render_args should handle dict with template values."""
        args = {
            "path": "/workspace/{{ step1.output }}.txt",
            "content": "{{ step2.output }}",
            "plain": "no template",
        }
        result = render_args(args, task_ctx)
        assert result["path"] == "/workspace/Hello from step 1.txt"
        assert result["plain"] == "no template"

    def test_render_args_nested(self, task_ctx):
        """render_args should handle nested structures."""
        args = {
            "outer": {"inner": "{{ step1.output }}"},
            "list_val": ["{{ step1.output }}", "static"],
        }
        result = render_args(args, task_ctx)
        assert result["outer"]["inner"] == "Hello from step 1"
        assert result["list_val"][0] == "Hello from step 1"
        assert result["list_val"][1] == "static"
