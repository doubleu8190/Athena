import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  Activity,
  Check,
  Clipboard,
  Cpu,
  RefreshCw,
  Search,
  Server,
  ShieldAlert,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { WorkerRegistryInstance } from "../../types"
import {
  registryState,
  registryStateLabel,
  registryStateTone,
  registryIdentityMessage,
  registrySidebarState,
} from "../../utils/workerRegistryProjection"
import type { RegistryFilter } from "../../utils/workerRegistryProjection"
import Badge from "../ui/Badge"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import ViewShell from "./ViewShell"

const FILTERS: { value: RegistryFilter; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "available", label: "可接单" },
  { value: "paused", label: "暂停接单" },
  { value: "full", label: "容量已满" },
  { value: "not_ready", label: "不可用" },
  { value: "disabled", label: "已禁用" },
]

function formatDate(value: string | null): string {
  if (!value) return "尚未检查"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? "时间不可用" : date.toLocaleString()
}

function WorkerRegistryView() {
  const [instances, setInstances] = useState<WorkerRegistryInstance[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [query, setQuery] = useState("")
  const [filter, setFilter] = useState<RegistryFilter>("all")
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const inFlight = useRef(false)
  const mounted = useRef(false)
  const hasLoaded = useRef(false)

  const reload = useCallback(async () => {
    if (inFlight.current) return
    inFlight.current = true
    if (hasLoaded.current) setRefreshing(true)
    else setLoading(true)
    try {
      const response = await apiClient.listWorkerRegistryInstances()
      if (!mounted.current) return
      setInstances(response)
      hasLoaded.current = true
      setLoaded(true)
      setError(null)
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : String(cause))
      }
    } finally {
      inFlight.current = false
      if (mounted.current) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    void reload()
    let interval: ReturnType<typeof setInterval> | null = null
    const updatePolling = () => {
      if (interval !== null) clearInterval(interval)
      interval = null
      if (document.visibilityState === "visible") {
        interval = setInterval(() => void reload(), 15_000)
      }
    }
    updatePolling()
    document.addEventListener("visibilitychange", updatePolling)
    return () => {
      mounted.current = false
      if (interval !== null) clearInterval(interval)
      document.removeEventListener("visibilitychange", updatePolling)
    }
  }, [reload])

  const filteredInstances = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase()
    return instances.filter((instance) => {
      const matchesSearch = !normalizedQuery ||
        instance.instance_key.toLocaleLowerCase().includes(normalizedQuery) ||
        instance.contract_id.toLocaleLowerCase().includes(normalizedQuery)
      return matchesSearch && (filter === "all" || registryState(instance) === filter)
    })
  }, [filter, instances, query])

  useEffect(() => {
    if (selectedId && filteredInstances.some((instance) => instance.instance_id === selectedId)) return
    setSelectedId(filteredInstances[0]?.instance_id ?? null)
  }, [filteredInstances, selectedId])

  const active = instances.find((instance) => instance.instance_id === selectedId) ?? null
  const sidebarState = registrySidebarState(
    loading,
    loaded,
    instances.length,
    filteredInstances.length,
  )
  const availableCount = instances.filter((instance) => registryState(instance) === "available").length
  const fullCount = instances.filter((instance) => registryState(instance) === "full").length
  const unavailableCount = instances.filter((instance) => {
    const state = registryState(instance)
    return state === "not_ready" || state === "disabled" || state === "paused"
  }).length

  return (
    <ViewShell
      side={
        <div className="p-4 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-athena-text">Worker 实例</h2>
            <button
              onClick={() => void reload()}
              disabled={refreshing || loading}
              title="刷新"
              aria-label="刷新 Worker 注册实例"
              className="p-1.5 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40 disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin" : ""}`} />
            </button>
          </div>
          <label className="relative block">
            <Search className="absolute left-2.5 top-2.5 w-4 h-4 text-athena-muted" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索实例键或契约 ID"
              className="input w-full pl-8 text-xs"
              aria-label="搜索实例键或契约 ID"
            />
          </label>
          <select
            value={filter}
            onChange={(event) => setFilter(event.target.value as RegistryFilter)}
            className="input w-full text-xs"
            aria-label="按状态筛选"
          >
            {FILTERS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
          {sidebarState === "loading" ? (
            <div className="text-xs text-athena-muted">正在读取注册实例…</div>
          ) : sidebarState === "load_failed" ? (
            <div className="space-y-2 text-xs text-athena-danger">
              <p>注册实例加载失败{error ? `：${error}` : ""}</p>
              <button onClick={() => void reload()} className="btn-ghost text-xs">重试</button>
            </div>
          ) : sidebarState === "empty" ? (
            <div className="text-xs text-athena-muted leading-relaxed">
              暂无已注册的 Worker。Worker 启动后会自动注册。
            </div>
          ) : sidebarState === "no_matches" ? (
            <div className="text-xs text-athena-muted leading-relaxed">没有匹配的实例，请调整搜索词或状态筛选。</div>
          ) : (
            <div className="space-y-1">
              {filteredInstances.map((instance) => {
                const state = registryState(instance)
                const selected = selectedId === instance.instance_id
                return (
                  <button
                    key={instance.instance_id}
                    onClick={() => setSelectedId(instance.instance_id)}
                    className={`w-full rounded-lg px-3 py-2 text-left transition-colors ${selected ? "bg-athena-accent/15 border border-athena-accent/40" : "border border-transparent hover:bg-athena-border/40"}`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-sm font-medium">{instance.instance_key}</span>
                      <span className="shrink-0 whitespace-nowrap">
                        <Badge tone={registryStateTone(state)}>{registryStateLabel(state)}</Badge>
                      </span>
                    </div>
                    <div className="mt-1 flex items-center justify-between gap-2 text-xs text-athena-muted">
                      <span className="truncate">{instance.contract_id} · v{instance.contract_revision}</span>
                      <span className="shrink-0">{instance.current_concurrency}/{instance.max_concurrency}</span>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </div>
      }
    >
      <Toolbar
        title="Worker 注册中心"
        subtitle={`已注册 ${instances.length} 个 · 页面可见时每 15 秒更新`}
        actions={<button onClick={() => void reload()} disabled={refreshing || loading} className="btn-ghost"><RefreshCw className={`h-4 w-4 ${refreshing ? "animate-spin" : ""}`} />刷新</button>}
      />
      <div className="p-6 space-y-6">
        {error ? (
          <div className={`rounded-lg border px-4 py-3 text-sm ${instances.length ? "border-athena-warning/40 text-athena-warning" : "border-athena-danger/40 text-athena-danger"}`}>
            {instances.length ? `刷新失败，当前保留上次数据：${error}` : `加载失败：${error}。请重试。`}
          </div>
        ) : null}
        {loading && !loaded ? (
          <div className="py-12 text-center text-sm text-athena-muted">正在读取注册中心…</div>
        ) : !loaded ? (
          <div className="card p-8 text-center">
            <h2 className="font-medium">注册中心暂时无法读取</h2>
            <p className="mt-1 text-sm text-athena-muted">检查后端连接后重试。</p>
            <button onClick={() => void reload()} className="btn-primary mt-4">重试</button>
          </div>
        ) : instances.length === 0 ? (
          <div className="card p-8 text-center">
            <Server className="mx-auto h-8 w-8 text-athena-muted" />
            <h2 className="mt-3 font-medium">暂无已注册的 Worker</h2>
            <p className="mt-1 text-sm text-athena-muted">Worker 启动并完成注册后会显示在这里。</p>
          </div>
        ) : (
          <>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard label="已注册" value={instances.length} icon={Server} />
              <StatCard label="可接单" value={availableCount} icon={Activity} hint="健康、接单且有剩余容量" />
              <StatCard label="不可用" value={unavailableCount} icon={ShieldAlert} hint="含暂停接单、健康异常或禁用" />
              <StatCard label="容量已满" value={fullCount} icon={Cpu} />
            </div>
            {active ? <InstanceDetails key={active.instance_id} instance={active} /> : (
              <div className="card p-8 text-center text-sm text-athena-muted">当前筛选条件下没有可查看的实例。</div>
            )}
          </>
        )}
      </div>
    </ViewShell>
  )
}

function InstanceDetails({ instance }: { instance: WorkerRegistryInstance }) {
  const [copied, setCopied] = useState<string | null>(null)
  const state = registryState(instance)

  const copySchema = async (name: string, schema: Record<string, unknown>) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(schema, null, 2))
      setCopied(name)
      window.setTimeout(() => setCopied((current) => current === name ? null : current), 1800)
    } catch {
      setCopied(`${name}-failed`)
    }
  }

  return (
    <div className="card max-w-5xl">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-athena-border px-5 py-4">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-athena-accent/10 text-athena-accent"><Server className="h-5 w-5" /></div>
          <div className="min-w-0">
            <h2 className="break-all font-semibold">{instance.instance_key}</h2>
            <p className="text-xs text-athena-muted">{instance.contract_id} · v{instance.contract_revision}</p>
          </div>
        </div>
        <Badge tone={registryStateTone(state)}>{registryStateLabel(state)}</Badge>
      </div>
      <div className="space-y-6 p-5">
        <section>
          <h3 className="mb-3 text-sm font-semibold">实例状态</h3>
          <div className="grid gap-3 text-sm sm:grid-cols-2 xl:grid-cols-3">
            <Info label="健康状态" value={instance.status === "ready" ? "健康" : instance.status === "disabled" ? "已禁用" : "不可用"} />
            <Info label="接单状态" value={instance.accepting_tasks ? "正在接单" : "暂停接单"} />
            <Info label="身份检查" value={registryIdentityMessage(instance.health_identity_valid)} />
            <Info label="容量占用" value={`${instance.current_concurrency} / ${instance.max_concurrency}`} />
            <Info label="可用容量" value={String(instance.available_concurrency)} />
            <Info label="执行超时" value={`${instance.execution_timeout_seconds} 秒`} />
            <Info label="最近健康检查" value={formatDate(instance.last_health_check_at)} />
            <Info label="连续成功 / 失败" value={`${instance.consecutive_health_successes} / ${instance.consecutive_health_failures}`} />
            <Info label="服务主体" value={instance.service_principal} />
            <Info label="实例 ID" value={instance.instance_id} />
            <Info label="注册地址" value={instance.base_url} />
            <Info label="最近更新" value={formatDate(instance.updated_at)} />
          </div>
        </section>
        <section className="grid gap-4 md:grid-cols-2">
          <DescriptionList title="能力范围" label="能做什么" values={instance.can_do} />
          <DescriptionList title="能力边界" label="不能做什么" values={instance.cannot_do} />
        </section>
        <section className="space-y-3">
          <h3 className="text-sm font-semibold">输入与输出契约</h3>
          <SchemaDisclosure
            title="输入 Schema"
            schema={instance.input_schema}
            copied={copied === "input"}
            copyFailed={copied === "input-failed"}
            onCopy={() => void copySchema("input", instance.input_schema)}
          />
          <SchemaDisclosure
            title="输出 Schema"
            schema={instance.output_schema}
            copied={copied === "output"}
            copyFailed={copied === "output-failed"}
            onCopy={() => void copySchema("output", instance.output_schema)}
          />
        </section>
      </div>
    </div>
  )
}

function Info({ label, value }: { label: string; value: string }) {
  return <div className="min-w-0 rounded-lg bg-athena-surface px-3 py-2"><div className="text-xs text-athena-muted">{label}</div><div className="mt-1 break-all">{value}</div></div>
}

function DescriptionList({ title, label, values }: { title: string; label: string; values: string[] }) {
  return (
    <div className="rounded-lg border border-athena-border p-4">
      <h3 className="text-sm font-semibold">{title}</h3>
      <p className="mt-1 text-xs text-athena-muted">{label}</p>
      {values.length ? <ul className="mt-3 list-disc space-y-1 pl-5 text-sm">{values.map((value, index) => <li key={`${index}-${value}`}>{value}</li>)}</ul> : <p className="mt-3 text-sm text-athena-muted">暂无说明</p>}
    </div>
  )
}

function SchemaDisclosure({
  title,
  schema,
  copied,
  copyFailed,
  onCopy,
}: {
  title: string
  schema: Record<string, unknown>
  copied: boolean
  copyFailed: boolean
  onCopy: () => void
}) {
  return (
    <details className="rounded-lg border border-athena-border">
      <summary className="cursor-pointer px-4 py-3 text-sm font-medium">{title}</summary>
      <div className="border-t border-athena-border p-4">
        <div className="mb-3 flex items-center justify-end gap-2">
          {copied ? <span className="text-xs text-athena-success">已复制</span> : null}
          {copyFailed ? <span className="text-xs text-athena-danger">复制失败，请手动选择文本</span> : null}
          <button className="btn-ghost text-xs" onClick={onCopy}>{copied ? <Check className="h-4 w-4" /> : <Clipboard className="h-4 w-4" />}复制 JSON</button>
        </div>
        <pre className="max-h-96 overflow-auto rounded-lg bg-athena-surface p-3 text-xs leading-relaxed">{JSON.stringify(schema, null, 2)}</pre>
      </div>
    </details>
  )
}

export default WorkerRegistryView
