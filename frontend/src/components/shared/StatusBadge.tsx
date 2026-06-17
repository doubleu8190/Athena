interface StatusBadgeProps {
  status: string
}

const colorMap: Record<string, string> = {
  connected: 'bg-success/15 text-success border-success/30',
  online: 'bg-success/15 text-success border-success/30',
  active: 'bg-success/15 text-success border-success/30',
  running: 'bg-accent/15 text-accent border-accent/30',
  installing: 'bg-accent/15 text-accent border-accent/30',
  pending: 'bg-warning/15 text-warning border-warning/30',
  idle: 'bg-warning/15 text-warning border-warning/30',
  disconnected: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  disabled: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  offline: 'bg-text-muted/15 text-text-muted border-text-muted/30',
  error: 'bg-error/15 text-error border-error/30',
  failed: 'bg-error/15 text-error border-error/30',
  uninstalling: 'bg-warning/15 text-warning border-warning/30',
  expired: 'bg-text-muted/15 text-text-muted border-text-muted/30',
}

export default function StatusBadge({ status }: StatusBadgeProps) {
  const colors = colorMap[status] ?? 'bg-text-muted/15 text-text-muted border-text-muted/30'

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${status === 'connected' || status === 'online' || status === 'running' ? 'bg-current animate-pulse' : 'bg-current'}`} />
      {status}
    </span>
  )
}
