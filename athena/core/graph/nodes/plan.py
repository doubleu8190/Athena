"""Plan node — calls the LLM to generate a TaskPlan.

Reuses the proven planner logic from ``athena.core.planner``:
- System prompt injection with tool descriptions
- LLM call (temperature 0.3, no tool use)
- JSON parsing from LLM output

On re-plan (when ``completed_steps`` is non-empty), injects a re-planning
context prompt so the LLM can adjust the remaining steps.
"""

from __future__ import annotations

import uuid
from typing import Any

from langgraph.types import RunnableConfig

from athena.core.graph.state import ExecutionState
from athena.core.graph.utils import (
    format_tools_for_prompt,
    parse_plan_json,
    build_replan_context,
)
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

# ── System prompt (extracted from planner.py) ────────────────────────────

PLANNER_SYSTEM_PROMPT = """You are Athena's task planner. Your job is to decompose user requests into a sequence of executable subtasks.

## Available Tools
{tools_description}

## Planning Rules
1. Each subtask maps to exactly ONE available tool.
2. Specify dependencies using step numbers (1-indexed). Independent steps can run in parallel.
3. For tools that depend on external services, provide a `fallback_tool` if an alternative with the same capability_tag exists.
4. Mark steps as `critical: false` if they are non-essential (e.g., cosmetic, informational).
5. Set `on_failure`:
   - "abort" for critical steps (default)
   - "skip" for non-critical steps that can be safely skipped
   - "fallback" when you specify a fallback_tool
6. Use `{{stepN.output}}` in arguments to reference previous step outputs.
   Tool outputs are structured JSON objects (parsed automatically from MCP responses).
   **CRITICAL — extract exact fields**: Never pass the entire step output as a
   downstream parameter unless the downstream tool truly expects the full object.
   Instead, navigate into the output to extract the exact value needed:
   - `{{step1.output.return[0].location}}` to get a coordinate string
   - `{{step1.output.summary}}` to get a text field
   - `{{step1.output.items[0].id}}` for nested array access
   Using bare `{{stepN.output}}` when the downstream tool expects a simple string
   (like coordinates or a filename) is the #1 cause of INVALID_PARAMS errors.

## Safety Boundaries
- Do NOT generate commands that modify system files
- Do NOT generate commands that access sensitive paths (/etc, /proc, /sys)

## Output Format
Return ONLY a valid JSON object:
{{
  "task_id": "must be uuid, for example: 123e4567-e89b-12d3-a456-426614174000",
  "subtasks": [
    {{
      "step": 1,
      "intent": "Brief description of what this step does",
      "tool_name": "exact_tool_name",
      "args": {{"param": "value"}},
      "depends_on": [],
      "critical": true,
      "on_failure": "abort",
      "fallback_tool": null
    }}
  ]
}}

## Example: Chaining geo → distance
User asks "distance from A to B". The correct plan extracts specific fields:

Step 1: maps_geo  args: {{"address": "City A"}}
Step 2: maps_geo  args: {{"address": "City B"}}
Step 3: maps_distance  args: {{
  "origins": "{{{{step1.output.return[0].location}}}}",
  "destination": "{{{{step2.output.return[0].location}}}}"
}}

DO NOT write `"origins": "{{{{step1.output}}}}"` — that passes the entire
JSON response object when the tool expects just a coordinate string.
"""


async def plan_node(
    state: ExecutionState,
    config: RunnableConfig,
) -> dict[str, Any]:
    """Generate (or re-generate) a TaskPlan via LLM.

    On first call: uses the conversation messages from ``state["messages"]``.
    On re-plan: injects completed step outputs + failed step info so the
    LLM can adjust the remaining plan.
    """
    llm_manager = config["configurable"]["llm_manager"]
    tool_registry = config["configurable"]["tool_registry"]

    log = bind_context(session_id=state.get("session_id", ""))

    # Build tool description for the system prompt
    tools = tool_registry.export_for_planner() if tool_registry else []
    tools_desc = format_tools_for_prompt(tools)
    system_prompt = PLANNER_SYSTEM_PROMPT.format(tools_description=tools_desc)

    # Build messages for LLM
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt}
    ]

    # Add dynamic system context (conversation summary + RAG memories) if present
    system_context = state.get("system_context")
    if system_context:
        messages.append({"role": "system", "content": system_context})

    # Add conversation messages from state (history + current user msg).
    # The checkpointer restores prior messages via add_messages reducer;
    # the caller only seeds the NEW HumanMessage.
    for msg in state.get("messages", []):
        # Convert LangChain message objects to dicts
        if hasattr(msg, "type") and hasattr(msg, "content"):
            role = msg.type  # "human" | "ai" | "system"
            # Normalize role to what the LLM expects
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"
            messages.append({"role": role, "content": msg.content})
        elif isinstance(msg, dict):
            messages.append(msg)

    # If re-planning, inject re-plan context
    if state.get("completed_steps"):
        replan_context = build_replan_context(state)
        messages.append({"role": "user", "content": replan_context})
        log.info("planning_replan_mode")

    # Call LLM (no tools — the plan IS the output)
    try:
        response = await llm_manager.generate(
            messages=messages,
            tools=None,
            max_tokens=4096,
            temperature=0.3,
        )

        plan_json = parse_plan_json(response.text)

        # Ensure task_id exists
        if not plan_json.get("task_id"):
            plan_json["task_id"] = str(uuid.uuid4())

        log.info(
            "plan_generated",
            task_id=plan_json.get("task_id"),
            subtask_count=len(plan_json.get("subtasks", [])),
            is_replan=bool(state.get("completed_steps")),
        )

        return {
            "task_plan": plan_json,
            "plan_error": None,
            "status": "executing",
        }

    except Exception as e:
        log.error("plan_generation_failed", error=str(e))
        return {
            "plan_error": str(e),
            "status": "failed",
        }
