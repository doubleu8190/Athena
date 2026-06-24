import { NavLink } from 'react-router'
import { MessageSquare, BarChart3, Server, Wrench, Smartphone, Shield, ClipboardList, X } from 'lucide-react'

interface SidebarProps {
  onClose?: () => void
}

const navItems = [
  {
    section: 'Console',
    items: [
      { to: '/console', label: 'Chat', icon: MessageSquare },
    ],
  },
  {
    section: 'Admin',
    items: [
      { to: '/admin/dashboard', label: 'Dashboard', icon: BarChart3 },
      { to: '/admin/mcp-servers', label: 'MCP Servers', icon: Server },
      { to: '/admin/skills', label: 'Skills', icon: Wrench },
      { to: '/admin/devices', label: 'Devices', icon: Smartphone },
      { to: '/admin/harness', label: 'Harness Rules', icon: Shield },
      { to: '/admin/audit', label: 'Audit Logs', icon: ClipboardList },
    ],
  },
]

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `flex items-center gap-3 px-4 py-2.5 rounded-xl text-sm transition-all duration-200 ${
    isActive
      ? 'bg-accent/10 text-accent font-medium'
      : 'text-text-secondary hover:bg-bg-elevated hover:text-text-primary'
  }`

export default function Sidebar({ onClose }: SidebarProps) {
  return (
    <aside className="flex h-full flex-col bg-bg-surface shadow-soft z-10">
      {/* Logo */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-border-subtle">
        <NavLink to="/console" className="flex items-center gap-2.5" onClick={onClose}>
          <span className="text-2xl">🦉</span>
          <span className="text-xl font-semibold font-serif text-text-primary tracking-tight">Athena</span>
        </NavLink>
        <button
          onClick={onClose}
          className="lg:hidden p-1.5 text-text-secondary hover:text-text-primary hover:bg-bg-elevated rounded-xl transition-all"
          aria-label="Close sidebar"
        >
          <X size={20} />
        </button>
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto px-3 py-5 space-y-6">
        {navItems.map((group) => (
          <div key={group.section}>
            <h3 className="px-4 mb-1.5 text-xs font-medium text-text-muted">
              {group.section}
            </h3>
            <ul className="space-y-0.5">
              {group.items.map((item) => (
                <li key={item.to}>
                  <NavLink to={item.to} className={linkClass} onClick={onClose}>
                    <item.icon size={18} />
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </nav>

      {/* Footer */}
      <div className="px-5 py-3 border-t border-border-subtle">
        <p className="text-xs text-text-muted">Athena v0.1.0</p>
      </div>
    </aside>
  )
}
