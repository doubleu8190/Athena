import { useLocation } from 'react-router'

const pageTitles: Record<string, string> = {
  '/console': 'Chat',
  '/admin/dashboard': 'Dashboard',
  '/admin/mcp-servers': 'MCP Servers',
  '/admin/skills': 'Skills',
  '/admin/devices': 'Devices',
  '/admin/harness': 'Harness Rules',
  '/admin/audit': 'Audit Logs',
}

export default function Header() {
  const location = useLocation()

  // Match the most specific route prefix
  const title =
    pageTitles[location.pathname] ||
    Object.entries(pageTitles).find(([key]) => location.pathname.startsWith(key))?.[1] ||
    'Athena'

  return (
    <header className="h-12 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 flex items-center justify-between px-6 shrink-0">
      <h1 className="font-semibold text-sm text-gray-900 dark:text-gray-100">{title}</h1>
      <div className="w-7 h-7 rounded-full bg-orange-100 dark:bg-orange-900 flex items-center justify-center text-orange-600 dark:text-orange-400 text-xs font-semibold">
        U
      </div>
    </header>
  )
}
