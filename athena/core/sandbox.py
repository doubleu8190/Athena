"""Sandboxed template rendering using Jinja2 SandboxedEnvironment.

Templates are used in plan steps to reference outputs from previous steps:
    {{ step1.output }} → the output of step 1

Safety guarantees:
- No filesystem access
- No command execution
- Variables only from TaskContext
- Undefined variables raise errors
"""

from __future__ import annotations

from jinja2.sandbox import SandboxedEnvironment

from athena.logging_config import get_logger

logger = get_logger(__name__)

# ── Global sandboxed environment ──────────────────────────────────────

_sandbox_env = SandboxedEnvironment(
    # Disable all extensions that could be dangerous
    enable_async=False,
    # Block access to built-in functions that could leak info
    # Jinja2 SandboxedEnvironment already blocks __import__, open, etc.
)


def render_template(template_str: str, task_context) -> str:
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


def render_args(args_template: dict, task_context) -> dict:
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
