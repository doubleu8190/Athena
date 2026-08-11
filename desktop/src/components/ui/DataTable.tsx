import type { ReactNode } from "react"
import { Loader2, Inbox } from "lucide-react"

export interface Column<T> {
  key: string
  header: string
  className?: string
  render: (row: T) => ReactNode
}

interface DataTableProps<T> {
  columns: Column<T>[]
  rows: T[]
  rowKey: (row: T) => string
  loading?: boolean
  emptyText?: string
  onRowClick?: (row: T) => void
}

function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading,
  emptyText = "暂无数据",
  onRowClick,
}: DataTableProps<T>) {
  return (
    <div className="card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-athena-border bg-athena-bg/50 text-left">
              {columns.map((col) => (
                <th
                  key={col.key}
                  className={`px-4 py-2.5 text-xs font-semibold text-athena-muted whitespace-nowrap ${col.className ?? ""}`}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td colSpan={columns.length} className="px-4 py-10 text-center">
                  <Loader2 className="w-5 h-5 animate-spin inline-block text-athena-muted" />
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="px-4 py-10 text-center">
                  <div className="flex flex-col items-center gap-2 text-athena-muted">
                    <Inbox className="w-6 h-6" />
                    <span className="text-xs">{emptyText}</span>
                  </div>
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr
                  key={rowKey(row)}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={`border-b border-athena-border/50 last:border-b-0 hover:bg-athena-bg/40 transition-colors ${
                    onRowClick ? "cursor-pointer" : ""
                  }`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`px-4 py-2.5 align-middle ${col.className ?? ""}`}
                    >
                      {col.render(row)}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

export default DataTable
