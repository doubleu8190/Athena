import type { TaskProgress, SubtaskProgress } from '../../stores/chatStore'

interface SubtaskCardProps {
  task: TaskProgress
}

const statusConfig: Record<SubtaskProgress['status'], { icon: string; color: string; label: string }> = {
  pending: { icon: '○', color: 'text-text-muted', label: 'Pending' },
  running: { icon: '◌', color: 'text-accent', label: 'Running' },
  completed: { icon: '✓', color: 'text-success', label: 'Done' },
  failed: { icon: '✗', color: 'text-error', label: 'Failed' },
  skipped: { icon: '⊘', color: 'text-warning', label: 'Skipped' },
  fallback: { icon: '↳', color: 'text-warning', label: 'Fallback' },
}

function SubtaskRow({ subtask }: { subtask: SubtaskProgress }) {
  const config = statusConfig[subtask.status]

  return (
    <div className="flex items-start gap-3 py-2 px-3 rounded-lg hover:bg-bg/50 transition-colors">
      <span className={`mt-0.5 text-sm font-bold ${config.color} ${subtask.status === 'running' ? 'animate-spin' : ''}`}>
        {config.icon}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-text-primary">{subtask.tool_name}</span>
          <span className={`text-xs px-1.5 py-0.5 rounded-full ${config.color} bg-current/10`}>
            {config.label}
          </span>
        </div>
        {subtask.intent && (
          <p className="text-xs text-text-secondary mt-0.5 truncate">{subtask.intent}</p>
        )}
        {subtask.output_preview && (
          <p className="text-xs text-text-muted mt-1 line-clamp-2">{subtask.output_preview}</p>
        )}
        {subtask.error && (
          <p className="text-xs text-error mt-1">{subtask.error}</p>
        )}
        {subtask.fallback_from && subtask.fallback_tool && (
          <p className="text-xs text-warning mt-1">
            Fallback: {subtask.fallback_from} → {subtask.fallback_tool}
          </p>
        )}
      </div>
    </div>
  )
}

export default function SubtaskCard({ task }: SubtaskCardProps) {
  const completedCount = task.subtasks.filter((s) => s.status === 'completed').length
  const statusColor = task.status === 'completed' ? 'border-success/30' : task.status === 'failed' ? 'border-error/30' : 'border-accent/30'

  return (
    <div className={`border ${statusColor} bg-bg-surface rounded-xl overflow-hidden`}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          {task.status === 'executing' && (
            <span className="w-2 h-2 bg-accent rounded-full animate-pulse" />
          )}
          {task.status === 'completed' && (
            <span className="text-success font-bold">✓</span>
          )}
          {task.status === 'failed' && (
            <span className="text-error font-bold">✗</span>
          )}
          <span className="text-sm font-medium text-text-primary">
            Task {task.task_id.slice(0, 8)}
          </span>
        </div>
        <span className="text-xs text-text-muted">
          {completedCount}/{task.subtasks.length} steps
        </span>
      </div>

      {/* Subtask list */}
      <div className="divide-y divide-border-subtle">
        {task.subtasks.map((subtask) => (
          <SubtaskRow key={subtask.step} subtask={subtask} />
        ))}
        {task.subtasks.length === 0 && task.status === 'generating' && (
          <div className="flex items-center gap-2 px-4 py-4 text-sm text-text-muted">
            <span className="animate-spin">◌</span>
            Generating plan...
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2 border-t border-border bg-bg/30">
        <p className="text-xs text-text-muted truncate">{task.summary}</p>
        {task.error && (
          <p className="text-xs text-error mt-1">{task.error}</p>
        )}
      </div>
    </div>
  )
}
