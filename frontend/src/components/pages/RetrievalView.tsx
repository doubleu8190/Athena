import { useCallback, useEffect, useMemo, useState } from "react"
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Clock3,
  Database,
  FileSearch,
  GitBranch,
  Loader2,
  Search,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type {
  RetrievalCandidate,
  RetrievalRunDetail,
  RetrievalRunListResponse,
  RetrievalRunSummary,
} from "../../types"
import { formatDateTime, truncate } from "../../utils/format"
import ViewShell from "./ViewShell"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import StatCard from "../ui/StatCard"

const PAGE_SIZE = 20
type CandidateFilter = "all" | "injected" | "selected" | "filtered"

const SCOPE_LABELS: Record<string, string> = {
  memory: "记忆",
  knowledge: "知识库",
  file: "附件",
}

function statusLabel(status: string): string {
  return {
    running: "运行中",
    succeeded: "成功",
    partial: "部分成功",
    failed: "失败",
    timeout: "超时",
    cancelled: "已取消",
  }[status] ?? status
}

function statusTone(status: string): "default" | "success" | "warning" | "danger" | "accent" {
  if (status === "succeeded") return "success"
  if (status === "partial" || status === "timeout") return "warning"
  if (status === "failed" || status === "cancelled") return "danger"
  if (status === "running") return "accent"
  return "default"
}

function scopeIcon(scope: string) {
  if (scope === "memory") return <Database className="w-3.5 h-3.5" />
  if (scope === "file") return <FileSearch className="w-3.5 h-3.5" />
  return <GitBranch className="w-3.5 h-3.5" />
}

function formatScore(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(4)
}

function candidateState(candidate: RetrievalCandidate): string {
  if (candidate.injected_into_context) return "已注入"
  if (candidate.selected_for_result) return "已选中"
  if (candidate.filter_reason) return "已过滤"
  return "候选"
}

function candidateStateTone(candidate: RetrievalCandidate): "default" | "success" | "warning" | "danger" | "accent" {
  if (candidate.injected_into_context) return "success"
  if (candidate.selected_for_result) return "accent"
  if (candidate.filter_reason) return "warning"
  return "default"
}

