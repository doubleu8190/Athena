import type { ToolCall, ToolCallInvocation } from "../types"
import { truncate } from "./format"

export interface ToolDisplayData {
  id: string
  name: string
  arguments: Record<string, unknown>
  status?: ToolCall["status"]
  output?: unknown
  error?: string | null
  duration_ms?: number
  risk_level?: ToolCall["risk_level"]
}

export interface ToolSummary {
  title: string
  summary: string
  outputPreview: string | null
  parsedOutput: unknown
}

export function toolDisplayData(
  toolCall: ToolCall | ToolCallInvocation,
  outputOverride?: unknown,
  errorOverride?: string | null,
): ToolDisplayData {
  if ("tool_name" in toolCall) {
    return {
      id: toolCall.id,
      name: toolCall.tool_name,
      arguments: toolCall.arguments ?? {},
      status: toolCall.status,
      output: outputOverride ?? toolCall.output,
      error: errorOverride ?? toolCall.error,
      duration_ms: toolCall.duration_ms,
      risk_level: toolCall.risk_level,
    }
  }
  return {
    id: toolCall.id,
    name: toolCall.name,
    arguments: toolCall.args ?? {},
    status: "pending",
    output: outputOverride,
    error: errorOverride,
  }
}

export function summarizeToolCall(data: ToolDisplayData): ToolSummary {
  const parsedOutput = parseMaybeJson(data.output)
  const error = data.error?.trim()
  if (error) {
    return {
      title: data.name,
      summary: "Failed",
      outputPreview: null,
      parsedOutput,
    }
  }

  if (data.status === "running" || data.status === "pending") {
    return {
      title: data.name,
      summary: "Waiting for output",
      outputPreview: null,
      parsedOutput,
    }
  }

  if (parsedOutput == null || parsedOutput === "") {
    return {
      title: data.name,
      summary: Object.keys(data.arguments).length
        ? `${Object.keys(data.arguments).length} argument${Object.keys(data.arguments).length === 1 ? "" : "s"}`
        : "No output",
      outputPreview: null,
      parsedOutput,
    }
  }

  const fileSummary = summarizeFileOutput(parsedOutput)
  if (fileSummary) return { title: data.name, ...fileSummary, parsedOutput }

  const shellSummary = summarizeShellOutput(parsedOutput)
  if (shellSummary) return { title: data.name, ...shellSummary, parsedOutput }

  if (Array.isArray(parsedOutput)) {
    const preview = parsedOutput.slice(0, 3).map(compactValue).filter(Boolean).join("\n")
    return {
      title: data.name,
      summary: `${parsedOutput.length} item${parsedOutput.length === 1 ? "" : "s"}`,
      outputPreview: preview ? truncate(preview, 260) : null,
      parsedOutput,
    }
  }

  if (typeof parsedOutput === "object") {
    const record = parsedOutput as Record<string, unknown>
    const keys = Object.keys(record)
    return {
      title: data.name,
      summary: keys.length ? `Returned ${keys.slice(0, 4).join(", ")}${keys.length > 4 ? "..." : ""}` : "Returned object",
      outputPreview: compactObjectPreview(record),
      parsedOutput,
    }
  }

  const text = String(parsedOutput)
  return {
    title: data.name,
    summary: firstMeaningfulLine(text) ?? "Completed",
    outputPreview: truncate(text, 360),
    parsedOutput,
  }
}

export function parseMaybeJson(value: unknown): unknown {
  if (typeof value !== "string") return value
  const text = value.trim()
  if (!text) return text
  if (!["{", "["].includes(text[0])) return value
  try {
    return JSON.parse(text)
  } catch {
    return value
  }
}

function summarizeFileOutput(value: unknown): Pick<ToolSummary, "summary" | "outputPreview"> | null {
  const record = asRecord(value)
  const file = asRecord(record?.file) ?? record
  if (!file) return null

  const filename = stringField(file, "filename") ?? stringField(file, "name")
  const mime = stringField(file, "mime_type") ?? stringField(file, "mime")
  const status = stringField(file, "status")
  const size = numberField(file, "size_bytes")

  if (!filename && !mime && size == null && !status) return null

  const parts = [
    filename ?? "File",
    mime,
    size != null ? formatBytes(size) : null,
    status,
  ].filter(Boolean)

  const capabilities = Array.isArray(file.capabilities)
    ? file.capabilities.map(String).slice(0, 5).join(", ")
    : null

  return {
    summary: parts.join(" · "),
    outputPreview: capabilities ? `Capabilities: ${capabilities}` : null,
  }
}

function summarizeShellOutput(value: unknown): Pick<ToolSummary, "summary" | "outputPreview"> | null {
  const record = asRecord(value)
  if (!record) return null
  const stdout = stringField(record, "stdout")
  const stderr = stringField(record, "stderr")
  const exitCode = numberField(record, "exit_code") ?? numberField(record, "returncode")
  if (stdout == null && stderr == null && exitCode == null) return null
  const summary = exitCode == null ? "Command completed" : `Exit code ${exitCode}`
  const preview = firstMeaningfulLine(stdout ?? "") ?? firstMeaningfulLine(stderr ?? "")
  return {
    summary,
    outputPreview: preview ? truncate(preview, 260) : null,
  }
}

function compactObjectPreview(record: Record<string, unknown>): string | null {
  const lines = Object.entries(record)
    .slice(0, 5)
    .map(([key, value]) => `${key}: ${compactValue(value)}`)
  return lines.length ? truncate(lines.join("\n"), 320) : null
}

function compactValue(value: unknown): string {
  if (value == null) return String(value)
  if (typeof value === "string") return truncate(value.replace(/\s+/g, " ").trim(), 140)
  if (typeof value === "number" || typeof value === "boolean") return String(value)
  if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`
  if (typeof value === "object") return `{${Object.keys(value as Record<string, unknown>).slice(0, 3).join(", ")}}`
  return String(value)
}

function firstMeaningfulLine(text: string): string | null {
  const line = text.split(/\r?\n/).map((item) => item.trim()).find(Boolean)
  return line ? truncate(line, 180) : null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function stringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key]
  return typeof value === "string" && value.trim() ? value.trim() : null
}

function numberField(record: Record<string, unknown>, key: string): number | null {
  const value = record[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}
