import type { LucideIcon } from "lucide-react"

interface StatCardProps {
  label: string
  value: string | number
  icon: LucideIcon
  hint?: string
}

function StatCard({ label, value, icon: Icon, hint }: StatCardProps) {
  return (
    <div className="card p-4 flex items-center gap-3">
      <div className="w-10 h-10 rounded-lg bg-athena-accent/10 text-athena-accent flex items-center justify-center flex-shrink-0">
        <Icon className="w-5 h-5" />
      </div>
      <div className="min-w-0">
        <div className="text-xs text-athena-muted">{label}</div>
        <div className="text-xl font-semibold tabular-nums">{value}</div>
        {hint ? <div className="text-[11px] text-athena-muted">{hint}</div> : null}
      </div>
    </div>
  )
}

export default StatCard