function RunListItem({
  run,
  selected,
  onClick,
}: {
  run: RetrievalRunSummary
  selected: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full text-left rounded-lg border px-3 py-3 transition-colors ${
        selected
          ? "border-athena-accent/60 bg-athena-accent/10"
          : "border-transparent hover:border-athena-border hover:bg-athena-border/20"
      }`}
    >
      <div className="flex items-start gap-2">
        <span className="mt-0.5 text-athena-accent">{scopeIcon(run.scope)}</span>
        <div className="min-w-0 flex-1">
          <div className="text-sm leading-snug break-words">{truncate(run.query, 90)}</div>
          <div className="flex items-center gap-2 mt-1.5">
            <span className="text-[11px] text-athena-muted">{SCOPE_LABELS[run.scope] ?? run.scope}</span>
            <Badge tone={statusTone(run.status)}>{statusLabel(run.status)}</Badge>
          </div>
          <div className="text-[11px] text-athena-muted mt-1.5 flex items-center gap-2">
            <span>{run.candidate_count} 候选</span>
            <span>·</span>
            <span>{run.injected_count} 注入</span>
            <span className="ml-auto">{formatDateTime(run.created_at)}</span>
          </div>
        </div>
      </div>
    </button>
  )
}

function CandidateTable({ candidates }: { candidates: RetrievalCandidate[] }) {
  const [expanded, setExpanded] = useState<string | null>(null)

  return (
    <div className="card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-athena-border bg-athena-bg/50 text-left">
              <th className="px-4 py-2.5 text-xs text-athena-muted">阶段</th>
              <th className="px-4 py-2.5 text-xs text-athena-muted">来源</th>
              <th className="px-4 py-2.5 text-xs text-athena-muted">候选</th>
              <th className="px-4 py-2.5 text-xs text-athena-muted">Native</th>
              <th className="px-4 py-2.5 text-xs text-athena-muted">Fusion</th>
              <th className="px-4 py-2.5 text-xs text-athena-muted">状态</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((candidate) => {
              const isExpanded = expanded === candidate.candidate_id
              return (
                <tr key={candidate.candidate_id} className="border-b border-athena-border/50 last:border-b-0">
                  <td colSpan={6} className="p-0">
                    <button
                      type="button"
                      className="w-full text-left grid grid-cols-[100px_110px_minmax(180px,1fr)_100px_100px_100px] items-center hover:bg-athena-bg/40"
                      onClick={() => setExpanded(isExpanded ? null : candidate.candidate_id)}
                    >
                      <span className="px-4 py-2.5 text-xs font-mono">{candidate.stage}</span>
                      <span className="px-4 py-2.5 text-xs text-athena-muted">{candidate.provider}</span>
                      <span className="px-4 py-2.5 min-w-0">
                        <span className="block font-mono text-xs truncate">{candidate.source_id}</span>
                        <span className="block text-[11px] text-athena-muted truncate">
                          {candidate.source_title ?? candidate.source_type}
                        </span>
                      </span>
                      <span className="px-4 py-2.5 text-xs text-athena-muted">
                        {candidate.native_rank ?? "—"} / {formatScore(candidate.native_score)}
                      </span>
                      <span className="px-4 py-2.5 text-xs text-athena-muted">
                        {candidate.fused_rank ?? "—"} / {formatScore(candidate.fused_score)}
                      </span>
                      <span className="px-4 py-2.5">
                        <Badge tone={candidateStateTone(candidate)}>{candidateState(candidate)}</Badge>
                      </span>
                    </button>
                    {isExpanded ? (
                      <div className="px-4 pb-4 pt-1 bg-athena-bg/30 space-y-2 text-xs">
                        <div className="text-athena-text whitespace-pre-wrap break-words">
                          {candidate.content_preview || "没有内容预览"}
                        </div>
                        <div className="flex flex-wrap gap-x-4 gap-y-1 text-athena-muted">
                          <span>source_id: {candidate.source_id}</span>
                          {candidate.revision_id ? <span>revision: {candidate.revision_id}</span> : null}
                          {candidate.document_version_id ? <span>version: {candidate.document_version_id}</span> : null}
                          {candidate.filter_reason ? <span className="text-athena-warning">原因: {candidate.filter_reason}</span> : null}
                          {candidate.locator && Object.keys(candidate.locator).length > 0 ? (
                            <span>定位: {JSON.stringify(candidate.locator)}</span>
                          ) : null}
                        </div>
                      </div>
                    ) : null}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function RetrievalDetail({ detail }: { detail: RetrievalRunDetail }) {
  const [filter, setFilter] = useState<CandidateFilter>("all")
  const filteredCandidates = useMemo(() => {
    if (filter === "injected") return detail.candidates.filter((item) => item.injected_into_context)
    if (filter === "selected") return detail.candidates.filter((item) => item.selected_for_result && !item.injected_into_context)
    if (filter === "filtered") return detail.candidates.filter((item) => Boolean(item.filter_reason))
    return detail.candidates
  }, [detail.candidates, filter])

  return (
    <>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="候选数" value={detail.candidate_count} icon={GitBranch} />
        <StatCard label="选中数" value={detail.selected_count} icon={CheckCircle2} />
        <StatCard label="注入数" value={detail.injected_count} icon={Database} />
        <StatCard label="耗时" value={detail.duration_ms == null ? "—" : `${Math.round(detail.duration_ms)} ms`} icon={Clock3} />
      </div>

      <div className="card p-5 space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="text-xs text-athena-muted mb-1">查询</div>
            <div className="text-base break-words">{detail.query}</div>
          </div>
          <Badge tone={statusTone(detail.status)}>{statusLabel(detail.status)}</Badge>
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 text-xs">
          <div><div className="text-athena-muted">范围</div><div className="mt-1">{SCOPE_LABELS[detail.scope] ?? detail.scope}</div></div>
          <div><div className="text-athena-muted">索引版本</div><div className="mt-1 font-mono break-all">{detail.index_generation ?? "—"}</div></div>
          <div><div className="text-athena-muted">创建时间</div><div className="mt-1">{formatDateTime(detail.created_at)}</div></div>
          <div><div className="text-athena-muted">会话</div><div className="mt-1 font-mono break-all">{detail.session_id ?? "—"}</div></div>
        </div>
        {detail.error_message ? (
          <div className="rounded-lg bg-athena-danger/10 border border-athena-danger/30 px-3 py-2 text-xs text-athena-danger">
            {detail.error_message}
          </div>
        ) : null}
        <details className="text-xs text-athena-muted">
          <summary className="cursor-pointer hover:text-athena-text">查看检索配置</summary>
          <pre className="mt-2 rounded-lg bg-athena-bg p-3 overflow-x-auto text-[11px] text-athena-text">
            {JSON.stringify(detail.config, null, 2)}
          </pre>
        </details>
      </div>

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">召回候选</h2>
            <p className="text-xs text-athena-muted mt-0.5">展开候选查看内容预览、定位和淘汰原因</p>
          </div>
          <div className="flex flex-wrap gap-1">
            {(["all", "injected", "selected", "filtered"] as CandidateFilter[]).map((value) => (
              <button
                type="button"
                key={value}
                onClick={() => setFilter(value)}
                className={`px-2.5 py-1 rounded-md text-xs transition-colors ${filter === value ? "bg-athena-accent/15 text-athena-accent" : "text-athena-muted hover:bg-athena-surface"}`}
              >
                {{ all: "全部", injected: "已注入", selected: "已选中", filtered: "已过滤" }[value]}
              </button>
            ))}
          </div>
        </div>
        <CandidateTable candidates={filteredCandidates} />
      </div>
    </>
  )
}

function RetrievalView() {
  const [data, setData] = useState<RetrievalRunListResponse | null>(null)
  const [detail, setDetail] = useState<RetrievalRunDetail | null>(null)
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [scope, setScope] = useState("")
  const [status, setStatus] = useState("")
  const [page, setPage] = useState(0)
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadRuns = useCallback(async () => {
    setLoading(true)
    try {
      const result = await apiClient.listRetrievalRuns({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        q: search.trim() || undefined,
        scope: scope || undefined,
        status: status || undefined,
      })
      setData(result)
      setError(null)
      if (result.items.length === 0) {
        setSelectedRunId(null)
        setDetail(null)
      } else if (!selectedRunId || !result.items.some((item) => item.run_id === selectedRunId)) {
        setSelectedRunId(result.items[0].run_id)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [page, search, scope, status, selectedRunId])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  useEffect(() => {
    if (!selectedRunId) return
    setDetailLoading(true)
    apiClient.getRetrievalRun(selectedRunId)
      .then(setDetail)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setDetailLoading(false))
  }, [selectedRunId])

  const items = data?.items ?? []
  const hasPrevious = page > 0
  const hasNext = (page + 1) * PAGE_SIZE < (data?.total ?? 0)

  return (
    <ViewShell
      side={
        <div className="p-4 space-y-4">
          <div>
            <h2 className="font-semibold">召回分析</h2>
            <p className="text-xs text-athena-muted mt-1">查看每次召回的完整处理过程</p>
          </div>
          <div className="relative">
            <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-athena-muted pointer-events-none" />
            <input value={search} onChange={(event) => { setSearch(event.target.value); setPage(0) }} placeholder="搜索查询…" className="input pl-9 py-1.5 text-sm" />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <select value={scope} onChange={(event) => { setScope(event.target.value); setPage(0) }} className="input px-2 py-1.5 text-xs">
              <option value="">全部范围</option>
              <option value="memory">记忆</option>
              <option value="knowledge">知识库</option>
              <option value="file">附件</option>
            </select>
            <select value={status} onChange={(event) => { setStatus(event.target.value); setPage(0) }} className="input px-2 py-1.5 text-xs">
              <option value="">全部状态</option>
              <option value="succeeded">成功</option>
              <option value="partial">部分成功</option>
              <option value="failed">失败</option>
              <option value="timeout">超时</option>
              <option value="running">运行中</option>
            </select>
          </div>
          <div className="space-y-1 overflow-y-auto max-h-[calc(100vh-250px)]">
            {loading ? (
              <div className="py-8 text-center text-athena-muted"><Loader2 className="w-5 h-5 animate-spin inline-block" /></div>
            ) : items.length === 0 ? (
              <div className="py-8 text-center text-xs text-athena-muted">暂无召回记录</div>
            ) : items.map((run) => (
              <RunListItem key={run.run_id} run={run} selected={run.run_id === selectedRunId} onClick={() => setSelectedRunId(run.run_id)} />
            ))}
          </div>
          <div className="flex items-center justify-between border-t border-athena-border pt-3">
            <span className="text-[11px] text-athena-muted">共 {data?.total ?? 0} 条</span>
            <div className="flex gap-1">
              <button type="button" onClick={() => setPage((value) => Math.max(0, value - 1))} disabled={!hasPrevious} className="btn-ghost p-1.5" title="上一页"><ChevronLeft className="w-4 h-4" /></button>
              <button type="button" onClick={() => setPage((value) => value + 1)} disabled={!hasNext} className="btn-ghost p-1.5" title="下一页"><ChevronRight className="w-4 h-4" /></button>
            </div>
          </div>
        </div>
      }
    >
      <Toolbar title="召回分析" subtitle="检查向量、关键词、融合和上下文注入结果" />
      <div className="p-6 space-y-6">
        {error ? <div className="text-sm text-athena-danger">加载失败：{error}</div> : null}
        {detailLoading ? (
          <div className="py-20 text-center text-athena-muted"><Loader2 className="w-6 h-6 animate-spin inline-block" /></div>
        ) : detail ? (
          <RetrievalDetail detail={detail} />
        ) : (
          <div className="py-20 text-center text-athena-muted">
            <CircleDot className="w-10 h-10 mx-auto mb-3 opacity-50" />
            <p className="text-sm">选择一条召回记录查看详情</p>
          </div>
        )}
      </div>
    </ViewShell>
  )
}

export default RetrievalView
