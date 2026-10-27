import { useChatStore } from "../store/chatStore"
import { Wifi, WifiOff, Loader2, AlertCircle, Cpu } from "lucide-react"

export default function StatusIndicator() {
  const connectionStatus = useChatStore((s) => s.connectionStatus)
  const agentStatus = useChatStore((s) => s.agentStatus)
  const error = useChatStore((s) => s.error)

  const connectionIcon = () => {
    switch (connectionStatus) {
      case "connected":
        return <Wifi className="w-4 h-4 text-athena-success" />
      case "connecting":
        return <Loader2 className="w-4 h-4 text-yellow-400 animate-spin" />
      case "error":
        return <AlertCircle className="w-4 h-4 text-athena-danger" />
      default:
        return <WifiOff className="w-4 h-4 text-athena-muted" />
    }
  }

  const connectionText = () => {
    switch (connectionStatus) {
      case "connected":
        return "已连接"
      case "connecting":
        return "正在连接"
      case "error":
        return "连接异常"
      default:
        return "未连接"
    }
  }

  const agentBadge = () => {
    switch (agentStatus) {
      case "queued":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            等待执行
          </span>
        )
      case "thinking":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Cpu className="w-4 h-4 animate-pulse" />
            正在分析
          </span>
        )
      case "running":
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-accent">
            <Loader2 className="w-4 h-4 animate-spin" />
            正在执行
          </span>
        )
      case "cancelling":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Loader2 className="w-4 h-4 animate-spin" />
            正在停止
          </span>
        )
      case "waiting_approval":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <AlertCircle className="w-4 h-4" />
            等待你确认
          </span>
        )
      case "error":
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-danger">
            <AlertCircle className="w-4 h-4" />
            执行失败
          </span>
        )
      default:
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-muted">
            {agentStatus === "completed" ? "任务已完成" : "空闲"}
          </span>
        )
    }
  }

  return (
    <div className="h-10 px-4 border-b border-athena-border bg-athena-surface flex items-center justify-between">
      <div className="flex items-center gap-4">
        {agentBadge()}
        {error && (
          <span className="text-xs text-athena-danger bg-red-500/10 px-2 py-0.5 rounded">
            {error}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2 text-xs text-athena-muted">
        {connectionIcon()}
        <span>{connectionText()}</span>
      </div>
    </div>
  )
}
