import { api } from '../client'

// ── Types ───────────────────────────────────────────────────────────────

export interface DashboardMetrics {
  tasks: Record<string, number>
  subtask_success_rate: number
  total_subtasks_24h: number
  harness_blocks_24h: number
}

export interface MCPServer {
  server_id: string
  name: string
  transport: string
  connection_config: Record<string, unknown>
  source: string
  enabled: boolean
  connection_status?: string  // 'connected' | 'disconnected' | 'connecting' — computed, not persisted
  created_at?: string
  updated_at?: string
}

export interface Skill {
  skill_id: string
  name: string
  version: string
  image_uri: string
  container_id?: string
  status: string
  allowed_domains?: string
  created_at?: string
}

export interface Device {
  device_id: string
  type: string
  connection_info?: Record<string, unknown>
  status: string
  last_heartbeat?: string
  created_at?: string
}

export interface HarnessRule {
  rule_id: string
  rule_type: string
  name?: string
  description?: string
  config_json: Record<string, unknown>
  priority: number
  enabled: boolean
  revision: number
  created_at?: string
  updated_at?: string
}

export interface AuditLog {
  event_id: string
  event_type: string
  actor_user_id?: string
  details_json?: Record<string, unknown>
  timestamp: string
}

export interface IMStatus {
  channels: Record<string, { configured: boolean; state?: string }>
}

// ── Dashboard ───────────────────────────────────────────────────────────

export function getDashboard() {
  return api.get<{ code: number; data: DashboardMetrics }>('/admin/dashboard')
}

// ── MCP Servers ─────────────────────────────────────────────────────────

export function getMCPServers() {
  return api.get<{ code: number; data: { items: MCPServer[] } }>('/admin/mcp-servers')
}

export function createMCPServer(body: {
  server_id: string
  name: string
  transport: string
  connection_config: Record<string, unknown>
  source?: string
}) {
  return api.post<{ code: number; data: MCPServer }>('/admin/mcp-servers', body)
}

export function deleteMCPServer(serverId: string) {
  return api.delete<{ code: number; data: null }>(`/admin/mcp-servers/${serverId}`)
}

export function updateMCPServerStatus(serverId: string, enabled: boolean) {
  return api.put<{ code: number; data: MCPServer }>(`/admin/mcp-servers/${serverId}/status`, { enabled })
}

// ── Skills ──────────────────────────────────────────────────────────────

export function getSkills() {
  return api.get<{ code: number; data: { items: Skill[] } }>('/admin/skills')
}

export function installSkill(body: {
  name: string
  version: string
  image_uri: string
  allowed_domains?: string
}) {
  return api.post<{ code: number; data: Skill }>('/admin/skills', body)
}

export function uninstallSkill(skillId: string) {
  return api.delete<{ code: number; data: null }>(`/admin/skills/${skillId}`)
}

// ── Devices ─────────────────────────────────────────────────────────────

export function getDevices() {
  return api.get<{ code: number; data: { items: Device[] } }>('/admin/devices')
}

export function registerDevice(body: {
  device_id: string
  type: string
  connection_info?: Record<string, unknown>
}) {
  return api.post<{ code: number; data: Device }>('/admin/devices', body)
}

export function deregisterDevice(deviceId: string) {
  return api.delete<{ code: number; data: null }>(`/admin/devices/${deviceId}`)
}

// ── Harness Rules ───────────────────────────────────────────────────────

export function getHarnessRules() {
  return api.get<{ code: number; data: { items: HarnessRule[] } }>('/admin/harness/rules')
}

export function updateHarnessRule(ruleId: string, body: {
  config_json?: Record<string, unknown>
  priority?: number
  enabled?: boolean
}) {
  return api.put<{ code: number; data: HarnessRule }>(`/admin/harness/rules/${ruleId}`, body)
}

export function reloadHarness() {
  return api.post<{ code: number; data: null }>('/admin/harness/reload')
}

// ── Audit Logs ──────────────────────────────────────────────────────────

export function getAuditLogs(params?: { event_type?: string; cursor?: string; limit?: number }) {
  return api.get<{ code: number; data: { items: AuditLog[]; next_cursor?: string } }>(
    '/admin/audit-logs',
    params as Record<string, string>
  )
}

// ── IM Status ───────────────────────────────────────────────────────────

export function getIMStatus() {
  return api.get<{ code: number; data: IMStatus }>('/admin/im/status')
}
