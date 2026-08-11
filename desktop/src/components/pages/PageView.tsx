import type { AppView } from "../../types"
import MemoryView from "./MemoryView"
import ToolsView from "./ToolsView"
import ApprovalsView from "./ApprovalsView"
import ProvidersView from "./ProvidersView"
import SessionDetailView from "./SessionDetailView"
import SettingsView from "./SettingsView"

interface PageViewProps {
  view: AppView
}

/**
 * 管理页分发器 — 渲染「248px 上下文侧栏 + 主内容」的完整工作区.
 * 各 View 自含状态与侧栏，作为 NavRail 旁的 flex 兄弟片段渲染。
 */
function PageView({ view }: PageViewProps) {
  switch (view) {
    case "memory":
      return <MemoryView />
    case "tools":
      return <ToolsView />
    case "approvals":
      return <ApprovalsView />
    case "providers":
      return <ProvidersView />
    case "session-detail":
      return <SessionDetailView />
    case "settings":
      return <SettingsView />
    default:
      return null
  }
}

export default PageView
