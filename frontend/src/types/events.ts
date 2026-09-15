/** 浏览器命令分发器和 SSE 投影使用的客户端事件名称。 */
export const ClientEventType = {
  USER_COMMAND: "user_command",
  APPROVAL_RESPONSE: "approval_response",
  SESSION_STOP: "session_stop",
  SESSION_RESUME: "session_resume",
} as const

export type ClientEventType = (typeof ClientEventType)[keyof typeof ClientEventType]

export const ApplicationEventType = {
  RUN_STARTED: "run.started",
  RUN_RESUMED: "run.resumed",
  RUN_PAUSED: "run.paused",
  RUN_FAILED: "run.failed",
  RUN_CANCELLED: "run.cancelled",
  RUN_COMPLETED: "run.completed",
  APPROVAL_REQUIRED: "approval.required",
  APPROVAL_RESOLVED: "approval.resolved",
  APPROVAL_EXPIRED: "approval.expired",
  MESSAGE_DELTA: "message.delta",
  MESSAGE_COMPLETED: "message.completed",
  MESSAGE_STARTED: "message.started",
  THINKING_STARTED: "thinking.started",
  THINKING_SUMMARY: "thinking.summary",
  THINKING_COMPLETED: "thinking.completed",
  STREAM_SNAPSHOT: "stream.snapshot",
  PLAN_CREATED: "plan.created",
  PLAN_COMPLETED: "plan.completed",
  PLAN_FAILED: "plan.failed",
  PLAN_CANCELLED: "plan.cancelled",
  TASK_QUEUED: "task.queued",
  TASK_STARTED: "task.started",
  TASK_RETRIED: "task.retrying",
  TASK_COMPLETED: "task.completed",
  TASK_FAILED: "task.failed",
  SYNTHESIS_STARTED: "synthesis.started",
  SYNTHESIS_COMPLETED: "synthesis.completed",
} as const

export type ApplicationEventType = (typeof ApplicationEventType)[keyof typeof ApplicationEventType]

export interface ApplicationEventEnvelope {
  schema_version?: number
  session_seq?: number
  event_type?: string
  durability?: string
  session_id?: string
  run_id?: string
  message_id?: string | null
  attachment_id?: string | null
  stream_id?: string
  stream_type?: string
  chunk_id?: number
  is_complete?: boolean
  parent_run_id?: string | null
  transition_id?: string | null
  payload?: Record<string, unknown>
  occurred_at?: string
}
