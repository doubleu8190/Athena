import { create } from "zustand"
import type {
  Session,
  Message,
  Step,
  ToolCall,
  ApprovalRequest,
  ConnectionStatus,
  AgentStatus,
  ThinkingState,
  AppView,
  Attachment,
  OrchestrationTask,
  OrchestrationPlan,
  ExecutionError,
  ExecutionTimelineEntry,
  RunTimelineEvent,
} from "../types"

interface ChatStore {
  // 连接状态
  connectionStatus: ConnectionStatus
  setConnectionStatus: (status: ConnectionStatus) => void

  // Agent 状态
  agentStatus: AgentStatus
  setAgentStatus: (status: AgentStatus) => void

  // 会话列表
  sessions: Session[]
  addSession: (session: Session) => void
  setSessions: (sessions: Session[]) => void
  removeSession: (sessionId: string) => void
  updateSession: (sessionId: string, updates: Partial<Session>) => void

  // 活跃会话
  activeSessionId: string | null
  setActiveSession: (sessionId: string | null) => void

  // 当前视图（从 localStorage 持久化，避免休眠/重挂载后丢失）
  activeView: AppView
  setActiveView: (view: AppView) => void

  // 消息
  messages: Message[]
  addMessage: (message: Message) => void
  updateMessage: (messageId: string, updates: Partial<Message>) => void
  removeMessage: (messageId: string) => void
  clearMessages: () => void
  setMessages: (messages: Message[]) => void


  // 执行步骤
  steps: Step[]
  addStep: (step: Step) => void
  updateStep: (stepId: string, updates: Partial<Step>) => void
  clearSteps: () => void
  setSteps: (steps: Step[]) => void

  // 工具调用
  toolCalls: ToolCall[]
  addToolCall: (toolCall: ToolCall) => void
  updateToolCall: (id: string, updates: Partial<ToolCall>) => void
  clearToolCalls: () => void
  setToolCalls: (toolCalls: ToolCall[]) => void

  // 审批
  pendingApprovals: ApprovalRequest[]
  addApproval: (approval: ApprovalRequest) => void
  mergeApprovals: (approvals: ApprovalRequest[]) => void
  resolveApproval: (approvalId: string, decision: string) => void
  clearApprovals: () => void

  // Thinking 状态
  thinking: ThinkingState | null
  setThinking: (thinking: ThinkingState) => void
  clearThinking: () => void

  // 面向用户的受控执行过程时间线
  executionTimeline: ExecutionTimelineEntry[]
  upsertExecutionTimelineEntry: (entry: ExecutionTimelineEntry) => void
  clearExecutionTimeline: () => void
  runTimelineEvents: RunTimelineEvent[]
  appendRunTimelineEvent: (event: RunTimelineEvent) => void
  clearRunTimelineEvents: () => void
  suppressedRunOutput: Record<string, number>
  suppressRunOutput: (runId: string, cursor: number) => void
  restoreRunOutput: (runId: string) => void

  // 中心编排
  orchestrationTasks: OrchestrationTask[]
  upsertOrchestrationTask: (task: OrchestrationTask) => void
  clearOrchestrationTasks: () => void
  orchestrationPlans: OrchestrationPlan[]
  upsertOrchestrationPlan: (plan: OrchestrationPlan) => void
  clearOrchestrationPlans: () => void

  // 错误
  error: string | null
  setError: (error: string | null) => void
  clearError: () => void
  errorDetail: ExecutionError | null
  setErrorDetail: (error: ExecutionError | null) => void
  clearErrorDetail: () => void

  attachments: Attachment[]
  setAttachments: (items: Attachment[]) => void
  upsertAttachment: (item: Attachment) => void
  removeAttachment: (id: string) => void
}

