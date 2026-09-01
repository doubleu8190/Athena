import { useCallback, useEffect, useState } from "react"
import { Server, KeyRound, Globe, Thermometer, Hash, Loader2, CheckCircle2, XCircle } from "lucide-react"
import { apiClient } from "../../api/client"
import type { ProviderInfo, TestProviderResult } from "../../types"
import ViewShell from "./ViewShell"
import ProviderQuickList from "../ctx/ProviderQuickList"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"

function ProvidersView() {
  const [providers, setProviders] = useState<ProviderInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [testing, setTesting] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, TestProviderResult>>({})

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const list = await apiClient.listProviders()
      setProviders(list)
      setSelected((cur) => cur ?? list[0]?.name ?? null)
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

  const active = providers.find((p) => p.name === selected) ?? null

  const handleTest = async (p: ProviderInfo) => {
    setTesting(p.name)
    try {
      const result = await apiClient.testProvider({
        provider: p.provider,
        model: p.model,
      })
      setResults((r) => ({ ...r, [p.name]: result }))
    } catch (e) {
      setResults((r) => ({
        ...r,
        [p.name]: {
          ok: false,
          latency_ms: null,
          error: e instanceof Error ? e.message : String(e),
        },
      }))
    } finally {
      setTesting(null)
    }
  }

  return (
    <ViewShell
      side={
        <ProviderQuickList
          providers={providers}
          selectedName={selected}
          onSelect={setSelected}
          onRefresh={reload}
          loading={loading}
        />
      }
    >
      <Toolbar
        title="LLM 提供商"
        subtitle="启动时从配置加载，只读展示 · 可测试连接"
      />
      <div className="p-6 space-y-6">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : active ? (
          <div className="max-w-2xl">
            <div className="card">
              <div className="flex items-center justify-between px-5 py-4 border-b border-athena-border">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-lg bg-athena-accent/10 text-athena-accent flex items-center justify-center">
                    <Server className="w-5 h-5" />
                  </div>
                  <div>
                    <h2 className="font-semibold">{active.name}</h2>
                    <p className="text-xs text-athena-muted">
                      {active.provider} · {active.model}
                    </p>
                  </div>
                </div>
                <Badge tone={active.api_key_configured ? "success" : "warning"}>
                  {active.api_key_configured ? "已配置密钥" : "未配置密钥"}
                </Badge>
              </div>
              <div className="p-5 space-y-4">
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  <InfoRow icon={KeyRound} label="API 密钥" value={active.api_key_masked || "（未配置）"} />
                  <InfoRow icon={Globe} label="Base URL" value={active.base_url || "（默认）"} />
                  <InfoRow icon={Thermometer} label="Temperature" value={active.temperature >= 0 ? String(active.temperature) : "全局默认"} />
                  <InfoRow icon={Hash} label="Max Tokens" value={active.max_tokens > 0 ? String(active.max_tokens) : "全局默认"} />
                </div>

                <div className="flex items-center gap-3 pt-2">
                  <button
                    onClick={() => handleTest(active)}
                    disabled={testing !== null}
                    className="btn-secondary"
                  >
                    {testing === active.name ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" /> 测试中…
                      </>
                    ) : (
                      "测试连接"
                    )}
                  </button>
                  {results[active.name] ? (
                    results[active.name].ok ? (
                      <span className="flex items-center gap-1.5 text-xs text-athena-success">
                        <CheckCircle2 className="w-4 h-4" />
                        连接成功 · {results[active.name].latency_ms} ms
                      </span>
                    ) : (
                      <span className="flex items-center gap-1.5 text-xs text-athena-danger">
                        <XCircle className="w-4 h-4" />
                        {results[active.name].error}
                      </span>
                    )
                  ) : null}
                </div>
              </div>
            </div>
          </div>
        ) : (
          <div className="text-sm text-athena-muted">暂无提供商</div>
        )}
      </div>
    </ViewShell>
  )
}

function InfoRow({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof KeyRound
  label: string
  value: string
}) {
  return (
    <div className="flex items-start gap-2">
      <Icon className="w-4 h-4 text-athena-muted mt-0.5 flex-shrink-0" />
      <div className="min-w-0">
        <div className="text-xs text-athena-muted">{label}</div>
        <div className="text-sm break-all">{value}</div>
      </div>
    </div>
  )
}

export default ProvidersView
