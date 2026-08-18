import { useEffect, useMemo, useRef, useState } from "react"
import {
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  ChevronRight,
  Loader2,
  X,
  XCircle,
} from "lucide-react"
import type { Message, Step, ToolCall } from "../types"
import {
  buildActivityGroups,
  type ActivityRequestGroup,
} from "../utils/activityModel"
import { StepCard } from "./StepCard"
import { ToolDetailPanel } from "./ToolDetailPanel"

interface ActivityPanelProps {
  messages: Message[]
  toolCalls: ToolCall[]
  steps: Step[]
  onClose: () => void
}

/**
 * Activity 面板的主轴是 Request -> Step。
 * 工具调用不再与 step 平级展示，而是挂在对应 tool_execution step 的详情里。
 */
export function ActivityPanel({
  messages,
  toolCalls,
  steps,
  onClose,
}: ActivityPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const [openKeys, setOpenKeys] = useState<Set<string>>(() => new Set())
  const [debug, setDebug] = useState(false)

  const groups = useMemo(
    () => buildActivityGroups(messages, steps, toolCalls),
    [messages, steps, toolCalls],
  )

  useEffect(() => {
    if (groups.length === 0) return
    setOpenKeys((prev) => {
      const next = new Set(prev)
      next.add(groups[groups.length - 1].key)
      return next
    })
  }, [groups.length])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [toolCalls.length, steps.length, messages.length])

  const toggle = (key: string) => {
    setOpenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const totalToolCalls = groups.reduce((total, group) => total + toolCount(group), 0)
  const totalSteps = groups.reduce((total, group) => total + group.steps.length, 0)

  return (
    <aside className="fixed inset-y-0 right-0 z-30 flex h-full w-[min(92vw,380px)] shrink-0 flex-col border-l border-athena-border bg-athena-surface shadow-2xl lg:relative lg:z-auto lg:w-[360px] lg:bg-athena-surface/40 lg:shadow-none">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-athena-border flex-shrink-0">
        <span className="text-sm font-semibold text-athena-text">Activity</span>
        <span className="text-xs text-athena-muted">
          {groups.length} request · {totalToolCalls} tool · {totalSteps} step
        </span>
        <button
          type="button"
          onClick={() => setDebug((value) => !value)}
          className={`ml-auto rounded-md border px-2 py-1 text-[11px] transition-colors ${
            debug
              ? "border-athena-accent/50 bg-athena-accent/10 text-athena-accent"
              : "border-athena-border text-athena-muted hover:text-athena-text"
          }`}
        >
          Debug
        </button>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex items-center justify-center w-7 h-7 rounded-md text-athena-muted hover:text-athena-text hover:bg-athena-bg transition-colors"
          title="Close panel"
          aria-label="Close activity panel"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3">
        {groups.length === 0 && (
          <p className="text-xs text-athena-muted">No activity yet.</p>
        )}

        {groups.map((group) => {
          const open = openKeys.has(group.key)
          const hasDetail = group.steps.length > 0 || group.orphanToolCalls.length > 0
          return (
            <div
              key={group.key}
              className="rounded-lg border border-athena-border bg-athena-bg/40 overflow-hidden"
            >
              <button
                type="button"
                onClick={() => toggle(group.key)}
                className="w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-athena-bg/60 transition-colors"
              >
                {open ? (
                  <ChevronDown className="w-4 h-4 text-athena-muted flex-shrink-0 mt-0.5" />
                ) : (
                  <ChevronRight className="w-4 h-4 text-athena-muted flex-shrink-0 mt-0.5" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="flex items-start gap-1.5 text-xs text-athena-text break-words line-clamp-2">
                    <StatusIcon status={group.status} />
                    {group.userMessage ? (
                      <span className="min-w-0 break-words">
                        {group.userMessage.content.trim() || "..."}
                      </span>
                    ) : (
                      <span className="text-athena-muted italic">Background work</span>
                    )}
                  </span>
                  <span className="block text-[11px] text-athena-muted mt-0.5">
                    {toolCount(group)} tool · {group.steps.length} step
                    {group.durationMs > 0 && <> · {formatDuration(group.durationMs)}</>}
                  </span>
                </span>
              </button>

              {open && hasDetail && (
                <div className="space-y-3 border-t border-athena-border px-3 pb-3 pt-3">
                  {group.steps.map((activityStep) => (
                    <div key={activityStep.step.id}>
                      <StepCard
                        step={activityStep.step}
                        toolCall={activityStep.toolCall}
                        label={activityStep.label}
                        debug={debug}
                        defaultOpen={activityStep.step.status === "failed"}
                      />
                      {activityStep.summary && (
                        <div className="ml-3 mt-1 text-[11px] text-athena-muted line-clamp-2">
                          {activityStep.summary}
                        </div>
                      )}
                    </div>
                  ))}

                  {group.orphanToolCalls.map((toolCall) => (
                    <div
                      key={toolCall.id}
                      className="rounded-lg border border-athena-border bg-athena-surface px-3 py-2"
                    >
                      <div className="mb-2 flex items-center gap-2 text-xs">
                        <AlertTriangle className="h-4 w-4 text-athena-warning" />
                        <span className="font-mono text-athena-text">{toolCall.tool_name}</span>
                        <span className="ml-auto text-athena-muted">Unlinked tool</span>
                      </div>
                      <ToolDetailPanel toolCall={toolCall} debug={debug} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </aside>
  )
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`
  return `${(ms / 1000).toFixed(1)}s`
}

function toolCount(group: ActivityRequestGroup): number {
  return group.steps.filter((item) => item.toolCall).length + group.orphanToolCalls.length
}

function StatusIcon({ status }: { status: ActivityRequestGroup["status"] }) {
  if (status === "running") {
    return <Loader2 className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 animate-spin text-athena-accent" />
  }
  if (status === "failed") {
    return <XCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-athena-danger" />
  }
  if (status === "completed") {
    return <CheckCircle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-athena-success" />
  }
  return <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-shrink-0 text-athena-muted" />
}
