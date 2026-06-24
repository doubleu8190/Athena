import { ShieldAlert, Clock } from 'lucide-react'
import type { ConfirmRequired } from '../../stores/chatStore'

interface Props {
  confirm: ConfirmRequired
  onApprove: () => void
  onDeny: () => void
}

export default function ConfirmModal({ confirm, onApprove, onDeny }: Props) {
  const riskColor =
    confirm.risk_level === 'critical'
      ? 'text-red-600 dark:text-red-400'
      : confirm.risk_level === 'high'
        ? 'text-yellow-600 dark:text-yellow-400'
        : 'text-blue-600 dark:text-blue-400'

  return (
    <div className="bg-yellow-50/60 dark:bg-yellow-950/20 rounded-xl p-4 border border-yellow-200 dark:border-yellow-800">
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <ShieldAlert size={16} className={riskColor} />
          <span className="text-sm font-semibold text-gray-800 dark:text-gray-200">
            Confirm Action
          </span>
        </div>
        <span
          className={`text-xs px-2 py-0.5 rounded-full font-medium ${
            confirm.risk_level === 'critical'
              ? 'bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400'
              : confirm.risk_level === 'high'
                ? 'bg-yellow-100 dark:bg-yellow-950 text-yellow-700 dark:text-yellow-400'
                : 'bg-blue-100 dark:bg-blue-950 text-blue-700 dark:text-blue-400'
          }`}
        >
          {confirm.risk_level.toUpperCase()} RISK
        </span>
      </div>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">{confirm.preview_text}</p>

      {confirm.cooling_off_seconds > 0 && (
        <div className="flex items-center gap-1.5 text-xs text-gray-500 mb-3">
          <Clock size={12} />
          Cooling off: {confirm.cooling_off_seconds}s
        </div>
      )}

      <div className="flex gap-2">
        <button
          onClick={onApprove}
          className="px-4 py-1.5 text-sm font-medium bg-green-500 hover:bg-green-600 text-white rounded-lg transition-colors"
        >
          Approve
        </button>
        <button
          onClick={onDeny}
          className="px-4 py-1.5 text-sm font-medium bg-red-500 hover:bg-red-600 text-white rounded-lg transition-colors"
        >
          Deny
        </button>
      </div>
    </div>
  )
}
