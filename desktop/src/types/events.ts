export const EventType = {
  // 会话生命周期
  SESSION_START: "session_start",
  SESSION_COMPLETE: "session_complete",
  SESSION_INTERRUPTED: "session_interrupted",
  SESSION_RECOVERED: "session_recovered",
  STREAM_START: "stream_start",
  STREAM_END: "stream_end",

  // LLM 调用
  LLM_CALL_START: "llm_call_start",
  LLM_TOKEN: "llm_token",
  LLM_CALL_END: "llm_call_end",

  // 工具执行
  TOOL_CALL_START: "tool_call_start",
  TOOL_CALL_END: "tool_call_end",
  TOOL_CALL_SKIPPED: "tool_call_skipped",

  // 审批生命周期
  APPROVAL_QUEUE_STATUS: "approval_queue_status",
  APPROVAL_REQUEST: "approval_request",
  APPROVAL_PROCESSING: "approval_processing",
  APPROVAL_RESULT: "approval_result",
  APPROVAL_TIMEOUT: "approval_timeout",

  // 子 Agent 生命周期
  SUB_AGENT_SPAWNED: "sub_agent_spawned",
  SUB_AGENT_START: "sub_agent_start",
  SUB_AGENT_PROGRESS: "sub_agent_progress",
  SUB_AGENT_COMPLETE: "sub_agent_complete",
  SUB_AGENT_FAILED: "sub_agent_failed",

  // 记忆系统
  MEMORY_EXTRACTED: "memory_extracted",
  MEMORY_SUMMARY_START: "memory_summary_start",
  MEMORY_SUMMARY_COMPLETE: "memory_summary_complete",
  MEMORY_SEARCH_START: "memory_search_start",
  MEMORY_SEARCH_COMPLETE: "memory_search_complete",
  MEMORY_SAVED: "memory_saved",

  // 上下文压缩
  CONTEXT_COMPRESS_START: "context_compress_start",
  CONTEXT_COMPRESS_COMPLETE: "context_compress_complete",
  CONTEXT_SUMMARY_SAVED: "context_summary_saved",

  // 恢复与错误
  RECOVERY_START: "recovery_start",
  RECOVERY_STEP: "recovery_step",
  RECOVERY_COMPLETE: "recovery_complete",
  ERROR: "error",
  ERROR_RECOVERED: "error_recovered",

  // 系统
  BUDGET_EXCEEDED: "budget_exceeded",
  SAFETY_WARNING: "safety_warning",
  PONG: "pong",
  SYSTEM_NOTICE: "system_notice",
  SYSTEM_MESSAGE: "system_message",
  FILE_TASK_CREATED: "file_task_created",
  FILE_TASK_PROGRESS: "file_task_progress",
  FILE_TASK_COMPLETED: "file_task_completed",
  FILE_TASK_FAILED: "file_task_failed",
  ATTACHMENT_UPDATED: "attachment_updated",
  AGENT_WAITING_FILE: "agent_waiting_file",
} as const

export type EventType = (typeof EventType)[keyof typeof EventType]

export const ClientEventType = {
  USER_COMMAND: "user_command",
  APPROVAL_RESPONSE: "approval_response",
  APPROVAL_CANCEL: "approval_cancel",
  SESSION_STOP: "session_stop",
  SESSION_RESUME: "session_resume",
  MEMORY_SAVE: "memory_save",
  SUBSCRIBE: "subscribe",
  PING: "ping",
} as const

export type ClientEventType = (typeof ClientEventType)[keyof typeof ClientEventType]
