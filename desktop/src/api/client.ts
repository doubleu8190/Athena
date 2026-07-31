/**
 * REST API 客户端 — 封装与后端 FastAPI 的 HTTP 通信.
 */
import type {
  CreateSessionResponse,
  Session,
  Message,
  ApprovalRequest,
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

  async getSteps(sessionId: string): Promise<unknown[]> {
    return this.request<unknown[]>(`/api/sessions/${sessionId}/steps`)
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
}

export const apiClient = new ApiClient()
