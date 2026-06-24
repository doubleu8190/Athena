import { AlertTriangle } from 'lucide-react'
import Modal from './Modal'

interface ConfirmModalProps {
  open: boolean
  title?: string
  message: string
  confirmLabel?: string
  confirmColor?: 'success' | 'error' | 'accent'
  onConfirm: () => void
  onCancel: () => void
  loading?: boolean
}

export default function ConfirmModal({
  open,
  title = 'Confirm Action',
  message,
  confirmLabel = 'Confirm',
  confirmColor = 'accent',
  onConfirm,
  onCancel,
  loading = false,
}: ConfirmModalProps) {
  const colorClasses = {
    success: 'bg-success hover:opacity-90',
    error: 'bg-error hover:opacity-90',
    accent: 'bg-accent hover:bg-accent-hover',
  }

  return (
    <Modal open={open} title={title} onClose={onCancel} size="sm">
      <div className="space-y-4">
        <div className="flex items-center gap-2 text-sm text-text-secondary">
          <AlertTriangle size={20} className="text-warning flex-shrink-0" />
          <span>{message}</span>
        </div>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-xl text-sm font-medium hover:bg-border transition-all disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`px-4 py-2 text-white rounded-xl text-sm font-medium transition-all shadow-soft disabled:opacity-50 ${colorClasses[confirmColor]}`}
          >
            {loading ? 'Processing...' : confirmLabel}
          </button>
        </div>
      </div>
    </Modal>
  )
}
