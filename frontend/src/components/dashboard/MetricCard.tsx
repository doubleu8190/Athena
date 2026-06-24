import type { ReactNode } from 'react'
import { TrendingUp, TrendingDown } from 'lucide-react'

interface MetricCardProps {
  title: string
  value: number | string
  subtitle?: string
  icon?: ReactNode
  trend?: 'up' | 'down'
  color?: 'default' | 'success' | 'warning' | 'error' | 'accent'
}

const topBorderMap = {
  default: 'border-t-border-subtle',
  success: 'border-t-success/40',
  warning: 'border-t-warning/40',
  error: 'border-t-error/40',
  accent: 'border-t-accent/40',
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
    <div className={`bg-bg-surface shadow-soft rounded-2xl p-5 border-t-[3px] ${topBorderMap[color]} hover:shadow-card-hover hover:-translate-y-0.5 transition-all duration-200`}>
      <div className="flex items-start justify-between mb-3">
        <span className="text-sm text-text-secondary">{title}</span>
        {icon && (
          <span className={`text-xl ${textColorMap[color]}`}>
            {icon}
          </span>
        )}
      </div>
      <div className={`text-3xl font-bold ${textColorMap[color]} mb-1`}>
        {value}
        {trend && (
          <span className={`inline-block ml-2 ${trend === 'up' ? 'text-success' : 'text-error'}`}>
            {trend === 'up' ? <TrendingUp size={16} className="inline" /> : <TrendingDown size={16} className="inline" />}
          </span>
        )}
      </div>
      {subtitle && <p className="text-xs text-text-muted">{subtitle}</p>}
    </div>
  )
}
