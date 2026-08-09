import { useState } from "react"
import {
  CheckCircle,
  XCircle,
  Clock,
  AlertTriangle,
  Loader2,
  ChevronDown,
  Wrench,
} from "lucide-react"
import type { ToolCall, ToolCallInvocation } from "../types"
import { safeStringify } from "../utils/safeStringify"

interface ToolCardProps {
  toolCall: ToolCall | ToolCallInvocation
  compact?: boolean
}

/**
 * 统一的工具调用卡片(单行扁平样式):
 * 单行: 状态图标 + 工具名 + 风险徽章 + 状态/耗时 + 展开箭头
 * 展开后才显示参数 / 输出 / 错误 —— 不再嵌套多层盒子。
 *
 * 同时兼容两种数据来源:
 * - 实时 store 的 ToolCall(带 status / risk_level / output / duration)
 * - 历史消息 Message.tool_calls 的 ToolCallInvocation(只有 id / name / args)
 */
export function ToolCard({ toolCall, compact = false }: ToolCardProps) {
  const [open, setOpen] = useState(false)

  const isToolCall = "tool_name" in toolCall
  const name = isToolCall ? toolCall.tool_name : toolCall.name
  const args = isToolCall ? toolCall.arguments ?? {} : toolCall.args ?? {}
  const status = isToolCall ? toolCall.status : undefined
  const riskLevel = isToolCall ? toolCall.risk_level : undefined
  const output = isToolCall ? toolCall.output : undefined
  const error = isToolCall ? toolCall.error : undefined
  const durationMs = isToolCall ? toolCall.duration_ms : undefined

  const argCount = Object.keys(args).length

  const statusIcon = () => {
    if (!status) {
      // 历史视图只有名称/参数,不猜测状态
      return <Wrench className="w-4 h-4 text-athena-muted" />
    }
    switch (status) {
      case "success":
        return <CheckCircle className="w-4 h-4 text-athena-success" />
      case "failed":
        return <XCircle className="w-4 h-4 text-athena-danger" />
      case "denied":
      case "timeout":
        return <AlertTriangle className="w-4 h-4 text-athena-warning" />
      case "running":
      default:
        return <Loader2 className="w-4 h-4 text-athena-accent animate-spin" />
    }
  }

  const statusLabel = () => {
    if (!status) return "CALLED"
    switch (status) {
      case "success":
        return "Success"
      case "failed":
        return "Failed"
      case "denied":
        return "Denied"
      case "timeout":
        return "Timeout"
      case "running":
        return "Running"
      default:
        return "Pending"
    }
  }

  const riskBadge = () => {
    if (!riskLevel) return null
    const className =
      riskLevel === "high"
        ? "risk-high"
        : riskLevel === "medium"
          ? "risk-medium"
          : "risk-low"
    return <span className={`risk-badge ${className}`}>{riskLevel.toUpperCase()}</span>
  }

  const header = (
    <div className="flex items-center gap-2 px-3 py-2 min-w-0">
      {statusIcon()}
      <span className="font-mono text-sm font-medium truncate">{name}</span>
      {riskBadge()}
      <span className="ml-auto text-xs text-athena-muted flex items-center gap-1 flex-shrink-0">
        {statusLabel()}
        {durationMs != null && (
          <span className="flex items-center gap-0.5">
            <Clock className="w-3 h-3" />
            {(durationMs / 1000).toFixed(1)}s
          </span>
        )}
        {!compact && (
          <ChevronDown
            className={`w-3.5 h-3.5 transition-transform ${open ? "rotate-180" : ""}`}
          />
        )}
      </span>
    </div>
  )

  return (
    <div
      className={`rounded-lg border overflow-hidden transition-colors ${
        status === "running"
          ? "border-athena-accent/50 bg-athena-bg/30"
          : "border-athena-border bg-athena-bg/40"
      }`}
    >
      {compact ? (
        header
      ) : (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="w-full text-left hover:bg-athena-bg/60 transition-colors"
        >
          {header}
        </button>
      )}

      {/* Body — 展开后显示参数 / 输出 / 错误 */}
      {!compact && open && (
        <div className="px-3 py-2 space-y-2 border-t border-athena-border">
          {argCount > 0 && (
            <div>
              <div className="text-xs text-athena-muted mb-1">
                Arguments ({argCount})
              </div>
              <pre className="text-xs bg-athena-bg rounded p-2 overflow-x-auto overflow-y-auto text-athena-text/80 max-h-64">
                {safeStringify(args)}
              </pre>
            </div>
          )}

          {output && status === "success" && (
            <div>
              <div className="text-xs text-athena-muted mb-1">Output</div>
              <pre className="text-xs bg-athena-bg rounded p-2 overflow-x-auto overflow-y-auto text-athena-text/80 max-h-64 whitespace-pre-wrap">
                {safeStringify(output)}
              </pre>
            </div>
          )}

          {error && (
            <div className="text-xs text-athena-danger bg-red-500/10 rounded p-2">
              {error}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
