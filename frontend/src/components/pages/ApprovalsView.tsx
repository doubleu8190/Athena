import { useCallback, useEffect, useState } from "react"
import {
  ClipboardCheck,
  ThumbsUp,
  ThumbsDown,
  ChevronLeft,
  ChevronRight,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { ApprovalHistoryEntry, ApprovalStats } from "../../types"
import { formatDateTime, truncate } from "../../utils/format"
import ViewShell from "./ViewShell"
import ApprovalFilters from "../ctx/ApprovalFilters"
import type { DecisionFilter } from "../ctx/ApprovalFilters"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import DataTable from "../ui/DataTable"
import type { Column } from "../ui/DataTable"
import type { ApprovalRequest } from "../../types"
import { ApprovalCard } from "../ApprovalDialog"

const PAGE_SIZE = 20

const DECISION_META = {
  approved: { tone: "success", label: "通过" },
  denied: { tone: "danger", label: "拒绝" },
  expired: { tone: "warning", label: "已过期" },
  cancelled: { tone: "default", label: "取消" },
  resolved: { tone: "default", label: "已处理" },
} as const

const RISK_TONE = {
  low: "success",
  medium: "warning",
  high: "danger",
} as const

function ApprovalsView() {
  const [logs, setLogs] = useState<ApprovalHistoryEntry[]>([])
  const [stats, setStats] = useState<ApprovalStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [decision, setDecision] = useState<DecisionFilter>("all")
  const [page, setPage] = useState(0)
  const [pending, setPending] = useState<ApprovalRequest[]>([])

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const [logsRes, statsRes, pendingRes] = await Promise.all([
        apiClient.listApprovalLogs({ limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
        apiClient.getApprovalStats(),
        apiClient.listPendingApprovals(),
      ])
      setLogs(
        decision === "all"
          ? logsRes
          : logsRes.filter((l) => l.resolution === decision),
      )
      setStats(statsRes)
      setPending(pendingRes)
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

  const columns: Column<ApprovalHistoryEntry>[] = [
    {
      key: "created_at",
      header: "时间",
      className: "whitespace-nowrap",
      render: (l) => <span className="text-xs text-athena-muted">{formatDateTime(l.created_at)}</span>,
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
      render: (l) => <Badge tone={RISK_TONE[l.risk_level] ?? "default"}>{l.risk_level || "未知"}</Badge>,
    },
    {
      key: "decision",
      header: "决策",
      render: (l) => {
        // Keep historical or newer server decisions visible even before this UI knows their label.
        const meta = DECISION_META[l.resolution as keyof typeof DECISION_META] ?? {
          tone: "default" as const,
          label: l.resolution || "未知",
        }
        return <Badge tone={meta.tone}>{meta.label}</Badge>
      },
    },
    {
      key: "expires_at",
      header: "过期时间",
      className: "whitespace-nowrap",
      render: (l) => <span className="text-xs text-athena-muted">{formatDateTime(l.expires_at)}</span>,
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
            {pending.length ? <section className="space-y-3">
              <div className="flex items-center justify-between"><h2 className="text-sm font-semibold">待审批</h2><Badge tone="warning">{pending.length}</Badge></div>
              {Object.entries(pending.reduce<Record<string, ApprovalRequest[]>>((groups, item) => {
                const key = item.approval_batch_id || item.approval_id
                groups[key] = [...(groups[key] || []), item]
                return groups
              }, {})).map(([batchId, requests]) => <ApprovalCard key={batchId} batchId={batchId} requests={requests} onSubmit={async (_id, approvalRequests, decisions) => {
                await apiClient.respondApprovalsIndividually(approvalRequests, decisions)
                await reload()
              }} />)}
            </section> : <div className="rounded-md border border-athena-border px-4 py-3 text-sm text-athena-muted">当前没有待审批操作</div>}
            <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
              <StatCard label="待审批" value={stats?.pending ?? "—"} icon={ClipboardCheck} />
              <StatCard label="已通过" value={stats?.approved ?? "—"} icon={ThumbsUp} />
              <StatCard label="已拒绝" value={stats?.denied ?? "—"} icon={ThumbsDown} />
            </div>

            <DataTable
              columns={columns}
              rows={logs}
              rowKey={(l) => l.approval_id}
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
