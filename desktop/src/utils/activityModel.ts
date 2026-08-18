import type { Message, Step, ToolCall } from "../types"
import { summarizeToolCall, toolDisplayData } from "./toolSummary"

export type ActivityStatus = "running" | "completed" | "failed" | "partial"

export interface ActivityStep {
  step: Step
  label: string
  summary: string | null
  toolCall: ToolCall | null
}

export interface ActivityRequestGroup {
  key: string
  userMessage: Message | null
  startedAt: string
  status: ActivityStatus
  durationMs: number
  steps: ActivityStep[]
  orphanToolCalls: ToolCall[]
}

export function buildActivityGroups(
  messages: Message[],
  steps: Step[],
  toolCalls: ToolCall[],
): ActivityRequestGroup[] {
  const userMsgs = messages
    .filter((m) => m.role === "user")
    .slice()
    .sort((a, b) => toTime(a.timestamp) - toTime(b.timestamp))

  const userByRun = new Map<string, Message>()
  for (const message of userMsgs) {
    const runId = mainRunId(message.run_id)
    if (runId && !userByRun.has(runId)) userByRun.set(runId, message)
  }

  const groupMap = new Map<string, MutableGroup>()
  const ensure = (key: string, userMessage: Message | null, startedAt: string): MutableGroup => {
    let group = groupMap.get(key)
    if (!group) {
      group = {
        key,
        userMessage,
        startedAt,
        messages: [],
        steps: [],
        orphanToolCalls: [],
      }
      groupMap.set(key, group)
    } else if (!group.userMessage && userMessage) {
      group.userMessage = userMessage
    }
    return group
  }

  const groupForRunOrTime = (runId: string | null | undefined, timeIso: string): MutableGroup => {
    const normalizedRun = mainRunId(runId)
    const header = normalizedRun ? userByRun.get(normalizedRun) ?? null : null
    if (normalizedRun && header) return ensure(`run:${normalizedRun}`, header, header.timestamp)
    const fallbackUser = userMsgForTime(userMsgs, toTime(timeIso))
    return ensure(fallbackUser ? `pos:${fallbackUser.id}` : "orphan", fallbackUser, fallbackUser?.timestamp ?? timeIso)
  }

  const sortedSteps = steps
    .slice()
    .sort((a, b) => a.step_number - b.step_number || toTime(a.started_at) - toTime(b.started_at))
  for (const step of sortedSteps) {
    groupForRunOrTime(step.run_id, step.started_at).steps.push(step)
  }

  const stepById = new Map(steps.map((step) => [step.id, step]))
  const toolCallsByStep = new Map<string, ToolCall>()
  for (const toolCall of toolCalls) {
    if (toolCall.step_id) toolCallsByStep.set(toolCall.step_id, toolCall)
  }

  const attachedToolIds = new Set<string>()
  for (const [stepId, toolCall] of toolCallsByStep) {
    if (stepById.has(stepId)) attachedToolIds.add(toolCall.id)
  }

  for (const toolCall of toolCalls) {
    if (attachedToolIds.has(toolCall.id)) continue
    const runId = toolCall.run_id ?? (toolCall.step_id ? stepById.get(toolCall.step_id)?.run_id : undefined)
    groupForRunOrTime(runId, toolCall.started_at).orphanToolCalls.push(toolCall)
  }

  for (const message of messages) {
    groupForRunOrTime(message.run_id, message.timestamp).messages.push(message)
  }

  for (const message of userMsgs) {
    const runId = mainRunId(message.run_id)
    ensure(runId ? `run:${runId}` : `pos:${message.id}`, message, message.timestamp)
  }

  const ordered: MutableGroup[] = []
  for (const message of userMsgs) {
    const runId = mainRunId(message.run_id)
    const key = runId ? `run:${runId}` : `pos:${message.id}`
    const group = groupMap.get(key)
    if (group) {
      ordered.push(group)
      groupMap.delete(key)
    }
  }
  ordered.push(...Array.from(groupMap.values()).sort((a, b) => toTime(a.startedAt) - toTime(b.startedAt)))

  return ordered.map((group) => {
    const activitySteps = group.steps.map((step) => {
      const toolCall = toolCallsByStep.get(step.id) ?? null
      return {
        step,
        toolCall,
        label: stepLabel(step, toolCall),
        summary: stepSummary(step, toolCall),
      }
    })
    return {
      key: group.key,
      userMessage: group.userMessage,
      startedAt: group.startedAt,
      steps: activitySteps,
      orphanToolCalls: group.orphanToolCalls,
      durationMs: groupDuration(activitySteps, group.orphanToolCalls),
      status: groupStatus(activitySteps, group.orphanToolCalls, group.messages),
    }
  })
}

export function mainRunId(runId: string | null | undefined): string | null {
  if (!runId) return null
  if (/^\d{20,}(?:_\d+)+$/.test(runId)) return runId.replace(/(?:_\d+)+$/, "")
  return runId
}

export function stepLabel(step: Step, toolCall: ToolCall | null): string {
  if (step.step_type === "tool_execution") return toolCall?.tool_name ?? "Tool execution"
  if (step.status === "running") return "Thinking"
  if (toolCall) return toolCall.tool_name
  return "Composing"
}

export function stepSummary(step: Step, toolCall: ToolCall | null): string | null {
  if (step.error_message) return step.error_message
  if (toolCall) return summarizeToolCall(toolDisplayData(toolCall)).summary
  if (step.step_type === "llm_call") {
    if (step.llm_input_tokens > 0 || step.llm_output_tokens > 0) {
      return `${formatTokens(step.llm_input_tokens)} in / ${formatTokens(step.llm_output_tokens)} out`
    }
    return step.status === "running" ? "Model is working" : null
  }
  return null
}

export function toTime(iso: string | null | undefined): number {
  if (!iso) return 0
  const value = new Date(iso).getTime()
  return Number.isNaN(value) ? 0 : value
}

interface MutableGroup {
  key: string
  userMessage: Message | null
  startedAt: string
  messages: Message[]
  steps: Step[]
  orphanToolCalls: ToolCall[]
}

function userMsgForTime(userMsgs: Message[], time: number): Message | null {
  let best: Message | null = null
  for (const message of userMsgs) {
    if (toTime(message.timestamp) <= time) best = message
    else break
  }
  return best
}

function groupDuration(steps: ActivityStep[], orphanToolCalls: ToolCall[]): number {
  const stepDuration = steps.reduce((total, item) => total + (item.step.duration_ms || 0), 0)
  const orphanDuration = orphanToolCalls.reduce((total, item) => total + (item.duration_ms || 0), 0)
  return stepDuration + orphanDuration
}

function groupStatus(
  steps: ActivityStep[],
  orphanToolCalls: ToolCall[],
  messages: Message[],
): ActivityStatus {
  const allStatuses = [
    ...steps.map((item) => item.step.status),
    ...orphanToolCalls.map((item) => item.status),
  ]
  if (allStatuses.some((status) => status === "running" || status === "pending")) return "running"
  if (hasFinalAssistantResponse(messages)) return "completed"
  if (allStatuses.some((status) => status === "failed" || status === "denied" || status === "timeout")) return "failed"
  if (allStatuses.length === 0) return "partial"
  return "completed"
}

function hasFinalAssistantResponse(messages: Message[]): boolean {
  return messages.some((message) => (
    message.role === "assistant" &&
    message.content.trim().length > 0 &&
    (message.tool_calls?.length ?? 0) === 0
  ))
}

function formatTokens(tokens: number): string {
  if (tokens < 1000) return String(tokens)
  return `${(tokens / 1000).toFixed(1)}k`
}
