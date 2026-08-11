import type { ReactNode } from "react"

type BadgeTone = "default" | "success" | "warning" | "danger" | "accent"

const TONES: Record<BadgeTone, string> = {
  default: "bg-athena-border/50 text-athena-muted",
  success: "bg-athena-success/15 text-athena-success",
  warning: "bg-athena-warning/15 text-athena-warning",
  danger: "bg-athena-danger/15 text-athena-danger",
  accent: "bg-athena-accent/15 text-athena-accent",
}

interface BadgeProps {
  tone?: BadgeTone
  children: ReactNode
  title?: string
}

function Badge({ tone = "default", children, title }: BadgeProps) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  )
}

export default Badge
