import { useState } from 'react'
import Modal from '../shared/Modal'

interface ConfirmModalProps {
  open: boolean
  taskId: string
  step: number
  description?: string
  onConfirm: (approved: boolean) => void
}

export default function ConfirmModal({ open, taskId, step, description, onConfirm }: ConfirmModalProps) {
  const [loading, setLoading] = useState(false)

  const handleAction = async (approved: boolean) => {
    setLoading(true)
    try {
      await onConfirm(approved)
    } finally {
      setLoading(false)
    }
  }

  return (
    <Modal open={open} title="Confirmation Required" onClose={() => {}} size="sm">
      <div className="space-y-4">
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <span className="text-warning text-lg">⚠</span>
          <span>A subtask requires your approval to proceed.</span>
        </div>

        <div className="bg-bg rounded-lg px-4 py-3 space-y-2 text-sm">
          <div className="flex justify-between">
            <span className="text-text-muted">Task</span>
            <span className="text-text-primary font-mono text-xs">{taskId}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-text-muted">Step</span>
            <span className="text-text-primary">#{step}</span>
          </div>
          {description && (
            <div className="flex justify-between">
              <span className="text-text-muted">Description</span>
              <span className="text-text-primary text-right max-w-[60%]">{description}</span>
            </div>
          )}
        </div>

        <div className="flex gap-3 justify-end">
          <button
            onClick={() => handleAction(false)}
            disabled={loading}
            className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-lg text-sm font-medium hover:bg-border transition-colors disabled:opacity-50"
          >
            Deny
          </button>
          <button
            onClick={() => handleAction(true)}
            disabled={loading}
            className="px-4 py-2 bg-success text-white rounded-lg text-sm font-medium hover:opacity-90 transition-opacity disabled:opacity-50"
          >
            {loading ? 'Processing...' : 'Approve'}
          </button>
        </div>
      </div>
    </Modal>
  )
}
