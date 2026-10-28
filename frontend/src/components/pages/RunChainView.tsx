import { useEffect, useMemo, useRef, useState } from "react"
import {
  Activity,
  Bot,
  Check,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  Clock3,
  Code2,
  Database,
  GitBranch,
  RefreshCw,
  Network,
  Search,
  ShieldCheck,
  Sparkles,
  Wrench,
  X,
} from "lucide-react"
import ViewShell from "./ViewShell"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import { apiClient } from "../../api/client"
import { useChatStore } from "../../store/chatStore"
import type {
  OrchestrationApprovalSnapshot,
  OrchestrationExecutionSnapshot,
  PlanVersionDetail,
  RunOrchestrationSnapshot,
  RunTimelineEvent,
  SessionRunSummary,
} from "../../types"
import { formatDuration, getActivityDuration, getRunTiming } from "./runTiming"

type EventStatus = "completed" | "failed" | "retry" | "approved" | "waiting" | "review" | "running" | "cancelled" | "info"
type EventKind = "run" | "llm" | "retrieval" | "tool" | "plan" | "worker" | "response" | "system"

interface RunEvent {
  id: string
  parentId?: string
  kind: EventKind
  name: string
  summary: string
  status: EventStatus
  duration: string
  time: string
  detail: string
  input?: string
  output?: string
  error?: string
  startedAt?: string
}

const demoEvents: RunEvent[] = [
  { id: "root", kind: "run", name: "用户请求", summary: "分析销售数据并生成季度摘要", status: "completed", duration: "12.4 s", time: "14:32:08.102", detail: "运行入口接收用户请求，创建 root run 并初始化执行上下文。", input: "分析 Q3 销售数据，找出主要变化并生成一份简短摘要。", output: "任务已完成，生成季度销售摘要。" },
  { id: "prepare", parentId: "root", kind: "system", name: "prepare_request", summary: "校验输入并整理请求上下文", status: "completed", duration: "42 ms", time: "14:32:08.109", detail: "规范化用户输入，绑定会话、用户与运行上下文。", input: "raw_message, session_id", output: "normalized_request, run_context" },
  { id: "decision-1", parentId: "root", kind: "llm", name: "decide_root_action", summary: "Root 判断需要补充业务口径", status: "completed", duration: "680 ms", time: "14:32:08.151", detail: "统一决策根据用户目标和系统资源目录请求记忆、知识库及当前附件检索。", input: "完整对话 + 资源目录 + 工具目录", output: "action=context；查询 Q3 收入口径与区域映射" },
  { id: "context-1", parentId: "root", kind: "retrieval", name: "acquire_context", summary: "执行 Root 请求的有界检索", status: "completed", duration: "1.8 s", time: "14:32:08.831", detail: "只读取 Root 选择的已装配通道，并按当前资源范围检索。", input: "memory + knowledge + file queries", output: "合并 12 条上下文，保留本轮结果摘要" },
  { id: "memory", parentId: "context-1", kind: "retrieval", name: "memory.search", summary: "找到 2 条用户偏好", status: "completed", duration: "420 ms", time: "14:32:08.904", detail: "记忆检索命中用户偏好：摘要使用中文、优先列出同比变化。", input: "query=Q3 收入口径", output: "2 条记忆，最高相关度 0.91" },
  { id: "knowledge", parentId: "context-1", kind: "retrieval", name: "knowledge.search", summary: "命中 8 个销售分析文档片段", status: "completed", duration: "790 ms", time: "14:32:08.917", detail: "知识库返回销售口径说明、区域映射与季度复盘文档。", input: "knowledge_base=sales-ops", output: "8 个片段，最高相关度 0.94" },
  { id: "files", parentId: "context-1", kind: "retrieval", name: "file.search", summary: "检索当前请求中的 3 个附件", status: "info", duration: "510 ms", time: "14:32:08.923", detail: "附件检索限制在本次请求已验证的文件目录内。", input: "Q3_sales.csv, region_map.csv, ...", output: "3 个文件可用" },
  { id: "decision-2", parentId: "root", kind: "llm", name: "decide_root_action", summary: "Root 依据资料生成 Worker 计划", status: "completed", duration: "1.2 s", time: "14:32:10.631", detail: "检索后重新决策，Root 选择 Worker 工具并把分析拆成可执行任务。", input: "本轮检索结果 + 用户完整目标 + 系统工具目录", output: "action=plan；创建 2 个分析任务和 1 个汇总任务" },
  { id: "plan", parentId: "root", kind: "plan", name: "materialize_execution_plan", summary: "校验并提交计划，启动 Worker", status: "completed", duration: "5.8 s", time: "14:32:13.840", detail: "应用层校验 DAG 与系统工具范围，再通过原子提交命令创建任务。", input: "max_parallelism=2", output: "3/3 tasks completed" },
  { id: "worker-region", parentId: "plan", kind: "worker", name: "worker.region_analysis", summary: "对比各区域销售表现", status: "completed", duration: "2.7 s", time: "14:32:13.903", detail: "Worker 对比三个区域的订单数、收入与客单价。", input: "task_id=tsk_region", output: "华东收入增长 18%；华北持平；华南下降 6%" },
  { id: "worker-trend", parentId: "plan", kind: "worker", name: "worker.trend_analysis", summary: "检测季度趋势和异常值", status: "completed", duration: "3.4 s", time: "14:32:13.911", detail: "第一次工具执行遇到限流，退避后重试并完成计算。", input: "task_id=tsk_trend", output: "收入环比 +11%；8 月有明显峰值" },
  { id: "worker-retry", parentId: "worker-trend", kind: "tool", name: "analytics.query · retry 1", summary: "rate limit，退避 600 ms 后重试", status: "retry", duration: "820 ms", time: "14:32:14.208", detail: "分析工具返回 429。执行器按重试策略等待 600ms 后再次提交。", input: "query=quarterly_revenue", output: "重试后返回 3 条聚合记录", error: "HTTP 429: rate limit exceeded" },
  { id: "approval", parentId: "root", kind: "tool", name: "approval.request", summary: "高风险导出操作等待审批", status: "approved", duration: "1.1 s", time: "14:32:18.742", detail: "导出汇总文件前触发高风险操作审批，审批人确认后继续执行。", input: "action=export_report, risk=high", output: "approved_by=operator@athena.local" },
  { id: "tool", parentId: "root", kind: "tool", name: "report.export", summary: "生成季度分析报告", status: "completed", duration: "1.4 s", time: "14:32:19.876", detail: "工具读取已授权的聚合结果并生成 CSV 报告。", input: "format=csv, rows=12", output: "Q3_sales_summary.csv · 18 KB" },
  { id: "synthesis", parentId: "plan", kind: "llm", name: "plan.synthesis", summary: "合并 Worker 结果并检查一致性", status: "completed", duration: "1.6 s", time: "14:32:21.312", detail: "汇总区域与趋势分析结果，检查统计口径并生成自然语言摘要。", input: "worker outputs: 2", output: "摘要 3 条要点，引用 4 个数据来源" },
  { id: "review", parentId: "plan", kind: "llm", name: "round_review", summary: "Round 完成后判断计划是否结束", status: "completed", duration: "420 ms", time: "14:32:22.872", detail: "Root 根据固定 Round 结果和剩余任务选择 FINISH；若需新一轮会提交 Revision。", input: "完整 DAG + Worker 结果 + 原始目标", output: "action=FINISH；final_status=completed" },
  { id: "response", parentId: "root", kind: "response", name: "final_response", summary: "向用户返回分析结果和报告", status: "completed", duration: "86 ms", time: "14:32:22.958", detail: "最终响应包含销售趋势、区域变化和生成文件链接。", input: "计划结果 + 生成文件引用", output: "Q3 销售收入环比增长 11%，华东区域增长最明显……" },
]

