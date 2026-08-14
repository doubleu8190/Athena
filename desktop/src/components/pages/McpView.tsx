import { useCallback, useEffect, useState } from "react"
import { Loader2, CheckCircle2, XCircle, Network, TerminalSquare, Cog } from "lucide-react"
import { apiClient } from "../../api/client"
import type { McpRegisterResult, McpServerConfig, McpServerInfo } from "../../types"
import ViewShell from "./ViewShell"
import McpServerQuickList from "../ctx/McpServerQuickList"
import Toolbar from "../ui/Toolbar"
import SectionCard from "../ui/SectionCard"
import Badge from "../ui/Badge"

const SAMPLE_JSON = `{
  "mcpServers": {
    "@mendableai/firecrawl-mcp-server": {
      "command": "npx",
      "args": ["-y", "mcprouter"],
      "env": { "SERVER_KEY": "p8j3rxm8wyddzg" }
    }
  }
}`

function McpView() {
  const [servers, setServers] = useState<McpServerInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [jsonText, setJsonText] = useState<string>(SAMPLE_JSON)
  const [parseError, setParseError] = useState<string | null>(null)
  const [registering, setRegistering] = useState(false)
  const [results, setResults] = useState<McpRegisterResult[] | null>(null)
  const [deleting, setDeleting] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiClient.listMcpServers()
      setServers(data.items)
      setSelected((cur) => {
        if (cur && data.items.some((s) => s.name === cur)) return cur
        return data.items[0]?.name ?? null
      })
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

  const handleRegister = async () => {
    setParseError(null)
    setResults(null)

    let parsed: unknown
    try {
      parsed = JSON.parse(jsonText)
    } catch (e) {
      setParseError(`JSON 解析失败：${e instanceof Error ? e.message : String(e)}`)
      return
    }

    const mcpServers = (parsed as { mcpServers?: unknown })?.mcpServers
    if (!mcpServers || typeof mcpServers !== "object" || Array.isArray(mcpServers)) {
      setParseError("格式错误：顶层必须是 { \"mcpServers\": { ... } } 对象")
      return
    }
    const count = Object.keys(mcpServers as Record<string, unknown>).length
    if (count === 0) {
      setParseError("mcpServers 中至少需要一个服务器配置")
      return
    }

    setRegistering(true)
    try {
      const resp = await apiClient.registerMcpServers({
        mcpServers: mcpServers as Record<string, McpServerConfig>,
      })
      setResults(resp.results)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setRegistering(false)
    }
  }

  const handleDelete = async (name: string) => {
    if (!window.confirm(`确定注销 MCP 服务器「${name}」吗？`)) return
    setDeleting(name)
    try {
      await apiClient.deleteMcpServer(name)
      await reload()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setDeleting(null)
    }
  }

  const active = servers.find((s) => s.name === selected) ?? null

  return (
    <ViewShell
      side={
        <McpServerQuickList
          servers={servers}
          selectedName={selected}
          onSelect={setSelected}
          onRefresh={reload}
          onDelete={handleDelete}
          loading={loading}
          deleting={deleting}
        />
      }
    >
      <Toolbar
        title="MCP 服务器"
        subtitle={`已注册 ${servers.length} 个 · 配置持久化，重启后自动恢复`}
      />
      <div className="p-6 space-y-6">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            <div className="max-w-3xl">
              <SectionCard
                title="注册 MCP 服务器"
                description="粘贴 mcpServers 配置（command + args + env），后端将启动子进程并注册其工具"
              >
                <textarea
                  value={jsonText}
                  onChange={(e) => {
                    setJsonText(e.target.value)
                    setParseError(null)
                    setResults(null)
                  }}
                  spellCheck={false}
                  className="textarea font-mono text-xs h-56 resize-y"
                />
                <div className="flex items-center gap-3 mt-3">
                  <button
                    onClick={handleRegister}
                    disabled={registering}
                    className="btn-primary"
                  >
                    {registering ? (
                      <>
                        <Loader2 className="w-4 h-4 animate-spin" /> 注册中…（首次 npx 下载可能较慢）
                      </>
                    ) : (
                      "注册"
                    )}
                  </button>
                  <button
                    onClick={() => {
                      setJsonText(SAMPLE_JSON)
                      setParseError(null)
                      setResults(null)
                    }}
                    disabled={registering}
                    className="btn-ghost"
                  >
                    重置示例
                  </button>
                </div>

                {parseError ? (
                  <div className="text-xs text-athena-danger mt-2">{parseError}</div>
                ) : null}

                {results ? (
                  <div className="mt-4 space-y-2">
                    {results.map((r) => (
                      <div key={r.name} className="flex items-start gap-2 text-sm">
                        {r.status === "connected" ? (
                          <CheckCircle2 className="w-4 h-4 text-athena-success mt-0.5 flex-shrink-0" />
                        ) : (
                          <XCircle className="w-4 h-4 text-athena-danger mt-0.5 flex-shrink-0" />
                        )}
                        <div className="min-w-0">
                          <span className="font-medium break-all">{r.name}</span>
                          {r.status === "connected" ? (
                            <span className="text-athena-success ml-1">· 已注册 {r.tool_count} 个工具</span>
                          ) : (
                            <span className="text-athena-danger ml-1">· 注册失败</span>
                          )}
                          {r.error ? (
                            <div className="text-xs text-athena-danger break-all mt-0.5">{r.error}</div>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : null}

                <p className="text-xs text-athena-muted mt-4 leading-relaxed">
                  注册成功的工具会出现在「工具管理」页，前缀为 mcp_&lt;server&gt;。env 中的值仅用于启动子进程，
                  列表展示掩码。连接失败的配置仍会保存，可修复后重新注册。
                </p>
              </SectionCard>
            </div>

            {active ? (
              <div className="max-w-3xl">
                <div className="card">
                  <div className="flex items-center justify-between px-5 py-4 border-b border-athena-border">
                    <div className="flex items-center gap-3">
                      <div className="w-10 h-10 rounded-lg bg-athena-accent/10 text-athena-accent flex items-center justify-center">
                        <Network className="w-5 h-5" />
                      </div>
                      <div>
                        <h2 className="font-semibold break-all">{active.name}</h2>
                        <p className="text-xs text-athena-muted">
                          {active.tool_count} 个工具 · {active.command}
                        </p>
                      </div>
                    </div>
                    <Badge tone={active.status === "connected" ? "success" : "danger"}>
                      {active.status === "connected" ? "已连接" : "连接失败"}
                    </Badge>
                  </div>
                  <div className="p-5 space-y-4">
                    <div className="flex items-start gap-2">
                      <TerminalSquare className="w-4 h-4 text-athena-muted mt-0.5 flex-shrink-0" />
                      <div className="min-w-0">
                        <div className="text-xs text-athena-muted">启动命令</div>
                        <div className="text-sm font-mono break-all">
                          {active.command} {active.args.join(" ")}
                        </div>
                      </div>
                    </div>
                    <div className="flex items-start gap-2">
                      <Cog className="w-4 h-4 text-athena-muted mt-0.5 flex-shrink-0" />
                      <div className="min-w-0">
                        <div className="text-xs text-athena-muted">环境变量（掩码）</div>
                        <div className="text-sm font-mono break-all">
                          {Object.keys(active.env_masked).length > 0
                            ? Object.entries(active.env_masked)
                                .map(([k, v]) => `${k}=${v}`)
                                .join("  ")
                            : "（无）"}
                        </div>
                      </div>
                    </div>
                    {active.error ? (
                      <div className="text-xs text-athena-danger break-all">{active.error}</div>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : (
              <div className="text-sm text-athena-muted">暂无已注册的 MCP 服务器</div>
            )}
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default McpView
