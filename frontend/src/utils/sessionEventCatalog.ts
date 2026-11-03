export const SESSION_EVENT_NAMES = [
  "run.queued", "run.started", "run.resumed", "run.stopping", "run.failed", "run.cancelled", "run.completed", "run.budget_exceeded", "run.root_suspended",
  "node.started", "node.completed", "node.failed", "retrieval.started", "retrieval.completed", "retrieval.failed",
  "retrieval.provider.started", "retrieval.provider.completed", "retrieval.provider.failed", "retrieval.selection.completed",
  "llm.started", "llm.completed", "llm.validation_failed", "tool.started", "tool.completed", "approval.required", "approval.resolved",
  "message.started", "message.persisted", "message.completed", "message.delta", "stream.snapshot",
  "thinking.started", "thinking.summary", "thinking.completed", "plan.accepted", "plan.created", "plan.cancelling",
  "plan.revised", "plan.completed", "plan.failed", "plan.cancelled", "round.started", "round.completed",
  "task.queued", "task.started", "task.retrying", "task.cancelling", "task.waiting_approval", "task.completed",
  "task.failed", "task.cancelled", "synthesis.started", "synthesis.completed", "agent.waiting_for_files",
  "attachment_updated", "file_processing_started", "file_processing_completed", "file_processing_failed",
] as const
