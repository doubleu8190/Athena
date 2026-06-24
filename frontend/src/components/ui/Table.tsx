import type { ReactNode } from 'react'

export interface Column<T> {
  key: string
  header: string
  className?: string
  render: (row: T, index: number) => ReactNode
}

interface TableProps<T> {
  columns: Column<T>[]
  data: T[]
  rowKey: (row: T, index: number) => string
  onRowClick?: (row: T) => void
}

export default function Table<T>({ columns, data, rowKey, onRowClick }: TableProps<T>) {
  if (data.length === 0) {
    return (
      <div className="py-12 text-center text-sm text-gray-400">
        No data
      </div>
    )
  }

  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-gray-200 dark:border-gray-800">
          {columns.map((col) => (
            <th
              key={col.key}
              className={`text-left py-3 px-4 font-medium text-gray-500 dark:text-gray-400 text-xs ${col.className || ''}`}
            >
              {col.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {data.map((row, i) => (
          <tr
            key={rowKey(row, i)}
            onClick={() => onRowClick?.(row)}
            className={`border-b border-gray-100 dark:border-gray-800/50 ${
              onRowClick ? 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/30' : ''
            } transition-colors`}
          >
            {columns.map((col) => (
              <td key={col.key} className={`py-3 px-4 ${col.className || ''}`}>
                {col.render(row, i)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