const demoRuns = [
  { id: "run_20261020_0042", title: "分析 Q3 销售数据", status: "success", time: "今天 14:32", duration: "12.4 s", events: 18 },
  { id: "run_20261020_0041", title: "整理客户反馈", status: "failed", time: "今天 13:58", duration: "8.1 s", events: 11 },
  { id: "run_20261020_0039", title: "生成周报初稿", status: "success", time: "今天 11:20", duration: "19.7 s", events: 26 },
  { id: "run_20261019_0188", title: "检索产品文档", status: "success", time: "昨天 17:46", duration: "4.3 s", events: 9 },
]

const kindMeta: Record<EventKind, { label: string; icon: typeof Activity; color: string }> = {
  run: { label: "运行", icon: Activity, color: "text-athena-accent" },
  llm: { label: "模型", icon: Bot, color: "text-sky-400" },
  retrieval: { label: "检索", icon: Database, color: "text-amber-400" },
  tool: { label: "工具", icon: Wrench, color: "text-rose-400" },
  plan: { label: "计划", icon: GitBranch, color: "text-violet-400" },
  worker: { label: "Worker", icon: Network, color: "text-cyan-400" },
  response: { label: "响应", icon: Sparkles, color: "text-emerald-400" },
  system: { label: "系统", icon: Code2, color: "text-athena-muted" },
}

const statusMeta: Record<EventStatus, { label: string; tone: "success" | "warning" | "danger" | "accent" | "default" }> = {
  completed: { label: "完成", tone: "success" },
  failed: { label: "失败", tone: "danger" },
  retry: { label: "已重试", tone: "warning" },
  approved: { label: "已审批", tone: "accent" },
  waiting: { label: "等待审批", tone: "warning" },
  review: { label: "等待审查", tone: "warning" },
  running: { label: "运行中", tone: "accent" },
  cancelled: { label: "已取消", tone: "default" },
  info: { label: "信息", tone: "default" },
}

type FilterKind = "all" | EventKind

