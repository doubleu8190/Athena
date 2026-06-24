import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getAuditLogs, type AuditLog } from '../api/endpoints/admin'
import Table, { type Column } from '../components/shared/Table'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'
import { ClipboardList } from 'lucide-react'

export default function AuditPage() {
  const { checkAuth } = useAuthStore()
  const [logs, setLogs] = useState<AuditLog[]>([])
  const [loading, setLoading] = useState(true)
  const [cursor, setCursor] = useState<string | undefined>()
  const [hasMore, setHasMore] = useState(false)
  const [eventType, setEventType] = useState('')

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetchLogs = useCallback(async (reset = false) => {
    try {
      setLoading(true)
      const params: Record<string, string> = { limit: '50' }
      if (eventType) params.event_type = eventType
      if (!reset && cursor) params.cursor = cursor

      const res = await getAuditLogs(params)
      const items = res.data?.items ?? []
      if (reset) {
        setLogs(items)
      } else {
        setLogs((prev) => [...prev, ...items])
      }
      setCursor(res.data?.next_cursor)
      setHasMore(!!res.data?.next_cursor)
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to load audit logs', 'error')
    } finally {
      setLoading(false)
    }
  }, [cursor, eventType])

  useEffect(() => {
    fetchLogs(true)
  }, [eventType]) // eslint-disable-line react-hooks/exhaustive-deps

  const [expandedRow, setExpandedRow] = useState<string | null>(null)

  const columns: Column<AuditLog>[] = [
    {
      key: 'timestamp',
      header: 'Timestamp',
      render: (l) => (
        <span className="text-xs text-text-secondary whitespace-nowrap">
          {new Date(l.timestamp).toLocaleString()}
        </span>
      ),
    },
    { key: 'event_type', header: 'Event Type' },
    {
      key: 'actor_user_id',
      header: 'Actor',
      render: (l) => l.actor_user_id || '—',
    },
    {
      key: 'details',
      header: 'Details',
      render: (l) => (
        <button
          onClick={() => setExpandedRow(expandedRow === l.event_id ? null : l.event_id)}
          className="text-xs text-accent hover:underline"
        >
          {expandedRow === l.event_id ? 'Collapse' : 'View'}
        </button>
      ),
    },
  ]

  const EVENT_TYPES = ['', 'harness_block', 'task_completed', 'task_failed', 'subtask_completed', 'subtask_failed', 'session_created', 'session_expired']

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">Audit Logs</h1>
          <p className="text-sm text-text-secondary mt-1">System event trail with cursor-based pagination</p>
        </div>
      </div>

      {/* Filter */}
      <div className="mb-4">
        <select
          value={eventType}
          onChange={(e) => { setEventType(e.target.value); setCursor(undefined) }}
          className="px-3 py-2 bg-bg-surface border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
        >
          <option value="">All event types</option>
          {EVENT_TYPES.filter(Boolean).map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
      </div>

      {logs.length === 0 && !loading ? (
        <EmptyState icon={<ClipboardList size={48} />} title="No Audit Logs" description="No system events recorded yet." />
      ) : (
        <>
          <Table
            columns={columns}
            data={logs}
            keyExtractor={(l) => l.event_id}
            isLoading={loading}
            emptyMessage="No audit logs found"
          />

          {/* Expandable details */}
          {expandedRow && (
            <div className="mt-2 shadow-soft rounded-xl bg-bg-surface p-4">
              <h3 className="text-sm font-semibold text-text-primary mb-2">Event Details</h3>
              <pre className="text-xs text-text-secondary overflow-x-auto whitespace-pre-wrap font-mono">
                {JSON.stringify(
                  logs.find((l) => l.event_id === expandedRow)?.details_json,
                  null,
                  2
                )}
              </pre>
            </div>
          )}

          {/* Load more */}
          {hasMore && (
            <div className="mt-4 text-center">
              <button
                onClick={() => fetchLogs(false)}
                disabled={loading}
                className="px-6 py-2 bg-bg-elevated border border-border text-text-secondary rounded-xl text-sm hover:bg-border transition-all disabled:opacity-50"
              >
                {loading ? 'Loading...' : 'Load More'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
