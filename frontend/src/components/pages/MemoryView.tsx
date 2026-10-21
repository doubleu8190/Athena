import { useCallback, useEffect, useState } from "react"
import {
  Database,
  Clock3,
  TrendingUp,
  ChevronLeft,
  ChevronRight,
  X,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { MemoryEntry, MemoryListResponse } from "../../types"
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [expiredOnly, setExpiredOnly] = useState(false)
  const [page, setPage] = useState(0)
  const [sessionId, setSessionId] = useState("")
  const [selected, setSelected] = useState<MemoryEntry | null>(null)
  const [revisions, setRevisions] = useState<MemoryEntry[]>([])
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

  const [searchedItems, setSearchedItems] = useState<MemoryEntry[] | null>(null)
  useEffect(() => {
    if (!search.trim()) { setSearchedItems(null); return }
    const timer = window.setTimeout(() => {
      void apiClient.searchMemory(search.trim(), 50).then(setSearchedItems).catch((e) => setError(e instanceof Error ? e.message : String(e)))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [search])

  const items = (searchedItems ?? data?.items ?? []).filter(
    (m) => !search || m.content.toLowerCase().includes(search.toLowerCase()),
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

  const openMemory = async (memory: MemoryEntry) => {
    try {
      const [detail, history] = await Promise.all([apiClient.getMemory(memory.id), apiClient.getMemoryRevisions(memory.id)])
      setSelected(detail)
      setRevisions(history)
      setError(null)
    } catch (e) { setError(e instanceof Error ? e.message : String(e)) }
  }

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
          onSessionChange={(value) => { setSessionId(value); setPage(0) }}
          onRefresh={reload}
        />
      }
    >
      <Toolbar
        title="记忆管理"
        subtitle={data ? `共 ${data.total} 条记忆` : "加载中…"}
      />
      <div className="p-6 space-y-6">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
              <StatCard label="记忆总数" value={data?.total ?? "—"} icon={Database} />
              <StatCard label="已过期" value={data?.expired ?? "—"} icon={Clock3} />
              <StatCard label="近 7 天新增" value={data?.recent_week ?? "—"} icon={TrendingUp} />
            </div>

            <DataTable
              columns={columns}
              rows={items}
              rowKey={(m) => m.id}
              loading={loading}
              emptyText="暂无记忆"
              onRowClick={(memory) => void openMemory(memory)}
            />

            {selected ? <section className="card p-5 space-y-4">
              <div className="flex items-start justify-between gap-3"><div><h2 className="text-sm font-semibold">记忆详情</h2><p className="mt-1 text-xs text-athena-muted font-mono break-all">{selected.id}</p></div><button type="button" title="关闭详情" aria-label="关闭详情" onClick={() => setSelected(null)} className="btn-ghost p-1.5"><X className="w-4 h-4" /></button></div>
              <p className="whitespace-pre-wrap break-words text-sm">{selected.content}</p>
              <details><summary className="cursor-pointer text-xs text-athena-muted">Metadata</summary><pre className="mt-2 max-h-48 overflow-auto rounded bg-athena-bg p-3 text-xs">{JSON.stringify(selected.metadata, null, 2)}</pre></details>
              <div><h3 className="mb-2 text-xs font-semibold text-athena-muted">修订历史 · {revisions.length}</h3><div className="space-y-2">{revisions.map((revision, index) => <div key={`${revision.id}-${index}`} className="border-l-2 border-athena-border pl-3"><p className="text-xs text-athena-muted">{formatDateTime(revision.created_at)}</p><p className="mt-1 text-sm break-words">{truncate(revision.content, 240)}</p></div>)}</div></div>
            </section> : null}

            <div className="flex items-center justify-between">
              <span className="text-xs text-athena-muted">
                第 {page + 1} / {totalPages} 页
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
                  onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                  disabled={page >= totalPages - 1}
                  className="btn-ghost px-2 py-1 text-xs"
                >
                  下一页 <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>

            {data && data.expired > 0 ? (
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
