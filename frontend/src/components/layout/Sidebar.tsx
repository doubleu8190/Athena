import { NavLink } from 'react-router'

interface SidebarProps {
  onClose?: () => void
}

const navItems = [
  {
    section: 'Console',
    items: [
      { to: '/console', label: 'Chat', icon: '💬' },
    ],
  },
  {
    section: 'Admin',
    items: [
      { to: '/admin/dashboard', label: 'Dashboard', icon: '📊' },
      { to: '/admin/mcp-servers', label: 'MCP Servers', icon: '🔌' },
      { to: '/admin/skills', label: 'Skills', icon: '🛠️' },
      { to: '/admin/devices', label: 'Devices', icon: '📱' },
      { to: '/admin/harness', label: 'Harness Rules', icon: '🛡️' },
      { to: '/admin/audit', label: 'Audit Logs', icon: '📋' },
    ],
  },
]

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
    isActive
      ? 'bg-accent/15 text-accent font-medium'
      : 'text-text-secondary hover:bg-bg-elevated hover:text-text-primary'
  }`

export default function Sidebar({ onClose }: SidebarProps) {
  return (
    <aside className="flex h-full flex-col bg-bg-surface border-r border-border">
      {/* Logo */}
      <div className="flex items-center justify-between px-4 py-4 border-b border-border">
        <NavLink to="/console" className="flex items-center gap-2" onClick={onClose}>
          <span className="text-2xl">🦉</span>
          <span className="text-lg font-bold text-text-primary">Athena</span>
        </NavLink>
        <button
          onClick={onClose}
          className="lg:hidden p-1 text-text-secondary hover:text-text-primary rounded"
        >
          ✕
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto px-3 py-4 space-y-6">
        {navItems.map((group) => (
          <div key={group.section}>
            <h3 className="px-3 mb-1 text-xs font-semibold text-text-muted uppercase tracking-wider">
              {group.section}
            </h3>
            <ul className="space-y-0.5">
              {group.items.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} className={linkClass} onClick={onClose}>
                    <span className="text-base">{item.icon}</span>
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      {/* Footer */}
      <div className="px-4 py-3 border-t border-border">
        <p className="text-xs text-text-muted">Athena v0.1.0</p>
      </div>
    </aside>
  )
}