function RunChainView() {
  const sessionId = useChatStore((state) => state.activeSessionId)
  const liveEvents = useChatStore((state) => state.runTimelineEvents)
  const demoMode = new URLSearchParams(window.location.search).get("demo") === "execution"
  const [runs, setRuns] = useState<SessionRunSummary[]>([])
  const [scope, setScope] = useState<"all" | "session">("all")
  const [runPage, setRunPage] = useState(0)
  const [runTotal, setRunTotal] = useState(0)
  const [runsHasMore, setRunsHasMore] = useState(false)
  const [runsLoading, setRunsLoading] = useState(false)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [now, setNow] = useState(Date.now)
  const [timeline, setTimeline] = useState<RunTimelineEvent[]>([])
  const [orchestration, setOrchestration] = useState<RunOrchestrationSnapshot | null>(null)
  const [selectedVersionDetail, setSelectedVersionDetail] = useState<PlanVersionDetail | null>(null)
  const [timelineCursor, setTimelineCursor] = useState(0)
  const [timelineHasMore, setTimelineHasMore] = useState(false)
  const [selectedRun, setSelectedRun] = useState<string | null>(demoMode ? demoRuns[0].id : null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState("root")
  const [viewMode, setViewMode] = useState<"timeline" | "tree">("timeline")
  const [filter, setFilter] = useState<FilterKind>("all")
  const [query, setQuery] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["root", "context-1", "decision-1", "decision-2", "plan", "worker-trend"]))
  const scopedSessionId = scope === "session" ? sessionId : undefined
  const selectedRunSummary = runs.find((run) => run.run_id === selectedRun)
  const selectedSessionId = selectedRunSummary?.session_id
  const selectedRunStatus = selectedRunSummary?.status
  const hasActiveRuns = runs.some((run) => !["completed", "failed", "cancelled"].includes(run.status))
  const timelineIdentity = `${selectedSessionId}:${selectedRun}`
  const timelineIdentityRef = useRef(timelineIdentity)
  timelineIdentityRef.current = timelineIdentity

  useEffect(() => {
    if (demoMode || (scope === "session" && !scopedSessionId)) {
      setRuns([])
      setRunTotal(0)
      setRunsLoading(false)
      setRunsHasMore(false)
      setTimeline([])
      setOrchestration(null)
      setSelectedRun(demoMode ? demoRuns[0].id : null)
      return
    }
    let cancelled = false
    let inFlight = false
    const refresh = async () => {
      if (inFlight) return
      inFlight = true
      setRunsLoading(true)
      try {
        const response = await apiClient.listRuns(50, runPage * 50, scopedSessionId ?? undefined)
        if (cancelled) return
        setRuns(response.items)
        setRunTotal(response.total)
        setRunsHasMore(response.has_more)
        setRunsError(null)
        setSelectedRun((current) => response.items.some((item) => item.run_id === current) ? current : response.items[0]?.run_id ?? null)
      } catch (error) {
        if (!cancelled) setRunsError(error instanceof Error ? error.message : "运行列表加载失败")
      } finally {
        inFlight = false
        if (!cancelled) setRunsLoading(false)
      }
    }
    void refresh()
    const timer = window.setInterval(() => { if (!document.hidden) void refresh() }, 10000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [demoMode, scope, scopedSessionId, runPage, refreshKey])

  useEffect(() => {
    if (demoMode || !hasActiveRuns) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [demoMode, hasActiveRuns])

  useEffect(() => {
    setTimeline([])
    setOrchestration(null)
    setTimelineCursor(0)
    setTimelineHasMore(false)
    setLoading(false)
    setLoadError(null)
    if (demoMode || !selectedSessionId || !selectedRun) {
      return
    }
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    Promise.all([
      apiClient.getRunTimeline(selectedSessionId, selectedRun),
      apiClient.getRunOrchestration(selectedRun),
    ])
      .then(([timelineResponse, snapshot]) => {
        if (cancelled) return
        setTimeline(timelineResponse.events)
        setTimelineCursor(timelineResponse.next_after_seq)
        setTimelineHasMore(timelineResponse.has_more)
        setOrchestration(snapshot)
      })
      .catch((error: unknown) => { if (!cancelled) setLoadError(error instanceof Error ? error.message : "运行链路加载失败") })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [demoMode, selectedSessionId, selectedRun, selectedRunStatus, refreshKey])

  useEffect(() => {
    if (demoMode || !selectedSessionId || !selectedRun || timelineHasMore || !selectedRunStatus || ["completed", "failed", "cancelled"].includes(selectedRunStatus)) return
    let cancelled = false
    let inFlight = false
    const timer = window.setInterval(async () => {
      if (document.hidden || inFlight) return
      inFlight = true
      try {
        const [response, snapshot] = await Promise.all([
          apiClient.getRunTimeline(selectedSessionId, selectedRun, timelineCursor),
          apiClient.getRunOrchestration(selectedRun),
        ])
        if (cancelled) return
        setTimeline((current) => [...new Map([...current, ...response.events].map((event) => [event.session_seq, event])).values()].sort((left, right) => left.session_seq - right.session_seq))
        setTimelineCursor(response.next_after_seq)
        setTimelineHasMore(response.has_more)
        setOrchestration(snapshot)
        setLoadError(null)
      } catch (error) {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : "运行链路同步失败")
      } finally {
        inFlight = false
      }
    }, 10000)
    return () => { cancelled = true; window.clearInterval(timer) }
  }, [demoMode, selectedSessionId, selectedRun, selectedRunStatus, timelineCursor, timelineHasMore])

  useEffect(() => {
    const plan = orchestration?.plan
    if (!orchestration) return
    setExpanded((current) => {
      const next = new Set(current)
      next.add(`run:${orchestration.run.run_id}`)
      next.add(`snapshot:run:${orchestration.run.run_id}`)
      if (!plan) return next
      next.add(`snapshot:plan:${plan.plan_id}`)
      for (const version of plan.versions) {
        if (version.version === plan.current_version) {
          next.add(`snapshot:version:${plan.plan_id}:${version.version}`)
        }
      }
      if (plan.active_round_id) next.add(`snapshot:round:${plan.active_round_id}`)
      return next
    })
  }, [orchestration])

  useEffect(() => {
    const prefix = "snapshot:version:"
    if (!selectedId.startsWith(prefix)) {
      setSelectedVersionDetail(null)
      return
    }
    const identity = selectedId.slice(prefix.length)
    const separator = identity.lastIndexOf(":")
    if (separator < 1) return
    const planId = identity.slice(0, separator)
    const version = Number(identity.slice(separator + 1))
    if (!Number.isInteger(version) || version < 1) return
    let cancelled = false
    setSelectedVersionDetail(null)
    apiClient.getPlanVersion(planId, version)
      .then((value) => { if (!cancelled) setSelectedVersionDetail(value) })
      .catch(() => { if (!cancelled) setSelectedVersionDetail(null) })
    return () => { cancelled = true }
  }, [selectedId])

  const loadMoreEvents = async () => {
    if (!selectedSessionId || !selectedRun || loading || !timelineHasMore) return
    const identity = timelineIdentity
    setLoading(true)
    setLoadError(null)
    try {
      const response = await apiClient.getRunTimeline(selectedSessionId, selectedRun, timelineCursor)
      if (identity !== timelineIdentityRef.current) return
      setTimeline((current) => {
        const bySeq = new Map(current.map((event) => [event.session_seq, event]))
        for (const event of response.events) bySeq.set(event.session_seq, event)
        return [...bySeq.values()].sort((left, right) => left.session_seq - right.session_seq)
      })
      setTimelineCursor(response.next_after_seq)
      setTimelineHasMore(response.has_more)
    } catch (error) {
      if (identity === timelineIdentityRef.current) setLoadError(error instanceof Error ? error.message : "更多链路事件加载失败")
    } finally {
      if (identity === timelineIdentityRef.current) setLoading(false)
    }
  }

  const runItems = demoMode ? demoRuns : runs.map((run) => {
    const created = run.created_at ? new Date(run.created_at) : null
    const timing = getRunTiming(run, [], now)
    return {
      id: run.run_id,
      title: run.title || `运行 ${run.run_id.slice(0, 12)}`,
      status: run.status === "completed" ? "success" : run.status,
      time: created ? created.toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "2-digit", day: "2-digit" }) : "时间未知",
      duration: formatDuration(timing.executionMs),
      events: selectedRun === run.run_id ? timeline.length : 0,
    }
  })
  const activeRun = runItems.find((run) => run.id === selectedRun) ?? runItems[0]
  const mergedTimeline = new Map(
    [...timeline, ...liveEvents.filter((event) => event.run_id === selectedRun)]
      .map((event) => [`${event.run_id}:${event.session_seq}`, event] as const),
  )
  const timelineEvents = [...mergedTimeline.values()]
    .sort((left, right) => left.session_seq - right.session_seq)
  const runTiming = selectedRunSummary ? getRunTiming(selectedRunSummary, timelineEvents, now) : null
  const hasRunActivity = timelineEvents.some((event) => event.payload.activity_id === `run:${selectedRun}`)
  const activities = demoMode
    ? demoEvents
    : [
        ...projectSnapshot(orchestration).filter((event) => event.id !== `snapshot:run:${selectedRun}` || !hasRunActivity),
        ...projectTimeline(timelineEvents, selectedRunSummary, now),
      ].map((event) => ({ ...event, parentId: event.parentId === `snapshot:run:${selectedRun}` && hasRunActivity ? `run:${selectedRun}` : event.parentId }))
  const selected = activities.find((event) => event.id === selectedId) ?? activities[0] ?? null
  const filteredEvents = useMemo(() => activities.filter((event) => {
    const matchesKind = filter === "all" || event.kind === filter
    const haystack = `${event.name} ${event.summary} ${event.detail}`.toLowerCase()
    return matchesKind && haystack.includes(query.toLowerCase())
  }), [activities, filter, query])
  const filteredIds = new Set(filteredEvents.map((event) => event.id))
  for (const event of filteredEvents) {
    let parentId = event.parentId
    while (parentId) {
      filteredIds.add(parentId)
      parentId = activities.find((candidate) => candidate.id === parentId)?.parentId
    }
  }

  const renderEvent = (event: RunEvent, depth = 0) => {
    const meta = kindMeta[event.kind]
    const Icon = meta.icon
    const children = activities.filter((candidate) => candidate.parentId === event.id)
    const isExpanded = expanded.has(event.id)
    if (!filteredIds.has(event.id)) return null
    return (
      <div key={event.id}>
        <button
          type="button"
          onClick={() => setSelectedId(event.id)}
          className={`group flex w-full items-center gap-3 border-b border-athena-border/60 px-3 py-3 text-left transition-colors hover:bg-athena-bg/70 ${selectedId === event.id ? "bg-athena-accent/10" : ""}`}
          style={{ paddingLeft: `${12 + depth * 22}px` }}
          aria-pressed={selectedId === event.id}
        >
          <span className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-md border border-athena-border bg-athena-surface">
            <Icon className={`h-4 w-4 ${meta.color}`} />
          </span>
          <span className="min-w-0 flex-1">
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="truncate font-mono text-xs font-medium text-athena-text">{event.name}</span>
              <Badge tone={statusMeta[event.status].tone}>{statusMeta[event.status].label}</Badge>
            </span>
            <span className="mt-1 block truncate text-xs text-athena-muted">{event.summary}</span>
          </span>
          <span className="hidden flex-shrink-0 text-[11px] tabular-nums text-athena-muted sm:block">{event.duration}</span>
          {children.length > 0 ? (
            <span
              role="button"
              tabIndex={0}
              className="rounded p-1 text-athena-muted hover:bg-athena-border/70 hover:text-athena-text"
              aria-label={isExpanded ? "折叠子事件" : "展开子事件"}
              onClick={(clickEvent) => {
                clickEvent.stopPropagation()
                setExpanded((current) => {
                  const next = new Set(current)
                  if (next.has(event.id)) next.delete(event.id)
                  else next.add(event.id)
                  return next
                })
              }}
              onKeyDown={(keyEvent) => {
                if (keyEvent.key === "Enter" || keyEvent.key === " ") {
                  keyEvent.preventDefault()
                  setExpanded((current) => {
                    const next = new Set(current)
                    if (next.has(event.id)) next.delete(event.id)
                    else next.add(event.id)
                    return next
                  })
                }
              }}
            >
              {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            </span>
          ) : <span className="w-6" />}
        </button>
        {viewMode === "tree" && isExpanded && children.map((child) => renderEvent(child, depth + 1))}
      </div>
    )
  }

  const kindFilters: { id: FilterKind; label: string }[] = [
    { id: "all", label: "全部" }, { id: "llm", label: "模型" }, { id: "retrieval", label: "检索" },
    { id: "tool", label: "工具" }, { id: "plan", label: "计划" }, { id: "worker", label: "Worker" },
  ]

  return (
    <ViewShell
      side={
        <div className="flex h-full flex-col">
          <div className="flex items-center justify-between border-b border-athena-border px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold">运行记录</h2>
              <p className="mt-0.5 text-[11px] text-athena-muted">{`${demoMode ? demoRuns.length : runTotal} 次运行`}{runsLoading ? " · 同步中" : ""}</p>
            </div>
            <button type="button" onClick={() => setRefreshKey((current) => current + 1)} disabled={runsLoading || loading} title="刷新运行记录" aria-label="刷新运行记录" className="rounded-md p-2 text-athena-muted hover:bg-athena-bg hover:text-athena-text"><RefreshCw className={`h-4 w-4 ${runsLoading ? "animate-spin" : ""}`} /></button>
          </div>
          {!demoMode && <div className="border-b border-athena-border px-4 py-2">
            <select aria-label="运行记录范围" value={scope} onChange={(event) => { setScope(event.target.value as "all" | "session"); setRunPage(0); setRuns([]); setSelectedRun(null) }} className="w-full rounded border border-athena-border bg-athena-surface px-2 py-1.5 text-xs">
              <option value="all">全部会话</option>
              <option value="session" disabled={!sessionId}>当前会话</option>
            </select>
          </div>}
          <div className="flex-1 overflow-y-auto">
            {runItems.map((run) => (
              <button
                type="button"
                key={run.id}
                onClick={() => { setSelectedRun(run.id); setSelectedId("root") }}
                className={`w-full border-b border-athena-border/60 px-4 py-3 text-left transition-colors hover:bg-athena-bg/70 ${selectedRun === run.id ? "bg-athena-accent/10" : ""}`}
              >
                <span className="flex items-center justify-between gap-2">
                  <span className="truncate text-xs font-medium">{run.title}</span>
                  <span className={`h-2 w-2 flex-shrink-0 rounded-full ${run.status === "success" ? "bg-athena-success" : run.status === "failed" ? "bg-athena-danger" : "bg-athena-accent"}`} />
                </span>
                <span className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-athena-muted">
                  <span>{run.time}</span><span title="实际执行耗时，不含排队">执行 {run.duration}</span>
                </span>
                <span className="mt-1 block truncate font-mono text-[10px] text-athena-muted/80">{run.id}</span>
              </button>
            ))}
            {!runsLoading && !runItems.length && <p className="px-4 py-6 text-xs text-athena-muted">暂无运行记录</p>}
          </div>
          {!demoMode && (runPage > 0 || runsHasMore) && <div className="flex items-center justify-between border-t border-athena-border px-4 py-2 text-xs">
            <button type="button" disabled={!runPage || runsLoading} onClick={() => { setRuns([]); setSelectedRun(null); setRunPage((page) => page - 1) }} className="disabled:opacity-40">上一页</button>
            <span>第 {runPage + 1} 页</span>
            <button type="button" disabled={!runsHasMore || runsLoading} onClick={() => { setRuns([]); setSelectedRun(null); setRunPage((page) => page + 1) }} className="disabled:opacity-40">下一页</button>
          </div>}
          <div className="border-t border-athena-border px-4 py-3 text-[11px] text-athena-muted">{runsError ? <span className="text-athena-danger">{runsError}</span> : demoMode ? "静态演示数据" : scope === "all" ? "全部会话 · 按提交时间倒序" : "当前会话"}</div>
        </div>
      }
    >
      <div className="flex h-full min-h-0 flex-col">
        <Toolbar
          title="运行链路"
          subtitle="任务的完整执行过程与上下文"
          actions={<span className="hidden items-center gap-1.5 text-xs text-athena-muted sm:inline-flex"><span className={`h-1.5 w-1.5 rounded-full ${demoMode ? "bg-athena-warning" : loading ? "bg-athena-accent animate-pulse" : "bg-athena-success"}`} />{demoMode ? "演示数据" : loading ? "正在同步" : "历史链路"}</span>}
        />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="border-b border-athena-border px-4 py-4 md:px-6">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-semibold">{activeRun?.title || (loading ? "正在加载运行…" : "选择运行记录")}</h2>
                  {activeRun && <Badge tone={activeRun.status === "success" ? "success" : statusMeta[snapshotStatus(activeRun.status)].tone}><Check className="h-3 w-3" />{activeRun.status === "success" ? "已完成" : statusMeta[snapshotStatus(activeRun.status)].label}</Badge>}
                </div>
                {selectedRun && <div className="mt-1.5 break-all font-mono text-[11px] text-athena-muted">{selectedRun}</div>}
              </div>
              <div className="flex items-center gap-2 text-xs text-athena-muted"><Clock3 className="h-3.5 w-3.5" />{activeRun?.time ?? "—"}</div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                { label: "执行耗时", value: demoMode ? activeRun?.duration ?? "—" : formatDuration(runTiming?.executionMs ?? null), icon: Clock3, color: "text-sky-400" },
                { label: "事件数", value: String(activities.length), icon: Activity, color: "text-violet-400" },
                { label: "Token", value: demoMode ? "9,704" : "—", icon: Bot, color: "text-amber-400" },
                { label: "重试 / 审批", value: `${activities.filter((event) => event.status === "retry").length} / ${activities.filter((event) => event.status === "approved").length}`, icon: ShieldCheck, color: "text-rose-400" },
              ].map(({ label, value, icon: Icon, color }) => (
                <div key={label} className="flex min-w-0 items-center gap-2.5 border-l border-athena-border pl-3 first:border-0 first:pl-0">
                  <Icon className={`h-4 w-4 flex-shrink-0 ${color}`} />
                  <div className="min-w-0"><div className="text-[10px] text-athena-muted">{label}</div><div className="truncate text-sm font-medium tabular-nums">{value}</div></div>
                </div>
              ))}
            </div>
            {!demoMode && <div className="mt-3 space-y-1 text-[11px] leading-5 text-athena-muted">
              <p>等待耗时 {formatDuration(runTiming?.queueMs ?? null)} · 端到端耗时 {formatDuration(runTiming?.totalMs ?? null)}（等待 + 执行）</p>
              <p>执行耗时从首次开始到结束，包含执行期间的审批、挂起及调度等待。父活动已包含子活动，且活动可能并行，各项耗时不能直接相加。</p>
            </div>}
            {loadError && <p className="mt-2 text-xs text-athena-danger">{loadError}</p>}
          </div>

          <div className="grid min-h-[560px] grid-cols-1 xl:grid-cols-[minmax(0,1fr)_340px]">
            <section className="min-w-0 border-b border-athena-border xl:border-b-0 xl:border-r" aria-label="执行事件">
              <div className="run-chain-toolbar sticky top-0 z-10 border-b border-athena-border bg-athena-bg px-4 py-3 md:px-5">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="flex items-center gap-1 rounded-md border border-athena-border p-0.5">
                    <button type="button" onClick={() => setViewMode("timeline")} className={`rounded px-2.5 py-1.5 text-xs ${viewMode === "timeline" ? "run-chain-view-selected bg-athena-border text-athena-text" : "text-athena-muted hover:text-athena-text"}`}><Activity className="mr-1.5 inline h-3.5 w-3.5" />时间线</button>
                    <button type="button" onClick={() => setViewMode("tree")} className={`rounded px-2.5 py-1.5 text-xs ${viewMode === "tree" ? "run-chain-view-selected bg-athena-border text-athena-text" : "text-athena-muted hover:text-athena-text"}`}><GitBranch className="mr-1.5 inline h-3.5 w-3.5" />执行树</button>
                  </div>
                  <label className="flex h-8 min-w-[170px] flex-1 items-center gap-2 rounded-md border border-athena-border bg-athena-surface px-2.5 sm:max-w-[230px]">
                    <Search className="h-3.5 w-3.5 flex-shrink-0 text-athena-muted" />
                    <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索事件" className="min-w-0 flex-1 bg-transparent text-xs outline-none placeholder:text-athena-muted" />
                    {query && <button type="button" onClick={() => setQuery("")} title="清除搜索" aria-label="清除搜索"><X className="h-3.5 w-3.5 text-athena-muted" /></button>}
                  </label>
                </div>
                <div className="mt-3 flex gap-1 overflow-x-auto pb-0.5">
                  {kindFilters.map((item) => (
                    <button key={item.id} type="button" onClick={() => setFilter(item.id)} className={`flex-shrink-0 rounded px-2.5 py-1 text-[11px] transition-colors ${filter === item.id ? "bg-athena-accent/15 text-athena-accent" : "text-athena-muted hover:bg-athena-surface hover:text-athena-text"}`}>{item.label}</button>
                  ))}
                </div>
              </div>
              {viewMode === "timeline" ? (
                <div className="divide-y divide-athena-border/40">
                  {filteredEvents.map((event) => {
                    const meta = kindMeta[event.kind]
                    const Icon = meta.icon
                    return (
                      <button key={event.id} type="button" onClick={() => setSelectedId(event.id)} className={`relative flex w-full items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-athena-bg/70 md:px-5 ${selectedId === event.id ? "bg-athena-accent/10" : ""}`}>
                        <div className="relative flex w-5 flex-shrink-0 justify-center pt-0.5"><span className={`z-[1] flex h-5 w-5 items-center justify-center rounded-full bg-athena-surface ring-1 ring-athena-border ${meta.color}`}><Icon className="h-3 w-3" /></span><span className="absolute top-5 h-[calc(100%+12px)] w-px bg-athena-border/70" /></div>
                        <div className="min-w-0 flex-1">
                          <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="font-mono text-xs font-medium">{event.name}</span><Badge tone={statusMeta[event.status].tone}>{statusMeta[event.status].label}</Badge><span className="ml-auto text-[10px] tabular-nums text-athena-muted">{event.duration}</span></div>
                          <p className="mt-1 text-xs leading-5 text-athena-muted">{event.summary}</p>
                          <div className="mt-1 text-[10px] text-athena-muted/80">{event.time}</div>
                        </div>
                      </button>
                    )
                  })}
                </div>
              ) : <div className="divide-y divide-athena-border/40">{activities.filter((event) => !event.parentId).map((event) => renderEvent(event))}</div>}
              {filteredEvents.length === 0 && <div className="px-5 py-12 text-center text-sm text-athena-muted">没有匹配的事件</div>}
              {!demoMode && timelineHasMore && <div className="border-t border-athena-border px-4 py-3 text-center">
                <button type="button" onClick={() => void loadMoreEvents()} disabled={loading} className="rounded-md border border-athena-border px-3 py-1.5 text-xs text-athena-muted transition-colors hover:bg-athena-surface hover:text-athena-text disabled:cursor-wait disabled:opacity-50">
                  {loading ? "正在加载…" : "加载更多事件"}
                </button>
                {loadError && <p className="mt-2 text-xs text-athena-danger">{loadError}</p>}
              </div>}
            </section>

            <aside className="run-chain-details min-w-0 bg-athena-surface" aria-label="事件详情">
              <div className="flex items-center justify-between border-b border-athena-border px-4 py-3">
                <div className="flex items-center gap-2"><Code2 className="h-4 w-4 text-athena-muted" /><h3 className="text-xs font-semibold">事件详情</h3></div>
                {selected && <Badge tone={statusMeta[selected.status].tone}>{statusMeta[selected.status].label}</Badge>}
              </div>
              {selected ? <div className="space-y-4 p-4">
                <div>
                  <div className="break-all font-mono text-sm font-medium">{selected.name}</div>
                  <p className="mt-1.5 text-xs leading-5 text-athena-muted">{selected.detail}</p>
                </div>
                <div className="grid grid-cols-2 gap-3 border-y border-athena-border py-3 text-xs">
                  <div><div className="text-[10px] text-athena-muted">类型</div><div className="mt-1">{kindMeta[selected.kind].label}</div></div>
                  <div><div className="text-[10px] text-athena-muted">耗时</div><div className="mt-1 tabular-nums">{selected.duration}</div></div>
                  <div><div className="text-[10px] text-athena-muted">开始时间</div><div className="mt-1 font-mono text-[10px]">{selected.time}</div></div>
                  <div><div className="text-[10px] text-athena-muted">活动 ID</div><div title={selected.id} className="mt-1 truncate font-mono text-[10px]">{selected.id}</div></div>
                </div>
                {selected.error && <div className="rounded-md border border-athena-danger/30 bg-athena-danger/5 p-3"><div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-athena-danger"><CircleAlert className="h-3.5 w-3.5" />错误信息</div><code className="break-words text-[11px] leading-5 text-athena-text/80">{selected.error}</code></div>}
                {selected.input && <DetailBlock title="输入" value={selected.input} />}
                {selected.output && <DetailBlock title="输出" value={selected.output} />}
                {selectedVersionDetail && selected?.id === `snapshot:version:${selectedVersionDetail.plan_id}:${selectedVersionDetail.version}` && <DetailBlock title="Plan DAG" value={JSON.stringify(selectedVersionDetail.dag_json, null, 2)} />}
                {selected.status === "approved" && <div className="flex items-start gap-2 rounded-md border border-athena-accent/30 bg-athena-accent/5 p-3 text-xs"><ShieldCheck className="mt-0.5 h-4 w-4 flex-shrink-0 text-athena-accent" /><div><div className="font-medium">审批已通过</div><div className="mt-1 text-[11px] text-athena-muted">审批事件已关联至当前运行</div></div></div>}
              </div>
              : <div className="p-4 text-xs text-athena-muted">{loading ? "正在加载链路事件…" : loadError || (runItems.length ? "该运行暂无可展示的链路事件" : "选择运行记录查看详情")}</div>}
            </aside>
          </div>
        </div>
      </div>
    </ViewShell>
  )
}

