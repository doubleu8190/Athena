"""TaskContext — runtime state for a task execution.

Holds step outputs (for template rendering), completed steps list,
and task-level metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskContext:
    """Runtime context for an executing task.

    Used by Executor to track progress and by Jinja2 SandboxedEnvironment
    for template variable resolution ({{ stepN.output }}).
    """
    task_id: str
    session_id: str
    user_id: str
    channel: str

    # Step outputs: {"step1": output_value, "step2": ...}
    step_outputs: dict[str, Any] = field(default_factory=dict)

    # Completed step numbers
    completed_steps: list[int] = field(default_factory=list)

    # Additional context injected into templates
    extra: dict[str, Any] = field(default_factory=dict)

    def get_output(self, step: int) -> Any:
        """Get the output of a completed step. Returns empty string if missing."""
        key = f"step{step}"
        return self.step_outputs.get(key, "")

    def complete_step(self, step: int, output: Any) -> None:
        """Mark a step as completed and store its output."""
        self.step_outputs[f"step{step}"] = output
        if step not in self.completed_steps:
            self.completed_steps.append(step)

    @property
    def is_finished(self) -> bool:
        """Check if the plan has been fully executed."""
        # This is set externally by the Executor
        return False
