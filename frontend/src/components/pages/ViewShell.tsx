import type { ReactNode } from "react"
import StatusIndicator from "../StatusIndicator"

interface ViewShellProps {
  side: ReactNode
  children: ReactNode
}

/**
 * 管理页布局壳：248px 上下文侧栏（aside）+ 主内容列（StatusIndicator + children）.
 * 作为 flex 容器内的兄弟片段渲染，与 NavRail / 聊天 Sidebar 并列。
 */
function ViewShell({ side, children }: ViewShellProps) {
  return (
    <>
      <aside className="max-h-[28vh] w-full flex-shrink-0 overflow-y-auto border-b border-athena-border bg-athena-surface md:max-h-none md:min-h-0 md:w-[248px] md:border-b-0 md:border-r">
        {side}
      </aside>
      <main className="flex-1 flex flex-col min-w-0 min-h-0">
        <StatusIndicator />
        <div className="flex-1 min-h-0 overflow-y-auto">{children}</div>
      </main>
    </>
  )
}

export default ViewShell
