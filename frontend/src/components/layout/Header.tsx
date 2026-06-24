import { Menu } from 'lucide-react'

interface HeaderProps {
  onMenuClick: () => void
}

export default function Header({ onMenuClick }: HeaderProps) {
  return (
    <header className="flex items-center gap-3 px-5 py-3 bg-bg-surface shadow-soft lg:px-8 sticky top-0 z-30 transition-shadow duration-200">
      <button
        onClick={onMenuClick}
        className="lg:hidden p-1.5 text-text-secondary hover:text-text-primary hover:bg-bg-elevated rounded-xl transition-all"
        aria-label="Toggle sidebar"
      >
        <Menu size={24} />
      </button>
      <span className="text-sm text-text-muted">
        Athena Console
      </span>
    </header>
  )
}
