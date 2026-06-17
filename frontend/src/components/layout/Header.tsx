interface HeaderProps {
  onMenuClick: () => void
}

export default function Header({ onMenuClick }: HeaderProps) {
  return (
    <header className="flex items-center gap-3 px-4 py-3 bg-bg-surface border-b border-border lg:px-6">
      <button
        onClick={onMenuClick}
        className="lg:hidden p-1 text-text-secondary hover:text-text-primary rounded"
        aria-label="Toggle sidebar"
      >
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M3 12h18M3 6h18M3 18h18" />
        </svg>
      </button>
      <span className="text-sm font-medium text-text-secondary">
        Athena Console
      </span>
    </header>
  )
}
