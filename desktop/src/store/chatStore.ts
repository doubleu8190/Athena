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
  resolveApproval: (approvalId: string, decision: string) => void
  clearApprovals: () => void

  // Thinking 状态
  thinking: ThinkingState | null
  setThinking: (thinking: ThinkingState) => void
  clearThinking: () => void

  // 错误
  error: string | null
  setError: (error: string | null) => void
  clearError: () => void
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
      pendingApprovals: [...state.pendingApprovals, approval],
    })),
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

  // 错误
  error: null,
  setError: (error) => set({ error }),
  clearError: () => set({ error: null }),
}))
