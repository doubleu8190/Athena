"""Sandboxed template rendering using Jinja2 SandboxedEnvironment.

Templates are used in plan steps to reference outputs from previous steps:
    {{ step1.output }} → the output of step 1
    {{ step1.output.return[0].location }} → nested field access

Safety guarantees:
- No filesystem access
- No command execution
- Variables only from TaskContext
- Undefined variables raise errors

Output normalization:
- MCP content blocks ([{"type": "text", "text": "..."}]) are unwrapped
- JSON strings are parsed into structured dicts/lists
- Enables precise nested field access in downstream templates
"""

from __future__ import annotations

import json
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

from athena.core.task_context import TaskContext
from athena.logging_config import get_logger

logger = get_logger(__name__)


# ── Output normalization ────────────────────────────────────────────────


def normalize_output(content: Any) -> Any:  # noqa: ANN401
    """Normalize MCP tool output for downstream template consumption.

    Transforms raw MCP content blocks into structured data so that
    downstream subtask templates can use precise field access like
    ``{{ step1.output.return[0].location }}``.

    Transformations applied in order:
    1. MCP content-block lists are unwrapped — extract ``text`` fields
       from ``[{"type": "text", "text": "..."}, ...]``.
    2. Combined text that looks like JSON is parsed into dicts/lists.
    3. Plain text and unparseable content are returned as-is.

    Args:
        content: Raw tool result content (any MCP shape).

    Returns:
        Normalized value: parsed dict/list, plain string, or original
        content if no transformation applies.
    """
    # 1. Unwrap MCP content blocks:
    #    [{"type": "text", "text": "..."}, {"type": "text", "text": "..."}]
    if isinstance(content, list) and content:
        texts: list[str] = []
        all_text_blocks = True
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_val = item.get("text")
                if text_val is not None:
                    texts.append(str(text_val))
            else:
                all_text_blocks = False
                break

        if all_text_blocks and texts:
            combined = "\n".join(texts)
            # Try to parse as JSON
            try:
                return json.loads(combined)
            except (json.JSONDecodeError, ValueError):
                return combined

        # Not all items were text blocks — return the list as-is
        return content

    # 2. Plain string that looks like JSON
    if isinstance(content, str):
        stripped = content.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                pass
        return content

    # 3. Anything else (numbers, bools, nested dicts, None, …) — passthrough
    return content

# ── Global sandboxed environment ──────────────────────────────────────

_sandbox_env = SandboxedEnvironment(
    # Disable all extensions that could be dangerous
    enable_async=False,
    # Block access to built-in functions that could leak info
    # Jinja2 SandboxedEnvironment already blocks __import__, open, etc.
)


def render_template(template_str: str, task_context: TaskContext) -> str:
    """Render a Jinja2 template string using only TaskContext variables.

    Args:
        template_str: Template string with {{ stepN.output }} references.
        task_context: TaskContext instance providing step outputs.

    Returns:
        Rendered string with all variables resolved.

    Raises:
        jinja2.UndefinedError: If a template references an undefined variable.
    """
    # Build the template context from TaskContext
    context = {}

    # Expose step outputs as stepN.output
    for key, value in task_context.step_outputs.items():
        context[key] = {"output": value}

    # Expose extra context
    context.update(task_context.extra)

    try:
        template = _sandbox_env.from_string(template_str)
        result = template.render(**context)
        return result
    except Exception as e:
        logger.error(
            "template_render_failed",
            template=template_str[:200],
            error=str(e),
        )
        raise


def render_args(args_template: dict[str, Any], task_context: TaskContext) -> dict[str, Any]:
    """Render each value in an args dict through the template engine.

    Supports both string values containing {{ ... }} and nested structures.
    Non-string values are passed through unchanged.

    Args:
        args_template: Dict of argument templates (values may contain `{{ }}`).
        task_context: TaskContext instance.

    Returns:
        Dict with all template values rendered to their final form.
    """
    result = {}
    for key, value in args_template.items():
        if isinstance(value, str) and "{{" in value:
            result[key] = render_template(value, task_context)
        elif isinstance(value, dict):
            result[key] = render_args(value, task_context)
        elif isinstance(value, list):
            result[key] = [
                render_template(v, task_context) if isinstance(v, str) and "{{" in v else v
                for v in value
            ]
        else:
            result[key] = value
    return result
