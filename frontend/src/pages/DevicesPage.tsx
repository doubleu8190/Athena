import { useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import Table, { type Column } from '../components/ui/Table'
import Badge from '../components/ui/Badge'
import Modal from '../components/ui/Modal'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import { useToast } from '../components/ui/Toast'
import type { Device } from '../api/endpoints/admin'
import { fetchDevices, registerDevice, deregisterDevice } from '../api/endpoints/admin'

export default function DevicesPage() {
  const [devices, setDevices] = useState<Device[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [form, setForm] = useState({ device_id: '', type: 'host', connection_info: '{}' })
  const { toast } = useToast()

  const load = async () => {
    try {
      const data = await fetchDevices()
      setDevices(data)
    } catch { toast('error', 'Failed to load devices') }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const handleRegister = async () => {
    try {
      await registerDevice({
        device_id: form.device_id,
        type: form.type,
        connection_info: JSON.parse(form.connection_info),
      })
      toast('success', 'Device registered')
      setShowForm(false)
      load()
    } catch { toast('error', 'Failed to register device') }
  }

  const handleDeregister = async () => {
    if (!deleteId) return
    try {
      await deregisterDevice(deleteId)
      toast('success', 'Device deregistered')
      load()
    } catch { toast('error', 'Failed to deregister device') }
    finally { setDeleteId(null) }
  }

  const columns: Column<Device>[] = [
    { key: 'device_id', header: 'ID', render: (r) => <span className="font-mono text-xs">{r.device_id}</span> },
    { key: 'type', header: 'Type', render: (r) => (
      <Badge variant={r.type === 'host' ? 'blue' : 'purple'}>{r.type}</Badge>
    )},
    { key: 'status', header: 'Status', render: (r) => (
      <Badge variant={r.status === 'online' ? 'green' : 'gray'} dot>{r.status}</Badge>
    )},
    { key: 'heartbeat', header: 'Heartbeat', render: (r) => (
      <span className="text-xs text-gray-400">{r.last_heartbeat ? new Date(r.last_heartbeat).toLocaleString() : '—'}</span>
    )},
    { key: 'actions', header: '', className: 'text-right', render: (r) => (
      <button onClick={() => setDeleteId(r.device_id)} className="p-1.5 rounded-md text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950 transition-colors" title="Deregister">
        <Trash2 size={14} />
      </button>
    )},
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Devices</h2>
        <button onClick={() => setShowForm(true)} className="px-4 py-2 bg-orange-500 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
          <Plus size={14} /> Register Device
        </button>
      </div>

      <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
        {loading ? (
          <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
        ) : (
          <Table columns={columns} data={devices} rowKey={(r) => r.device_id} />
        )}
      </div>

      <Modal open={showForm} onClose={() => setShowForm(false)} title="Register Device">
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Device ID</label>
            <input value={form.device_id} onChange={(e) => setForm({ ...form, device_id: e.target.value })} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Type</label>
            <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm">
              <option value="host">host</option>
              <option value="android">android</option>
            </select>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Connection Info (JSON)</label>
            <textarea value={form.connection_info} onChange={(e) => setForm({ ...form, connection_info: e.target.value })} rows={3} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-mono" />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors">Cancel</button>
            <button onClick={handleRegister} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors">Register</button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog open={!!deleteId} onClose={() => setDeleteId(null)} onConfirm={handleDeregister} title="Deregister Device" message="Are you sure you want to deregister this device?" confirmLabel="Deregister" variant="danger" />
    </div>
  )
}
