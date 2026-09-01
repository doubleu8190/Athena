import { RefreshCw } from "lucide-react"
import SearchInput from "../ui/SearchInput"

type EnabledFilter = "all" | "enabled" | "disabled"
type RiskFilter = "all" | "low" | "medium" | "high"

interface ToolFiltersProps {
  search: string
  onSearchChange: (value: string) => void
  enabled: EnabledFilter
  onEnabledChange: (value: EnabledFilter) => void
  risk: RiskFilter
  onRiskChange: (value: RiskFilter) => void
  onRefresh: () => void
}

function Segmented<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="flex rounded-lg bg-athena-bg border border-athena-border p-0.5">
      {options.map((opt) => (
        <button
          key={opt.value}
          onClick={() => onChange(opt.value)}
          className={`flex-1 px-2 py-1 rounded-md text-xs font-medium transition-colors ${
            value === opt.value
              ? "bg-athena-accent/20 text-athena-accent"
              : "text-athena-muted hover:text-athena-text"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  )
}

function ToolFilters({
  search,
  onSearchChange,
  enabled,
  onEnabledChange,
  risk,
  onRiskChange,
  onRefresh,
}: ToolFiltersProps) {
  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">工具筛选</h2>
        <button
          onClick={onRefresh}
          title="刷新"
          className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      <SearchInput value={search} onChange={onSearchChange} placeholder="搜索工具…" />
      <div className="space-y-1.5">
        <div className="text-xs text-athena-muted">启用状态</div>
        <Segmented<EnabledFilter>
          options={[
            { value: "all", label: "全部" },
            { value: "enabled", label: "启用" },
            { value: "disabled", label: "停用" },
          ]}
          value={enabled}
          onChange={onEnabledChange}
        />
      </div>
      <div className="space-y-1.5">
        <div className="text-xs text-athena-muted">风险等级</div>
        <Segmented<RiskFilter>
          options={[
            { value: "all", label: "全部" },
            { value: "low", label: "低" },
            { value: "medium", label: "中" },
            { value: "high", label: "高" },
          ]}
          value={risk}
          onChange={onRiskChange}
        />
      </div>
    </div>
  )
}

export default ToolFilters
export type { EnabledFilter, RiskFilter }
