import {
  MessageSquare,
  Database,
  Cpu,
  ClipboardCheck,
  Server,
  History,
  Settings,
  Sparkles,
  Network,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { AppView } from "../types"

interface NavItem {
  view: AppView
  label: string
  icon: LucideIcon
}

const NAV_ITEMS: NavItem[] = [
  { view: "chat", label: "对话", icon: MessageSquare },
  { view: "memory", label: "记忆管理", icon: Database },
  { view: "tools", label: "工具管理", icon: Cpu },
  { view: "approvals", label: "审批日志", icon: ClipboardCheck },
  { view: "providers", label: "LLM 提供商", icon: Server },
  { view: "mcp", label: "MCP 服务器", icon: Network },
  { view: "session-detail", label: "会话详情", icon: History },
  { view: "settings", label: "系统设置", icon: Settings },
]

interface NavRailProps {
  activeView: AppView
  onSelectView: (view: AppView) => void
}

function NavRail({ activeView, onSelectView }: NavRailProps) {
  return (
    <nav className="w-[52px] h-full flex-shrink-0 bg-athena-surface border-r border-athena-border flex flex-col items-center py-3 overflow-y-auto">
      <div className="mb-4 flex-shrink-0">
        <Sparkles className="w-6 h-6 text-athena-accent" />
      </div>
      <div className="flex flex-col items-center gap-1">
        {NAV_ITEMS.map(({ view, label, icon: Icon }) => {
          const active = activeView === view
          return (
            <button
              key={view}
              onClick={() => onSelectView(view)}
              title={label}
              aria-label={label}
              className={`relative w-10 h-10 rounded-lg flex items-center justify-center transition-colors ${
                active
                  ? "bg-athena-accent/15 text-athena-accent"
                  : "text-athena-muted hover:text-athena-text hover:bg-athena-border/40"
              }`}
            >
              <Icon className="w-5 h-5" />
              {active ? (
                <span className="absolute left-0 top-1/2 -translate-y-1/2 w-0.5 h-6 rounded-full bg-athena-accent" />
              ) : null}
            </button>
          )
        })}
      </div>
    </nav>
  )
}

export default NavRail
