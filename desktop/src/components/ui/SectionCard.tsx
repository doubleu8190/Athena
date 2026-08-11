import type { ReactNode } from "react"

interface SectionCardProps {
  title: string
  description?: string
  actions?: ReactNode
  children: ReactNode
}

function SectionCard({ title, description, actions, children }: SectionCardProps) {
  return (
    <div className="card">
      <div className="flex items-center justify-between px-5 py-3 border-b border-athena-border">
        <div>
          <h3 className="text-sm font-semibold">{title}</h3>
          {description ? (
            <p className="text-xs text-athena-muted mt-0.5">{description}</p>
          ) : null}
        </div>
        {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
      </div>
      <div className="p-5">{children}</div>
    </div>
  )
}

export default SectionCard
