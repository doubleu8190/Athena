import type { ToolCall } from "../types"
import { safeStringify } from "../utils/safeStringify"
import { summarizeToolCall, toolDisplayData } from "../utils/toolSummary"

interface ToolDetailPanelProps {
  toolCall: ToolCall
  debug?: boolean
}

export function ToolDetailPanel({ toolCall, debug = false }: ToolDetailPanelProps) {
  const summary = summarizeToolCall(toolDisplayData(toolCall))
  const argCount = Object.keys(toolCall.arguments ?? {}).length

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-athena-border/60 bg-athena-bg/30 px-3 py-2">
        <div className="text-[11px] uppercase text-athena-muted">Summary</div>
        <div className="mt-1 text-xs text-athena-text/90">{summary.summary}</div>
        {summary.outputPreview && (
          <pre className="mt-2 max-h-28 overflow-auto whitespace-pre-wrap rounded bg-athena-bg px-2 py-1.5 text-[11px] text-athena-text/75">
            {summary.outputPreview}
          </pre>
        )}
      </div>

      {argCount > 0 && (
        <DetailBlock title={`Arguments (${argCount})`} value={toolCall.arguments} />
      )}

      {toolCall.output && (
        <DetailBlock title="Output" value={toolCall.output} maxLen={debug ? 50000 : 12000} />
      )}

      {toolCall.error && (
        <div className="rounded-md border border-red-500/25 bg-red-500/10 px-3 py-2 text-xs text-athena-danger whitespace-pre-wrap">
          {toolCall.error}
        </div>
      )}

      {debug && (
        <DetailBlock
          title="Metadata"
          value={{
            id: toolCall.id,
            step_id: toolCall.step_id,
            run_id: toolCall.run_id,
            started_at: toolCall.started_at,
            completed_at: toolCall.completed_at,
            duration_ms: toolCall.duration_ms,
            risk_level: toolCall.risk_level,
            error_stack: toolCall.error_stack,
          }}
          maxLen={12000}
        />
      )}
    </div>
  )
}

function DetailBlock({
  title,
  value,
  maxLen = 12000,
}: {
  title: string
  value: unknown
  maxLen?: number
}) {
  return (
    <div>
      <div className="mb-1 text-[11px] uppercase text-athena-muted">{title}</div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md bg-athena-bg/80 px-3 py-2 text-xs text-athena-text/80">
        {safeStringify(value, maxLen)}
      </pre>
    </div>
  )
}
