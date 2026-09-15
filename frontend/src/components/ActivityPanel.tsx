import { useEffect, useMemo, useRef } from "react"
import { CheckCircle, Clock, Loader2, MessageSquare, X, XCircle } from "lucide-react"
import type { AgentStatus, Message } from "../types"
import { buildActivityGroups, type ActivityRequestGroup } from "../utils/activityModel"

interface ActivityPanelProps {
  messages: Message[]
  agentStatus?: AgentStatus
  onClose: () => void
}

/** Activity 面板只做请求索引，执行细节由聊天区的 EXECUTION PROGRESS 展示。 */
export function ActivityPanel({ messages, agentStatus = "idle", onClose }: ActivityPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  const groups = useMemo(() => buildActivityGroups(messages, [], []), [messages])
  const latestGroupKey = groups[groups.length - 1]?.key
  const isAgentActive = agentStatus === "thinking" || agentStatus === "running" || agentStatus === "waiting_approval"

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages.length])

  return (
    <aside className="fixed inset-y-0 right-0 z-30 flex h-full w-[min(92vw,380px)] shrink-0 flex-col border-l border-athena-border bg-athena-surface shadow-2xl lg:relative lg:z-auto lg:w-[360px] lg:bg-athena-surface/40 lg:shadow-none">
      <div className="flex items-center gap-2 border-b border-athena-border px-4 py-3">
        <MessageSquare className="h-4 w-4 text-athena-accent" />
        <span className="text-sm font-semibold text-athena-text">Requests</span>
        <span className="text-xs text-athena-muted">{groups.length} request{groups.length === 1 ? "" : "s"}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md text-athena-muted transition-colors hover:bg-athena-bg hover:text-athena-text"
          title="Close requests"
          aria-label="Close requests"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-2 overflow-y-auto p-3">
        {groups.length === 0 ? (
          <p className="text-xs text-athena-muted">No requests yet.</p>
        ) : (
          groups.map((group) => {
            const active = group.key === latestGroupKey && isAgentActive
            return <RequestItem key={group.key} group={group} active={active} />
          })
        )}
      </div>
    </aside>
  )
}

function RequestItem({ group, active }: { group: ActivityRequestGroup; active: boolean }) {
  const failed = group.status === "failed"
  const completed = !active && group.status === "completed"

  return (
    <article className="rounded-lg border border-athena-border bg-athena-bg/40 px-3 py-3">
      <div className="flex items-start gap-2">
        <RequestStatusIcon active={active} failed={failed} completed={completed} />
        <div className="min-w-0 flex-1">
          <div className="text-xs font-semibold uppercase tracking-wide text-athena-muted">User request</div>
          <div className="mt-1 break-words text-sm leading-5 text-athena-text">
            {group.userMessage?.content.trim() || "Background request"}
          </div>
          <div className="mt-2 flex items-center gap-1 text-[11px] text-athena-muted">
            <Clock className="h-3 w-3" />
            {formatTime(group.startedAt)}
            <span className="px-0.5">·</span>
            <span className={failed ? "text-athena-danger" : active ? "text-athena-accent" : "text-athena-success"}>
              {active ? "Running" : failed ? "Failed" : completed ? "Completed" : "Waiting"}
            </span>
          </div>
        </div>
      </div>

    </article>
  )
}

function RequestStatusIcon({ active, failed, completed }: { active: boolean; failed: boolean; completed: boolean }) {
  if (active) return <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-athena-accent" />
  if (failed) return <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-athena-danger" />
  if (completed) return <CheckCircle className="mt-0.5 h-4 w-4 shrink-0 text-athena-success" />
  return <Clock className="mt-0.5 h-4 w-4 shrink-0 text-athena-muted" />
}

function formatTime(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}
