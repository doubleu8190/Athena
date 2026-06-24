import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getMCPServers, createMCPServer, deleteMCPServer, updateMCPServerStatus, type MCPServer } from '../api/endpoints/admin'
import Table, { type Column } from '../components/shared/Table'
import Modal from '../components/shared/Modal'
import StatusBadge from '../components/shared/StatusBadge'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'
import ConfirmModal from '../components/shared/ConfirmModal'
import { Server } from 'lucide-react'

export default function MCPServersPage() {
  const { checkAuth } = useAuthStore()
  const [servers, setServers] = useState<MCPServer[]>([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState({ server_id: '', name: '', transport: 'stdio', connection_config: '{}', source: 'external' })
  const [submitting, setSubmitting] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetchServers = useCallback(async () => {
    try {
      setLoading(true)
      const res = await getMCPServers()
      setServers(res.data?.items ?? [])
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to load MCP servers', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchServers() }, [fetchServers])

  const handleCreate = async () => {
    if (!form.server_id || !form.name) {
      showToast('Server ID and Name are required', 'warning')
      return
    }
    setSubmitting(true)
    try {
      let config: Record<string, unknown>
      try {
        config = JSON.parse(form.connection_config)
      } catch {
        showToast('Invalid JSON in connection config', 'error')
        setSubmitting(false)
        return
      }
      await createMCPServer({
        server_id: form.server_id,
        name: form.name,
        transport: form.transport,
        connection_config: config,
        source: form.source,
      })
      showToast('MCP Server registered', 'success')
      setShowModal(false)
      setForm({ server_id: '', name: '', transport: 'stdio', connection_config: '{}', source: 'external' })
      await fetchServers()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to create server', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const handleDelete = async () => {
    if (!deleteTarget) return
    try {
      await deleteMCPServer(deleteTarget)
      showToast('MCP Server deleted', 'success')
      setDeleteTarget(null)
      await fetchServers()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to delete server', 'error')
    }
  }

  const handleToggle = async (server: MCPServer) => {
    const newEnabled = !server.enabled
    try {
      await updateMCPServerStatus(server.server_id, newEnabled)
      showToast(`Server ${newEnabled ? 'enabled' : 'disabled'}`, 'success')
      await fetchServers()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to update status', 'error')
    }
  }

  const columns: Column<MCPServer>[] = [
    { key: 'server_id', header: 'Server ID' },
    { key: 'name', header: 'Name' },
    { key: 'transport', header: 'Transport' },
    {
      key: 'enabled',
      header: 'Enabled',
      render: (s) => <StatusBadge status={s.enabled ? 'enabled' : 'disabled'} />,
    },
    {
      key: 'connection_status',
      header: 'Connection',
      render: (s) => <StatusBadge status={s.connection_status || 'unknown'} />,
    },
    { key: 'source', header: 'Source' },
    {
      key: 'actions',
      header: 'Actions',
      render: (s) => (
        <div className="flex gap-2">
          <button
            onClick={() => handleToggle(s)}
            className="text-xs px-2.5 py-1 rounded-xl bg-bg-elevated text-text-secondary hover:text-text-primary transition-all"
          >
            {s.enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            onClick={() => setDeleteTarget(s.server_id)}
            className="text-xs px-2.5 py-1 rounded-xl bg-bg-elevated text-error hover:bg-error/10 transition-all"
          >
            Delete
          </button>
        </div>
      ),
    },
  ]

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">MCP Servers</h1>
          <p className="text-sm text-text-secondary mt-1">Manage Model Context Protocol servers</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all shadow-soft"
        >
          + Register Server
        </button>
      </div>

      {servers.length === 0 && !loading ? (
        <EmptyState
          icon={<Server size={48} />}
          title="No MCP Servers"
          description="Register your first MCP server to extend Athena's capabilities."
          action={{ label: 'Register Server', onClick: () => setShowModal(true) }}
        />
      ) : (
        <Table
          columns={columns}
          data={servers}
          keyExtractor={(s) => s.server_id}
          isLoading={loading}
          emptyMessage="No MCP servers registered"
        />
      )}

      <ConfirmModal
        open={!!deleteTarget}
        title="Delete MCP Server"
        message={`Are you sure you want to delete MCP server "${deleteTarget}"? This action cannot be undone.`}
        confirmLabel="Delete"
        confirmColor="error"
        onConfirm={handleDelete}
        onCancel={() => setDeleteTarget(null)}
      />

      <Modal open={showModal} title="Register MCP Server" onClose={() => setShowModal(false)} size="lg">
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Server ID *</label>
              <input
                value={form.server_id}
                onChange={(e) => setForm({ ...form, server_id: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
                placeholder="e.g. my-server"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Name *</label>
              <input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
                placeholder="e.g. My MCP Server"
              />
            </div>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Transport</label>
              <select
                value={form.transport}
                onChange={(e) => setForm({ ...form, transport: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
              >
                <option value="stdio">stdio</option>
                <option value="http">http</option>
                <option value="sse">sse</option>
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Source</label>
              <select
                value={form.source}
                onChange={(e) => setForm({ ...form, source: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
              >
                <option value="external">external</option>
                <option value="builtin">builtin</option>
                <option value="skill">skill</option>
              </select>
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Connection Config (JSON)</label>
            <textarea
              value={form.connection_config}
              onChange={(e) => setForm({ ...form, connection_config: e.target.value })}
              rows={6}
              className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary font-mono focus:outline-none focus:border-accent"
              placeholder='{"command": "python", "args": ["-m", "my_mcp_server"]}'
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              onClick={() => setShowModal(false)}
              className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-xl text-sm hover:bg-border transition-all"
            >
              Cancel
            </button>
            <button
              onClick={handleCreate}
              disabled={submitting}
              className="px-4 py-2 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all disabled:opacity-50 shadow-soft"
            >
              {submitting ? 'Creating...' : 'Register'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
