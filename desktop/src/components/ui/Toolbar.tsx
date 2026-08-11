import type { ReactNode } from "react"

interface ToolbarProps {
  title: string
  subtitle?: string
  actions?: ReactNode
}

function Toolbar({ title, subtitle, actions }: ToolbarProps) {
  return (
    <div className="flex items-center justify-between px-6 py-4 border-b border-athena-border">
      <div>
        <h1 className="text-lg font-semibold">{title}</h1>
        {subtitle ? (
          <p className="text-xs text-athena-muted mt-0.5">{subtitle}</p>
        ) : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  )
}

export default Toolbar
