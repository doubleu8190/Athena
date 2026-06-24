import { type ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: string
  render?: (item: T) => ReactNode
  className?: string
}

interface TableProps<T> {
  columns: Column<T>[]
  data: T[]
  keyExtractor: (item: T) => string | number
  emptyMessage?: string
  isLoading?: boolean
}

export default function Table<T>({
  columns,
  data,
  keyExtractor,
  emptyMessage = 'No data',
  isLoading = false,
}: TableProps<T>) {
  if (isLoading) {
    return (
      <div className="shadow-soft rounded-2xl overflow-hidden">
        <div className="animate-pulse">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="flex gap-4 px-6 py-3 border-b border-border-subtle last:border-b-0">
              {columns.map((col) => (
                <div key={col.key} className="flex-1 h-4 animate-shimmer rounded" />
              ))}
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="shadow-soft rounded-2xl p-8 text-center text-text-muted text-sm bg-bg-surface">
        {emptyMessage}
      </div>
    )
  }

  return (
    <div className="shadow-soft rounded-2xl overflow-hidden bg-bg-surface">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="bg-bg-elevated border-b border-border-subtle">
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`px-6 py-3 text-left text-xs font-semibold text-text-secondary sticky top-0 bg-bg-elevated z-10 ${col.className ?? ''}`}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border-subtle">
            {data.map((item) => (
              <tr key={keyExtractor(item)} className="hover:bg-bg/70 transition-colors">
                {columns.map((col) => (
                  <td key={col.key} className={`px-6 py-3 text-sm text-text-primary ${col.className ?? ''}`}>
                    {col.render
                      ? col.render(item)
                      : String((item as Record<string, unknown>)[col.key] ?? '')}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
