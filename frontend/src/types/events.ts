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
  MESSAGE_DELTA: "message.delta",
  MESSAGE_COMPLETED: "message.completed",
  MESSAGE_STARTED: "message.started",
  THINKING_STARTED: "thinking.started",
  THINKING_SUMMARY: "thinking.summary",
  THINKING_COMPLETED: "thinking.completed",
  STREAM_SNAPSHOT: "stream.snapshot",
} as const

export type ApplicationEventType = (typeof ApplicationEventType)[keyof typeof ApplicationEventType]

export interface ApplicationEventEnvelope {
  schema_version?: number
  session_seq?: number
  event_id?: number
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
  payload?: Record<string, unknown>
  occurred_at?: string
}
