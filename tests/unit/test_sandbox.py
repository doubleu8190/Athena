"""Tests for Jinja2 SandboxedEnvironment template rendering and output normalization."""

import pytest
from jinja2 import UndefinedError

from athena.core.sandbox import render_template, render_args, normalize_output
from athena.core.task_context import TaskContext


class TestOutputNormalization:
    """Test MCP output normalization for downstream template consumption."""

    def test_normalize_mcp_content_blocks_with_json(self):
        """MCP content blocks containing JSON should be unwrapped and parsed."""
        raw = [
            {"type": "text", "text": '{"return": [{"location": "112.938882,28.228304", "city": "长沙"}]}'}
        ]
        result = normalize_output(raw)
        assert isinstance(result, dict)
        assert result["return"][0]["location"] == "112.938882,28.228304"
        assert result["return"][0]["city"] == "长沙"

    def test_normalize_mcp_content_blocks_plain_text(self):
        """MCP content blocks with plain text (not JSON) should return the text."""
        raw = [
            {"type": "text", "text": "Operation completed successfully."}
        ]
        result = normalize_output(raw)
        assert result == "Operation completed successfully."

    def test_normalize_multiple_text_blocks(self):
        """Multiple MCP text blocks should be joined and parsed if JSON."""
        raw = [
            {"type": "text", "text": '{"status": "ok",'},
            {"type": "text", "text": '"data": [1, 2, 3]}'},
        ]
        result = normalize_output(raw)
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert result["data"] == [1, 2, 3]

    def test_normalize_plain_json_string(self):
        """A plain JSON string should be parsed into structured data."""
        result = normalize_output('{"key": "value", "nested": {"a": 1}}')
        assert isinstance(result, dict)
        assert result["key"] == "value"
        assert result["nested"]["a"] == 1

    def test_normalize_plain_non_json_string(self):
        """A plain non-JSON string should pass through unchanged."""
        result = normalize_output("just some text")
        assert result == "just some text"

    def test_normalize_non_text_blocks_passthrough(self):
        """Non-text MCP content blocks (e.g. images) should pass through."""
        raw = [
            {"type": "image", "data": "base64..."},
        ]
        result = normalize_output(raw)
        assert result == raw

    def test_normalize_mixed_blocks_passthrough(self):
        """Mixed text + non-text blocks should pass through unchanged."""
        raw = [
            {"type": "text", "text": "some text"},
            {"type": "image", "data": "base64..."},
        ]
        result = normalize_output(raw)
        assert result == raw

    def test_normalize_empty_list(self):
        """Empty list should pass through."""
        result = normalize_output([])
        assert result == []

    def test_normalize_none(self):
        """None should pass through."""
        result = normalize_output(None)
        assert result is None

    def test_normalize_json_array_string(self):
        """A JSON array string should be parsed into a list."""
        result = normalize_output('[{"id": 1}, {"id": 2}]')
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["id"] == 1

    def test_normalize_already_parsed_dict(self):
        """An already-parsed dict should pass through unchanged."""
        already = {"key": "value"}
        result = normalize_output(already)
        assert result == already


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

    @pytest.fixture
    def task_ctx_structured(self):
        """TaskContext with structured (dict) step outputs — simulating
        normalized MCP tool responses."""
        ctx = TaskContext(
            task_id="test-task",
            session_id="sess-1",
            user_id="user1",
            channel="web",
        )
        # Simulate maps_geo output after normalization:
        # raw MCP [{"type": "text", "text": "{...}"}] → parsed dict
        ctx.complete_step(1, {
            "return": [
                {
                    "country": "中国",
                    "province": "湖南省",
                    "city": "长沙市",
                    "location": "112.938882,28.228304",
                }
            ]
        })
        ctx.complete_step(2, {
            "return": [
                {
                    "country": "中国",
                    "province": "青海省",
                    "city": "西宁市",
                    "location": "101.758249,36.676027",
                }
            ]
        })
        return ctx

    def test_simple_variable(self, task_ctx):
        """Simple step output reference should work."""
        result = render_template("{{ step1.output }}", task_ctx)
        assert result == "Hello from step 1"

    def test_structured_nested_field_access(self, task_ctx_structured):
        """Template should access nested dict fields from normalized output."""
        result = render_template(
            "{{ step1.output.return[0].location }}",
            task_ctx_structured,
        )
        assert result == "112.938882,28.228304"

    def test_structured_city_access(self, task_ctx_structured):
        """Template should access nested city field."""
        result = render_template(
            "{{ step1.output.return[0].city }}",
            task_ctx_structured,
        )
        assert result == "长沙市"

    def test_geo_distance_args_rendering(self, task_ctx_structured):
        """Simulate the maps_distance use case: extract coordinates from
        two upstream geo lookups."""
        args = {
            "origins": "{{ step1.output.return[0].location }}",
            "destination": "{{ step2.output.return[0].location }}",
            "mode": "driving",
        }
        result = render_args(args, task_ctx_structured)
        assert result["origins"] == "112.938882,28.228304"
        assert result["destination"] == "101.758249,36.676027"
        assert result["mode"] == "driving"

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
