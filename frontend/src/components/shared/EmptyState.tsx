import type { ReactNode } from 'react'
import { PackageOpen } from 'lucide-react'

interface EmptyStateProps {
  icon?: string | ReactNode
  title: string
  description?: string
  action?: { label: string; onClick: () => void }
}

export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 text-center">
      {typeof icon === 'string' ? (
        <span className="text-5xl mb-4">{icon}</span>
      ) : icon ? (
        <span className="text-5xl mb-4 text-text-muted/40">{icon}</span>
      ) : (
        <PackageOpen size={64} className="text-text-muted/30 mb-4" />
      )}
      <h3 className="text-lg font-medium text-text-primary mb-2">{title}</h3>
      {description && <p className="text-sm text-text-secondary max-w-md mb-6">{description}</p>}
      {action && (
        <button
          onClick={action.onClick}
          className="px-4 py-2 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all shadow-soft"
        >
          {action.label}
        </button>
      )}
    </div>
  )
}
