import assert from "node:assert/strict"
import test from "node:test"
import type { WorkerRegistryInstance } from "../src/types/index.ts"
import {
  registryIdentityMessage,
  registrySidebarState,
  registryState,
  registryStateLabel,
} from "../src/utils/workerRegistryProjection.ts"

function instance(overrides: Partial<WorkerRegistryInstance> = {}): WorkerRegistryInstance {
  return {
    instance_id: "instance-1",
    instance_key: "worker-1",
    service_principal: "worker-service",
    base_url: "https://worker.example",
    status: "ready",
    accepting_tasks: true,
    health_identity_valid: true,
    consecutive_health_failures: 0,
    consecutive_health_successes: 2,
    last_health_check_at: null,
    created_at: "2026-11-01T00:00:00Z",
    updated_at: "2026-11-01T00:00:00Z",
    contract_id: "writer",
    contract_revision: 1,
    execution_timeout_seconds: 30,
    max_concurrency: 2,
    current_concurrency: 0,
    available_concurrency: 2,
    can_do: ["write"],
    cannot_do: ["execute code"],
    input_schema: { type: "object" },
    output_schema: { type: "object" },
    ...overrides,
  }
}

test("registry state separates health, acceptance, and remaining capacity", () => {
  assert.equal(registryState(instance()), "available")
  assert.equal(registryState(instance({ accepting_tasks: false })), "paused")
  assert.equal(registryState(instance({ current_concurrency: 2, available_concurrency: 0 })), "full")
  assert.equal(registryState(instance({ status: "not_ready", health_identity_valid: false })), "not_ready")
  assert.equal(registryState(instance({ status: "disabled" })), "disabled")
  assert.equal(registryStateLabel("full"), "容量已满")
})

test("sidebar distinguishes initial failure from a loaded empty registry", () => {
  assert.equal(registrySidebarState(false, false, 0, 0), "load_failed")
  assert.equal(registrySidebarState(true, false, 0, 0), "loading")
  assert.equal(registrySidebarState(false, true, 0, 0), "empty")
  assert.equal(registrySidebarState(false, true, 2, 0), "no_matches")
})

test("identity copy does not treat a default or failed probe as verified", () => {
  const newlyRegistered = instance({
    status: "not_ready",
    accepting_tasks: false,
    health_identity_valid: true,
    last_health_check_at: null,
  })
  const healthProbeFailed = instance({
    status: "not_ready",
    accepting_tasks: false,
    health_identity_valid: true,
    consecutive_health_failures: 1,
    last_health_check_at: "2026-11-01T00:01:00Z",
  })

  assert.equal(registryState(newlyRegistered), "not_ready")
  assert.equal(registryIdentityMessage(newlyRegistered.health_identity_valid), "未发现身份不匹配")
  assert.equal(registryIdentityMessage(healthProbeFailed.health_identity_valid), "未发现身份不匹配")
  assert.equal(registryIdentityMessage(false), "身份不匹配")
})
