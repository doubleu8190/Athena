"""Utility functions shared across graph nodes.

Extracted from planner.py and executor.py to avoid circular imports
while keeping the graph nodes thin.
"""

from __future__ import annotations

import json
import re
from typing import Any


def format_tools_for_prompt(tools: list[dict[str, Any]]) -> str:
    """Format the tool list for inclusion in the planner system prompt."""
    if not tools:
        return "No tools available."

    lines = []
    for t in tools:
        schema = json.dumps(t.get("parameters_schema", {}), indent=2)
        tags = ", ".join(t.get("capability_tags", []))
        lines.append(f"""
### {t['name']}
- **Server**: {t.get('source_server_id', 'unknown')}
- **Description**: {t.get('description', 'No description')}
- **Risk Level**: {t.get('risk_level', 'medium')}
- **Capability Tags**: {tags or 'none'}
- **Parameters Schema**:
```json
{schema}
```
""")
    return "\n".join(lines)


def parse_plan_json(text: str) -> dict[str, Any]:
    """Extract and parse the JSON plan from LLM response text.

    Handles markdown code-fences and embedded JSON.
    """
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (fences)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON block within text
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            return json.loads(match.group(0))
        raise ValueError(
            f"Failed to parse plan JSON from LLM response: {text[:500]}"
        )


def build_replan_context(state: dict[str, Any]) -> str:
    """Build a context string for dynamic re-planning.

    Injects completed step outputs and failed step info so the LLM can
    generate a sensible adjusted plan.
    """
    lines = ["## Replanning Context", ""]

    completed = state.get("completed_steps", [])
    if completed:
        lines.append("### Completed Steps:")
        for step in sorted(completed):
            output_preview = str(
                state.get("step_outputs", {}).get(f"step{step}", "")
            )[:200]
            lines.append(f"- Step {step}: {output_preview}")
        lines.append("")

    failed = state.get("failed_steps", [])
    if failed:
        lines.append("### Failed Steps:")
        for step in sorted(failed):
            lines.append(
                f"- Step {step}: execution failed, "
                f"please find alternative approach"
            )
        lines.append("")

    lines.append(
        "Please generate a NEW plan for the remaining work, "
        "considering what has already been completed."
    )
    return "\n".join(lines)
