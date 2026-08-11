import { RefreshCw } from "lucide-react"
import SearchInput from "../ui/SearchInput"
import FilterRow from "../ui/FilterRow"

interface MemoryFiltersProps {
  search: string
  onSearchChange: (value: string) => void
  pinnedOnly: boolean
  onPinnedChange: (value: boolean) => void
  expiredOnly: boolean
  onExpiredChange: (value: boolean) => void
  onRefresh: () => void
}

function MemoryFilters({
  search,
  onSearchChange,
  pinnedOnly,
  onPinnedChange,
  expiredOnly,
  onExpiredChange,
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
        <FilterRow label="仅固定" checked={pinnedOnly} onChange={onPinnedChange} />
        <FilterRow label="仅已过期" checked={expiredOnly} onChange={onExpiredChange} />
      </div>
    </div>
  )
}

export default MemoryFilters