function DetailBlock({ title, value }: { title: string; value: string }) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between text-[10px] font-medium text-athena-muted"><span>{title}</span><button type="button" title={`复制${title}`} aria-label={`复制${title}`} onClick={() => void navigator.clipboard?.writeText(value)} className="rounded p-1 hover:bg-athena-border/60 hover:text-athena-text"><Code2 className="h-3 w-3" /></button></div>
      <pre className="max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-md border border-athena-border bg-athena-bg p-2.5 font-mono text-[10px] leading-5 text-athena-text/80">{value}</pre>
    </div>
  )
}

function projectSnapshot(snapshot: RunOrchestrationSnapshot | null): RunEvent[] {
  if (!snapshot) return []
  const runId = snapshot.run.run_id
  const runNodeId = `snapshot:run:${runId}`
  const externalEffectStatus = snapshot.run.cancellation_external_effect_status
  const externalEffectDetail = externalEffectStatus === "unknown"
    ? "外部副作用结果未知；停止不能保证撤回已发送的操作"
    : externalEffectStatus === "none"
      ? "未观察到在途外部副作用"
      : `外部副作用状态：${externalEffectStatus}`
  const events: RunEvent[] = [{
    id: runNodeId,
    kind: "run",
    name: "Run 状态快照",
    summary: `持久状态：${snapshot.run.status}`,
    status: snapshotStatus(snapshot.run.status),
    duration: "—",
    time: formatTimestamp(snapshot.run.updated_at || snapshot.run.created_at),
    detail: `取消 epoch ${snapshot.run.cancellation_epoch} · ${externalEffectDetail}`,
  }]
  const taskNodes = new Map<string, string>()
  const executionNodes = new Map<string, string>()

  if (snapshot.plan) {
    const plan = snapshot.plan
    const planNodeId = `snapshot:plan:${plan.plan_id}`
    events.push({
      id: planNodeId,
      parentId: runNodeId,
      kind: "plan",
      name: `Plan · ${plan.plan_id}`,
      summary: `${plan.status} · 当前版本 v${plan.current_version}`,
      status: snapshotStatus(plan.status),
      duration: `${plan.max_parallelism} 并发`,
      time: formatTimestamp(plan.versions[0]?.created_at ?? snapshot.run.created_at),
      detail: `${plan.user_goal} · cancel epoch ${plan.cancellation_epoch}`,
    })
    const versionNodes = new Map<number, string>()
    for (const version of plan.versions) {
      const versionNodeId = `snapshot:version:${plan.plan_id}:${version.version}`
      versionNodes.set(version.version, versionNodeId)
      events.push({
        id: versionNodeId,
        parentId: planNodeId,
        kind: "plan",
        name: `Plan Version v${version.version}`,
        summary: version.change_reason || (version.version === 1 ? "初始版本" : "未记录修订原因"),
        status: version.version === plan.current_version ? snapshotStatus(plan.status) : "info",
        duration: version.version === plan.current_version ? "当前" : "历史",
        time: formatTimestamp(version.created_at),
        detail: version.based_on_round_id
          ? `基于 Round ${version.based_on_round_id} 修订；选择此节点可查看不可变 DAG。`
          : "选择此节点可查看不可变 DAG。",
      })
    }
    for (const round of plan.rounds) {
      const roundNodeId = `snapshot:round:${round.round_id}`
      events.push({
        id: roundNodeId,
        parentId: versionNodes.get(round.plan_version) ?? planNodeId,
        kind: "plan",
        name: `Round ${round.round_number}`,
        summary: `${round.status} · ${round.terminal_task_count}/${round.expected_task_count} tasks`,
        status: snapshotStatus(round.status),
        duration: `v${round.plan_version}`,
        time: formatTimestamp(round.created_at),
        detail: `round_id=${round.round_id}${round.completed_at ? ` · 完成于 ${formatTimestamp(round.completed_at)}` : ""}`,
      })
      for (const task of round.tasks) {
        const taskNodeId = `snapshot:task:${round.round_id}:${task.task_id}`
        taskNodes.set(task.task_id, taskNodeId)
        events.push({
          id: taskNodeId,
          parentId: roundNodeId,
          kind: "worker",
          name: task.title || task.task_id,
          summary: task.status,
          status: snapshotStatus(task.status),
          duration: `${task.executions.length} 次尝试`,
          time: formatTimestamp(task.terminal_at ?? round.created_at),
          detail: `${task.objective} · task_id=${task.task_id}`,
        })
        for (const execution of task.executions) {
          const executionNodeId = `snapshot:execution:${execution.execution_id}`
          executionNodes.set(execution.execution_id, executionNodeId)
          events.push(executionEvent(execution, taskNodeId, executionNodeId))
        }
      }
    }
  }

  for (const approval of snapshot.approvals) {
    events.push(approvalEvent(approval, executionNodes, taskNodes, runNodeId))
  }
  return events
}

