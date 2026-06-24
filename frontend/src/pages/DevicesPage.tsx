import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getDevices, registerDevice, deregisterDevice, type Device } from '../api/endpoints/admin'
import Table, { type Column } from '../components/shared/Table'
import Modal from '../components/shared/Modal'
import StatusBadge from '../components/shared/StatusBadge'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'
import ConfirmModal from '../components/shared/ConfirmModal'
import { Smartphone } from 'lucide-react'

export default function DevicesPage() {
  const { checkAuth } = useAuthStore()
  const [devices, setDevices] = useState<Device[]>([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState({ device_id: '', type: 'host', connection_info: '' })
  const [submitting, setSubmitting] = useState(false)
  const [deregisterTarget, setDeregisterTarget] = useState<string | null>(null)

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetchDevices = useCallback(async () => {
    try {
      setLoading(true)
      const res = await getDevices()
      setDevices(res.data?.items ?? [])
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to load devices', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchDevices() }, [fetchDevices])

  const handleRegister = async () => {
    if (!form.device_id) {
      showToast('Device ID is required', 'warning')
      return
    }
    setSubmitting(true)
    try {
      let connInfo: Record<string, unknown> | undefined
      if (form.connection_info.trim()) {
        try {
          connInfo = JSON.parse(form.connection_info)
        } catch {
          showToast('Invalid JSON in connection info', 'error')
          setSubmitting(false)
          return
        }
      }
      await registerDevice({
        device_id: form.device_id,
        type: form.type,
        connection_info: connInfo,
      })
      showToast('Device registered', 'success')
      setShowModal(false)
      setForm({ device_id: '', type: 'host', connection_info: '' })
      await fetchDevices()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to register device', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const handleDeregister = async () => {
    if (!deregisterTarget) return
    try {
      await deregisterDevice(deregisterTarget)
      showToast('Device deregistered', 'success')
      setDeregisterTarget(null)
      await fetchDevices()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to deregister device', 'error')
    }
  }

  const columns: Column<Device>[] = [
    { key: 'device_id', header: 'Device ID' },
    { key: 'type', header: 'Type' },
    {
      key: 'status',
      header: 'Status',
      render: (d) => <StatusBadge status={d.status} />,
    },
    {
      key: 'connection_info',
      header: 'Connection Info',
      render: (d) => (
        <span className="text-xs text-text-muted">
          {d.connection_info ? JSON.stringify(d.connection_info).slice(0, 50) + '...' : '—'}
        </span>
      ),
    },
    {
      key: 'last_heartbeat',
      header: 'Last Heartbeat',
      render: (d) => (
        <span className="text-xs text-text-muted">
          {d.last_heartbeat ? new Date(d.last_heartbeat).toLocaleString() : '—'}
        </span>
      ),
    },
    {
      key: 'actions',
      header: 'Actions',
      render: (d) => (
        <button
          onClick={() => setDeregisterTarget(d.device_id)}
          className="text-xs px-2.5 py-1 rounded-xl bg-bg-elevated text-error hover:bg-error/10 transition-all"
        >
          Deregister
        </button>
      ),
    },
  ]

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">Devices</h1>
          <p className="text-sm text-text-secondary mt-1">Manage registered devices and their connection status</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all shadow-soft"
        >
          + Register Device
        </button>
      </div>

      {devices.length === 0 && !loading ? (
        <EmptyState
          icon={<Smartphone size={48} />}
          title="No Devices"
          description="Register a device to connect it to Athena."
          action={{ label: 'Register Device', onClick: () => setShowModal(true) }}
        />
      ) : (
        <Table
          columns={columns}
          data={devices}
          keyExtractor={(d) => d.device_id}
          isLoading={loading}
          emptyMessage="No devices registered"
        />
      )}

      <ConfirmModal
        open={!!deregisterTarget}
        title="Deregister Device"
        message={`Are you sure you want to deregister device "${deregisterTarget}"? This action cannot be undone.`}
        confirmLabel="Deregister"
        confirmColor="error"
        onConfirm={handleDeregister}
        onCancel={() => setDeregisterTarget(null)}
      />

      <Modal open={showModal} title="Register Device" onClose={() => setShowModal(false)} size="md">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Device ID *</label>
            <input
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
              placeholder="e.g. macbook-pro-01"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Type</label>
            <select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary focus:outline-none focus:border-accent"
            >
              <option value="host">host</option>
              <option value="android">android</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Connection Info (JSON, optional)</label>
            <textarea
              value={form.connection_info}
              onChange={(e) => setForm({ ...form, connection_info: e.target.value })}
              rows={4}
              className="w-full px-3 py-2 bg-bg border border-border rounded-xl text-sm text-text-primary font-mono focus:outline-none focus:border-accent"
              placeholder='{"hostname": "device.local", "port": 22}'
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowModal(false)} className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-xl text-sm hover:bg-border transition-all">
              Cancel
            </button>
            <button
              onClick={handleRegister}
              disabled={submitting}
              className="px-4 py-2 bg-accent text-white rounded-xl text-sm font-medium hover:bg-accent-hover transition-all disabled:opacity-50 shadow-soft"
            >
              {submitting ? 'Registering...' : 'Register'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
