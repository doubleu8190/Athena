import { NavLink } from 'react-router'
import {
  MessageCircle,
  LayoutDashboard,
  Plug,
  Puzzle,
  Smartphone,
  Shield,
  FileText,
  Brain,
  Sun,
  Moon,
} from 'lucide-react'

interface Props {
  dark: boolean
  onToggleTheme: () => void
}

const navItems = [
  { to: '/console', icon: MessageCircle, label: 'Chat' },
  { to: '/admin/dashboard', icon: LayoutDashboard, label: 'Dashboard' },
  { to: '/admin/mcp-servers', icon: Plug, label: 'MCP Servers' },
  { to: '/admin/skills', icon: Puzzle, label: 'Skills' },
  { to: '/admin/devices', icon: Smartphone, label: 'Devices' },
  { to: '/admin/harness', icon: Shield, label: 'Harness' },
  { to: '/admin/audit', icon: FileText, label: 'Audit Logs' },
  { to: '/admin/memory', icon: Brain, label: 'Memory' },
]

const linkBase =
  'flex items-center gap-2 px-2.5 py-1.5 rounded-md text-xs font-medium transition-colors'

export default function Sidebar({ dark, onToggleTheme }: Props) {
  return (
    <aside className="w-52 bg-white dark:bg-gray-900 border-r border-gray-200 dark:border-gray-800 flex flex-col shrink-0">
      {/* Logo */}
      <div className="h-12 flex items-center gap-2 px-4 border-b border-gray-200 dark:border-gray-800">
        <span className="text-xl">🦉</span>
        <span className="font-semibold text-sm tracking-tight text-gray-900 dark:text-gray-100">
          Athena
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 py-2 px-2 space-y-0.5 overflow-y-auto">
        {navItems.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/console'}
            className={({ isActive }) =>
              `${linkBase} ${
                isActive
                  ? 'bg-orange-50 dark:bg-orange-950 text-orange-700 dark:text-orange-300'
                  : 'text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800'
              }`
            }
          >
            <Icon size={14} className="shrink-0" />
            {label}
          </NavLink>
        ))}
      </nav>

      {/* Theme toggle + version */}
      <div className="p-2 border-t border-gray-200 dark:border-gray-800 space-y-1.5">
        <button
          onClick={onToggleTheme}
          className="w-full flex items-center gap-2 px-2.5 py-1.5 rounded-md text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
        >
          {dark ? <Sun size={14} /> : <Moon size={14} />}
          <span>{dark ? 'Light Mode' : 'Dark Mode'}</span>
        </button>
        <div className="text-[10px] text-gray-400 dark:text-gray-600 text-center">
          Athena v0.1.0
        </div>
      </div>
    </aside>
  )
}
