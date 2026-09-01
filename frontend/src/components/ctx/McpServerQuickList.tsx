import { RefreshCw, Trash2, Loader2 } from "lucide-react"
import type { McpServerInfo } from "../../types"

interface McpServerQuickListProps {
  servers: McpServerInfo[]
  selectedName: string | null
  onSelect: (name: string) => void
  onRefresh: () => void
  onDelete: (name: string) => void
  loading?: boolean
  deleting?: string | null
}

function McpServerQuickList({
  servers,
  selectedName,
  onSelect,
  onRefresh,
  onDelete,
  loading,
  deleting,
}: McpServerQuickListProps) {
  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-athena-text">已注册服务器</h2>
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
      ) : servers.length === 0 ? (
        <div className="text-xs text-athena-muted">暂无已注册的 MCP 服务器</div>
      ) : (
        <div className="space-y-1">
          {servers.map((s) => {
            const active = selectedName === s.name
            const connected = s.status === "connected"
            return (
              <div
                key={s.name}
                onClick={() => onSelect(s.name)}
                className={`group w-full text-left px-3 py-2 rounded-lg cursor-pointer transition-colors ${
                  active
                    ? "bg-athena-accent/15 border border-athena-accent/40"
                    : "border border-transparent hover:bg-athena-border/40"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span
                    title={connected ? "已连接" : "连接失败"}
                    className={`w-2 h-2 rounded-full flex-shrink-0 ${
                      connected ? "bg-athena-success" : "bg-athena-danger"
                    }`}
                  />
                  <span className="text-sm font-medium truncate flex-1">{s.name}</span>
                  {deleting === s.name ? (
                    <Loader2 className="w-3.5 h-3.5 text-athena-muted animate-spin flex-shrink-0" />
                  ) : (
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        onDelete(s.name)
                      }}
                      title="注销该服务器"
                      className="opacity-0 group-hover:opacity-100 p-1 rounded text-athena-muted hover:text-athena-danger transition-opacity flex-shrink-0"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
                <div className="text-xs text-athena-muted mt-0.5 truncate">
                  {s.command} · {s.tool_count} 工具
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default McpServerQuickList
