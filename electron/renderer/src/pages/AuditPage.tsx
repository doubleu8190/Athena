import { useEffect, useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'
import Table, { type Column } from '../components/ui/Table'
import Badge from '../components/ui/Badge'
import { useToast } from '../components/ui/Toast'
import type { AuditLog as AuditLogType } from '../api/endpoints/admin'
import { fetchAuditLogs } from '../api/endpoints/admin'

const EVENT_TYPES = [
  'harness_block',
  'task_completed',
  'task_failed',
  'subtask_completed',
  'subtask_failed',
  'server_registered',
  'server_deleted',
]

const eventVariant = (type: string): 'blue' | 'green' | 'red' | 'gray' => {
  if (type.includes('block')) return 'red'
  if (type.includes('completed')) return 'green'
  if (type.includes('failed')) return 'red'
  return 'blue'
}

export default function AuditPage() {
  const [logs, setLogs] = useState<AuditLogType[]>([])
  const [loading, setLoading] = useState(true)
  const [filter, setFilter] = useState('')
  const [cursor, setCursor] = useState<string | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const { toast } = useToast()

  const load = async (reset = false) => {
    try {
      const res = await fetchAuditLogs({
        event_type: filter || undefined,
        cursor: reset ? undefined : (cursor ?? undefined),
        limit: 20,
      })
      if (reset) {
        setLogs(res.items)
      } else {
        setLogs((prev) => [...prev, ...res.items])
      }
      setCursor(res.next_cursor)
      setHasMore(!!res.next_cursor)
    } catch { toast('error', 'Failed to load audit logs') }
    finally { setLoading(false) }
  }

  useEffect(() => {
    setLoading(true)
    load(true)
  }, [filter])

  const toggleExpand = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const rowKey = (r: AuditLogType, i: number) => `${r.timestamp}-${i}`

  const columns: Column<AuditLogType>[] = [
    { key: 'timestamp', header: 'Timestamp', render: (r) => (
      <span className="text-xs text-gray-500 font-mono">{new Date(r.timestamp).toLocaleString()}</span>
    )},
    { key: 'event_type', header: 'Event', render: (r) => (
      <Badge variant={eventVariant(r.event_type)}>{r.event_type}</Badge>
    )},
    { key: 'actor_user_id', header: 'Operator', render: (r) => (
      <span className="text-xs text-gray-600 dark:text-gray-400">{r.actor_user_id}</span>
    )},
    { key: 'expand', header: '', className: 'text-right', render: (r, i) => {
      const key = rowKey(r, i)
      return (
        <button onClick={() => toggleExpand(key)} className="p-1 rounded text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
          {expanded.has(key) ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
      )
    }},
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Audit Logs</h2>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="px-3 py-1.5 text-sm rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-300"
        >
          <option value="">All Types</option>
          {EVENT_TYPES.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
        {loading && logs.length === 0 ? (
          <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
        ) : logs.length === 0 ? (
          <div className="py-12 text-center text-sm text-gray-400">No audit logs found</div>
        ) : (
          <div>
            <Table columns={columns} data={logs} rowKey={(r, i) => `${r.timestamp}-${i}`} />
            {/* Expanded detail rows */}
            {logs.map((log, i) =>
              expanded.has(rowKey(log, i)) ? (
                <div key={`detail-${i}`} className="px-4 py-3 bg-gray-50 dark:bg-gray-950 border-t border-gray-100 dark:border-gray-800/50">
                  <pre className="text-xs text-gray-600 dark:text-gray-400 font-mono whitespace-pre-wrap overflow-x-auto">
                    {JSON.stringify(log.details, null, 2)}
                  </pre>
                </div>
              ) : null
            )}
          </div>
        )}
      </div>

      {hasMore && (
        <div className="text-center">
          <button
            onClick={() => load(false)}
            className="text-sm text-orange-500 hover:text-orange-600 font-medium"
          >
            Load More
          </button>
        </div>
      )}
    </div>
  )
}