function executionEvent(
  execution: OrchestrationExecutionSnapshot,
  parentId: string,
  id: string,
): RunEvent {
  const error = execution.error?.message || execution.error?.code
  return {
    id,
    parentId,
    kind: "worker",
    name: `Execution attempt ${execution.attempt}`,
    summary: `${execution.status}${execution.fencing_token === null ? "" : ` · fence ${execution.fencing_token}`}`,
    status: snapshotStatus(execution.status),
    duration: "—",
    time: formatTimestamp(execution.created_at),
    detail: `execution_id=${execution.execution_id} · thread=${execution.worker_thread_id}${execution.result_ref ? ` · result_ref=${execution.result_ref}` : ""}`,
    error: error ? String(error) : undefined,
  }
}

function approvalEvent(
  approval: OrchestrationApprovalSnapshot,
  executionNodes: Map<string, string>,
  taskNodes: Map<string, string>,
  fallbackParent: string,
): RunEvent {
  const status = approval.status.toLowerCase()
  const eventStatus: EventStatus = status === "approved" || status === "resolved" && approval.decision === "approve"
    ? "approved"
    : status === "pending" ? "waiting"
      : status === "cancelled" ? "cancelled" : status === "expired" || status === "rejected" || status === "denied" ? "failed" : "info"
  return {
    id: `snapshot:approval:${approval.approval_id}`,
    parentId: (approval.execution_id && executionNodes.get(approval.execution_id))
      || (approval.task_id && taskNodes.get(approval.task_id))
      || fallbackParent,
    kind: "tool",
    name: `审批 · ${approval.tool_name}`,
    summary: `${approval.status}${approval.decision ? ` · ${approval.decision}` : ""} · ${approval.risk_level}`,
    status: eventStatus,
    duration: "—",
    time: formatTimestamp(approval.created_at),
    detail: `approval_id=${approval.approval_id}${approval.interrupt_id ? ` · interrupt_id=${approval.interrupt_id}` : ""}${approval.decided_at ? ` · 决定于 ${formatTimestamp(approval.decided_at)}` : ""}`,
  }
}

