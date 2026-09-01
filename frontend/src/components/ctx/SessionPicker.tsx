import { useEffect, useState } from "react"
import { RefreshCw } from "lucide-react"
import type { Session } from "../../types"
import { apiClient } from "../../api/client"
import SearchInput from "../ui/SearchInput"

interface SessionPickerProps {
  sessionId: string | null
  onSelect: (sessionId: string) => void
}

/**
 * 会话详情页的会话选择器 — 自取会话列表，不依赖 chat store.
 */
function SessionPicker({ sessionId, onSelect }: SessionPickerProps) {
  const [sessions, setSessions] = useState<Session[]>([])
  const [search, setSearch] = useState("")
  const [loading, setLoading] = useState(true)

  const load = async () => {
    setLoading(true)
    try {
      setSessions(await apiClient.listSessions())
    } catch {
      setSessions([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const filtered = sessions.filter(
    (s) => !search || s.title.toLowerCase().includes(search.toLowerCase()),
  )

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">选择会话</h2>
        <button
          onClick={load}
          title="刷新"
          className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      <SearchInput value={search} onChange={setSearch} placeholder="搜索会话…" />
      <div className="space-y-1">
        {loading ? (
          <div className="text-xs text-athena-muted">加载中…</div>
        ) : filtered.length === 0 ? (
          <div className="text-xs text-athena-muted">暂无会话</div>
        ) : (
          filtered.map((s) => {
            const active = sessionId === s.id
            return (
              <button
                key={s.id}
                onClick={() => onSelect(s.id)}
                className={`w-full text-left px-3 py-2 rounded-lg transition-colors ${
                  active
                    ? "bg-athena-accent/15 border border-athena-accent/40"
                    : "border border-transparent hover:bg-athena-border/40"
                }`}
              >
                <div className="text-sm font-medium truncate">{s.title}</div>
                <div className="text-xs text-athena-muted mt-0.5">
                  {new Date(s.updated_at).toLocaleString()}
                </div>
              </button>
            )
          })
        )}
      </div>
    </div>
  )
}

export default SessionPicker
