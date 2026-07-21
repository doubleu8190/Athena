import { useEffect, useState } from 'react'
import { Plus, Trash2, Search, Edit3, Brain } from 'lucide-react'
import Table, { type Column } from '../components/ui/Table'
import Badge from '../components/ui/Badge'
import Modal from '../components/ui/Modal'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import { useToast } from '../components/ui/Toast'
import type { Memory, MemorySearchResult } from '../api/endpoints/admin'
import {
  fetchMemories,
  createMemory,
  updateMemory,
  deleteMemory,
  searchMemories,
} from '../api/endpoints/admin'

// ── Type & category badge helpers ──────────────────────────────────────

const typeVariant: Record<string, 'blue' | 'purple' | 'gray'> = {
  atomic_fact: 'blue',
  paragraph_summary: 'purple',
}

const categoryVariant: Record<string, 'green' | 'orange' | 'yellow' | 'gray'> = {
  preference: 'green',
  profile: 'green',
  project: 'orange',
  technical_decision: 'orange',
  technical_discussion: 'orange',
  problem_solving: 'yellow',
  planning: 'yellow',
}

function TypeBadge({ type }: { type: string }) {
  const label = type === 'atomic_fact' ? 'Fact' : type === 'paragraph_summary' ? 'Summary' : type
  return <Badge variant={typeVariant[type] ?? 'gray'}>{label}</Badge>
}

function CategoryBadge({ category }: { category: string }) {
  return <Badge variant={categoryVariant[category] ?? 'gray'}>{category}</Badge>
}

// ── Main page ──────────────────────────────────────────────────────────

