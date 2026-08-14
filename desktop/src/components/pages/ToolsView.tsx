import { useCallback, useEffect, useState, useRef } from "react"
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

const RISK_OPTIONS: Array<"low" | "medium" | "high"> = ["low", "medium", "high"]

/** 行内风险等级下拉选择器 */
function RiskLevelSelect({
  value,
  onChange,
  disabled,
}: {
  value: "low" | "medium" | "high"
  onChange: (v: "low" | "medium" | "high") => void
  disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener("mousedown", handler)
    return () => document.removeEventListener("mousedown", handler)
  }, [open])

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((o) => !o)}
        className="cursor-pointer disabled:cursor-not-allowed disabled:opacity-50"
      >
        <Badge tone={RISK_TONE[value]}>{value}</Badge>
      </button>
      {open && (
        <div className="absolute z-50 mt-1 left-0 min-w-[80px] rounded-md border border-athena-border bg-athena-surface shadow-lg py-0.5">
          {RISK_OPTIONS.map((opt) => (
            <button
              key={opt}
              type="button"
              className={`w-full text-left px-3 py-1 text-xs hover:bg-athena-hover ${
                opt === value ? "text-athena-accent font-medium" : "text-athena-text"
              }`}
              onClick={() => {
                onChange(opt)
                setOpen(false)
              }}
            >
              <Badge tone={RISK_TONE[opt]}>{opt}</Badge>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

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

  /** 乐观更新通用方法 */
  const optimisticUpdate = useCallback(
    (toolName: string, patch: Partial<ToolInfo>, apiCall: Promise<unknown>) => {
      setData((d) =>
        d
          ? {
              ...d,
              items: d.items.map((i) =>
                i.name === toolName ? { ...i, ...patch } : i,
              ),
            }
          : d,
      )
      setBusy(toolName)
      apiCall
        .then(() => {
          // 成功后刷新以获取准确的统计值（如 high_risk 计数）
          reload()
        })
        .catch((e) => {
          // 失败回滚：重新加载
          console.error("update failed", e)
          reload()
        })
        .finally(() => setBusy(null))
    },
    [reload],
  )

  const handleToggle = useCallback(
    (tool: ToolInfo) => {
      const target = !tool.enabled
      // 乐观更新 enabled 计数
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
      apiClient
        .setToolEnabled(tool.name, target)
        .then(() => reload())
        .catch((e) => {
          console.error("toggle failed", e)
          reload()
        })
        .finally(() => setBusy(null))
    },
    [reload],
  )

  const handleRiskChange = useCallback(
    (tool: ToolInfo, newRisk: "low" | "medium" | "high") => {
      if (newRisk === tool.risk_level) return
      const oldRisk = tool.risk_level
      // 乐观更新 + high_risk 计数
      setData((d) => {
        if (!d) return d
        const oldHigh = d.items.filter((i) => i.risk_level === "high").length
        const newItems = d.items.map((i) =>
          i.name === tool.name ? { ...i, risk_level: newRisk } : i,
        )
        const newHigh = newItems.filter((i) => i.risk_level === "high").length
        return { ...d, items: newItems, high_risk: d.high_risk - oldHigh + newHigh }
      })
      setBusy(tool.name)
      apiClient
        .updateTool(tool.name, { risk_level: newRisk })
        .then(() => reload())
        .catch((e) => {
          console.error("risk update failed", e)
          // 回滚
          setData((d) => {
            if (!d) return d
            const rolled = d.items.map((i) =>
              i.name === tool.name ? { ...i, risk_level: oldRisk } : i,
            )
            const oldHigh = rolled.filter((i) => i.risk_level === "high").length
            return { ...d, items: rolled, high_risk: oldHigh }
          })
        })
        .finally(() => setBusy(null))
    },
    [reload],
  )

  const handleApprovalToggle = useCallback(
    (tool: ToolInfo) => {
      const target = !tool.require_approval
      optimisticUpdate(
        tool.name,
        { require_approval: target },
        apiClient.updateTool(tool.name, { require_approval: target }),
      )
    },
    [optimisticUpdate],
  )

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
      render: (t) =>
        busy === t.name ? (
          <Loader2 className="w-4 h-4 animate-spin text-athena-muted" />
        ) : (
          <RiskLevelSelect
            value={t.risk_level}
            onChange={(v) => handleRiskChange(t, v)}
            disabled={busy === t.name}
          />
        ),
    },
    {
      key: "approval",
      header: "审批",
      render: (t) =>
        busy === t.name ? (
          <Loader2 className="w-4 h-4 animate-spin text-athena-muted" />
        ) : (
          <Toggle
            checked={t.require_approval}
            onChange={() => handleApprovalToggle(t)}
            label={`${t.require_approval ? "取消" : "启用"} ${t.name} 审批`}
          />
        ),
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
