import {
  MessageSquare,
  Database,
  LibraryBig,
  Cpu,
  ClipboardCheck,
  Server,
  History,
  Settings,
  Sparkles,
  Network,
  GitBranch,
  FlaskConical,
} from "lucide-react"
import type { LucideIcon } from "lucide-react"
import type { AppView } from "../types"

interface NavItem {
  view: AppView
  label: string
  icon: LucideIcon
}

export const NAV_ITEMS: NavItem[] = [
  { view: "chat", label: "对话", icon: MessageSquare },
  { view: "knowledge", label: "知识库", icon: LibraryBig },
  { view: "memory", label: "记忆管理", icon: Database },
  { view: "tools", label: "工具管理", icon: Cpu },
  { view: "approvals", label: "审批日志", icon: ClipboardCheck },
  { view: "providers", label: "LLM 提供商", icon: Server },
  { view: "mcp", label: "MCP 服务器", icon: Network },
  { view: "retrieval", label: "召回分析", icon: GitBranch },
  { view: "evaluation", label: "评测实验", icon: FlaskConical },
  { view: "session-detail", label: "会话详情", icon: History },
  { view: "settings", label: "系统设置", icon: Settings },
]

interface NavRailProps {
  activeView: AppView
  onSelectView: (view: AppView) => void
}

function NavRail({ activeView, onSelectView }: NavRailProps) {
  return (
    <nav className="z-40 flex h-12 w-full flex-shrink-0 flex-row items-center justify-between border-t border-athena-border bg-athena-surface px-1 md:h-full md:w-[52px] md:flex-col md:justify-start md:border-r md:border-t-0 md:py-3">
      <div className="hidden flex-shrink-0 md:mb-4 md:block">
        <Sparkles className="w-6 h-6 text-athena-accent" />
      </div>
      <div className="flex min-w-0 flex-1 items-center justify-around gap-0.5 overflow-x-auto md:flex-none md:flex-col md:items-center md:justify-start md:gap-1">
        {NAV_ITEMS.map(({ view, label, icon: Icon }) => {
          const active = activeView === view
          return (
            <button
              key={view}
              onClick={() => onSelectView(view)}
              title={label}
              aria-label={label}
              className={`relative h-9 w-9 flex-shrink-0 rounded-lg flex items-center justify-center transition-colors md:h-10 md:w-10 ${
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
