import assert from "node:assert/strict"
import test from "node:test"
import { apiClient } from "../src/api/client.ts"

test("run list requests server-side terminal filtering without dropping compatibility", async () => {
  const originalFetch = globalThis.fetch
  const requests: string[] = []
  globalThis.fetch = async (input) => {
    requests.push(String(input))
    return new Response(JSON.stringify({ items: [], total: 0, has_more: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
  }

  try {
    await apiClient.listRuns(50, 100, "session/a", true)
    await apiClient.listRuns(25, 0, "session/b")
  } finally {
    globalThis.fetch = originalFetch
  }

  const terminalUrl = new URL(requests[0]!, "http://localhost")
  assert.equal(terminalUrl.pathname, "/api/runs")
  assert.deepEqual(Object.fromEntries(terminalUrl.searchParams), {
    limit: "50",
    offset: "100",
    session_id: "session/a",
    terminal_only: "true",
  })

  const compatibleUrl = new URL(requests[1]!, "http://localhost")
  assert.equal(compatibleUrl.searchParams.get("session_id"), "session/b")
  assert.equal(compatibleUrl.searchParams.has("terminal_only"), false)
})