export const useChatStore = create<ChatStore>((set) => ({
  // 连接状态
  connectionStatus: "disconnected",
  setConnectionStatus: (status) => set({ connectionStatus: status }),

  // Agent 状态
  agentStatus: "idle",
  setAgentStatus: (status) => set({ agentStatus: status }),

  // 会话列表
  sessions: [],
  addSession: (session) =>
    set((state) => {
      if (state.sessions.some((s) => s.id === session.id)) return {}
      return { sessions: [session, ...state.sessions] }
    }),
  setSessions: (sessions) => set({ sessions }),
  removeSession: (sessionId) =>
    set((state) => ({
      sessions: state.sessions.filter((s) => s.id !== sessionId),
    })),
  updateSession: (sessionId, updates) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.id === sessionId ? { ...s, ...updates } : s,
      ),
    })),

  // 活跃会话
  activeSessionId: null,
  setActiveSession: (sessionId) => set({ activeSessionId: sessionId }),

  // 当前视图：优先从 localStorage 恢复，默认 "chat"
  activeView: ((): AppView => {
    try {
      const saved = localStorage.getItem("athena:activeView")
      if (saved && ["chat", "memory", "tools", "approvals", "providers", "session-detail", "run-chain", "settings", "mcp", "retrieval", "evaluation"].includes(saved)) {
        return saved as AppView
      }
    } catch { /* 忽略不可用的本地存储内容，使用默认视图。 */ }
    return "chat"
  })(),
  setActiveView: (view) => {
    try { localStorage.setItem("athena:activeView", view) } catch { /* 忽略本地存储写入失败，不影响内存状态。 */ }
    set({ activeView: view })
  },

  // 消息
  messages: [],
  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),
  updateMessage: (messageId, updates) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === messageId ? { ...m, ...updates } : m,
      ),
    })),
  removeMessage: (messageId) =>
    set((state) => ({
      messages: state.messages.filter((m) => m.id !== messageId),
    })),
  clearMessages: () => set({ messages: [] }),
  setMessages: (messages) => set({ messages }),


  // 执行步骤
  steps: [],
  addStep: (step) =>
    set((state) => ({ steps: [...state.steps, step] })),
  updateStep: (stepId, updates) =>
    set((state) => ({
      steps: state.steps.map((s) =>
        s.id === stepId ? { ...s, ...updates } : s,
      ),
    })),
  clearSteps: () => set({ steps: [] }),
  setSteps: (steps) => set({ steps }),

  // 工具调用
  toolCalls: [],
  addToolCall: (toolCall) =>
    set((state) => ({ toolCalls: [...state.toolCalls, toolCall] })),
  updateToolCall: (id, updates) =>
    set((state) => ({
      toolCalls: state.toolCalls.map((tc) =>
        tc.id === id ? { ...tc, ...updates } : tc,
      ),
    })),
  clearToolCalls: () => set({ toolCalls: [] }),
  setToolCalls: (toolCalls) => set({ toolCalls }),

  // 审批
  pendingApprovals: [],
  addApproval: (approval) =>
    set((state) => ({
      pendingApprovals: state.pendingApprovals.some((item) => item.approval_id === approval.approval_id)
        ? state.pendingApprovals.map((item) => item.approval_id === approval.approval_id ? { ...item, ...approval } : item)
        : [...state.pendingApprovals, approval],
    })),
  mergeApprovals: (approvals) =>
    set((state) => {
      const merged = new Map(
        state.pendingApprovals.map((approval) => [approval.approval_id, approval]),
      )
      approvals.forEach((approval) => merged.set(approval.approval_id, approval))
      return { pendingApprovals: Array.from(merged.values()) }
    }),
  resolveApproval: (approvalId, _decision) =>
    set((state) => ({
      pendingApprovals: state.pendingApprovals.filter(
        (a) => a.approval_id !== approvalId,
      ),
    })),
  clearApprovals: () => set({ pendingApprovals: [] }),

  // Thinking 状态
  thinking: null,
  setThinking: (thinking) => set({ thinking }),
  clearThinking: () => set({ thinking: null }),

  executionTimeline: [],
  runTimelineEvents: [],
  suppressedRunOutput: (() => {
    try {
      const value = localStorage.getItem("athena:suppressed-run-output")
      return value ? JSON.parse(value) as Record<string, number> : {}
    } catch { return {} }
  })(),
  suppressRunOutput: (runId, cursor) => set((state) => ({
    suppressedRunOutput: (() => {
      const suppressedRunOutput = { ...state.suppressedRunOutput, [runId]: cursor }
      try { localStorage.setItem("athena:suppressed-run-output", JSON.stringify(suppressedRunOutput)) } catch { /* Keep the in-memory cutoff if storage is unavailable. */ }
      return suppressedRunOutput
    })(),
  })),
  restoreRunOutput: (runId) => set((state) => {
    const { [runId]: _removed, ...remaining } = state.suppressedRunOutput
    try { localStorage.setItem("athena:suppressed-run-output", JSON.stringify(remaining)) } catch { /* Keep the in-memory state if storage is unavailable. */ }
    return { suppressedRunOutput: remaining }
  }),
  appendRunTimelineEvent: (event) => set((state) => {
    if (state.runTimelineEvents.some((item) => item.session_seq === event.session_seq && item.run_id === event.run_id)) return {}
    return { runTimelineEvents: [...state.runTimelineEvents, event].slice(-5000) }
  }),
  clearRunTimelineEvents: () => set({ runTimelineEvents: [] }),
  upsertExecutionTimelineEntry: (entry) =>
    set((state) => {
      const existingIndex = state.executionTimeline.findIndex((item) => item.id === entry.id)
      if (existingIndex < 0) {
        return { executionTimeline: [...state.executionTimeline, entry] }
      }
      const executionTimeline = [...state.executionTimeline]
      executionTimeline[existingIndex] = {
        ...executionTimeline[existingIndex],
        ...entry,
        metadata: {
          ...executionTimeline[existingIndex].metadata,
          ...entry.metadata,
        },
      }
      return { executionTimeline }
    }),
  clearExecutionTimeline: () => set({ executionTimeline: [] }),

  // 中心编排
  orchestrationTasks: [],
  upsertOrchestrationTask: (task) =>
    set((state) => ({
      orchestrationTasks: (() => {
        const key = `${task.plan_id}:${task.task_id}`
        const index = state.orchestrationTasks.findIndex((entry) => `${entry.plan_id}:${entry.task_id}` === key)
        if (index < 0) return [...state.orchestrationTasks, task]
        const existing = state.orchestrationTasks[index]
        if (existing.session_seq != null && task.session_seq != null && task.session_seq < existing.session_seq) return state.orchestrationTasks
        const next = [...state.orchestrationTasks]
        const definedTask = Object.fromEntries(Object.entries(task).filter(([, value]) => value !== undefined)) as Partial<OrchestrationTask>
        next[index] = {
          ...existing,
          ...definedTask,
          title: task.title.trim() ? task.title : existing.title,
          metadata: { ...existing.metadata, ...task.metadata },
        }
        return next
      })(),
    })),
  clearOrchestrationTasks: () => set({ orchestrationTasks: [] }),
  orchestrationPlans: [],
  upsertOrchestrationPlan: (plan) =>
    set((state) => {
      const index = state.orchestrationPlans.findIndex((entry) => entry.plan_id === plan.plan_id)
      if (index < 0) return { orchestrationPlans: [...state.orchestrationPlans, plan] }
      const existing = state.orchestrationPlans[index]
      if (existing.session_seq != null && plan.session_seq != null && plan.session_seq < existing.session_seq) return {}
      const next = [...state.orchestrationPlans]
      next[index] = { ...existing, ...Object.fromEntries(Object.entries(plan).filter(([, value]) => value !== undefined)), metadata: { ...existing.metadata, ...plan.metadata } }
      return { orchestrationPlans: next }
    }),
  clearOrchestrationPlans: () => set({ orchestrationPlans: [] }),

  // 错误
  error: null,
  setError: (error) => set({ error }),
  clearError: () => set({ error: null }),
  errorDetail: null,
  setErrorDetail: (errorDetail) => set({ errorDetail }),
  clearErrorDetail: () => set({ errorDetail: null }),

  attachments: [],
  setAttachments: (attachments) => set({ attachments }),
  upsertAttachment: (item) => set((state) => ({
    attachments: state.attachments.some((entry) => entry.id === item.id)
      ? state.attachments.map((entry) => entry.id === item.id ? item : entry)
      : [...state.attachments, item],
  })),
  removeAttachment: (id) => set((state) => ({
    attachments: state.attachments.filter((entry) => entry.id !== id),
  })),
}))
