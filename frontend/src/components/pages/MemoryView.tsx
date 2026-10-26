import { useCallback, useEffect, useState } from "react"
import {
  Activity,
  Database,
  Clock3,
  TrendingUp,
  ChevronLeft,
  ChevronRight,
  X,
  Trash2,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type {
  MemoryEntry,
  MemoryEpisodeEntry,
  MemoryEpisodeListResponse,
  MemoryJobListResponse,
  MemoryListResponse,
  MemoryRelationEntry,
  RetrievalRunDetail,
  RetrievalRunSummary,
} from "../../types"
import { useChatStore } from "../../store/chatStore"
import { formatDateTime, truncate } from "../../utils/format"
import ViewShell from "./ViewShell"
import MemoryFilters from "../ctx/MemoryFilters"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import DataTable from "../ui/DataTable"
import type { Column } from "../ui/DataTable"

const PAGE_SIZE = 15

function MemoryView() {
  const [data, setData] = useState<MemoryListResponse | null>(null)
  const [episodeData, setEpisodeData] = useState<MemoryEpisodeListResponse | null>(null)
  const [viewMode, setViewMode] = useState<"memories" | "episodes" | "activity">("memories")
  const [episodesLoading, setEpisodesLoading] = useState(false)
  const [activityLoading, setActivityLoading] = useState(false)
  const [jobData, setJobData] = useState<MemoryJobListResponse | null>(null)
  const [retrievalRuns, setRetrievalRuns] = useState<RetrievalRunSummary[]>([])
  const [selectedRetrieval, setSelectedRetrieval] = useState<RetrievalRunDetail | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [expiredOnly, setExpiredOnly] = useState(false)
  const [page, setPage] = useState(0)
  const [sessionId, setSessionId] = useState("")
  const [selected, setSelected] = useState<MemoryEntry | null>(null)
  const [selectedEpisode, setSelectedEpisode] = useState<MemoryEpisodeEntry | null>(null)
  const [revisions, setRevisions] = useState<MemoryEntry[]>([])
  const [relations, setRelations] = useState<MemoryRelationEntry[]>([])
  const [deleting, setDeleting] = useState(false)
  const sessions = useChatStore((state) => state.sessions)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.listMemories({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        expired: expiredOnly,
        session_id: sessionId || undefined,
      })
      setData(res)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [page, expiredOnly, sessionId])

  useEffect(() => {
    reload()
  }, [reload])

  const reloadEpisodes = useCallback(async () => {
    setEpisodesLoading(true)
    try {
      const res = await apiClient.listMemoryEpisodes({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        session_id: sessionId || undefined,
      })
      setEpisodeData(res)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setEpisodesLoading(false)
    }
  }, [page, sessionId])

  useEffect(() => {
    if (viewMode === "episodes") void reloadEpisodes()
  }, [viewMode, reloadEpisodes])

  const reloadActivity = useCallback(async () => {
    setActivityLoading(true)
    try {
      const [jobs, memoryRuns, episodeRuns] = await Promise.all([
        apiClient.listMemoryJobs({ limit: 20, session_id: sessionId || undefined }),
        apiClient.listRetrievalRuns({ limit: 20, provider: "memory", session_id: sessionId || undefined }),
        apiClient.listRetrievalRuns({ limit: 20, provider: "episode", session_id: sessionId || undefined }),
      ])
      const recentRuns = new Map<string, RetrievalRunSummary>()
      for (const run of [...memoryRuns.items, ...episodeRuns.items]) recentRuns.set(run.run_id, run)
      setJobData(jobs)
      setRetrievalRuns([...recentRuns.values()].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setActivityLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    if (viewMode === "activity") void reloadActivity()
  }, [viewMode, reloadActivity])

  const openRetrieval = async (runId: string) => {
    try {
      setSelectedRetrieval(await apiClient.getRetrievalRun(runId))
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const [searchedItems, setSearchedItems] = useState<MemoryEntry[] | null>(null)
  useEffect(() => {
    if (viewMode !== "memories" || !search.trim()) { setSearchedItems(null); return }
    const timer = window.setTimeout(() => {
      void apiClient.searchMemory(search.trim(), 50).then(setSearchedItems).catch((e) => setError(e instanceof Error ? e.message : String(e)))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [search, viewMode])

  const items = (searchedItems ?? data?.items ?? []).filter(
    (m) => !search || m.content.toLowerCase().includes(search.toLowerCase()),
  )
  const episodeItems = (episodeData?.items ?? []).filter((episode) =>
    !search || `${episode.content} ${episode.summary.title}`.toLowerCase().includes(search.toLowerCase()),
  )

  const columns: Column<MemoryEntry>[] = [
    {
      key: "content",
      header: "内容",
      className: "min-w-[280px] max-w-[480px]",
      render: (m) => <div className="text-sm leading-snug break-words">{truncate(m.content, 140)}</div>,
    },
    {
      key: "created",
      header: "创建时间",
      className: "whitespace-nowrap",
      render: (m) => <span className="text-xs text-athena-muted">{formatDateTime(m.created_at)}</span>,
    },
    {
      key: "access",
      header: "访问",
      className: "whitespace-nowrap",
      render: (m) => (
        <span className="text-xs text-athena-muted">
          {m.access_count} 次 · 最近访问 {formatDateTime(m.last_accessed_at)}
          {m.expires_at ? ` · ${formatDateTime(m.expires_at)} 到期` : ""}
          {m.valid_until ? ` · 事实有效至 ${formatDateTime(m.valid_until)}` : ""}
        </span>
      ),
    },
  ]

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1
  const episodeTotalPages = episodeData ? Math.max(1, Math.ceil(episodeData.total / PAGE_SIZE)) : 1

  const openMemory = async (memory: MemoryEntry) => {
    try {
      const [detail, history, related] = await Promise.all([apiClient.getMemory(memory.id), apiClient.getMemoryRevisions(memory.id), apiClient.getMemoryRelations(memory.id)])
      setSelected(detail)
      setRevisions(history)
      setRelations(related)
      setError(null)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

  const forgetMemory = async () => {
    if (!selected || deleting) return
    if (!window.confirm("遗忘这条记忆及同来源的任务回顾和派生经验？旧轮次不会重新生成，原始聊天记录仍保留。")) return
    setDeleting(true)
    try {
      await apiClient.deleteMemory(selected.id)
      setSearchedItems((current) => current?.filter((item) => item.id !== selected.id) ?? null)
      setSelected(null)
      setRevisions([])
      setRelations([])
      await reload()
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  const forgetEpisode = async () => {
    if (!selectedEpisode || deleting) return
    if (!window.confirm("遗忘这条任务回顾及其派生经验？相同任务来源不会再次生成回顾。原始会话和执行记录仍保留。")) return
    setDeleting(true)
    try {
      await apiClient.deleteMemoryEpisode(selectedEpisode.episode_id)
      setSelectedEpisode(null)
      await reloadEpisodes()
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(false)
    }
  }

  const fact = selected?.metadata.fact && typeof selected.metadata.fact === "object"
    ? selected.metadata.fact as Record<string, unknown>
    : null
  const sourceRefsValue = fact?.source_refs ?? selected?.metadata.source_refs
  const sourceRefs = Array.isArray(sourceRefsValue)
    ? sourceRefsValue.filter((source): source is Record<string, unknown> => Boolean(source) && typeof source === "object")
    : []

  return (
    <ViewShell
      side={
        <MemoryFilters
          search={search}
          onSearchChange={setSearch}
          expiredOnly={expiredOnly}
          onExpiredChange={setExpiredOnly}
          sessionId={sessionId}
          sessions={sessions}
          onSessionChange={(value) => { setSessionId(value); setPage(0); setSelectedEpisode(null); setSelectedRetrieval(null) }}
          onRefresh={() => { if (viewMode === "episodes") void reloadEpisodes(); else if (viewMode === "activity") void reloadActivity(); else void reload() }}
        />
      }
    >
      <Toolbar
        title="记忆管理"
        subtitle={viewMode === "memories"
          ? (data ? `共 ${data.total} 条事实与经验记忆` : "加载中…")
          : viewMode === "episodes"
            ? (episodeData ? `共 ${episodeData.total} 条任务回顾` : "加载中…")
            : "查看记忆写入队列和最近召回结果"}
      />
      <div className="p-6 space-y-6">
        <div className="flex gap-2 border-b border-athena-border pb-3">
          <button type="button" onClick={() => { setViewMode("memories"); setPage(0); setSelectedEpisode(null); setSelectedRetrieval(null) }} className={`rounded px-3 py-1.5 text-xs ${viewMode === "memories" ? "bg-athena-surface font-semibold" : "text-athena-muted"}`}>事实与经验</button>
          <button type="button" onClick={() => { setViewMode("episodes"); setPage(0); setSelected(null); setSelectedRetrieval(null) }} className={`rounded px-3 py-1.5 text-xs ${viewMode === "episodes" ? "bg-athena-surface font-semibold" : "text-athena-muted"}`}>任务回顾</button>
          <button type="button" onClick={() => { setViewMode("activity"); setSelected(null); setSelectedEpisode(null) }} className={`inline-flex items-center gap-1 rounded px-3 py-1.5 text-xs ${viewMode === "activity" ? "bg-athena-surface font-semibold" : "text-athena-muted"}`}><Activity className="h-3.5 w-3.5" />处理与召回</button>
        </div>
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            {viewMode === "memories" ? <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
              <StatCard label="记忆总数" value={data?.total ?? "—"} icon={Database} />
              <StatCard label="已过期" value={data?.expired ?? "—"} icon={Clock3} />
              <StatCard label="近 7 天新增" value={data?.recent_week ?? "—"} icon={TrendingUp} />
            </div> : null}

            {viewMode === "memories" ? <DataTable
              columns={columns}
              rows={items}
              rowKey={(m) => m.id}
              loading={loading}
              emptyText="暂无记忆"
              onRowClick={(memory) => void openMemory(memory)}
            /> : viewMode === "episodes" ? <DataTable
              columns={[
                { key: "title", header: "任务回顾", className: "min-w-[280px]", render: (episode) => <div><p className="text-sm font-medium">{truncate(episode.summary.title, 120)}</p><p className="mt-1 text-xs text-athena-muted">{truncate(episode.summary.goal, 160)}</p></div> },
                { key: "outcome", header: "结果", className: "whitespace-nowrap", render: (episode) => <span className="text-xs text-athena-muted">{episode.summary.outcome}</span> },
                { key: "created", header: "创建时间", className: "whitespace-nowrap", render: (episode) => <span className="text-xs text-athena-muted">{formatDateTime(episode.created_at)}</span> },
              ] as Column<MemoryEpisodeEntry>[]}
              rows={episodeItems}
              rowKey={(episode) => episode.episode_id}
              loading={episodesLoading}
              emptyText="暂无任务回顾"
              onRowClick={setSelectedEpisode}
            /> : null}

            {viewMode === "activity" ? <div className="grid gap-5 xl:grid-cols-2">
              <section className="card overflow-hidden">
                <div className="border-b border-athena-border px-4 py-3">
                  <h2 className="text-sm font-semibold">最近记忆处理</h2>
                  <p className="mt-1 text-xs text-athena-muted">只显示状态、尝试次数和排期，不展示原始对话正文。</p>
                </div>
                <div className="divide-y divide-athena-border/60">
                  {(jobData?.items ?? []).map((job) => <div key={job.turn_id} className="px-4 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <Badge tone={job.status === "succeeded" ? "success" : job.status === "failed" ? "warning" : "default"}>{job.status}</Badge>
                      <span className="text-[11px] text-athena-muted">尝试 {job.attempt} 次 · 更新于 {formatDateTime(job.updated_at)}</span>
                    </div>
                    <p className="mt-1 break-all font-mono text-[11px] text-athena-muted">{job.turn_id}</p>
                    {job.status === "queued" ? <p className="mt-1 text-xs text-athena-muted">下次处理：{formatDateTime(job.available_at)}</p> : null}
                  </div>)}
                  {!activityLoading && !jobData?.items.length ? <p className="px-4 py-8 text-center text-xs text-athena-muted">没有记忆处理记录。</p> : null}
                  {activityLoading && !jobData ? <p className="px-4 py-8 text-center text-xs text-athena-muted">正在读取处理状态…</p> : null}
                </div>
              </section>
              <section className="card overflow-hidden">
                <div className="border-b border-athena-border px-4 py-3">
                  <h2 className="text-sm font-semibold">最近记忆召回</h2>
                  <p className="mt-1 text-xs text-athena-muted">打开一条记录可查看候选是否进入结果或上下文，以及过滤原因。</p>
                </div>
                <div className="divide-y divide-athena-border/60">
                  {retrievalRuns.map((run) => <button key={run.run_id} type="button" onClick={() => void openRetrieval(run.run_id)} className={`block w-full px-4 py-3 text-left hover:bg-athena-surface/70 ${selectedRetrieval?.run_id === run.run_id ? "bg-athena-surface" : ""}`}>
                    <div className="flex items-center justify-between gap-3"><span className="text-xs font-medium">{truncate(run.query, 100)}</span><span className="shrink-0 text-[11px] text-athena-muted">{formatDateTime(run.created_at)}</span></div>
                    <p className="mt-1 text-[11px] text-athena-muted">{run.status} · 候选 {run.candidate_count} · 选中 {run.selected_count} · 上下文 {run.context_count ?? "未记录"}</p>
                  </button>)}
                  {!activityLoading && !retrievalRuns.length ? <p className="px-4 py-8 text-center text-xs text-athena-muted">没有记忆召回记录。</p> : null}
                </div>
              </section>
            </div> : null}

            {viewMode === "activity" && selectedRetrieval ? <section className="card p-5 space-y-4">
              <div>
                <h2 className="text-sm font-semibold">召回诊断</h2>
                <p className="mt-1 text-xs text-athena-muted">{selectedRetrieval.scope} · {selectedRetrieval.trace_status} · {selectedRetrieval.duration_ms ?? "—"} ms</p>
                <p className="mt-2 whitespace-pre-wrap text-sm">{selectedRetrieval.query}</p>
              </div>
              {selectedRetrieval.providers.length ? <div className="grid gap-2 md:grid-cols-2">
                {selectedRetrieval.providers.filter((provider) => provider.provider === "memory" || provider.provider === "episode").map((provider) => <div key={provider.provider} className="rounded border border-athena-border p-3 text-xs">
                  <p className="font-semibold">{provider.provider} · {provider.status}</p>
                  <p className="mt-1 text-athena-muted">候选 {provider.candidate_count} / 选中 {provider.selected_count} · {Math.round(provider.duration_ms)} ms</p>
                  {provider.channels.map((channel) => <p key={channel.channel} className="mt-1 text-athena-muted">{channel.channel}: {channel.status} · {channel.hit_count} 命中{channel.error_message ? ` · ${channel.error_message}` : ""}</p>)}
                </div>)}
              </div> : null}
              <div className="space-y-2">
                {selectedRetrieval.candidates.filter((candidate) => candidate.provider === "memory" || candidate.provider === "episode").map((candidate) => {
                  const reason = candidate.filter_reason ?? (candidate.context_selected ? "已进入上下文" : candidate.selection_stage ? `未进入下一阶段 · ${candidate.selection_stage}` : candidate.selected_for_result ? "召回结果中，未进入上下文" : "未选入召回结果")
                  return <div key={candidate.candidate_id} className="rounded border border-athena-border p-3">
                    <div className="flex flex-wrap items-center gap-2"><Badge tone={candidate.context_selected ? "success" : candidate.selected_for_result ? "accent" : "default"}>{reason}</Badge><span className="text-[11px] text-athena-muted">{candidate.provider} · {candidate.source_type} · 命中 {candidate.hits.length} 个通道</span></div>
                    <p className="mt-2 text-xs">{candidate.source_title || candidate.content_preview || candidate.source_id}</p>
                    <p className="mt-1 break-all font-mono text-[10px] text-athena-muted">{candidate.source_id}</p>
                  </div>
                })}
              </div>
            </section> : null}

            {viewMode === "memories" && selected ? <section className="card p-5 space-y-4">
              <div className="flex items-start justify-between gap-3"><div><h2 className="text-sm font-semibold">记忆详情</h2><p className="mt-1 text-xs text-athena-muted font-mono break-all">{selected.id}</p></div><div className="flex items-center gap-1"><button type="button" onClick={() => void forgetMemory()} disabled={deleting} className="btn-ghost px-2 py-1 text-xs text-athena-danger disabled:opacity-50"><Trash2 className="mr-1 h-3.5 w-3.5" />{deleting ? "处理中…" : "遗忘此记忆"}</button><button type="button" title="关闭详情" aria-label="关闭详情" onClick={() => setSelected(null)} className="btn-ghost p-1.5"><X className="w-4 h-4" /></button></div></div>
              <p className="whitespace-pre-wrap break-words text-sm">{selected.content}</p>
              {fact || selected.metadata.scope_kind ? <div className="rounded border border-athena-border p-3 text-xs"><h3 className="font-semibold">记忆类型与适用范围</h3><dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1"><dt className="text-athena-muted">类型</dt><dd>{String(selected.metadata.type ?? selected.metadata.category ?? "fact")}</dd><dt className="text-athena-muted">范围</dt><dd>{String(fact?.scope_kind ?? selected.metadata.scope_kind ?? "未知")} · {String(fact?.scope_id ?? selected.metadata.scope_id ?? "")}</dd>{fact ? <><dt className="text-athena-muted">主题</dt><dd>{String(fact.subject_key ?? "未标注")} / {String(fact.fact_key ?? "未标注")}</dd><dt className="text-athena-muted">有效性</dt><dd>{selected.validity_status}{selected.valid_until ? ` · 至 ${formatDateTime(selected.valid_until)}` : ""}</dd></> : null}</dl>{selected.metadata.type === "procedure" ? <p className="mt-2 text-athena-muted">可复用经验；请结合下方来源与验证边界使用。</p> : null}</div> : null}
              {sourceRefs.length ? <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">来源证据 · {sourceRefs.length}</h3><div className="space-y-2">{sourceRefs.map((source, index) => <div key={`${String(source.source_type)}-${String(source.source_id)}-${index}`} className="rounded border border-athena-border p-3"><p className="font-mono text-[11px] text-athena-muted">{String(source.source_type ?? "source")} · {String(source.source_id ?? "")}{source.observed_at ? ` · ${formatDateTime(String(source.observed_at))}` : ""}</p>{typeof source.excerpt === "string" ? <p className="mt-2 whitespace-pre-wrap break-words text-xs">{truncate(source.excerpt, 800)}</p> : null}</div>)}</div></div> : null}
              {relations.length ? <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">替代与冲突关系 · {relations.length}</h3><div className="space-y-2">{relations.map((relation) => <div key={relation.id} className="rounded border border-athena-border p-3"><p className="text-[11px] text-athena-muted">{relation.kind === "contradicts" ? "矛盾" : "替代"} · {relation.related_status} · {formatDateTime(relation.created_at)}</p><p className="mt-1 font-mono text-[11px] text-athena-muted">{relation.related_memory_id}</p><p className="mt-1 text-xs">{truncate(relation.related_content, 500)}</p></div>)}</div></div> : <p className="text-xs text-athena-muted">暂无替代或冲突关系。</p>}
              <details><summary className="cursor-pointer text-xs text-athena-muted">Metadata</summary><pre className="mt-2 max-h-48 overflow-auto rounded bg-athena-bg p-3 text-xs">{JSON.stringify(selected.metadata, null, 2)}</pre></details>
              <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">修订历史 · {revisions.length}</h3><div className="space-y-2">{revisions.map((revision, index) => <div key={`${revision.id}-${index}`} className="border-l-2 border-athena-border pl-3"><p className="text-xs text-athena-muted">{formatDateTime(revision.created_at)}</p><p className="mt-1 text-sm break-words">{truncate(revision.content, 240)}</p></div>)}</div></div>
            </section> : null}

            {viewMode === "episodes" && selectedEpisode ? <section className="card p-5 space-y-4">
              <div className="flex items-start justify-between gap-3"><div><h2 className="text-sm font-semibold">任务回顾详情</h2><p className="mt-1 font-mono text-xs text-athena-muted break-all">{selectedEpisode.episode_id}</p></div><div className="flex gap-1"><button type="button" onClick={() => void forgetEpisode()} disabled={deleting} className="btn-ghost px-2 py-1 text-xs text-athena-danger disabled:opacity-50"><Trash2 className="mr-1 h-3.5 w-3.5" />{deleting ? "处理中…" : "遗忘此回顾"}</button><button type="button" title="关闭详情" aria-label="关闭详情" onClick={() => setSelectedEpisode(null)} className="btn-ghost p-1.5"><X className="h-4 w-4" /></button></div></div>
              <div className="grid gap-3 text-sm"><div><h3 className="text-xs font-semibold text-athena-muted">目标 · {selectedEpisode.summary.outcome}</h3><p className="mt-1 whitespace-pre-wrap">{selectedEpisode.summary.goal}</p></div><div><h3 className="text-xs font-semibold text-athena-muted">结果</h3><p className="mt-1 whitespace-pre-wrap">{selectedEpisode.summary.result}</p></div>{selectedEpisode.summary.attempts ? <div><h3 className="text-xs font-semibold text-athena-muted">关键尝试</h3><p className="mt-1 whitespace-pre-wrap">{selectedEpisode.summary.attempts}</p></div> : null}{selectedEpisode.summary.verification_scope ? <div><h3 className="text-xs font-semibold text-athena-muted">验证范围</h3><p className="mt-1 whitespace-pre-wrap">{selectedEpisode.summary.verification_scope}</p></div> : <p className="text-xs text-athena-muted">没有记录可确认的验证范围。</p>}</div>
              {selectedEpisode.summary.reusable_learnings.length ? <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">可复用经验</h3><ul className="list-disc space-y-1 pl-5 text-sm">{selectedEpisode.summary.reusable_learnings.map((learning, index) => <li key={index}>{learning}</li>)}</ul></div> : null}
              {selectedEpisode.summary.unresolved.length ? <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">未解决事项</h3><ul className="list-disc space-y-1 pl-5 text-sm">{selectedEpisode.summary.unresolved.map((item, index) => <li key={index}>{item}</li>)}</ul></div> : null}
              <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">来源引用 · {selectedEpisode.source_refs.length}</h3><div className="space-y-2">{selectedEpisode.source_refs.map((source, index) => <div key={`${source.source_type}-${source.source_id}-${index}`} className="rounded border border-athena-border p-3"><p className="font-mono text-[11px] text-athena-muted">{source.source_type} · {source.source_id} · {formatDateTime(source.observed_at)}</p><p className="mt-2 whitespace-pre-wrap break-words text-xs">{truncate(source.excerpt, 800)}</p></div>)}</div></div>
            </section> : null}

            {viewMode !== "activity" ? <div className="flex items-center justify-between">
              <span className="text-xs text-athena-muted">
                第 {page + 1} / {viewMode === "memories" ? totalPages : episodeTotalPages} 页
              </span>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(0, p - 1))}
                  disabled={page === 0}
                  className="btn-ghost px-2 py-1 text-xs"
                >
                  <ChevronLeft className="w-4 h-4" /> 上一页
                </button>
                <button
                  onClick={() => setPage((p) => Math.min((viewMode === "memories" ? totalPages : episodeTotalPages) - 1, p + 1))}
                  disabled={page >= (viewMode === "memories" ? totalPages : episodeTotalPages) - 1}
                  className="btn-ghost px-2 py-1 text-xs"
                >
                  下一页 <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div> : null}

            {viewMode === "memories" && data && data.expired > 0 ? (
              <div className="flex items-center gap-2 text-xs text-athena-muted">
                <Badge tone="warning">提示</Badge>
                有 {data.expired} 条记忆已过期，可在侧栏勾选「仅已过期」查看。
              </div>
            ) : null}
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default MemoryView
