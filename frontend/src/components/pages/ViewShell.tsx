import type { ReactNode } from "react"

interface ViewShellProps {
  side: ReactNode
  children: ReactNode
}

/**
 * 管理页内容布局：上下文侧栏 + 可滚动主内容。
 */
function ViewShell({ side, children }: ViewShellProps) {
  return (
    <>
      <aside className="max-h-[28vh] w-full flex-shrink-0 overflow-y-auto border-b border-athena-border bg-athena-surface md:max-h-none md:min-h-0 md:w-[248px] md:border-b-0 md:border-r">
        {side}
      </aside>
      <main className="flex-1 flex flex-col min-w-0 min-h-0">
        <div className="flex-1 min-h-0 overflow-y-auto">{children}</div>
      </main>
    </>
  )
}

export default ViewShell
