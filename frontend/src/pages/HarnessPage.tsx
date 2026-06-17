import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getHarnessRules, updateHarnessRule, reloadHarness, type HarnessRule } from '../api/endpoints/admin'
import Table, { type Column } from '../components/shared/Table'
import Modal from '../components/shared/Modal'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'

export default function HarnessPage() {
  const { checkAuth } = useAuthStore()
  const [rules, setRules] = useState<HarnessRule[]>([])
  const [loading, setLoading] = useState(true)
  const [editingRule, setEditingRule] = useState<HarnessRule | null>(null)
  const [editConfig, setEditConfig] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [reloading, setReloading] = useState(false)

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetchRules = useCallback(async () => {
    try {
      setLoading(true)
      const res = await getHarnessRules()
      const items = res.data?.items ?? []
      // Sort by priority descending
      items.sort((a, b) => b.priority - a.priority)
      setRules(items)
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to load rules', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchRules() }, [fetchRules])

  const openEdit = (rule: HarnessRule) => {
    setEditingRule(rule)
    setEditConfig(JSON.stringify(rule.config_json, null, 2))
  }

  const handleSave = async () => {
    if (!editingRule) return
    setSubmitting(true)
    try {
      let config: Record<string, unknown> | undefined
      if (editConfig.trim()) {
        try {
          config = JSON.parse(editConfig)
        } catch {
          showToast('Invalid JSON in config', 'error')
          setSubmitting(false)
          return
        }
      }
      await updateHarnessRule(editingRule.rule_id, { config_json: config })
      showToast('Rule updated', 'success')
      setEditingRule(null)
      await fetchRules()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to update rule', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const handleToggle = async (rule: HarnessRule) => {
    try {
      await updateHarnessRule(rule.rule_id, { enabled: !rule.enabled })
      showToast(`Rule ${rule.enabled ? 'disabled' : 'enabled'}`, 'success')
      await fetchRules()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to toggle rule', 'error')
    }
  }

  const handleReload = async () => {
    setReloading(true)
    try {
      await reloadHarness()
      showToast('Harness rules reloaded', 'success')
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to reload', 'error')
    } finally {
      setReloading(false)
    }
  }

  const columns: Column<HarnessRule>[] = [
    { key: 'priority', header: 'Priority' },
    { key: 'rule_type', header: 'Rule Type' },
    {
      key: 'name',
      header: 'Name',
      render: (r) => r.name || r.rule_type,
    },
    {
      key: 'description',
      header: 'Description',
      render: (r) => (
        <span className="text-xs text-text-muted">{r.description || '—'}</span>
      ),
    },
    {
      key: 'enabled',
      header: 'Enabled',
      render: (r) => (
        <button
          onClick={() => handleToggle(r)}
          className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
            r.enabled ? 'bg-success' : 'bg-bg-elevated border border-border'
          }`}
        >
          <span
            className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
              r.enabled ? 'translate-x-[18px]' : 'translate-x-[2px]'
            }`}
          />
        </button>
      ),
    },
    { key: 'revision', header: 'Revision' },
    {
      key: 'actions',
      header: 'Actions',
      render: (r) => (
        <button
          onClick={() => openEdit(r)}
          className="text-xs px-2 py-1 rounded bg-bg-elevated text-text-secondary hover:text-text-primary transition-colors"
        >
          Edit
        </button>
      ),
    },
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">Harness Rules</h1>
          <p className="text-sm text-text-secondary mt-1">Security engine rules — ordered by priority</p>
        </div>
        <button
          onClick={handleReload}
          disabled={reloading}
          className="px-4 py-2 bg-bg-elevated border border-border text-text-secondary rounded-lg text-sm font-medium hover:bg-border transition-colors disabled:opacity-50"
        >
          {reloading ? 'Reloading...' : '↻ Reload Cache'}
        </button>
      </div>

      {rules.length === 0 && !loading ? (
        <EmptyState icon="🛡️" title="No Harness Rules" description="No security rules configured." />
      ) : (
        <Table
          columns={columns}
          data={rules}
          keyExtractor={(r) => r.rule_id}
          isLoading={loading}
          emptyMessage="No rules configured"
        />
      )}

      <Modal open={!!editingRule} title={`Edit Rule: ${editingRule?.rule_id}`} onClose={() => setEditingRule(null)} size="lg">
        <div className="space-y-4">
          {editingRule && (
            <div className="flex gap-4 text-sm">
              <span className="text-text-muted">Type: <strong className="text-text-primary">{editingRule.rule_type}</strong></span>
              <span className="text-text-muted">Priority: <strong className="text-text-primary">{editingRule.priority}</strong></span>
              <span className="text-text-muted">Revision: <strong className="text-text-primary">{editingRule.revision}</strong></span>
            </div>
          )}
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Config (JSON)</label>
            <textarea
              value={editConfig}
              onChange={(e) => setEditConfig(e.target.value)}
              rows={12}
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary font-mono focus:outline-none focus:border-accent"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditingRule(null)} className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-lg text-sm hover:bg-border transition-colors">
              Cancel
            </button>
            <button
              onClick={handleSave}
              disabled={submitting}
              className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors disabled:opacity-50"
            >
              {submitting ? 'Saving...' : 'Save Changes'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
