interface MetricCardProps {
  title: string
  value: number | string
  subtitle?: string
  icon?: string
  trend?: 'up' | 'down'
  color?: 'default' | 'success' | 'warning' | 'error' | 'accent'
}

const colorMap = {
  default: 'border-border',
  success: 'border-success/30',
  warning: 'border-warning/30',
  error: 'border-error/30',
  accent: 'border-accent/30',
}

const textColorMap = {
  default: 'text-text-primary',
  success: 'text-success',
  warning: 'text-warning',
  error: 'text-error',
  accent: 'text-accent',
}

export default function MetricCard({ title, value, subtitle, icon, trend, color = 'default' }: MetricCardProps) {
  return (
    <div className={`bg-bg-surface border ${colorMap[color]} rounded-xl p-5 hover:border-text-muted/30 transition-colors`}>
      <div className="flex items-start justify-between mb-3">
        <span className="text-sm text-text-secondary">{title}</span>
        {icon && <span className="text-xl">{icon}</span>}
      </div>
      <div className={`text-3xl font-bold ${textColorMap[color]} mb-1`}>
        {value}
        {trend && (
          <span className={`text-sm ml-2 ${trend === 'up' ? 'text-success' : 'text-error'}`}>
            {trend === 'up' ? '↑' : '↓'}
          </span>
        )}
      </div>
      {subtitle && <p className="text-xs text-text-muted">{subtitle}</p>}
    </div>
  )
}
