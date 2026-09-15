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

  // 中心编排
  orchestrationTasks: OrchestrationTask[]
  upsertOrchestrationTask: (task: OrchestrationTask) => void
  clearOrchestrationTasks: () => void

  // 错误
  error: string | null
  setError: (error: string | null) => void
  clearError: () => void

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
      if (saved && ["chat", "memory", "tools", "approvals", "providers", "session-detail", "settings", "mcp"].includes(saved)) {
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

  // 中心编排
  orchestrationTasks: [],
  upsertOrchestrationTask: (task) =>
    set((state) => ({
      orchestrationTasks: state.orchestrationTasks.some((entry) => entry.task_id === task.task_id)
        ? state.orchestrationTasks.map((entry) => entry.task_id === task.task_id ? task : entry)
        : [...state.orchestrationTasks, task],
    })),
  clearOrchestrationTasks: () => set({ orchestrationTasks: [] }),

  // 错误
  error: null,
  setError: (error) => set({ error }),
  clearError: () => set({ error: null }),

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
