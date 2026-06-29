"""Planner — LLM-powered task plan generator.

Receives a UnifiedMessage and full context, calls the LLM with the
available tool list, and generates a structured TaskPlan with subtasks.

Key features:
- Injects tool list with descriptions, parameter schemas, and capability_tags
- Prompts for multi-path fallback hints
- Returns structured JSON plan with subtask dependencies
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from athena.core.llm_provider.manager import LLMProviderManager
from athena.core.message import UnifiedMessage
from athena.core.context import SessionContext
from athena.logging_config import bind_context, get_logger

logger = get_logger(__name__)

# ── System prompt template ────────────────────────────────────────────

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

## Safety Boundaries
- All file operations MUST be confined to /workspace/
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
"""


@dataclass
class SubtaskDef:
    """Definition of a single subtask within a plan."""
    step: int
    intent: str
    tool_name: str
    args: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)
    critical: bool = True
    on_failure: Literal["abort", "skip", "fallback"] = "abort"
    fallback_tool: dict[str, Any] | None = None


@dataclass
class TaskPlan:
    """A complete task execution plan."""
    task_id: str
    subtasks: list[SubtaskDef]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskPlan:
        subtasks = []
        for s in data.get("subtasks", []):
            subtasks.append(SubtaskDef(
                step=s["step"],
                intent=s.get("intent", ""),
                tool_name=s["tool_name"],
                args=s.get("args", {}),
                depends_on=s.get("depends_on", []),
                critical=s.get("critical", True),
                on_failure=s.get("on_failure", "abort"),
                fallback_tool=s.get("fallback_tool"),
            ))
        return cls(
            task_id=data.get("task_id", str(uuid.uuid4())),
            subtasks=subtasks,
        )


class Planner:
    """Generates task execution plans using an LLM.

    Injects available tools with schemas, safety rules, and context.
    Returns a structured TaskPlan for the Executor.
    """

    def __init__(self, llm_manager: LLMProviderManager):
        self.llm = llm_manager
        from athena.mcp_client.registry import get_tool_registry
        self.tool_registry = get_tool_registry()

    async def generate_plan(
        self,
        message: UnifiedMessage,
        session_ctx: SessionContext,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> TaskPlan:
        """Generate a task plan from a user message.

        Args:
            message: The unified user message.
            session_ctx: Current session context with history and memories.
            context_messages: Optional pre-built messages list from
                ContextManager.inject_context(). When provided, used
                directly (planner system prompt is always prepended).
                When None, falls back to building minimal context from
                session_ctx alone.

        Returns:
            A TaskPlan with ordered subtasks and dependencies.
        """
        logger = bind_context(
            session_id=session_ctx.session_id,
            channel=message.channel,
        )

        # Build the tools description for the system prompt
        tools = self.tool_registry.export_for_planner() if self.tool_registry else []
        tools_desc = self._format_tools(tools)

        system_prompt = PLANNER_SYSTEM_PROMPT.format(tools_description=tools_desc)

        # Build messages for LLM
        # Planner's own system prompt always comes first
        messages = [{"role": "system", "content": system_prompt}]

        if context_messages is not None:
            # Use pre-built rich context from ContextManager.inject_context().
            # Already contains: system(summary+memories) + message_history
            # + current user message. No need to re-add conversation
            # summary or user message.
            messages.extend(context_messages)
        else:
            # Fallback: build minimal context from session_ctx alone.
            if session_ctx.conversation_summary:
                messages.append({
                    "role": "system",
                    "content": f"[Previous context]\n{session_ctx.conversation_summary}",
                })
            messages.append({"role": "user", "content": message.content})

        logger.info("planner messages_built", planner_messages = messages)
        # Call LLM (no tools for planning — the plan IS the output)
        try:
            response = await self.llm.generate(
                messages=messages,
                tools=None,  # Planner doesn't use tool calling
                max_tokens=4096,
                temperature=0.3,  # Lower temperature for structured planning
            )
            logger.info("planner llm_response", llm_response=response)
            # Parse the JSON plan from the response
            plan_json = self._parse_plan(response.text)

            logger.info(
                "plan_generated",
                task_id=plan_json.get("task_id"),
                subtask_count=len(plan_json.get("subtasks", [])),
                token_usage=response.token_usage,
            )

            return TaskPlan.from_dict(plan_json)

        except Exception as e:
            logger.error("plan_generation_failed", error=str(e))
            raise

    def _format_tools(self, tools: list[dict[str, Any]]) -> str:
        """Format the tool list for inclusion in the system prompt."""
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
- **Supports Preview**: {t.get('supports_preview', False)}
- **Capability Tags**: {tags or 'none'}
- **Parameters Schema**:
```json
{schema}
```
""")
        return "\n".join(lines)

    def _parse_plan(self, text: str) -> dict[str, Any]:
        """Extract and parse the JSON plan from LLM response text."""
        # Strip markdown code blocks if present
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
            import re
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                return json.loads(match.group(0))
            raise ValueError(f"Failed to parse plan JSON from LLM response: {text[:500]}")
