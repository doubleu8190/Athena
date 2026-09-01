import { useCallback, useEffect, useState } from "react"
import {
  ClipboardCheck,
  ThumbsUp,
  ThumbsDown,
  Timer,
  ChevronLeft,
  ChevronRight,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { ApprovalLog, ApprovalStats } from "../../types"
import { formatDateTime, truncate } from "../../utils/format"
import ViewShell from "./ViewShell"
import ApprovalFilters from "../ctx/ApprovalFilters"
import type { DecisionFilter } from "../ctx/ApprovalFilters"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import DataTable from "../ui/DataTable"
import type { Column } from "../ui/DataTable"

const PAGE_SIZE = 20

const DECISION_META = {
  approved: { tone: "success", label: "通过" },
  denied: { tone: "danger", label: "拒绝" },
  timeout: { tone: "warning", label: "超时" },
} as const

const RISK_TONE = {
  low: "success",
  medium: "warning",
  high: "danger",
} as const

function ApprovalsView() {
  const [logs, setLogs] = useState<ApprovalLog[]>([])
  const [stats, setStats] = useState<ApprovalStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<DecisionFilter>("all")
  const [page, setPage] = useState(0)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const [logsRes, statsRes] = await Promise.all([
        apiClient.listApprovalLogs({ limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
        apiClient.getApprovalStats(),
      ])
      setLogs(
        decision === "all"
          ? logsRes
          : logsRes.filter((l) => l.decision === decision),
      )
      setStats(statsRes)
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [page, decision])

  useEffect(() => {
    reload()
  }, [reload])

  const hasMore = logs.length === PAGE_SIZE

  const columns: Column<ApprovalLog>[] = [
    {
      key: "timestamp",
      header: "时间",
      className: "whitespace-nowrap",
      render: (l) => <span className="text-xs text-athena-muted">{formatDateTime(l.timestamp)}</span>,
    },
    {
      key: "tool",
      header: "工具",
      className: "whitespace-nowrap",
      render: (l) => <span className="font-mono text-sm">{l.tool_name}</span>,
    },
    {
      key: "session",
      header: "会话",
      className: "whitespace-nowrap",
      render: (l) => (
        <span className="text-xs text-athena-muted font-mono">{truncate(l.session_id, 20)}</span>
      ),
    },
    {
      key: "risk",
      header: "风险",
      render: (l) => <Badge tone={RISK_TONE[l.risk_level]}>{l.risk_level}</Badge>,
    },
    {
      key: "decision",
      header: "决策",
      render: (l) => <Badge tone={DECISION_META[l.decision].tone}>{DECISION_META[l.decision].label}</Badge>,
    },
    {
      key: "latency",
      header: "响应耗时",
      className: "whitespace-nowrap",
      render: (l) => <span className="text-xs text-athena-muted">{Math.round(l.decision_time_ms)} ms</span>,
    },
    {
      key: "arguments",
      header: "参数",
      className: "max-w-[220px]",
      render: (l) => (
        <span className="text-xs text-athena-muted font-mono break-all">
          {truncate(JSON.stringify(l.arguments), 60)}
        </span>
      ),
    },
  ]

  return (
    <ViewShell
      side={
        <ApprovalFilters
          decision={decision}
          onDecisionChange={(d) => {
            setDecision(d)
            setPage(0)
          }}
          onRefresh={reload}
        />
      }
    >
      <Toolbar title="审批日志" subtitle="工具调用的审批决策记录" />
      <div className="p-6 space-y-6">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="今日审批" value={stats?.today_total ?? "—"} icon={ClipboardCheck} />
              <StatCard label="通过率" value={stats ? `${Math.round((stats.approval_rate ?? 0) * 100)}%` : "—"} icon={ThumbsUp} />
              <StatCard label="今日拒绝" value={stats?.today_denied ?? "—"} icon={ThumbsDown} />
              <StatCard label="今日超时" value={stats?.today_timeout ?? "—"} icon={Timer} />
            </div>

            <DataTable
              columns={columns}
              rows={logs}
              rowKey={(l) => l.id}
              loading={loading}
              emptyText="暂无审批日志"
            />

            <div className="flex items-center justify-end gap-2">
              <button
                onClick={() => setPage((p) => Math.max(0, p - 1))}
                disabled={page === 0}
                className="btn-ghost px-2 py-1 text-xs"
              >
                <ChevronLeft className="w-4 h-4" /> 上一页
              </button>
              <button
                onClick={() => setPage((p) => p + 1)}
                disabled={!hasMore}
                className="btn-ghost px-2 py-1 text-xs"
              >
                下一页 <ChevronRight className="w-4 h-4" />
              </button>
            </div>
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default ApprovalsView
