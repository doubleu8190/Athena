import type { SubtaskEvent } from '../../stores/chatStore'
import Badge from '../ui/Badge'

interface Props {
  subtask: SubtaskEvent
}

const statusConfig: Record<string, { label: string; variant: 'green' | 'red' | 'yellow' | 'blue' | 'gray' | 'orange' }> = {
  running: { label: 'Running', variant: 'blue' },
  completed: { label: 'Done', variant: 'green' },
  failed: { label: 'Failed', variant: 'red' },
  skipped: { label: 'Skipped', variant: 'yellow' },
  fallback: { label: 'Fallback', variant: 'orange' },
}

export default function SubtaskCard({ subtask }: Props) {
  const config = statusConfig[subtask.status || 'running'] || statusConfig.running

  return (
    <div className="bg-white dark:bg-gray-900 rounded-lg border border-gray-200 dark:border-gray-800 px-3 py-2 shadow-sm">
      <div className="flex items-center gap-2">
        {subtask.status === 'running' && (
          <span className="w-1.5 h-1.5 bg-blue-500 rounded-full animate-pulse shrink-0" />
        )}
        <span className="text-xs font-mono text-gray-400 shrink-0">#{subtask.step}</span>
        <span className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">
          {subtask.tool_name || subtask.intent || 'Loading...'}
        </span>
        <Badge variant={config.variant} className="ml-auto shrink-0 text-[10px]">
          {config.label}
        </Badge>
      </div>
      {subtask.intent && (
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 ml-6">{subtask.intent}</p>
      )}
      {subtask.output_preview && (
        <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 ml-6 font-mono truncate">
          → {subtask.output_preview}
        </p>
      )}
      {subtask.error && (
        <p className="text-xs text-red-500 mt-1 ml-6 truncate">{subtask.error}</p>
      )}
      {subtask.status === 'fallback' && subtask.fallback_tool && (
        <p className="text-xs text-orange-500 mt-1 ml-6">
          Fallback: {subtask.original_tool} → {subtask.fallback_tool}
        </p>
      )}
    </div>
  )
}
