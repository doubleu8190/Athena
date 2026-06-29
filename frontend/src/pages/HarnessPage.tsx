import { useEffect, useState } from 'react'
import { RefreshCw, Edit3, Plus } from 'lucide-react'
import Table, { type Column } from '../components/ui/Table'
import { PriorityBadge } from '../components/ui/Badge'
import Modal from '../components/ui/Modal'
import { useToast } from '../components/ui/Toast'
import type { HarnessRule } from '../api/endpoints/admin'
import { fetchHarnessRules, createHarnessRule, updateHarnessRule, reloadHarnessCache } from '../api/endpoints/admin'

export default function HarnessPage() {
  const [rules, setRules] = useState<HarnessRule[]>([])
  const [loading, setLoading] = useState(true)
  const [editRule, setEditRule] = useState<HarnessRule | null>(null)
  const [editJson, setEditJson] = useState('')
  const [editPathPattern, setEditPathPattern] = useState('')
  const [editPermissions, setEditPermissions] = useState<'r' | 'w' | 'rw'>('rw')
  const [showCreate, setShowCreate] = useState(false)
  const [createForm, setCreateForm] = useState({
    rule_id: '',
    rule_type: 'blacklist',
    name: '',
    description: '',
    config_json: '{}',
    priority: 0,
    enabled: true,
  })
  const [createPathPattern, setCreatePathPattern] = useState('')
  const [createPermissions, setCreatePermissions] = useState<'r' | 'w' | 'rw'>('rw')
  const { toast } = useToast()

  const load = async () => {
    try {
      const data = await fetchHarnessRules()
      // sort by priority ascending (P1 first)
      setRules(data.sort((a, b) => a.priority - b.priority))
    } catch { toast('error', 'Failed to load harness rules') }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const handleToggle = async (rule: HarnessRule) => {
    try {
      await updateHarnessRule(rule.rule_id, { enabled: !rule.enabled })
      toast('success', rule.enabled ? 'Rule disabled' : 'Rule enabled')
      load()
    } catch { toast('error', 'Failed to update rule') }
  }

  const handleEdit = async () => {
    if (!editRule) return
    try {
      let config: Record<string, unknown>
      if (editRule.rule_type === 'path_permission') {
        config = { path: editPathPattern, permissions: editPermissions }
      } else {
        config = JSON.parse(editJson)
      }
      await updateHarnessRule(editRule.rule_id, { config_json: config })
      toast('success', 'Rule updated')
      setEditRule(null)
      load()
    } catch { toast('error', 'Invalid JSON or update failed') }
  }

  const handleCreate = async () => {
    try {
      let config: Record<string, unknown>
      if (createForm.rule_type === 'path_permission') {
        config = { path: createPathPattern, permissions: createPermissions }
      } else {
        config = JSON.parse(createForm.config_json)
      }
      await createHarnessRule({
        rule_id: createForm.rule_id,
        rule_type: createForm.rule_type,
        name: createForm.name,
        description: createForm.description || undefined,
        config_json: config,
        priority: createForm.priority,
        enabled: createForm.enabled,
      })
      toast('success', 'Rule created')
      setShowCreate(false)
      setCreateForm({ rule_id: '', rule_type: 'blacklist', name: '', description: '', config_json: '{}', priority: 0, enabled: true })
      setCreatePathPattern('')
      setCreatePermissions('rw')
      load()
    } catch { toast('error', 'Invalid JSON or create failed') }
  }

  const handleReload = async () => {
    try {
      await reloadHarnessCache()
      toast('success', 'Harness rules reloaded')
    } catch { toast('error', 'Failed to reload rules') }
  }

  const columns: Column<HarnessRule>[] = [
    { key: 'priority', header: 'Pri', render: (r) => <PriorityBadge priority={r.priority} /> },
    { key: 'rule_type', header: 'Type', render: (r) => <span className="text-xs text-gray-500">{r.rule_type}</span> },
    { key: 'name', header: 'Name', render: (r) => <span className="font-medium text-sm">{r.name}</span> },
    { key: 'description', header: 'Description', render: (r) => <span className="text-xs text-gray-500 truncate max-w-60 inline-block">{r.description}</span> },
    { key: 'config', header: 'Config', render: (r) =>
      r.rule_type === 'path_permission' ? (
        <span className="text-xs text-gray-500 font-mono">
          {(r.config.path as string) || '?'} [{(r.config.permissions as string) || 'rw'}]
        </span>
      ) : null
    },
    { key: 'enabled', header: '', render: (r) => (
      <button
        onClick={() => handleToggle(r)}
        className={`relative inline-flex items-center h-5 w-9 rounded-full transition-colors ${r.enabled ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-700'}`}
      >
        <span className={`inline-block w-4 h-4 rounded-full bg-white shadow transform transition-transform ${r.enabled ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
      </button>
    )},
    { key: 'actions', header: '', className: 'text-right', render: (r) => (
      <button
        onClick={() => {
          setEditRule(r)
          if (r.rule_type === 'path_permission') {
            setEditPathPattern((r.config.path as string) || '')
            setEditPermissions((r.config.permissions as 'r' | 'w' | 'rw') || 'rw')
          } else {
            setEditJson(JSON.stringify(r.config, null, 2))
          }
        }}
        className="p-1.5 rounded-md text-gray-400 hover:text-orange-500 hover:bg-orange-50 dark:hover:bg-orange-950 transition-colors"
        title="Edit"
      >
        <Edit3 size={14} />
      </button>
    )},
  ]

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Harness Rules</h2>
        <div className="flex items-center gap-2">
          <button onClick={() => setShowCreate(true)} className="px-4 py-2 bg-orange-500 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
            <Plus size={14} /> Create Rule
          </button>
          <button onClick={handleReload} className="px-4 py-2 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-gray-700 dark:text-gray-300 rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
            <RefreshCw size={14} /> Reload Cache
          </button>
        </div>
      </div>

      <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
        {loading ? (
          <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
        ) : (
          <Table columns={columns} data={rules} rowKey={(r) => r.rule_id} />
        )}
      </div>

      {/* Edit Rule Modal */}
      <Modal open={!!editRule} onClose={() => setEditRule(null)} title={editRule ? `Edit: ${editRule.name}` : ''}>
        <div className="space-y-3">
          {editRule?.rule_type === 'path_permission' ? (
            <>
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Path Pattern</label>
                <input
                  value={editPathPattern}
                  onChange={(e) => setEditPathPattern(e.target.value)}
                  placeholder="/temp/**"
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-mono"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Permissions</label>
                <div className="flex gap-4">
                  {(['r', 'w', 'rw'] as const).map((p) => (
                    <label key={p} className="flex items-center gap-1.5 cursor-pointer">
                      <input
                        type="radio"
                        name="editPermissions"
                        value={p}
                        checked={editPermissions === p}
                        onChange={() => setEditPermissions(p)}
                        className="text-orange-500 focus:ring-orange-500"
                      />
                      <span className="text-sm text-gray-700 dark:text-gray-300">
                        {p === 'r' ? 'Read (r)' : p === 'w' ? 'Write (w)' : 'Read + Write (rw)'}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Config (JSON)</label>
              <textarea
                value={editJson}
                onChange={(e) => setEditJson(e.target.value)}
                rows={12}
                className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-950 px-3 py-2 text-sm font-mono"
              />
            </div>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setEditRule(null)} className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors">Cancel</button>
            <button onClick={handleEdit} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors">Save</button>
          </div>
        </div>
      </Modal>

      {/* Create Rule Modal */}
      <Modal open={showCreate} onClose={() => setShowCreate(false)} title="Create Harness Rule">
        <div className="space-y-3">
          <InputField label="Rule ID" value={createForm.rule_id} onChange={(v) => setCreateForm({ ...createForm, rule_id: v })} />
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Rule Type</label>
            <select
              value={createForm.rule_type}
              onChange={(e) => setCreateForm({ ...createForm, rule_type: e.target.value })}
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm"
            >
              <option value="blacklist">blacklist</option>
              <option value="path_boundary">path_boundary</option>
              <option value="path_permission">path_permission</option>
              <option value="quota">quota</option>
              <option value="cooling_off">cooling_off</option>
            </select>
          </div>
          <InputField label="Name" value={createForm.name} onChange={(v) => setCreateForm({ ...createForm, name: v })} />
          <InputField label="Description" value={createForm.description} onChange={(v) => setCreateForm({ ...createForm, description: v })} />
          {createForm.rule_type === 'path_permission' ? (
            <>
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Path Pattern</label>
                <input
                  value={createPathPattern}
                  onChange={(e) => setCreatePathPattern(e.target.value)}
                  placeholder="/temp/**"
                  className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-mono"
                />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Permissions</label>
                <div className="flex gap-4">
                  {(['r', 'w', 'rw'] as const).map((p) => (
                    <label key={p} className="flex items-center gap-1.5 cursor-pointer">
                      <input
                        type="radio"
                        name="createPermissions"
                        value={p}
                        checked={createPermissions === p}
                        onChange={() => setCreatePermissions(p)}
                        className="text-orange-500 focus:ring-orange-500"
                      />
                      <span className="text-sm text-gray-700 dark:text-gray-300">
                        {p === 'r' ? 'Read (r)' : p === 'w' ? 'Write (w)' : 'Read + Write (rw)'}
                      </span>
                    </label>
                  ))}
                </div>
              </div>
            </>
          ) : (
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Config (JSON)</label>
              <textarea
                value={createForm.config_json}
                onChange={(e) => setCreateForm({ ...createForm, config_json: e.target.value })}
                rows={8}
                className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-gray-50 dark:bg-gray-950 px-3 py-2 text-sm font-mono"
              />
            </div>
          )}
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Priority</label>
            <input
              type="number"
              value={createForm.priority}
              onChange={(e) => setCreateForm({ ...createForm, priority: parseInt(e.target.value) || 0 })}
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm"
            />
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="checkbox"
              checked={createForm.enabled}
              onChange={(e) => setCreateForm({ ...createForm, enabled: e.target.checked })}
              className="rounded border-gray-300 dark:border-gray-700 text-orange-500 focus:ring-orange-500"
            />
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Enabled</span>
          </label>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowCreate(false)} className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors">Cancel</button>
            <button onClick={handleCreate} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors">Create</button>
          </div>
        </div>
      </Modal>
    </div>
  )
}

function InputField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{label}</label>
      <input value={value} onChange={(e) => onChange(e.target.value)} className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm" />
    </div>
  )
}
