import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { sendMessage, sendConfirm, type MessageRequest } from '../api/endpoints/chat'
import type { SSECallbacks } from '../api/sse'

export interface SubtaskProgress {
  step: number
  tool_name: string
  intent: string
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'fallback'
  output_preview?: string
  error?: string
  fallback_from?: string
  fallback_tool?: string
}

export interface TaskProgress {
  task_id: string
  subtask_count: number
  summary: string
  subtasks: SubtaskProgress[]
  status: 'generating' | 'executing' | 'completed' | 'failed'
  error?: string
}

export interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  task?: TaskProgress | null
}

export interface Session {
  id: string
  title: string
  lastActive: number
}

interface ChatState {
  sessions: Session[]
  activeSessionId: string | null
  messages: Message[]
  isStreaming: boolean
  currentTask: TaskProgress | null
  abortController: AbortController | null

  createSession: () => string
  selectSession: (id: string) => void
  sendMessage: (content: string) => Promise<void>
  confirmAction: (taskId: string, step: number, approved: boolean) => Promise<void>
  abortStream: () => void
}

let messageCounter = 0

export const useChatStore = create<ChatState>()(
  persist(
    (set, get) => ({
  sessions: [],
  activeSessionId: null,
  messages: [],
  isStreaming: false,
  currentTask: null,
  abortController: null,

  createSession: () => {
    const id = `session_${Date.now()}`
    const session: Session = {
      id,
      title: 'New Session',
      lastActive: Date.now(),
    }
    set((s) => ({
      sessions: [session, ...s.sessions],
      activeSessionId: id,
      messages: [],
      currentTask: null,
    }))
    return id
  },

  selectSession: (id: string) => {
    set({ activeSessionId: id })
    // In the future, load messages from server
  },

  sendMessage: async (content: string) => {
    const state = get()
    let sessionId = state.activeSessionId

    // Auto-create session if none active
    if (!sessionId) {
      sessionId = get().createSession()
    }

    const userMsg: Message = {
      id: `msg_${++messageCounter}`,
      role: 'user',
      content,
      timestamp: Date.now(),
    }

    set((s) => ({
      messages: [...s.messages, userMsg],
      isStreaming: true,
      currentTask: null,
    }))

    // Update session title based on first message
    set((s) => ({
      sessions: s.sessions.map((ses) =>
        ses.id === sessionId && ses.title === 'New Session'
          ? { ...ses, title: content.slice(0, 40) + (content.length > 40 ? '...' : ''), lastActive: Date.now() }
          : ses
      ),
    }))

    const reqBody: MessageRequest = {
      content,
      session_id: sessionId || undefined,
    }

    // Build assistant message placeholder
    const assistantMsgId = `msg_${++messageCounter}`
    const assistantMsg: Message = {
      id: assistantMsgId,
      role: 'assistant',
      content: '',
      timestamp: Date.now(),
    }

    set((s) => ({
      messages: [...s.messages, assistantMsg],
    }))

    const callbacks: SSECallbacks = {
      onEvent: (eventType, data) => {
        switch (eventType) {
          case 'plan_generating':
            set({
              currentTask: {
                task_id: (data.task_id as string) || 'pending',
                subtask_count: 0,
                summary: 'Generating plan...',
                subtasks: [],
                status: 'generating',
              },
            })
            break

          case 'plan_generated': {
            const count = (data.subtask_count as number) || 0
            set((s) => ({
              currentTask: s.currentTask
                ? {
                    ...s.currentTask,
                    task_id: (data.task_id as string) || s.currentTask.task_id,
                    subtask_count: count,
                    summary: (data.summary as string) || '',
                    status: 'executing',
                  }
                : {
                    task_id: (data.task_id as string) || '',
                    subtask_count: count,
                    summary: (data.summary as string) || '',
                    subtasks: [],
                    status: 'executing',
                  },
            }))
            break
          }

          case 'subtask_started':
            set((s) => {
              if (!s.currentTask) return s
              const newSubtask: SubtaskProgress = {
                step: (data.step as number) || 0,
                tool_name: (data.tool_name as string) || '',
                intent: (data.intent as string) || '',
                status: 'running',
              }
              return {
                currentTask: {
                  ...s.currentTask,
                  subtasks: [...s.currentTask.subtasks.filter((st) => st.step !== newSubtask.step), newSubtask],
                },
              }
            })
            break

          case 'subtask_completed':
            set((s) => {
              if (!s.currentTask) return s
              return {
                currentTask: {
                  ...s.currentTask,
                  subtasks: s.currentTask.subtasks.map((st) =>
                    st.step === (data.step as number)
                      ? { ...st, status: 'completed' as const, output_preview: data.output_preview as string }
                      : st
                  ),
                },
              }
            })
            break

          case 'subtask_failed':
            set((s) => {
              if (!s.currentTask) return s
              return {
                currentTask: {
                  ...s.currentTask,
                  subtasks: s.currentTask.subtasks.map((st) =>
                    st.step === (data.step as number)
                      ? { ...st, status: 'failed' as const, error: data.error as string }
                      : st
                  ),
                },
              }
            })
            break

          case 'subtask_skipped':
            set((s) => {
              if (!s.currentTask) return s
              return {
                currentTask: {
                  ...s.currentTask,
                  subtasks: s.currentTask.subtasks.map((st) =>
                    st.step === (data.step as number)
                      ? { ...st, status: 'skipped' as const, error: data.error as string }
                      : st
                  ),
                },
              }
            })
            break

          case 'subtask_fallback':
            set((s) => {
              if (!s.currentTask) return s
              return {
                currentTask: {
                  ...s.currentTask,
                  subtasks: s.currentTask.subtasks.map((st) =>
                    st.step === (data.step as number)
                      ? {
                          ...st,
                          status: 'fallback' as const,
                          fallback_from: data.original_tool as string,
                          fallback_tool: data.fallback_tool as string,
                        }
                      : st
                  ),
                },
              }
            })
            break

          case 'task_completed':
            set((s) => ({
              currentTask: s.currentTask
                ? { ...s.currentTask, status: 'completed' as const, summary: (data.summary as string) || s.currentTask.summary }
                : null,
              isStreaming: false,
            }))
            // Update assistant message
            set((state) => ({
              messages: state.messages.map((m) =>
                m.id === assistantMsgId
                  ? { ...m, content: (data.summary as string) || 'Task completed', task: state.currentTask }
                  : m
              ),
            }))
            break

          case 'task_failed':
            set((s) => ({
              currentTask: s.currentTask
                ? { ...s.currentTask, status: 'failed' as const, error: (data.error as string) || 'Unknown error' }
                : null,
              isStreaming: false,
            }))
            set((state) => ({
              messages: state.messages.map((m) =>
                m.id === assistantMsgId
                  ? { ...m, content: `❌ Task failed: ${(data.error as string) || 'Unknown error'}`, task: state.currentTask }
                  : m
              ),
            }))
            break

          case 'error':
            set({ isStreaming: false })
            set((state) => ({
              messages: state.messages.map((m) =>
                m.id === assistantMsgId
                  ? { ...m, content: `❌ Error: ${(data.message as string) || 'Unknown error'}` }
                  : m
              ),
            }))
            break
        }
      },

      onError: (error) => {
        set({ isStreaming: false })
        set((state) => ({
          messages: state.messages.map((m) =>
            m.id === assistantMsgId
              ? { ...m, content: `❌ Connection error: ${error.message}` }
              : m
          ),
        }))
      },

      onDone: () => {
        set({ isStreaming: false, abortController: null })
        // Attach current task to the assistant message
        const finalTask = get().currentTask
        if (finalTask) {
          set((state) => ({
            messages: state.messages.map((m) =>
              m.id === assistantMsgId ? { ...m, task: finalTask } : m
            ),
          }))
        }
      },
    }

    const controller = sendMessage(reqBody, callbacks)
    set({ abortController: controller })
  },

  confirmAction: async (taskId: string, step: number, approved: boolean) => {
    await sendConfirm({ task_id: taskId, step, approved })
  },

  abortStream: () => {
    const { abortController } = get()
    if (abortController) {
      abortController.abort()
      set({ isStreaming: false, abortController: null })
    }
  },
    }),
    {
      name: 'athena-chat-store',
      partialize: (state) => ({
        sessions: state.sessions,
        activeSessionId: state.activeSessionId,
      }),
    }
  )
)