export default function MemoriesPage() {
  const [memories, setMemories] = useState<Memory[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [editingMemory, setEditingMemory] = useState<Memory | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [form, setForm] = useState({ key: '', value: '', meta: '' })
  const { toast } = useToast()

  // Search state
  const [searchQuery, setSearchQuery] = useState('')
  const [searchType, setSearchType] = useState<'all' | 'atomic_fact' | 'paragraph_summary'>('all')
  const [searchResults, setSearchResults] = useState<MemorySearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)

  // ── Data loading ────────────────────────────────────────────────────

  const load = async () => {
    try {
      const data = await fetchMemories({ limit: 200 })
      setMemories(data)
    } catch {
      toast('error', 'Failed to load memories')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  // ── Create / Edit ───────────────────────────────────────────────────

  const openCreate = () => {
    setEditingMemory(null)
    setForm({ key: '', value: '', meta: '' })
    setShowForm(true)
  }

  const openEdit = (m: Memory) => {
    setEditingMemory(m)
    setForm({
      key: m.key,
      value: m.value,
      meta: m.meta && Object.keys(m.meta).length > 0 ? JSON.stringify(m.meta, null, 2) : '',
    })
    setShowForm(true)
  }

  const handleSave = async () => {
    if (!form.key.trim()) {
      toast('error', 'Key is required')
      return
    }
    if (!form.value.trim()) {
      toast('error', 'Value is required')
      return
    }

    let meta: Record<string, unknown> | undefined
    if (form.meta.trim()) {
      try {
        const parsed = JSON.parse(form.meta)
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          toast('error', 'Meta must be a JSON object')
          return
        }
        meta = parsed
      } catch {
        toast('error', 'Meta is not valid JSON')
        return
      }
    }

    try {
      if (editingMemory) {
        await updateMemory(editingMemory.memory_id, { value: form.value, meta })
        toast('success', 'Memory updated')
      } else {
        await createMemory({ key: form.key, value: form.value, meta })
        toast('success', 'Memory created')
      }
      setShowForm(false)
      setSearchResults(null)
      load()
    } catch {
      toast('error', editingMemory ? 'Failed to update memory' : 'Failed to create memory')
    }
  }

  // ── Delete ──────────────────────────────────────────────────────────

  const handleDelete = async () => {
    if (!deleteId) return
    try {
      await deleteMemory(deleteId)
      toast('success', 'Memory deleted')
      setSearchResults(null)
      load()
    } catch {
      toast('error', 'Failed to delete memory')
    } finally {
      setDeleteId(null)
    }
  }

  // ── Search ──────────────────────────────────────────────────────────

  const handleSearch = async () => {
    if (!searchQuery.trim()) {
      setSearchResults(null)
      return
    }
    setSearching(true)
    try {
      const results = await searchMemories({
        query: searchQuery,
        top_k: 20,
        type: searchType === 'all' ? undefined : searchType,
      })
      setSearchResults(results)
    } catch {
      toast('error', 'Search failed')
    } finally {
      setSearching(false)
    }
  }

  const clearSearch = () => {
    setSearchQuery('')
    setSearchResults(null)
  }

  // ── Table columns ───────────────────────────────────────────────────

  const displayData: (Memory | MemorySearchResult)[] = searchResults ?? memories

  const columns: Column<Memory | MemorySearchResult>[] = [
    {
      key: 'key',
      header: 'Key',
      render: (r) => (
        <span className="font-mono text-xs max-w-[200px] truncate block" title={r.key}>
          {r.key}
        </span>
      ),
    },
    {
      key: 'value',
      header: 'Value',
      render: (r) => (
        <span className="text-sm text-gray-700 dark:text-gray-300 max-w-[300px] truncate block" title={r.value}>
          {r.value}
        </span>
      ),
    },
    {
      key: 'type',
      header: 'Type',
      render: (r) => {
        const type = (r.meta as Record<string, unknown>)?.type as string
        return type ? <TypeBadge type={type} /> : <span className="text-gray-400 text-xs">—</span>
      },
    },
    {
      key: 'category',
      header: 'Category',
      render: (r) => {
        const category = (r.meta as Record<string, unknown>)?.category as string
        return category ? <CategoryBadge category={category} /> : <span className="text-gray-400 text-xs">—</span>
      },
    },
    ...(searchResults
      ? [{
          key: 'score',
          header: 'Score',
          render: (r: Memory | MemorySearchResult) => {
            const score = (r as MemorySearchResult).score
            if (score == null) return <span className="text-gray-400 text-xs">—</span>
            const pct = Math.round(score * 100)
            return (
              <span className={`text-xs font-mono ${pct >= 80 ? 'text-green-600 dark:text-green-400' : pct >= 60 ? 'text-yellow-600 dark:text-yellow-400' : 'text-gray-400'}`}>
                {pct}%
              </span>
            )
          },
        } as Column<Memory | MemorySearchResult>]
      : []),
    {
      key: 'updated_at',
      header: 'Updated',
      render: (r) => (
        <span className="text-xs text-gray-400">
          {r.updated_at ? new Date(r.updated_at).toLocaleDateString() : '—'}
        </span>
      ),
    },
    {
      key: 'actions',
      header: '',
      className: 'text-right',
      render: (r) => (
        <div className="flex items-center justify-end gap-1">
          <button
            onClick={() => openEdit(r)}
            className="p-1.5 rounded-md text-gray-400 hover:text-blue-500 hover:bg-blue-50 dark:hover:bg-blue-950 transition-colors"
            title="Edit"
          >
            <Edit3 size={14} />
          </button>
          <button
            onClick={() => setDeleteId(r.memory_id)}
            className="p-1.5 rounded-md text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950 transition-colors"
            title="Delete"
          >
            <Trash2 size={14} />
          </button>
        </div>
      ),
    },
  ]

  // ── Render ──────────────────────────────────────────────────────────

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Brain size={20} className="text-orange-500" />
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">User Memory</h2>
          <span className="text-xs text-gray-400 ml-1">
            {searchResults ? `${searchResults.length} results` : `${memories.length} items`}
          </span>
        </div>
        <button
          onClick={openCreate}
          className="px-4 py-2 bg-orange-500 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5"
        >
          <Plus size={14} /> Add Memory
        </button>
      </div>

      {/* Search bar */}
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            placeholder="Semantic search..."
            className="w-full pl-9 pr-3 py-2 rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 text-sm"
          />
        </div>
        <select
          value={searchType}
          onChange={(e) => setSearchType(e.target.value as typeof searchType)}
          className="rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm"
        >
          <option value="all">All types</option>
          <option value="atomic_fact">Facts</option>
          <option value="paragraph_summary">Summaries</option>
        </select>
        <button
          onClick={handleSearch}
          disabled={searching}
          className="px-4 py-2 bg-gray-100 dark:bg-gray-800 hover:bg-gray-200 dark:hover:bg-gray-700 text-sm font-medium rounded-lg transition-colors disabled:opacity-50"
        >
          {searching ? '...' : 'Search'}
        </button>
        {searchResults && (
          <button
            onClick={clearSearch}
            className="px-3 py-2 text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition-colors"
          >
            Clear
          </button>
        )}
      </div>

      {/* Table */}
      <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 overflow-hidden">
        {loading ? (
          <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
        ) : (
          <Table columns={columns} data={displayData} rowKey={(r) => r.memory_id} />
        )}
      </div>

      {/* Create / Edit Modal */}
      <Modal
        open={showForm}
        onClose={() => setShowForm(false)}
        title={editingMemory ? 'Edit Memory' : 'Add Memory'}
      >
        <div className="space-y-3">
          <InputField
            label="Key"
            value={form.key}
            onChange={(v) => setForm({ ...form, key: v })}
            disabled={!!editingMemory}
            placeholder="e.g. auto_preference_abc123"
          />
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">Value</label>
            <textarea
              value={form.value}
              onChange={(e) => setForm({ ...form, value: e.target.value })}
              rows={4}
              placeholder="Memory content..."
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
              Meta (JSON, optional)
            </label>
            <textarea
              value={form.meta}
              onChange={(e) => setForm({ ...form, meta: e.target.value })}
              rows={3}
              placeholder='{"type": "atomic_fact", "category": "preference"}'
              className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-mono"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button
              onClick={() => setShowForm(false)}
              className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleSave}
              className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors"
            >
              {editingMemory ? 'Update' : 'Create'}
            </button>
          </div>
        </div>
      </Modal>

      {/* Delete Confirm */}
      <ConfirmDialog
        open={!!deleteId}
        onClose={() => setDeleteId(null)}
        onConfirm={handleDelete}
        title="Delete Memory"
        message="Are you sure you want to delete this memory? This will remove it from both SQLite and ChromaDB."
        confirmLabel="Delete"
        variant="danger"
      />
    </div>
  )
}

// ── Input field helper ─────────────────────────────────────────────────

function InputField({
  label,
  value,
  onChange,
  disabled = false,
  placeholder = '',
}: {
  label: string
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  placeholder?: string
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">{label}</label>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        disabled={disabled}
        placeholder={placeholder}
        className="w-full rounded-lg border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-sm disabled:opacity-50 disabled:cursor-not-allowed"
      />
    </div>
  )
}
