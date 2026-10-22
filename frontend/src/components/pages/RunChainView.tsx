import { useMemo, useState } from "react"
import { useEffect } from "react"
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
  ListFilter,
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
import type { Message, RunTimelineEvent, SessionRunSummary } from "../../types"

type EventStatus = "completed" | "failed" | "retry" | "approved" | "waiting" | "running" | "cancelled" | "info"
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
}

const demoEvents: RunEvent[] = [
  { id: "root", kind: "run", name: "用户请求", summary: "分析销售数据并生成季度摘要", status: "completed", duration: "12.4 s", time: "14:32:08.102", detail: "运行入口接收用户请求，创建 root run 并初始化执行上下文。", input: "分析 Q3 销售数据，找出主要变化并生成一份简短摘要。", output: "任务已完成，生成季度销售摘要。" },
  { id: "prepare", parentId: "root", kind: "system", name: "prepare_request", summary: "校验输入并整理请求上下文", status: "completed", duration: "42 ms", time: "14:32:08.109", detail: "规范化用户输入，绑定会话、用户与运行上下文。", input: "raw_message, session_id", output: "normalized_request, run_context" },
  { id: "understand", parentId: "root", kind: "llm", name: "understand_task", summary: "识别目标、约束与所需数据", status: "completed", duration: "680 ms", time: "14:32:08.151", detail: "模型将请求解析为数据分析与摘要生成两个目标。", input: "用户问题 + 会话摘要", output: "目标：分析 Q3 销售数据；约束：输出简短摘要" },
  { id: "context", parentId: "root", kind: "retrieval", name: "acquire_context", summary: "汇总记忆、知识库和文件上下文", status: "completed", duration: "1.8 s", time: "14:32:08.831", detail: "并行检索多个上下文来源，并按相关性去重合并。", input: "query=Q3 销售数据", output: "合并 12 条上下文，丢弃 3 条低相关结果" },
  { id: "memory", parentId: "context", kind: "retrieval", name: "memory.search", summary: "找到 2 条用户偏好", status: "completed", duration: "420 ms", time: "14:32:08.904", detail: "记忆检索命中用户偏好：摘要使用中文、优先列出同比变化。", input: "top_k=5", output: "2 条记忆，最高相关度 0.91" },
  { id: "knowledge", parentId: "context", kind: "retrieval", name: "knowledge.search", summary: "命中 8 个销售分析文档片段", status: "completed", duration: "790 ms", time: "14:32:08.917", detail: "知识库返回销售口径说明、区域映射与季度复盘文档。", input: "collection=sales-ops", output: "8 个片段，最高相关度 0.94" },
  { id: "files", parentId: "context", kind: "retrieval", name: "file.search", summary: "扫描 4 个附件，过滤 1 个", status: "info", duration: "510 ms", time: "14:32:08.923", detail: "附件解析后按数据日期和字段完整度过滤；1 个过期文件未加入上下文。", input: "Q3_sales.csv, region_map.csv, ...", output: "3 个文件可用；1 个文件因日期早于 Q3 被过滤" },
  { id: "answer-call", parentId: "root", kind: "llm", name: "llm.generate", summary: "生成分析方案与任务拆分", status: "completed", duration: "3.2 s", time: "14:32:10.631", detail: "模型生成分析计划，第一次调用超时后自动重试成功。", input: "上下文 12 条 + 用户目标", output: "创建 2 个并行分析任务和 1 个汇总任务" },
  { id: "attempt-1", parentId: "answer-call", kind: "llm", name: "attempt 1", summary: "provider request timeout", status: "retry", duration: "2.0 s", time: "14:32:10.645", detail: "provider 在超时阈值内未返回响应，调度器按策略进行一次重试。", input: "model=reasoning-large, timeout=2000ms", output: "无响应", error: "TimeoutError: provider request exceeded 2000ms" },
  { id: "attempt-2", parentId: "answer-call", kind: "llm", name: "attempt 2", summary: "重试成功，返回结构化计划", status: "completed", duration: "1.2 s", time: "14:32:12.672", detail: "重试使用相同模型与输入，成功返回结构化计划。", input: "retry=1", output: "plan_id=pln_7f23, task_count=3" },
  { id: "plan", parentId: "root", kind: "plan", name: "plan.execute", summary: "并行执行 2 个 Worker，等待汇总", status: "completed", duration: "5.8 s", time: "14:32:13.840", detail: "计划包含区域分析、趋势分析与最终汇总三个任务。", input: "max_parallelism=2", output: "3/3 tasks completed" },
  { id: "worker-region", parentId: "plan", kind: "worker", name: "worker.region_analysis", summary: "对比各区域销售表现", status: "completed", duration: "2.7 s", time: "14:32:13.903", detail: "Worker 对比三个区域的订单数、收入与客单价。", input: "task_id=tsk_region", output: "华东收入增长 18%；华北持平；华南下降 6%" },
  { id: "worker-trend", parentId: "plan", kind: "worker", name: "worker.trend_analysis", summary: "检测季度趋势和异常值", status: "completed", duration: "3.4 s", time: "14:32:13.911", detail: "第一次工具执行遇到限流，退避后重试并完成计算。", input: "task_id=tsk_trend", output: "收入环比 +11%；8 月有明显峰值" },
  { id: "worker-retry", parentId: "worker-trend", kind: "tool", name: "analytics.query · retry 1", summary: "rate limit，退避 600 ms 后重试", status: "retry", duration: "820 ms", time: "14:32:14.208", detail: "分析工具返回 429。执行器按重试策略等待 600ms 后再次提交。", input: "query=quarterly_revenue", output: "重试后返回 3 条聚合记录", error: "HTTP 429: rate limit exceeded" },
  { id: "approval", parentId: "root", kind: "tool", name: "approval.request", summary: "高风险导出操作等待审批", status: "approved", duration: "1.1 s", time: "14:32:18.742", detail: "导出汇总文件前触发高风险操作审批，审批人确认后继续执行。", input: "action=export_report, risk=high", output: "approved_by=operator@athena.local" },
  { id: "tool", parentId: "root", kind: "tool", name: "report.export", summary: "生成季度分析报告", status: "completed", duration: "1.4 s", time: "14:32:19.876", detail: "工具读取已授权的聚合结果并生成 CSV 报告。", input: "format=csv, rows=12", output: "Q3_sales_summary.csv · 18 KB" },
  { id: "synthesis", parentId: "plan", kind: "llm", name: "plan.synthesis", summary: "合并 Worker 结果并检查一致性", status: "completed", duration: "1.6 s", time: "14:32:21.312", detail: "汇总区域与趋势分析结果，检查统计口径并生成自然语言摘要。", input: "worker outputs: 2", output: "摘要 3 条要点，引用 4 个数据来源" },
  { id: "response", parentId: "root", kind: "response", name: "final_response", summary: "向用户返回分析结果和报告", status: "completed", duration: "86 ms", time: "14:32:22.958", detail: "最终响应包含销售趋势、区域变化和生成文件链接。", input: "synthesis + export artifact", output: "Q3 销售收入环比增长 11%，华东区域增长最明显……" },
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
  const [messages, setMessages] = useState<Message[]>([])
  const [timeline, setTimeline] = useState<RunTimelineEvent[]>([])
  const [timelineCursor, setTimelineCursor] = useState(0)
  const [timelineHasMore, setTimelineHasMore] = useState(false)
  const [selectedRun, setSelectedRun] = useState<string | null>(demoMode ? demoRuns[0].id : null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState("root")
  const [viewMode, setViewMode] = useState<"timeline" | "tree">("timeline")
  const [filter, setFilter] = useState<FilterKind>("all")
  const [query, setQuery] = useState("")
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["root", "context", "answer-call", "plan", "worker-trend"]))
  useEffect(() => {
    if (demoMode || !sessionId) {
      setRuns([])
      setMessages([])
      setTimeline([])
      setSelectedRun(demoMode ? demoRuns[0].id : null)
      return
    }
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    Promise.all([apiClient.listSessionRuns(sessionId), apiClient.getMessages(sessionId)])
      .then(([items, sessionMessages]) => {
        if (cancelled) return
        setRuns(items)
        setMessages(sessionMessages)
        setSelectedRun((current) => items.some((item) => item.run_id === current) ? current : items[0]?.run_id ?? null)
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : "运行列表加载失败")
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [demoMode, sessionId])

  useEffect(() => {
    if (demoMode || !sessionId || !selectedRun) {
      setTimeline([])
      setTimelineCursor(0)
      setTimelineHasMore(false)
      return
    }
    let cancelled = false
    setLoading(true)
    setLoadError(null)
    apiClient.getRunTimeline(sessionId, selectedRun)
      .then((response) => {
        if (cancelled) return
        setTimeline(response.events)
        setTimelineCursor(response.next_after_seq)
        setTimelineHasMore(response.has_more)
      })
      .catch((error: unknown) => { if (!cancelled) setLoadError(error instanceof Error ? error.message : "运行链路加载失败") })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [demoMode, sessionId, selectedRun])

  const loadMoreEvents = async () => {
    if (!sessionId || !selectedRun || loading || !timelineHasMore) return
    setLoading(true)
    setLoadError(null)
    try {
      const response = await apiClient.getRunTimeline(sessionId, selectedRun, timelineCursor)
      setTimeline((current) => {
        const bySeq = new Map(current.map((event) => [event.session_seq, event]))
        for (const event of response.events) bySeq.set(event.session_seq, event)
        return [...bySeq.values()].sort((left, right) => left.session_seq - right.session_seq)
      })
      setTimelineCursor(response.next_after_seq)
      setTimelineHasMore(response.has_more)
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "更多链路事件加载失败")
    } finally {
      setLoading(false)
    }
  }

  const runItems = demoMode ? demoRuns : runs.map((run) => {
    const userMessage = messages.find((message) => message.run_id === run.run_id && message.role === "user")
    const created = run.created_at ? new Date(run.created_at) : null
    const updated = run.updated_at ? new Date(run.updated_at) : null
    const durationMs = created && updated ? Math.max(0, updated.getTime() - created.getTime()) : null
    return {
      id: run.run_id,
      title: userMessage?.content.slice(0, 48) || `运行 ${run.run_id.slice(0, 12)}`,
      status: run.status === "completed" ? "success" : run.status === "failed" ? "failed" : "running",
      time: created ? created.toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit", month: "2-digit", day: "2-digit" }) : "时间未知",
      duration: durationMs == null ? "—" : `${(durationMs / 1000).toFixed(1)} s`,
      events: selectedRun === run.run_id ? timeline.length : 0,
    }
  })
  const activeRun = runItems.find((run) => run.id === selectedRun) ?? runItems[0]
  const activities = demoMode
    ? demoEvents
    : projectTimeline([
        ...new Map([...timeline, ...liveEvents.filter((event) => event.run_id === selectedRun)].map((event) => [`${event.run_id}:${event.session_seq}`, event])).values(),
      ])
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
              <p className="mt-0.5 text-[11px] text-athena-muted">{loading ? "正在加载…" : `${runItems.length} 次运行`}</p>
            </div>
            <button type="button" title="筛选运行记录" aria-label="筛选运行记录" className="rounded-md p-2 text-athena-muted hover:bg-athena-bg hover:text-athena-text"><ListFilter className="h-4 w-4" /></button>
          </div>
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
                  <span>{run.time}</span><span>{run.duration}</span>
                </span>
                <span className="mt-1 block truncate font-mono text-[10px] text-athena-muted/80">{run.id}</span>
              </button>
            ))}
          </div>
          <div className="border-t border-athena-border px-4 py-3 text-[11px] text-athena-muted">{loadError ? <span className="text-athena-danger">{loadError}</span> : demoMode ? "静态演示数据" : sessionId ? "当前会话" : "请先选择会话"}</div>
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
                  {activeRun && <Badge tone={activeRun.status === "success" ? "success" : activeRun.status === "failed" ? "danger" : "accent"}><Check className="h-3 w-3" />{activeRun.status === "success" ? "已完成" : activeRun.status === "failed" ? "失败" : "运行中"}</Badge>}
                </div>
                {selectedRun && <div className="mt-1.5 break-all font-mono text-[11px] text-athena-muted">{selectedRun}</div>}
              </div>
              <div className="flex items-center gap-2 text-xs text-athena-muted"><Clock3 className="h-3.5 w-3.5" />{activeRun?.time ?? "—"}</div>
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
              {[
                { label: "总耗时", value: activeRun?.duration ?? "—", icon: Clock3, color: "text-sky-400" },
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
                  <div><div className="text-[10px] text-athena-muted">事件 ID</div><div className="mt-1 truncate font-mono text-[10px]">evt_{selected.id}_a81f</div></div>
                </div>
                {selected.error && <div className="rounded-md border border-athena-danger/30 bg-athena-danger/5 p-3"><div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-athena-danger"><CircleAlert className="h-3.5 w-3.5" />错误信息</div><code className="break-words text-[11px] leading-5 text-athena-text/80">{selected.error}</code></div>}
                {selected.input && <DetailBlock title="输入" value={selected.input} />}
                {selected.output && <DetailBlock title="输出" value={selected.output} />}
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

function projectTimeline(rawEvents: RunTimelineEvent[]): RunEvent[] {
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
    const errorValue = payload.error
    const error = typeof errorValue === "object" && errorValue !== null
      ? String((errorValue as { message?: unknown }).message || (errorValue as { code?: unknown }).code || "")
      : typeof errorValue === "string" ? errorValue : ""
    const next: RunEvent = {
      id,
      parentId: String(payload.parent_activity_id || payload.parent_run_id || "") || undefined,
      kind,
      name: eventName,
      summary: String(payload.summary || error || event.event_type),
      status,
      duration: payload.duration_ms == null ? "—" : formatDuration(Number(payload.duration_ms)),
      time: formatTimestamp(event.occurred_at),
      detail: error || String(payload.summary || `事件 ${event.event_type} 已记录。`),
      error: error || undefined,
    }
    activities.set(id, existing ? { ...existing, ...next, parentId: existing.parentId || next.parentId } : next)
  }
  return [...activities.values()].sort((left, right) => left.time.localeCompare(right.time))
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

function formatDuration(value: number): string {
  if (!Number.isFinite(value)) return "—"
  return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`
}

function formatTimestamp(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("zh-CN", { hour12: false })
}

export default RunChainView
