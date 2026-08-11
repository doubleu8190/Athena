import { RefreshCw } from "lucide-react"
import type { ProviderInfo } from "../../types"

interface ProviderQuickListProps {
  providers: ProviderInfo[]
  selectedName: string | null
  onSelect: (name: string) => void
  onRefresh: () => void
  loading?: boolean
}

function ProviderQuickList({
  providers,
  selectedName,
  onSelect,
  onRefresh,
  loading,
}: ProviderQuickListProps) {
  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">提供商</h2>
        <button
          onClick={onRefresh}
          title="刷新"
          className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 transition-colors"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
      </div>
      {loading ? (
        <div className="text-xs text-athena-muted">加载中…</div>
      ) : providers.length === 0 ? (
        <div className="text-xs text-athena-muted">暂无提供商</div>
      ) : (
        <div className="space-y-1">
          {providers.map((p) => {
            const active = selectedName === p.name
            return (
              <button
                key={p.name}
                onClick={() => onSelect(p.name)}
                className={`w-full text-left px-3 py-2 rounded-lg transition-colors ${
                  active
                    ? "bg-athena-accent/15 border border-athena-accent/40"
                    : "border border-transparent hover:bg-athena-border/40"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      p.api_key_configured ? "bg-athena-success" : "bg-athena-warning"
                    }`}
                  />
                  <span className="text-sm font-medium truncate">{p.name}</span>
                </div>
                <div className="text-xs text-athena-muted mt-0.5 truncate">
                  {p.provider} · {p.model}
                </div>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default ProviderQuickList
