/** 浏览器命令分发器使用的客户端命令名称。 */
export const ClientEventType = {
  USER_COMMAND: "user_command",
  SESSION_STOP: "session_stop",
} as const

export type ClientEventType = (typeof ClientEventType)[keyof typeof ClientEventType]

/** 聊天 SSE 只允许传输用户可见事件，Trace 不进入该协议。 */
export const ApplicationEventType = {
  SESSION_SNAPSHOT: "session.snapshot",
  TURN_STARTED: "turn.started",
  PROGRESS_CREATED: "progress.created",
  TASK_RESULT: "task.result",
  ASSISTANT_STARTED: "assistant.started",
  ASSISTANT_DELTA: "assistant.delta",
  ASSISTANT_COMPLETED: "assistant.completed",
  TURN_FAILED: "turn.failed",
  TURN_CANCELLED: "turn.cancelled",
} as const

export type ApplicationEventType = (typeof ApplicationEventType)[keyof typeof ApplicationEventType]

export interface ExecutionError {
  code: string
  message: string
  error_type?: string
  retryable?: boolean
  phase?: string | null
  category?: string | null
  stack?: string | null
}

/** 历史与实时协议共用的可见内容。time 使用服务端时间。 */
export interface ProgressContent {
  time: string
  content: string
}

export interface ProgressSnapshotItem extends ProgressContent {
  progress_id: string
}

export interface TaskSnapshot {
  task_id: string
  execution_id: string
  worker_name: string
  task_title: string
  status: string
  process: ProgressSnapshotItem[]
  result: ProgressContent | null
}

export interface AssistantSnapshot extends ProgressContent {
  message_id: string
  completed: boolean
  last_chunk_id: number
}

export interface SessionTurnSnapshot {
  run_id: string
  run_status: string
  user: (ProgressContent & { message_id: string }) | null
  root_process: ProgressSnapshotItem[]
  tasks: TaskSnapshot[]
  assistant: AssistantSnapshot | null
}

export interface SessionSnapshotPayload {
  run_id: string | null
  run_status: string | null
  turns: SessionTurnSnapshot[]
}

/** SSE 信封携带恢复令牌；run_id 仅用于当前流内归组。 */
export interface ApplicationEventEnvelope {
  schema_version?: number
  event_type: ApplicationEventType | string
  durability?: string
  session_id?: string
  run_id?: string | null
  message_id?: string | null
  stream_id?: string
  chunk_id?: number | null
  is_complete?: boolean
  sync_token?: string
  payload?: Record<string, unknown>
  error_detail?: ExecutionError | null
  occurred_at?: string
}
