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
} from "lucide-react"
import { apiClient } from "../../api/client"
import type { Message, Session, Step, ToolCall } from "../../types"
import { formatDateTime } from "../../utils/format"
import { useChatStore } from "../../store/chatStore"
import ViewShell from "./ViewShell"
import SessionPicker from "../ctx/SessionPicker"
import StatCard from "../ui/StatCard"
import Toolbar from "../ui/Toolbar"
import Badge from "../ui/Badge"

function SessionDetailView() {
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [session, setSession] = useState<Session | null>(null)
  const [messages, setMessages] = useState<Message[]>([])
  const [steps, setSteps] = useState<Step[]>([])
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([])
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draftTitle, setDraftTitle] = useState("")

  const reload = useCallback(async () => {
    if (!sessionId) return
    try {
      const [s, msgs, st, tc] = await Promise.all([
        apiClient.getSession(sessionId),
        apiClient.getMessages(sessionId),
        apiClient.getSteps(sessionId),
        apiClient.getToolCalls(sessionId) as Promise<ToolCall[]>,
      ])
      setSession(s)
      setMessages(msgs)
      setSteps(st)
      setToolCalls(tc)
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
    setEditing(false)
  }

  const handleRename = async () => {
    if (!session || !draftTitle.trim()) return
    try {
      const updated = await apiClient.updateSessionTitle(session.id, draftTitle.trim())
      setSession(updated)
      useChatStore.getState().updateSession(session.id, { title: updated.title })
      setEditing(false)
    } catch (e) {
      console.error("rename failed", e)
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

  const tokenStats = steps.reduce(
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

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <StatCard label="消息数" value={messages.length} icon={MessageSquare} />
              <StatCard label="步骤数" value={steps.length} icon={ListTree} />
              <StatCard label="工具调用" value={toolCalls.length} icon={Wrench} />
              <StatCard
                label="Token 用量"
                value={`${tokenStats.input.toLocaleString()} / ${tokenStats.output.toLocaleString()}`}
                icon={Coins}
                hint="输入 / 输出"
              />
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
