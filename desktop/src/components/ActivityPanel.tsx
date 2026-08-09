import { useEffect, useMemo, useRef, useState } from "react"
import { X, ChevronDown, ChevronRight, MessageSquare } from "lucide-react"
import { ToolCard } from "./ToolCard"
import { StepCard } from "./StepCard"
import type { Message, Step, ToolCall } from "../types"

interface ActivityPanelProps {
  messages: Message[]
  toolCalls: ToolCall[]
  steps: Step[]
  onClose: () => void
}

interface ActivityGroup {
  key: string
  userMessage: Message | null
  toolCalls: ToolCall[]
  steps: Step[]
}

function toTime(iso: string): number {
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? 0 : t
}

/** 子 run（"主run_序号"）→ 主 run；主 run 自身不变 */
function mainRunId(runId: string | null | undefined): string | null {
  if (!runId) return null
  const idx = runId.lastIndexOf("_")
  return idx === -1 ? runId : runId.slice(0, idx)
}

function userMsgForTime(userMsgs: Message[], time: number): Message | null {
  let best: Message | null = null
  for (const m of userMsgs) {
    if (toTime(m.timestamp) <= time) best = m
    else break
  }
  return best
}

function positionKey(userMsgs: Message[], timeIso: string): string {
  const u = userMsgForTime(userMsgs, toTime(timeIso))
  return u ? `pos:${u.id}` : "orphan"
}

/**
 * 活动面板（双视图的"工作台/activity"列）：
 * 按用户请求（run）分组展示工具调用与执行步骤 —— 每条用户消息成为一个小节，
 * 直接回答"这些步骤是哪个任务产生的"。借鉴 OpenClaw #74018 任务导向的 activity 视图。
 *
 * 分组键优先用 run_id（后端已把 run_id 写入 messages / steps，join 稳定）；
 * 缺失时（实时本地消息、旧数据）按时间回退到最近一次用户请求。
 */
