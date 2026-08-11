import { RefreshCw } from "lucide-react"

type DecisionFilter = "all" | "approved" | "denied" | "timeout"

interface ApprovalFiltersProps {
  decision: DecisionFilter
  onDecisionChange: (value: DecisionFilter) => void
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

function ApprovalFilters({
  decision,
  onDecisionChange,
  onRefresh,
}: ApprovalFiltersProps) {
  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">日志筛选</h2>
        <button
          onClick={onRefresh}
          title="刷新"
          className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      <div className="space-y-1.5">
        <div className="text-xs text-athena-muted">决策</div>
        <Segmented<DecisionFilter>
          options={[
            { value: "all", label: "全部" },
            { value: "approved", label: "通过" },
            { value: "denied", label: "拒绝" },
            { value: "timeout", label: "超时" },
          ]}
          value={decision}
          onChange={onDecisionChange}
        />
      </div>
    </div>
  )
}

export default ApprovalFilters
export type { DecisionFilter }
