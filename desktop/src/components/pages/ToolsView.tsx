import { useCallback, useEffect, useState } from "react"
import { Cpu, CheckCircle2, ShieldAlert, Activity, Loader2 } from "lucide-react"
import { apiClient } from "../../api/client"
import type { ToolInfo, ToolListResponse } from "../../types"
import { formatRelative, truncate } from "../../utils/format"
import ViewShell from "./ViewShell"
import ToolFilters from "../ctx/ToolFilters"
import type { EnabledFilter, RiskFilter } from "../ctx/ToolFilters"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import Toggle from "../ui/Toggle"
import DataTable from "../ui/DataTable"
import type { Column } from "../ui/DataTable"

const RISK_TONE = {
  low: "success",
  medium: "warning",
  high: "danger",
} as const

function ToolsView() {
  const [data, setData] = useState<ToolListResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [search, setSearch] = useState("")
  const [enabledFilter, setEnabledFilter] = useState<EnabledFilter>("all")
  const [riskFilter, setRiskFilter] = useState<RiskFilter>("all")

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      setData(await apiClient.listTools())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    reload()
  }, [reload])

  const [busy, setBusy] = useState<string | null>(null)

  const handleToggle = async (tool: ToolInfo) => {
    const target = !tool.enabled
    // 乐观更新
    setData((d) =>
      d
        ? {
            ...d,
            items: d.items.map((i) =>
              i.name === tool.name ? { ...i, enabled: target } : i,
            ),
            enabled: d.enabled + (target ? 1 : -1),
          }
        : d,
    )
    setBusy(tool.name)
    try {
      await apiClient.setToolEnabled(tool.name, target)
    } catch (e) {
      // 失败回滚
      setData((d) =>
        d
          ? {
              ...d,
              items: d.items.map((i) =>
                i.name === tool.name ? { ...i, enabled: !target } : i,
              ),
              enabled: d.enabled - (target ? 1 : -1),
            }
          : d,
      )
      console.error("toggle failed", e)
    } finally {
      setBusy(null)
    }
  }

  const filtered = (data?.items ?? []).filter((t) => {
    if (enabledFilter === "enabled" && !t.enabled) return false
    if (enabledFilter === "disabled" && t.enabled) return false
    if (riskFilter !== "all" && t.risk_level !== riskFilter) return false
    if (search && !t.name.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const columns: Column<ToolInfo>[] = [
    {
      key: "name",
      header: "工具",
      className: "whitespace-nowrap",
      render: (t) => (
        <div>
          <div className="font-mono text-sm">{t.name}</div>
          <div className="text-[11px] text-athena-muted">{t.execution_mode}</div>
        </div>
      ),
    },
    {
      key: "description",
      header: "描述",
      className: "max-w-[320px]",
      render: (t) => (
        <div className="text-xs text-athena-muted break-words">{truncate(t.description, 100)}</div>
      ),
    },
    {
      key: "risk",
      header: "风险",
      render: (t) => <Badge tone={RISK_TONE[t.risk_level]}>{t.risk_level}</Badge>,
    },
    {
      key: "approval",
      header: "审批",
      render: (t) =>
        t.require_approval ? <Badge tone="warning">需审批</Badge> : <span className="text-xs text-athena-muted">—</span>,
    },
    {
      key: "last_called",
      header: "最近调用",
      className: "whitespace-nowrap",
      render: (t) => (
        <span className="text-xs text-athena-muted">{t.last_called_at ? formatRelative(t.last_called_at) : "从未"}</span>
      ),
    },
    {
      key: "enabled",
      header: "启用",
      className: "whitespace-nowrap",
      render: (t) =>
        busy === t.name ? (
          <Loader2 className="w-4 h-4 animate-spin text-athena-muted" />
        ) : (
          <Toggle checked={t.enabled} onChange={() => handleToggle(t)} label={`启用 ${t.name}`} />
        ),
    },
  ]

  return (
    <ViewShell
      side={
        <ToolFilters
          search={search}
          onSearchChange={setSearch}
          enabled={enabledFilter}
          onEnabledChange={setEnabledFilter}
          risk={riskFilter}
          onRiskChange={setRiskFilter}
          onRefresh={reload}
        />
      }
    >
      <Toolbar
        title="工具管理"
        subtitle={data ? `共 ${data.total} 个工具，已启用 ${data.enabled} 个` : "加载中…"}
      />
      <div className="p-6 space-y-6">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="工具总数" value={data?.total ?? "—"} icon={Cpu} />
              <StatCard label="已启用" value={data?.enabled ?? "—"} icon={CheckCircle2} />
              <StatCard label="高风险" value={data?.high_risk ?? "—"} icon={ShieldAlert} />
              <StatCard label="今日调用" value={data?.calls_today ?? "—"} icon={Activity} />
            </div>

            <DataTable
              columns={columns}
              rows={filtered}
              rowKey={(t) => t.name}
              loading={loading}
              emptyText="没有匹配的工具"
            />
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default ToolsView
