import { useChatStore } from "../store/chatStore"
import { Wifi, WifiOff, Loader2, AlertCircle, Cpu, CheckCircle } from "lucide-react"

export default function StatusIndicator() {
  const connectionStatus = useChatStore((s) => s.connectionStatus)
  const agentStatus = useChatStore((s) => s.agentStatus)
  const error = useChatStore((s) => s.error)
  const sessions = useChatStore((s) => s.sessions)

  const completedCount = sessions.filter((s) => s.status === "completed").length

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
        return "Connected"
      case "connecting":
        return "Connecting..."
      case "error":
        return "Connection Error"
      default:
        return "Disconnected"
    }
  }

  const agentBadge = () => {
    switch (agentStatus) {
      case "thinking":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <Cpu className="w-4 h-4 animate-pulse" />
            Thinking
          </span>
        )
      case "running":
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-accent">
            <Loader2 className="w-4 h-4 animate-spin" />
            Running
          </span>
        )
      case "waiting_approval":
        return (
          <span className="flex items-center gap-1.5 text-sm text-yellow-400">
            <AlertCircle className="w-4 h-4" />
            Waiting for Approval
          </span>
        )
      case "error":
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-danger">
            <AlertCircle className="w-4 h-4" />
            Error
          </span>
        )
      default:
        return (
          <span className="flex items-center gap-1.5 text-sm text-athena-muted">
            Idle
          </span>
        )
    }
  }

  return (
    <div className="h-10 px-4 border-b border-athena-border bg-athena-surface flex items-center justify-between">
      <div className="flex items-center gap-4">
        {agentBadge()}
        {completedCount > 0 && (
          <>
            <span className="text-athena-border">|</span>
            <span className="flex items-center gap-1.5 text-xs text-athena-muted">
              <CheckCircle className="w-3.5 h-3.5 text-athena-success" />
              <span>{completedCount} completed</span>
            </span>
          </>
        )}
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