export function ActivityPanel({ messages, toolCalls, steps, onClose }: ActivityPanelProps) {
  const scrollRef = useRef<HTMLDivElement>(null)
  // 记录展开明细的分组；默认只展开最新一组（正在进行的任务）
  const [openKeys, setOpenKeys] = useState<Set<string>>(() => new Set())

  const groups = useMemo<ActivityGroup[]>(() => {
    const userMsgs = messages
      .filter((m) => m.role === "user")
      .slice()
      .sort((a, b) => toTime(a.timestamp) - toTime(b.timestamp))
    const userByRun = new Map<string, Message>()
    for (const m of userMsgs) {
      if (m.run_id && !userByRun.has(m.run_id)) userByRun.set(m.run_id, m)
    }
    const stepIdToRun = new Map<string, string>()
    for (const s of steps) {
      if (s.run_id) stepIdToRun.set(s.id, s.run_id)
    }

    const groupMap = new Map<string, ActivityGroup>()
    const ensure = (key: string, userMessage: Message | null): ActivityGroup => {
      let g = groupMap.get(key)
      if (!g) {
        g = { key, userMessage, toolCalls: [], steps: [] }
        groupMap.set(key, g)
      }
      return g
    }

    const placeStep = (s: Step) => {
      const mr = mainRunId(s.run_id)
      const header = mr ? userByRun.get(mr) : undefined
      const g = header
        ? ensure(`run:${mr}`, header)
        : ensure(positionKey(userMsgs, s.started_at), userMsgForTime(userMsgs, toTime(s.started_at)))
      g.steps.push(s)
    }
    const placeTc = (tc: ToolCall) => {
      const rid = tc.run_id ?? stepIdToRun.get(tc.step_id ?? "")
      const mr = rid ? mainRunId(rid) : null
      const header = mr ? userByRun.get(mr) : undefined
      const g = header
        ? ensure(`run:${mr}`, header)
        : ensure(positionKey(userMsgs, tc.started_at), userMsgForTime(userMsgs, toTime(tc.started_at)))
      g.toolCalls.push(tc)
    }

    const sortedSteps = steps.slice().sort((a, b) => toTime(a.started_at) - toTime(b.started_at))
    for (const s of sortedSteps) placeStep(s)
    const sortedTc = toolCalls.slice().sort((a, b) => toTime(a.started_at) - toTime(b.started_at))
    for (const tc of sortedTc) placeTc(tc)

    return [...groupMap.values()]
  }, [messages, steps, toolCalls])

  // 出现新分组时自动展开最新一组（正在进行的任务）
  useEffect(() => {
    if (groups.length === 0) return
    setOpenKeys((prev) => {
      const next = new Set(prev)
      next.add(groups[groups.length - 1].key)
      return next
    })
  }, [groups.length])

  // 新工具调用/步骤到达时滚到底部，便于实时跟踪
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

  const totalToolCalls = groups.reduce((n, g) => n + g.toolCalls.length, 0)
  const totalSteps = groups.reduce((n, g) => n + g.steps.length, 0)
  const groupDuration = (g: ActivityGroup): number => {
    if (g.steps.length) return g.steps.reduce((n, s) => n + (s.duration_ms || 0), 0)
    return g.toolCalls.reduce((n, tc) => n + (tc.duration_ms || 0), 0)
  }

  return (
    <aside className="w-80 shrink-0 border-l border-athena-border bg-athena-surface/40 flex flex-col h-full">
      <div className="flex items-center gap-2 px-4 py-3 border-b border-athena-border flex-shrink-0">
        <span className="text-sm font-semibold text-athena-text">Activity</span>
        <span className="text-xs text-athena-muted">
          {groups.length} request · {totalToolCalls} tool · {totalSteps} step
        </span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto inline-flex items-center justify-center w-7 h-7 rounded-md text-athena-muted hover:text-athena-text hover:bg-athena-bg transition-colors"
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

        {groups.map((g) => {
          const open = openKeys.has(g.key)
          const hasDetail = g.toolCalls.length > 0 || g.steps.length > 0
          return (
            <div
              key={g.key}
              className="rounded-lg border border-athena-border bg-athena-bg/40 overflow-hidden"
            >
              {/* 分组头：用户请求 + 统计 */}
              <button
                type="button"
                onClick={() => toggle(g.key)}
                className="w-full flex items-start gap-2 px-3 py-2 text-left hover:bg-athena-bg/60 transition-colors"
              >
                {open ? (
                  <ChevronDown className="w-4 h-4 text-athena-muted flex-shrink-0 mt-0.5" />
                ) : (
                  <ChevronRight className="w-4 h-4 text-athena-muted flex-shrink-0 mt-0.5" />
                )}
                <span className="min-w-0 flex-1">
                  <span className="block text-xs text-athena-text break-words line-clamp-2">
                    {g.userMessage ? (
                      <>
                        <MessageSquare className="w-3 h-3 inline-block mr-1 text-athena-accent -mt-0.5" />
                        {g.userMessage.content.trim() || "…"}
                      </>
                    ) : (
                      <span className="text-athena-muted italic">Background work</span>
                    )}
                  </span>
                  <span className="block text-[11px] text-athena-muted mt-0.5">
                    {g.toolCalls.length} tool · {g.steps.length} step
                    {groupDuration(g) > 0 && <> · {formatDuration(groupDuration(g))}</>}
                  </span>
                </span>
              </button>

              {/* 分组明细（默认收起，最新一组展开） */}
              {open && hasDetail && (
                <div className="px-3 pb-3 space-y-2 border-t border-athena-border">
                  {g.toolCalls.map((tc) => (
                    <ToolCard key={tc.id} toolCall={tc} />
                  ))}
                  {g.steps.map((s) => (
                    <StepCard key={s.id} step={s} compact />
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
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(1)}s`
}
