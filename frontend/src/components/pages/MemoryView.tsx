import { useCallback, useEffect, useState } from "react"
import {
  Database,
  Clock3,
  TrendingUp,
  ChevronLeft,
  ChevronRight,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { MemoryEntry, MemoryListResponse } from "../../types"
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

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.listMemories({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        expired: expiredOnly,
      })
      setData(res)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [page, expiredOnly])

  useEffect(() => {
    reload()
  }, [reload])

  const items = (data?.items ?? []).filter(
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
          {m.access_count} 次 · {formatDateTime(m.expires_at)} 到期
        </span>
      ),
    },
  ]

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1

  return (
    <ViewShell
      side={
        <MemoryFilters
          search={search}
          onSearchChange={setSearch}
          expiredOnly={expiredOnly}
          onExpiredChange={setExpiredOnly}
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
            />

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
