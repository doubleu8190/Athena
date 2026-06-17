import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getDevices, registerDevice, deregisterDevice, type Device } from '../api/endpoints/admin'
import Table, { type Column } from '../components/shared/Table'
import Modal from '../components/shared/Modal'
import StatusBadge from '../components/shared/StatusBadge'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'

export default function DevicesPage() {
  const { checkAuth } = useAuthStore()
  const [devices, setDevices] = useState<Device[]>([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState({ device_id: '', type: 'host', connection_info: '' })
  const [submitting, setSubmitting] = useState(false)

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

  const handleDeregister = async (deviceId: string) => {
    if (!window.confirm(`Deregister device "${deviceId}"?`)) return
    try {
      await deregisterDevice(deviceId)
      showToast('Device deregistered', 'success')
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
          onClick={() => handleDeregister(d.device_id)}
          className="text-xs px-2 py-1 rounded bg-bg-elevated text-error hover:bg-error/10 transition-colors"
        >
          Deregister
        </button>
      ),
    },
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">Devices</h1>
          <p className="text-sm text-text-secondary mt-1">Manage registered devices and their connection status</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors"
        >
          + Register Device
        </button>
      </div>

      {devices.length === 0 && !loading ? (
        <EmptyState
          icon="📱"
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

      <Modal open={showModal} title="Register Device" onClose={() => setShowModal(false)} size="md">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Device ID *</label>
            <input
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
              placeholder="e.g. macbook-pro-01"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Type</label>
            <select
              value={form.type}
              onChange={(e) => setForm({ ...form, type: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
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
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary font-mono focus:outline-none focus:border-accent"
              placeholder='{"hostname": "device.local", "port": 22}'
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowModal(false)} className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-lg text-sm hover:bg-border transition-colors">
              Cancel
            </button>
            <button
              onClick={handleRegister}
              disabled={submitting}
              className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors disabled:opacity-50"
            >
              {submitting ? 'Registering...' : 'Register'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
