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
      <div className="border border-border rounded-lg overflow-hidden">
        <div className="animate-pulse">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="flex gap-4 px-6 py-3 border-b border-border last:border-b-0">
              {columns.map((col) => (
                <div key={col.key} className="flex-1 h-4 bg-bg-elevated rounded" />
              ))}
            </div>
          ))}
        </div>
      </div>
    )
  }

  if (data.length === 0) {
    return (
      <div className="border border-border rounded-lg p-8 text-center text-text-muted text-sm">
        {emptyMessage}
      </div>
    )
  }

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="bg-bg-surface border-b border-border">
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`px-6 py-3 text-left text-xs font-semibold text-text-secondary uppercase tracking-wider ${col.className ?? ''}`}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {data.map((item) => (
              <tr key={keyExtractor(item)} className="hover:bg-bg-surface/50 transition-colors">
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
