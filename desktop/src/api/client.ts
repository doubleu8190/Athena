/**
 * REST API 客户端 — 封装与后端 FastAPI 的 HTTP 通信.
 */
import type {
  CreateSessionResponse,
  Session,
  Message,
  Step,
  ApprovalRequest,
  ApprovalLog,
  ApprovalStats,
  MemoryEntry,
  MemoryListResponse,
  ProviderInfo,
  SettingsView,
  TestProviderResult,
  ToolListResponse,
  McpRegisterPayload,
  McpRegisterResponse,
  McpServerListResponse,
  Attachment,
  AttachmentUploadItem,
  FileTask,
} from "../types"

class ApiClient {
  private baseUrl: string = "http://127.0.0.1:8000"

  setBaseUrl(url: string): void {
    this.baseUrl = url.replace(/\/$/, "")
  }

  getBaseUrl(): string {
    return this.baseUrl
  }

  private async request<T>(
    path: string,
    options: RequestInit = {},
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const res = await fetch(url, {
      headers: {
        "Content-Type": "application/json",
        ...options.headers,
      },
      ...options,
    })
    if (!res.ok) {
      const errorText = await res.text().catch(() => res.statusText)
      throw new Error(`API ${res.status}: ${errorText}`)
    }
    if (res.status === 204) return undefined as T
    return res.json()
  }

  // ─── 健康检查 ─────────────────────────────────────────────────

  async healthCheck(): Promise<{ status: string; version: string }> {
    return this.request<{ status: string; version: string }>("/api/health")
  }

  // ─── 会话管理 ─────────────────────────────────────────────────

  async listSessions(): Promise<Session[]> {
    return this.request<Session[]>("/api/sessions")
  }

