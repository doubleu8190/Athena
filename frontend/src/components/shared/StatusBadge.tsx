import { CheckCircle2, XCircle, Loader2 } from 'lucide-react'

interface StatusBadgeProps {
  status: string
}

const colorMap: Record<string, string> = {
  connected: 'bg-success/15 text-success border-success/30',
  online: 'bg-success/15 text-success border-success/30',
  active: 'bg-success/15 text-success border-success/30',
  enabled: 'bg-success/15 text-success border-success/30',
  running: 'bg-accent/15 text-accent border-accent/30',
  installing: 'bg-accent/15 text-accent border-accent/30',
  connecting: 'bg-warning/15 text-warning border-warning/30',
  pending: 'bg-warning/15 text-warning border-warning/30',
  idle: 'bg-warning/15 text-warning border-warning/30',
  disconnected: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  disabled: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  offline: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  unknown: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  error: 'bg-error/15 text-error border-error/30',
  failed: 'bg-error/15 text-error border-error/30',
  uninstalling: 'bg-warning/15 text-warning border-warning/30',
  expired: 'bg-text-muted/15 text-text-muted border-text-muted/30',
}

const iconMap: Record<string, typeof CheckCircle2> = {
  connected: CheckCircle2,
  online: CheckCircle2,
  active: CheckCircle2,
  enabled: CheckCircle2,
  running: Loader2,
  installing: Loader2,
  connecting: Loader2,
  failed: XCircle,
  error: XCircle,
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const colors = colorMap[status] ?? 'bg-text-muted/15 text-text-muted border-text-muted/30'
  const StatusIcon = iconMap[status]

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors}`}
    >
      {StatusIcon ? (
        <StatusIcon size={12} className={status === 'running' || status === 'installing' || status === 'connecting' ? 'animate-spin' : ''} />
      ) : (
        <span className={`w-1.5 h-1.5 rounded-full ${status === 'pending' || status === 'idle' || status === 'uninstalling' ? 'bg-current animate-pulse' : 'bg-current'}`} />
      )}
      {status}
    </span>
  )
}
