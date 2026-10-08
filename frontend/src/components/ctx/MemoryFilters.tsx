import { RefreshCw } from "lucide-react"
import SearchInput from "../ui/SearchInput"
import FilterRow from "../ui/FilterRow"

interface MemoryFiltersProps {
  search: string
  onSearchChange: (value: string) => void
  expiredOnly: boolean
  onExpiredChange: (value: boolean) => void
  sessionId: string
  sessions: Array<{ id: string; title: string }>
  onSessionChange: (value: string) => void
  onRefresh: () => void
}

function MemoryFilters({
  search,
  onSearchChange,
  expiredOnly,
  onExpiredChange,
  sessionId,
  sessions,
  onSessionChange,
  onRefresh,
}: MemoryFiltersProps) {
  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">记忆筛选</h2>
        <button
          onClick={onRefresh}
          title="刷新"
          className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      <SearchInput
        value={search}
        onChange={onSearchChange}
        placeholder="搜索记忆内容…"
      />
      <div className="space-y-2">
        <FilterRow label="仅已过期" checked={expiredOnly} onChange={onExpiredChange} />
      </div>
      <label className="block space-y-1 text-xs text-athena-muted">
        <span>会话</span>
        <select value={sessionId} onChange={(event) => onSessionChange(event.target.value)} className="input px-2 py-2 text-xs">
          <option value="">全部会话</option>
          {sessions.map((session) => <option key={session.id} value={session.id}>{session.title}</option>)}
        </select>
      </label>
    </div>
  )
}

export default MemoryFilters
