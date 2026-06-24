import type { TaskProgress, SubtaskProgress } from '../../stores/chatStore'
import { Circle, Loader2, CheckCircle2, XCircle, Slash, CornerDownRight } from 'lucide-react'

interface SubtaskCardProps {
  task: TaskProgress
}

const statusConfig: Record<SubtaskProgress['status'], { Icon: typeof Circle; color: string; label: string }> = {
  pending: { Icon: Circle, color: 'text-text-muted', label: 'Pending' },
  running: { Icon: Loader2, color: 'text-accent', label: 'Running' },
  completed: { Icon: CheckCircle2, color: 'text-success', label: 'Done' },
  failed: { Icon: XCircle, color: 'text-error', label: 'Failed' },
  skipped: { Icon: Slash, color: 'text-warning', label: 'Skipped' },
  fallback: { Icon: CornerDownRight, color: 'text-warning', label: 'Fallback' },
}

function SubtaskRow({ subtask }: { subtask: SubtaskProgress }) {
  const { Icon, color, label } = statusConfig[subtask.status]

  return (
    <div className="flex items-start gap-3 py-2 px-3 rounded-xl hover:bg-bg/50 transition-colors">
      <span className={`mt-0.5 ${color} ${subtask.status === 'running' ? 'animate-spin' : ''}`}>
        <Icon size={16} />
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-medium text-text-primary">{subtask.tool_name}</span>
          <span className={`text-xs px-1.5 py-0.5 rounded-full ${color} bg-current/10`}>
            {label}
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
  const statusAccent = task.status === 'completed' ? 'border-t-success/50' : task.status === 'failed' ? 'border-t-error/50' : 'border-t-accent/50'

  return (
    <div className={`shadow-soft bg-bg-surface rounded-2xl overflow-hidden border-t-[3px] ${statusAccent}`}>
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border-subtle">
        <div className="flex items-center gap-2">
          {task.status === 'executing' && (
            <span className="w-2 h-2 bg-accent rounded-full animate-pulse" />
          )}
          {task.status === 'completed' && (
            <CheckCircle2 size={16} className="text-success" />
          )}
          {task.status === 'failed' && (
            <XCircle size={16} className="text-error" />
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
            <Loader2 size={16} className="animate-spin" />
            Generating plan...
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-4 py-2.5 border-t border-border-subtle bg-bg/30">
        <p className="text-xs text-text-muted truncate">{task.summary}</p>
        {task.error && (
          <p className="text-xs text-error mt-1">{task.error}</p>
        )}
      </div>
    </div>
  )
}
