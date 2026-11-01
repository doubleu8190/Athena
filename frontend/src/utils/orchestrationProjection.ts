export function summarizeResult(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim().slice(0, 220)
  if (!value || typeof value !== "object") return null
  const record = value as Record<string, unknown>
  const candidate = record.summary ?? record.result ?? record.message ?? record.output
  return typeof candidate === "string" && candidate.trim() ? candidate.trim().slice(0, 220) : null
}
