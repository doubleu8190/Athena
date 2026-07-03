import { create } from 'zustand'

// ── Types ─────────────────────────────────────────────────────────────

export interface Session {
  id: string
  title: string
  createdAt: number
}

// ── Deprecated: SubtaskEvent kept for backward compat with old SSE events ──
export interface SubtaskEvent {
  step: number
  tool_name?: string
  intent?: string
  status?: string
  output_preview?: string
  error?: string
  reason?: string
  original_tool?: string
  fallback_tool?: string
}

export interface ToolCallEvent {
  tool_name: string
  tool_call_id?: string
  args_preview?: string
  status: 'running' | 'success' | 'error'
  output_preview?: string
}

export interface ConfirmRequired {
  task_id: string
  step: number
  tool_name: string
  risk_level: string
  preview_text: string
  reason: string
  cooling_off_seconds: number
  timeout_seconds: number
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  // Tool-calling agent events (current)
  toolCalls?: ToolCallEvent[]
  // Deprecated: subtask events kept for backward compat
  plan?: { task_id: string; subtasks: { step: number; tool_name: string; intent: string }[] }
  subtasks?: SubtaskEvent[]
  confirmRequired?: ConfirmRequired
  // SSE streaming state
  isStreaming?: boolean
  taskStatus?: 'thinking' | 'generating_plan' | 'executing' | 'completed' | 'failed'
}

// ── Helpers ────────────────────────────────────────────────────────────

let counter = 0
function generateId(): string {
  counter++
  return `${Date.now()}-${counter}`
}

function generateTitle(content: string): string {
  return content.slice(0, 30).replace(/\n/g, ' ')
}

// ── Store ──────────────────────────────────────────────────────────────

interface ChatState {
  sessions: Session[]
  activeSessionId: string | null
  messages: Record<string, Message[]>

  // Session actions
  createSession: () => string
  deleteSession: (id: string) => void
  setActiveSession: (id: string) => void

  // Message actions
  addMessage: (sessionId: string, msg: Message) => void
  updateMessage: (sessionId: string, msgId: string, patch: Partial<Message>) => void

  // Convenience: add user message + create placeholder assistant message
  sendUserMessage: (content: string) => { userMsg: Message; assistantMsg: Message; sessionId: string }
}

export const useChatStore = create<ChatState>()(
  (set, get) => ({
      sessions: [],
      activeSessionId: null,
      messages: {},

      createSession: () => {
        const id = generateId()
        const session: Session = {
          id,
          title: 'New Session',
          createdAt: Date.now(),
        }
        set((s) => ({
          sessions: [session, ...s.sessions],
          activeSessionId: id,
        }))
        return id
      },

      deleteSession: (id) => {
        set((s) => {
          const sessions = s.sessions.filter((ses) => ses.id !== id)
          const { [id]: _, ...messages } = s.messages
          return {
            sessions,
            messages,
            activeSessionId: s.activeSessionId === id
              ? (sessions[0]?.id ?? null)
              : s.activeSessionId,
          }
        })
      },

      setActiveSession: (id) => set({ activeSessionId: id }),

      addMessage: (sessionId, msg) => {
        set((s) => ({
          messages: {
            ...s.messages,
            [sessionId]: [...(s.messages[sessionId] || []), msg],
          },
        }))
        // Auto-name session on first user message
        const session = get().sessions.find((ses) => ses.id === sessionId)
        if (session && session.title === 'New Session' && msg.role === 'user') {
          set((s) => ({
            sessions: s.sessions.map((ses) =>
              ses.id === sessionId ? { ...ses, title: generateTitle(msg.content) } : ses
            ),
          }))
        }
      },

      updateMessage: (sessionId, msgId, patch) => {
        set((s) => ({
          messages: {
            ...s.messages,
            [sessionId]: (s.messages[sessionId] || []).map((m) =>
              m.id === msgId ? { ...m, ...patch } : m
            ),
          },
        }))
      },

      sendUserMessage: (content) => {
        const state = get()
        let sessionId = state.activeSessionId
        if (!sessionId) {
          sessionId = state.createSession()
        }

        const userMsg: Message = {
          id: generateId(),
          role: 'user',
          content,
          timestamp: Date.now(),
        }

        const assistantMsg: Message = {
          id: generateId(),
          role: 'assistant',
          content: '',
          timestamp: Date.now(),
          isStreaming: true,
          taskStatus: 'thinking',
          toolCalls: [],
        }

        set((s) => ({
          messages: {
            ...s.messages,
            [sessionId!]: [...(s.messages[sessionId!] || []), userMsg, assistantMsg],
          },
        }))

        return { userMsg, assistantMsg, sessionId: sessionId! }
      },
    })
  )