  async createSession(title: string = "New Session"): Promise<CreateSessionResponse> {
    return this.request<CreateSessionResponse>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ title }),
    })
  }

  async getSession(sessionId: string): Promise<Session> {
    return this.request<Session>(`/api/sessions/${sessionId}`)
  }

  async deleteSession(sessionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/sessions/${sessionId}`, {
      method: "DELETE",
    })
  }

  async getMessages(sessionId: string, limit?: number): Promise<Message[]> {
    const params = limit ? `?limit=${limit}` : ""
    return this.request<Message[]>(`/api/sessions/${sessionId}/messages${params}`)
  }

  async getSteps(sessionId: string): Promise<Step[]> {
    return this.request<Step[]>(`/api/sessions/${sessionId}/steps`)
  }

  async getToolCalls(sessionId: string, status?: string): Promise<unknown[]> {
    const params = status ? `?status=${status}` : ""
    return this.request<unknown[]>(`/api/sessions/${sessionId}/tool_calls${params}`)
  }

  async stopSession(sessionId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/sessions/${sessionId}/stop`, {
      method: "POST",
    })
  }

  async uploadAttachments(sessionId: string, files: File[]): Promise<AttachmentUploadItem[]> {
    const form = new FormData()
    files.forEach((file) => form.append("files", file))
    const response = await fetch(`${this.baseUrl}/api/sessions/${sessionId}/attachments`, {
      method: "POST",
      body: form,
    })
    if (!response.ok) throw new Error(`Upload failed: ${response.status} ${await response.text()}`)
    const data = await response.json() as { items: AttachmentUploadItem[] }
    return data.items
  }

  async listAttachments(sessionId: string): Promise<Attachment[]> {
    return this.request<Attachment[]>(`/api/sessions/${sessionId}/attachments`)
  }

  async deleteAttachment(sessionId: string, fileId: string): Promise<void> {
    await this.request(`/api/sessions/${sessionId}/attachments/${fileId}`, { method: "DELETE" })
  }

  async retryAttachment(sessionId: string, fileId: string): Promise<AttachmentUploadItem> {
    return this.request<AttachmentUploadItem>(`/api/sessions/${sessionId}/attachments/${fileId}/retry`, { method: "POST" })
  }

  async getFileTask(sessionId: string, taskId: string): Promise<FileTask> {
    return this.request<FileTask>(`/api/sessions/${sessionId}/file-tasks/${taskId}`)
  }

  // ─── 审批管理 ─────────────────────────────────────────────────

  async listPendingApprovals(sessionId?: string): Promise<ApprovalRequest[]> {
    const params = sessionId ? `?session_id=${sessionId}` : ""
    return this.request<ApprovalRequest[]>(`/api/approvals${params}`)
  }

  async respondApproval(
    approvalId: string,
    action: "allow" | "deny",
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      `/api/approvals/${approvalId}/respond`,
      {
        method: "POST",
        body: JSON.stringify({ action }),
      },
    )
  }

  async cancelApproval(approvalId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/approvals/${approvalId}/cancel`, {
      method: "POST",
    })
  }

  async getApprovalLogs(sessionId: string): Promise<unknown[]> {
    return this.request<unknown[]>(`/api/approvals/logs/${sessionId}`)
  }

  async updateSessionTitle(
    sessionId: string,
    title: string,
  ): Promise<Session> {
    return this.request<Session>(`/api/sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    })
  }

  // ─── 工具管理 ─────────────────────────────────────────────────

  async listTools(): Promise<ToolListResponse> {
    return this.request<ToolListResponse>("/api/tools")
  }

  async setToolEnabled(
    name: string,
    enabled: boolean,
  ): Promise<{ status: string; name: string; enabled: boolean }> {
    return this.request<{ status: string; name: string; enabled: boolean }>(
      `/api/tools/${encodeURIComponent(name)}`,
      { method: "PATCH", body: JSON.stringify({ enabled }) },
    )
  }

  async updateTool(
    name: string,
    config: {
      enabled?: boolean
      risk_level?: string
      require_approval?: boolean
    },
  ): Promise<{
    status: string
    name: string
    enabled: boolean
    risk_level: string
    require_approval: boolean
  }> {
    return this.request(
      `/api/tools/${encodeURIComponent(name)}`,
      { method: "PATCH", body: JSON.stringify(config) },
    )
  }

  // ─── 记忆管理 ─────────────────────────────────────────────────

  async listMemories(opts?: {
    limit?: number
    offset?: number
    pinned?: boolean
    expired?: boolean
    session_id?: string
  }): Promise<MemoryListResponse> {
    const params = new URLSearchParams()
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    if (opts?.pinned) params.set("pinned", "true")
    if (opts?.expired) params.set("expired", "true")
    if (opts?.session_id) params.set("session_id", opts.session_id)
    const qs = params.toString()
    return this.request<MemoryListResponse>(
      `/api/memory${qs ? `?${qs}` : ""}`,
    )
  }

  async getMemory(memoryId: string): Promise<MemoryEntry> {
    return this.request<MemoryEntry>(`/api/memory/${memoryId}`)
  }

  async deleteMemory(memoryId: string): Promise<{ status: string }> {
    return this.request<{ status: string }>(`/api/memory/${memoryId}`, {
      method: "DELETE",
    })
  }

  async setMemoryPinned(
    memoryId: string,
    pinned: boolean,
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      `/api/memory/${memoryId}/pin?pinned=${pinned}`,
      { method: "POST" },
    )
  }

  async updateMemory(
    memoryId: string,
    content: string,
  ): Promise<{ status: string; memory_id: string }> {
    return this.request<{ status: string; memory_id: string }>(
      `/api/memory/${memoryId}`,
      { method: "PATCH", body: JSON.stringify({ content }) },
    )
  }

  // ─── 审批日志 ─────────────────────────────────────────────────

  async listApprovalLogs(opts?: {
    session_id?: string
    limit?: number
    offset?: number
  }): Promise<ApprovalLog[]> {
    const params = new URLSearchParams()
    if (opts?.session_id) params.set("session_id", opts.session_id)
    if (opts?.limit != null) params.set("limit", String(opts.limit))
    if (opts?.offset != null) params.set("offset", String(opts.offset))
    const qs = params.toString()
    return this.request<ApprovalLog[]>(
      `/api/approvals/logs${qs ? `?${qs}` : ""}`,
    )
  }

  async getApprovalStats(): Promise<ApprovalStats> {
    return this.request<ApprovalStats>("/api/approvals/stats")
  }

  // ─── LLM 提供商 ───────────────────────────────────────────────

  async listProviders(): Promise<ProviderInfo[]> {
    return this.request<ProviderInfo[]>("/api/providers")
  }

  async testProvider(body: {
    provider: string
    model: string
    api_key?: string
    base_url?: string
  }): Promise<TestProviderResult> {
    return this.request<TestProviderResult>("/api/providers/test", {
      method: "POST",
      body: JSON.stringify(body),
    })
  }

  // ─── 系统设置 ─────────────────────────────────────────────────

  async getSettings(): Promise<SettingsView> {
    return this.request<SettingsView>("/api/settings")
  }

  // ─── MCP 服务器 ───────────────────────────────────────────────

  async listMcpServers(): Promise<McpServerListResponse> {
    return this.request<McpServerListResponse>("/api/mcp/servers")
  }

  async registerMcpServers(payload: McpRegisterPayload): Promise<McpRegisterResponse> {
    return this.request<McpRegisterResponse>("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(payload),
    })
  }

  async deleteMcpServer(name: string): Promise<{ status: string; name: string }> {
    return this.request<{ status: string; name: string }>(
      `/api/mcp/servers/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    )
  }
}

export const apiClient = new ApiClient()
