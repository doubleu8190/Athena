import { useEffect, useState } from 'react'
import { Plus, Trash2, Power, PowerOff } from 'lucide-react'
import Table, { type Column } from '../components/ui/Table'
import Badge from '../components/ui/Badge'
import Modal from '../components/ui/Modal'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import { useToast } from '../components/ui/Toast'
import type { MCPServer } from '../api/endpoints/admin'
import {
  fetchMCPServers,
  createMCPServer,
  deleteMCPServer,
  updateMCPServerStatus,
} from '../api/endpoints/admin'

export default function MCPServersPage() {
  const [servers, setServers] = useState<MCPServer[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [form, setForm] = useState({ server_id: '', name: '', transport: 'stdio', connection_config: '{}', source: 'external' })
  const { toast } = useToast()

  const load = async () => {
    try {
      const data = await fetchMCPServers()
      setServers(data)
    } catch {
      toast('error', 'Failed to load MCP servers')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const handleCreate = async () => {
    try {
      await createMCPServer({
        server_id: form.server_id,
        name: form.name,
        transport: form.transport,
        connection_config: JSON.parse(form.connection_config),
        source: form.source,
      })
      toast('success', 'Server registered')
      setShowForm(false)
      load()
    } catch {
      toast('error', 'Failed to register server')
    }
  }

  const handleDelete = async () => {
    if (!deleteId) return
    try {
      await deleteMCPServer(deleteId)
      toast('success', 'Server deleted')
      load()
    } catch {
      toast('error', 'Failed to delete server')
    } finally {
      setDeleteId(null)
    }
  }

  const handleToggle = async (server: MCPServer) => {
    try {
      await updateMCPServerStatus(server.server_id, !server.enabled)
      toast('success', server.enabled ? 'Server disabled' : 'Server enabled')
      load()
    } catch {
      toast('error', 'Failed to update status')
    }
  }

  const columns: Column<MCPServer>[] = [
    { key: 'server_id', header: 'Server ID', render: (r) => <span className="font-mono text-xs">{r.server_id}</span> },
    { key: 'name', header: 'Name', render: (r) => <span className="font-medium text-sm">{r.name}</span> },
    { key: 'transport', header: 'Transport', render: (r) => <Badge variant="gray">{r.transport}</Badge> },
    { key: 'status', header: 'Status', render: (r) => (
      <Badge variant={r.enabled ? 'green' : 'gray'} dot>{r.enabled ? 'Enabled' : 'Disabled'}</Badge>
    )},
    { key: 'connection', header: 'Connection', render: (r) => (
      <span className={`text-xs ${r.connection_status === 'connected' ? 'text-green-600 dark:text-green-400' : 'text-gray-400'}`}>
        {r.connection_status}
      </span>
    )},
    { key: 'actions', header: '', className: 'text-right', render: (r) => (
      <div className="flex items-center justify-end gap-1">
        <button onClick={() => handleToggle(r)} className="p-1.5 rounded-md text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors" title={r.enabled ? 'Disable' : 'Enable'}>
          {r.enabled ? <PowerOff size={14} /> : <Power size={14} />}
        </button>
        <button onClick={() => setDeleteId(r.server_id)} className="p-1.5 rounded-md text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950 transition-colors" title="Delete">
          <Trash2 size={14} />
        </button>
      </div>
    )},
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">MCP Servers</h2>
        <button onClick={() => setShowForm(true)} className="px-4 py-2 bg-orange-500 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
          <Plus size={14} /> Register Server
        </button>
      </div>

      <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
        {loading ? (
          <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
        ) : (
          <Table columns={columns} data={servers} rowKey={(r) => r.server_id} />
        )}
      </div>

      {/* Register Form Modal */}
      <Modal open={showForm} onClose={() => setShowForm(false)} title="Register MCP Server">
        <div className="space-y-3">
          <InputField label="Server ID" value={form.server_id} onChange={(v) => setForm({ ...form, server_id: v })} />
          <InputField label="Name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Transport</label>
            <select value={form.transport} onChange={(e) => setForm({ ...form, transport: e.target.value })} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm">
              <option value="stdio">stdio</option>
              <option value="http">http</option>
              <option value="sse">sse</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Connection Config (JSON)</label>
            <textarea value={form.connection_config} onChange={(e) => setForm({ ...form, connection_config: e.target.value })} rows={4} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-mono" />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors">Cancel</button>
            <button onClick={handleCreate} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors">Register</button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog open={!!deleteId} onClose={() => setDeleteId(null)} onConfirm={handleDelete} title="Delete Server" message="Are you sure you want to delete this MCP server?" confirmLabel="Delete" variant="danger" />
    </div>
  )
}

function InputField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm" />
    </div>
  )
}
