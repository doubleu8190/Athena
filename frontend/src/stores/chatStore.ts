import { create } from 'zustand'
import { getHistory, getSessions } from '../api/endpoints/chat'

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

const ACTIVE_SESSION_KEY = 'athena-active-session-id'

function loadActiveSessionId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_SESSION_KEY)
  } catch {
    return null
  }
}

function saveActiveSessionId(id: string | null): void {
  try {
    if (id) {
      localStorage.setItem(ACTIVE_SESSION_KEY, id)
    } else {
      localStorage.removeItem(ACTIVE_SESSION_KEY)
    }
  } catch {
    // localStorage may be unavailable
  }
}

// ── Store ──────────────────────────────────────────────────────────────

interface ChatState {
  sessions: Session[]
  activeSessionId: string | null
  messages: Record<string, Message[]>
  historyLoading: boolean
  sessionsLoading: boolean

  // Session actions
  createSession: () => string
  deleteSession: (id: string) => void
  setActiveSession: (id: string) => void

  // Message actions
  addMessage: (sessionId: string, msg: Message) => void
  updateMessage: (sessionId: string, msgId: string, patch: Partial<Message>) => void

  // History
  loadHistory: (sessionId: string) => Promise<void>
  loadAllSessions: () => Promise<void>
  restoreLastSession: () => Promise<void>

  // Convenience: add user message + create placeholder assistant message
  sendUserMessage: (content: string) => { userMsg: Message; assistantMsg: Message; sessionId: string }
}

export const useChatStore = create<ChatState>()(
  (set, get) => ({
      sessions: [],
      activeSessionId: null,
      messages: {},
      historyLoading: false,
      sessionsLoading: false,

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
        saveActiveSessionId(id)
        return id
      },

      deleteSession: (id) => {
        set((s) => {
          const sessions = s.sessions.filter((ses) => ses.id !== id)
          const { [id]: _, ...messages } = s.messages
          const nextActive = s.activeSessionId === id
            ? (sessions[0]?.id ?? null)
            : s.activeSessionId
          saveActiveSessionId(nextActive)
          return {
            sessions,
            messages,
            activeSessionId: nextActive,
          }
        })
      },

      setActiveSession: (id) => {
        set({ activeSessionId: id })
        saveActiveSessionId(id)
      },

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

      /**
       * Load message history from the backend for a given session.
       * If the session doesn't exist in the store yet, creates it.
       */
      loadHistory: async (sessionId: string) => {
        set({ historyLoading: true })
        try {
          const data = await getHistory(sessionId)

          // Ensure session exists in the store
          const existingSession = get().sessions.find((s) => s.id === sessionId)
          if (!existingSession) {
            // Derive title from first user message
            const firstUserMsg = data.messages.find((m) => m.role === 'user')
            const title = firstUserMsg ? generateTitle(firstUserMsg.content) : 'Restored Session'
            const session: Session = {
              id: sessionId,
              title,
              createdAt: firstUserMsg?.timestamp || Date.now(),
            }
            set((s) => ({
              sessions: [session, ...s.sessions],
            }))
          }

          set((s) => ({
            messages: {
              ...s.messages,
              [sessionId]: data.messages,
            },
            activeSessionId: sessionId,
          }))
          saveActiveSessionId(sessionId)
        } catch (err) {
          console.error('Failed to load history:', err)
        } finally {
          set({ historyLoading: false })
        }
      },

      /**
       * Load all sessions from the backend (SQLite checkpointer).
       * Merges with existing local sessions without duplicates.
       */
      loadAllSessions: async () => {
        set({ sessionsLoading: true })
        try {
          const data = await getSessions()
          if (data.sessions.length === 0) return

          const existingIds = new Set(get().sessions.map((s) => s.id))
          const newSessions = data.sessions
            .filter((s) => !existingIds.has(s.id))
            .map((s) => ({
              id: s.id,
              title: s.title,
              createdAt: s.createdAt,
            }))

          if (newSessions.length > 0) {
            set((s) => ({
              sessions: [...s.sessions, ...newSessions].sort(
                (a, b) => b.createdAt - a.createdAt
              ),
            }))
          }
        } catch (err) {
          console.error('Failed to load sessions:', err)
        } finally {
          set({ sessionsLoading: false })
        }
      },

      /**
       * Restore the last active session from localStorage on app startup.
       * First loads all sessions from the backend, then restores the last active one.
       */
      restoreLastSession: async () => {
        await get().loadAllSessions()
        const lastSessionId = loadActiveSessionId()
        if (lastSessionId) {
          await get().loadHistory(lastSessionId)
        }
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
