import { useCallback, useEffect, useState } from "react"
import {
  History,
  MessageSquare,
  ListTree,
  Wrench,
  Coins,
  Pencil,
  Check,
  X,
  Bot,
  Brain,
} from "lucide-react"
import { apiClient } from "../../api/client"
import type {
  Project,
  Message,
  Session,
  SessionMemorySettings,
  SessionRunSummary,
} from "../../types"
import { formatDateTime } from "../../utils/format"
import { useChatStore } from "../../store/chatStore"
import ViewShell from "./ViewShell"
import SessionPicker from "../ctx/SessionPicker"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"
import Toggle from "../ui/Toggle"

function SessionDetailView() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [runs, setRuns] = useState<SessionRunSummary[]>([])
  const [memorySettings, setMemorySettings] = useState<SessionMemorySettings | null>(null)
  const [projects, setProjects] = useState<Project[]>([])
  const [memorySettingsSaving, setMemorySettingsSaving] = useState(false)
  const [memorySettingsError, setMemorySettingsError] = useState<string | null>(null)
  const steps = useChatStore((state) => state.steps)
  const toolCalls = useChatStore((state) => state.toolCalls)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState("")

  const reload = useCallback(async () => {
    if (!sessionId) return
    try {
      const [s, msgs, runList] = await Promise.all([
        apiClient.getSession(sessionId),
        apiClient.getMessages(sessionId),
        apiClient.listSessionRuns(sessionId),
      ])
      setSession(s)
      setMessages(msgs)
      setRuns(runList)
      try {
        const [settings, projects] = await Promise.all([
          apiClient.getSessionMemorySettings(sessionId),
          apiClient.listProjects(),
        ])
        setMemorySettings(settings)
        setProjects(projects)
        setMemorySettingsError(null)
      } catch (settingsError) {
        setMemorySettings(null)
        setMemorySettingsError(
          settingsError instanceof Error
            ? settingsError.message
            : "记忆设置加载失败",
        )
      }
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [sessionId])

  useEffect(() => {
    reload()
  }, [reload])

  const handleSelect = (id: string) => {
    setSessionId(id)
    setMemorySettings(null)
    setProjects([])
    setMemorySettingsError(null)
    setError(null)
    setEditing(false)
  }

  const handleRename = async () => {
    if (!session || !draftTitle.trim()) return
    try {
      const updated = await apiClient.updateSessionTitle(session.id, draftTitle.trim())
      setSession(updated)
      useChatStore.getState().updateSession(session.id, {
        title: updated.title,
      })
      setEditing(false)
    } catch (e) {
      setError(e instanceof Error ? e.message : "会话重命名失败")
    }
  }

  const updateMemorySettings = async (
    update: Partial<Pick<SessionMemorySettings, "use_memories" | "generate_memories">>,
  ) => {
    if (!sessionId || !memorySettings || memorySettingsSaving) return
    setMemorySettingsSaving(true)
    try {
      const saved = await apiClient.updateSessionMemorySettings(sessionId, {
        use_memories: update.use_memories ?? memorySettings.use_memories,
        generate_memories: update.generate_memories ?? memorySettings.generate_memories,
      })
      setMemorySettings(saved)
      setMemorySettingsError(null)
    } catch (e) {
      setMemorySettingsError(e instanceof Error ? e.message : "记忆设置保存失败")
    } finally {
      setMemorySettingsSaving(false)
    }
  }

  const statusTone =
    session?.status === "completed"
      ? "success"
      : session?.status === "running"
        ? "accent"
        : "default"

  if (!sessionId) {
    return (
      <ViewShell side={<SessionPicker sessionId={null} onSelect={handleSelect} />}>
        <div className="h-full flex flex-col items-center justify-center text-athena-muted gap-3">
          <History className="w-10 h-10" />
          <p className="text-sm">从左侧选择一个会话查看详情</p>
        </div>
      </ViewShell>
    )
  }

  const sessionSteps = steps.filter((step) => step.session_id === sessionId)
  const sessionRunIds = new Set(runs.map((run) => run.run_id))
  const sessionToolCalls = toolCalls.filter((call) => call.run_id && sessionRunIds.has(call.run_id))
  const tokenStats = sessionSteps.reduce(
    (acc, s) => ({
      input: acc.input + (s.llm_input_tokens || 0),
      output: acc.output + (s.llm_output_tokens || 0),
    }),
    { input: 0, output: 0 },
  )

  const lastMessages = messages.slice(-5)

  return (
    <ViewShell side={<SessionPicker sessionId={sessionId} onSelect={handleSelect} />}>
      <Toolbar
        title="会话详情"
        subtitle={session ? `创建于 ${formatDateTime(session.created_at)}` : "加载中…"}
      />
      <div className="p-6 space-y-6 overflow-y-auto">
        {error ? (
          <div className="text-sm text-athena-danger">加载失败：{error}</div>
        ) : (
          <>
            {/* 标题 + 内联重命名 */}
            <div className="card">
              <div className="px-5 py-4 border-b border-athena-border">
                {editing ? (
                  <div className="flex items-center gap-2">
                    <input
                      value={draftTitle}
                      onChange={(e) => setDraftTitle(e.target.value)}
                      className="input flex-1"
                      placeholder="会话标题"
                      autoFocus
                    />
                    <button onClick={handleRename} className="btn-primary px-3 py-1.5 text-sm">
                      <Check className="w-4 h-4" /> 保存
                    </button>
                    <button onClick={() => setEditing(false)} className="btn-ghost px-3 py-1.5 text-sm">
                      <X className="w-4 h-4" />
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-3 min-w-0">
                      <h2 className="font-semibold truncate">{session?.title}</h2>
                      {session ? <Badge tone={statusTone}>{session.status}</Badge> : null}
                    </div>
                    <button
                      onClick={() => {
                        setEditing(true)
                        setDraftTitle(session?.title ?? "")
                      }}
                      className="p-2 rounded-lg text-athena-muted hover:text-athena-text hover:bg-athena-border/40"
                      title="重命名"
                    >
                      <Pencil className="w-4 h-4" />
                    </button>
                  </div>
                )}
              </div>
              <div className="p-5 grid grid-cols-2 gap-4 text-sm">
                <div>
                  <div className="text-xs text-athena-muted">会话 ID</div>
                  <div className="font-mono text-xs break-all">{sessionId}</div>
                </div>
                <div>
                  <div className="text-xs text-athena-muted">最后更新</div>
                  <div className="text-sm">{session ? formatDateTime(session.updated_at) : "—"}</div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="px-5 py-4 border-b border-athena-border flex items-center gap-2">
                <Brain className="w-4 h-4 text-athena-muted" />
                <h3 className="text-sm font-semibold">本会话的记忆控制</h3>
              </div>
              <div className="px-5 divide-y divide-athena-border/60">
                <div className="py-4">
                  <div className="block text-sm font-medium">所属项目</div>
                  <p className="text-xs text-athena-muted mt-1 mb-3">
                    会话在创建时归入项目；项目同时决定项目级记忆的共享范围。
                  </p>
                  <div className="rounded-lg border border-athena-border bg-athena-surface px-3 py-2 text-sm">
                    {projects.find((item) => item.id === session?.project_id)?.name ?? "未归属项目"}
                  </div>
                </div>
                <div className="flex items-center justify-between gap-4 py-4">
                  <div>
                    <div className="text-sm font-medium">使用已有记忆</div>
                    <div className="text-xs text-athena-muted mt-1">关闭后，本会话的模型请求不会读取历史记忆。</div>
                  </div>
                  <Toggle
                    checked={memorySettings?.use_memories ?? true}
                    onChange={(checked) => updateMemorySettings({ use_memories: checked })}
                    disabled={!memorySettings || memorySettingsSaving}
                    label="使用已有记忆"
                  />
                </div>
                <div className="flex items-center justify-between gap-4 py-4">
                  <div>
                    <div className="text-sm font-medium">从本会话生成记忆</div>
                    <div className="text-xs text-athena-muted mt-1">Worker 处理时会读取当前开关；已跳过的轮次不会因重新开启自动补写，也不会删除已保存记忆。</div>
                  </div>
                  <Toggle
                    checked={memorySettings?.generate_memories ?? true}
                    onChange={(checked) => updateMemorySettings({ generate_memories: checked })}
                    disabled={!memorySettings || memorySettingsSaving}
                    label="从本会话生成记忆"
                  />
                </div>
                {memorySettingsError ? (
                  <p className="py-3 text-xs text-athena-danger">记忆设置不可用：{memorySettingsError}</p>
                ) : null}
              </div>
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="消息数" value={messages.length} icon={MessageSquare} />
              <StatCard label="步骤数" value={sessionSteps.length} icon={ListTree} />
              <StatCard label="工具调用" value={sessionToolCalls.length} icon={Wrench} />
              <StatCard
                label="Token 用量"
                value={`${tokenStats.input.toLocaleString()} / ${tokenStats.output.toLocaleString()}`}
                icon={Coins}
                hint="输入 / 输出"
              />
            </div>

            <div className="card overflow-hidden">
              <div className="px-5 py-3 border-b border-athena-border flex items-center justify-between">
                <h3 className="text-sm font-semibold">运行记录</h3>
                <span className="text-xs text-athena-muted">{runs.length} 次</span>
              </div>
              {runs.length === 0 ? <p className="px-5 py-6 text-sm text-athena-muted">暂无运行记录</p> : (
                <div className="divide-y divide-athena-border/60">
                  {runs.map((run) => <div key={run.run_id} className="flex flex-wrap items-center gap-3 px-5 py-3 text-xs">
                    <Badge tone={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : run.status === "running" ? "accent" : "default"}>{run.status}</Badge>
                    <code className="min-w-0 flex-1 truncate text-athena-muted">{run.run_id}</code>
                    <span className="text-athena-muted">{run.created_at ? formatDateTime(run.created_at) : "—"}</span>
                  </div>)}
                </div>
              )}
            </div>

            {/* 最近消息预览 */}
            <div className="card">
              <div className="px-5 py-3 border-b border-athena-border">
                <h3 className="text-sm font-semibold">最近消息</h3>
              </div>
              <div className="p-5 space-y-3 max-h-80 overflow-y-auto">
                {lastMessages.length === 0 ? (
                  <div className="text-sm text-athena-muted">暂无消息</div>
                ) : (
                  lastMessages.map((m) => (
                    <div key={m.id} className="flex gap-3">
                      <Bot className="w-4 h-4 text-athena-muted mt-1 flex-shrink-0" />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-medium">
                            {m.role === "user" ? "用户" : m.role === "assistant" ? "助手" : m.role}
                          </span>
                          <span className="text-[11px] text-athena-muted">{formatDateTime(m.timestamp)}</span>
                        </div>
                        <p className="text-sm text-athena-text/90 break-words whitespace-pre-wrap mt-0.5">
                          {m.content.length > 300 ? `${m.content.slice(0, 300)}…` : m.content}
                        </p>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </ViewShell>
  )
}

export default SessionDetailView