function snapshotStatus(value: string): EventStatus {
  const status = value.toLowerCase()
  if (status.includes("fail") || status.includes("error")) return "failed"
  if (status === "waiting_review") return "review"
  if (status.includes("approv")) return "waiting"
  if (status.includes("cancel")) return "cancelled"
  if (status.includes("running") || status.includes("cancelling") || status.includes("queued")) return "running"
  if (status.includes("pending") || status.includes("created") || status.includes("interrupted")) return "waiting"
  if (status.includes("completed")) return "completed"
  return "info"
}

function projectTimeline(rawEvents: RunTimelineEvent[], run?: SessionRunSummary, now = Date.now()): RunEvent[] {
  const activities = new Map<string, RunEvent>()
  for (const event of rawEvents) {
    const payload = event.payload
    const id = String(payload.activity_id || payload.call_id || payload.tool_call_id || `${event.event_type}:${event.session_seq}`)
    const eventName = String(payload.name || payload.node || payload.tool_name || payload.activity_type || event.event_type)
    const eventType = String(payload.activity_type || event.event_type)
    const kind: EventKind = eventType.includes("llm") || event.event_type.startsWith("llm.")
      ? "llm"
      : eventType.includes("retriev") || event.event_type.startsWith("retrieval.")
        ? "retrieval"
        : eventType.includes("tool") || event.event_type.startsWith("tool.") || event.event_type.startsWith("approval.")
          ? "tool"
          : eventType.includes("plan") || event.event_type.startsWith("plan.")
            ? "plan"
            : eventType.includes("task") || event.event_type.startsWith("task.")
              ? "worker"
              : eventType.includes("response") || event.event_type.includes("message.completed")
                ? "response"
                : eventType === "run" || event.event_type.startsWith("run.")
                  ? "run"
                  : "system"
    const status = timelineStatus(String(payload.status || event.event_type), event.event_type)
    const existing = activities.get(id)
    const startedAt = existing?.startedAt ?? (typeof payload.started_at === "string" ? payload.started_at
      : event.event_type.endsWith(".started") || event.event_type === "message.delta" ? event.occurred_at : undefined)
    const completedAt = typeof payload.completed_at === "string" ? payload.completed_at
      : /\.(completed|failed|cancelled)$/.test(event.event_type) ? event.occurred_at : undefined
    const errorValue = payload.error
    const error = typeof errorValue === "object" && errorValue !== null
      ? String((errorValue as { message?: unknown }).message || (errorValue as { code?: unknown }).code || "")
      : typeof errorValue === "string" ? errorValue : ""
    const retrievalSummary = event.event_type === "retrieval.completed"
      ? `${String(payload.raw_hit_count ?? "未知")} 次命中 · ${String(payload.candidate_count ?? 0)} 个候选 · ${String(payload.bundle_count ?? 0)} 条进入资料窗口`
      : event.event_type === "retrieval.selection.completed"
        ? `本轮最终保留 ${String(payload.context_count ?? 0)} 条 · 记录${payload.trace_status === "complete" ? "完整" : "不完整"}`
        : event.event_type.startsWith("retrieval.provider.")
          ? `${String(payload.candidate_count ?? 0)} 个候选 · ${String(payload.result_count ?? 0)} 条模块输出${error ? ` · ${error}` : ""}`
          : ""
    const next: RunEvent = {
      id,
      parentId: String(payload.parent_activity_id || payload.parent_run_id || "") || undefined,
      kind,
      name: eventName,
      summary: String(payload.summary || error || retrievalSummary || event.event_type),
      status,
      duration: kind === "run" && run ? formatDuration(getRunTiming(run, rawEvents, now).executionMs)
        : getActivityDuration(startedAt, completedAt, payload.duration_ms),
      startedAt,
      // 活动合并后保留首次服务端事件时间；历史分页缺少 started 事件时使用服务端 started_at。
      time: existing?.time ?? formatTimestamp(
        typeof payload.started_at === "string" ? payload.started_at : event.occurred_at,
      ),
      detail: error || String(payload.summary || `事件 ${event.event_type} 已记录。`),
      error: error || undefined,
    }
    activities.set(id, existing ? { ...existing, ...next, parentId: existing.parentId || next.parentId } : next)
  }
  // 输入按持久化 session_seq 排序，Map 顺序能保留活动起始顺序且不受本地日期边界影响。
  return [...activities.values()]
}

function timelineStatus(status: string, eventType: string): EventStatus {
  if (status.includes("fail") || status.includes("error")) return "failed"
  if (status.includes("expir")) return "failed"
  if (status.includes("retry") || eventType.includes("retry")) return "retry"
  if (status.includes("approv") || eventType === "approval.resolved") return "approved"
  if (status.includes("waiting") || status.includes("pending")) return "waiting"
  if (status.includes("running") || status.includes("started") || status.includes("queued")) return "running"
  if (status.includes("cancel")) return "cancelled"
  return "completed"
}

function formatTimestamp(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour12: false })
}

export default RunChainView
