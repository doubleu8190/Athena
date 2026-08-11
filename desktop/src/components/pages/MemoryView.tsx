import { useCallback, useEffect, useState } from "react"
import {
  Database,
  Pin,
  Clock3,
  TrendingUp,
  Pencil,
  Trash2,
  Check,
  X,
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
import Toggle from "../ui/Toggle"
import DataTable from "../ui/DataTable"
import type { Column } from "../ui/DataTable"

const PAGE_SIZE = 15

function MemoryView() {
  const [data, setData] = useState<MemoryListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [pinnedOnly, setPinnedOnly] = useState(false)
  const [expiredOnly, setExpiredOnly] = useState(false)
  const [page, setPage] = useState(0)
  const [editing, setEditing] = useState<{ id: string; content: string } | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const res = await apiClient.listMemories({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        pinned: pinnedOnly,
        expired: expiredOnly,
      })
      setData(res)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [page, pinnedOnly, expiredOnly])

  useEffect(() => {
    reload()
  }, [reload])

  const items = (data?.items ?? []).filter(
    (m) => !search || m.content.toLowerCase().includes(search.toLowerCase()),
  )

  const handleTogglePin = async (m: MemoryEntry) => {
    try {
      await apiClient.setMemoryPinned(m.id, !m.pinned)
      reload()
    } catch (e) {
      console.error("pin failed", e)
    }
  }

  const handleDelete = async (m: MemoryEntry) => {
    if (!window.confirm("删除这条记忆？此操作不可撤销。")) return
    try {
      await apiClient.deleteMemory(m.id)
      reload()
    } catch (e) {
      console.error("delete failed", e)
    }
  }

  const handleSaveEdit = async () => {
    if (!editing) return
    try {
      await apiClient.updateMemory(editing.id, editing.content)
      setEditing(null)
      reload()
    } catch (e) {
      console.error("edit failed", e)
    }
  }

  const columns: Column<MemoryEntry>[] = [
    {
      key: "content",
      header: "内容",
      className: "min-w-[280px] max-w-[480px]",
      render: (m) =>
        editing?.id === m.id ? (
          <div className="space-y-2">
            <textarea
              value={editing.content}
              onChange={(e) => setEditing({ id: m.id, content: e.target.value })}
              className="textarea text-sm"
              rows={3}
            />
            <div className="flex gap-2">
              <button onClick={handleSaveEdit} className="btn-primary px-2 py-1 text-xs">
                <Check className="w-3 h-3" /> 保存
              </button>
              <button onClick={() => setEditing(null)} className="btn-ghost px-2 py-1 text-xs">
                <X className="w-3 h-3" /> 取消
              </button>
            </div>
          </div>
        ) : (
          <div className="text-sm leading-snug break-words">{truncate(m.content, 140)}</div>
        ),
    },
    {
      key: "pinned",
      header: "固定",
      render: (m) => (
        <Toggle checked={m.pinned} onChange={() => handleTogglePin(m)} label="固定记忆" />
      ),
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
          {m.access_count} 次{m.pinned ? "" : ` · ${formatDateTime(m.expires_at)} 到期`}
        </span>
      ),
    },
    {
      key: "actions",
      header: "操作",
      className: "whitespace-nowrap",
      render: (m) => (
        <div className="flex items-center gap-1">
          <button
            onClick={() => setEditing({ id: m.id, content: m.content })}
            className="p-1.5 rounded-md text-athena-muted hover:text-athena-text hover:bg-athena-border/40"
            title="编辑"
          >
            <Pencil className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => handleDelete(m)}
            className="p-1.5 rounded-md text-athena-muted hover:text-athena-danger hover:bg-athena-danger/10"
            title="删除"
          >
            <Trash2 className="w-3.5 h-3.5" />
          </button>
        </div>
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
          pinnedOnly={pinnedOnly}
          onPinnedChange={setPinnedOnly}
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
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="记忆总数" value={data?.total ?? "—"} icon={Database} />
              <StatCard label="固定" value={data?.pinned ?? "—"} icon={Pin} />
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
                有 {data.expired} 条记忆已过期（未固定）。可在侧栏勾选「仅已过期」查看。
              </div>
            ) : null}
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default MemoryView
