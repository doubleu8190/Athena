import { useEffect, useState } from 'react'
import { Plus, Trash2 } from 'lucide-react'
import Badge from '../components/ui/Badge'
import Modal from '../components/ui/Modal'
import ConfirmDialog from '../components/ui/ConfirmDialog'
import { useToast } from '../components/ui/Toast'
import type { Skill } from '../api/endpoints/admin'
import { fetchSkills, installSkill, uninstallSkill } from '../api/endpoints/admin'

export default function SkillsPage() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [form, setForm] = useState({ name: '', version: '', image_uri: '', allowed_domains: '' })
  const { toast } = useToast()

  const load = async () => {
    try {
      const data = await fetchSkills()
      setSkills(data)
    } catch { toast('error', 'Failed to load skills') }
    finally { setLoading(false) }
  }

  useEffect(() => { load() }, [])

  const handleInstall = async () => {
    try {
      await installSkill({
        name: form.name,
        version: form.version,
        image_uri: form.image_uri,
        allowed_domains: form.allowed_domains || undefined,
      })
      toast('success', 'Skill installed')
      setShowForm(false)
      load()
    } catch { toast('error', 'Failed to install skill') }
  }

  const handleUninstall = async () => {
    if (!deleteId) return
    try {
      await uninstallSkill(deleteId)
      toast('success', 'Skill uninstalled')
      load()
    } catch { toast('error', 'Failed to uninstall skill') }
    finally { setDeleteId(null) }
  }

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Skills</h2>
        <button onClick={() => setShowForm(true)} className="px-4 py-2 bg-orange-500 hover:bg-orange-600 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-1.5">
          <Plus size={14} /> Install Skill
        </button>
      </div>

      {loading ? (
        <div className="py-12 text-center text-sm text-gray-400">Loading...</div>
      ) : skills.length === 0 ? (
        <div className="py-12 text-center text-sm text-gray-400">No skills installed</div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {skills.map((skill) => (
            <div key={skill.skill_id} className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 p-5">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <h3 className="font-semibold text-base text-gray-900 dark:text-gray-100">{skill.name}</h3>
                  <p className="text-xs text-gray-400 mt-0.5">v{skill.version}</p>
                </div>
                <Badge variant={skill.status === 'installed' ? 'green' : 'gray'}>{skill.status}</Badge>
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 space-y-1">
                <div>Image: <code className="text-xs bg-gray-100 dark:bg-gray-800 px-1 rounded">{skill.image_uri}</code></div>
                <div>Domains: {skill.allowed_domains || '—'}</div>
              </div>
              <button onClick={() => setDeleteId(skill.skill_id)} className="mt-3 text-xs text-red-500 hover:text-red-700 font-medium flex items-center gap-1">
                <Trash2 size={12} /> Uninstall
              </button>
            </div>
          ))}
        </div>
      )}

      <Modal open={showForm} onClose={() => setShowForm(false)} title="Install Skill">
        <div className="space-y-3">
          <InputField label="Name" value={form.name} onChange={(v) => setForm({ ...form, name: v })} />
          <InputField label="Version" value={form.version} onChange={(v) => setForm({ ...form, version: v })} />
          <InputField label="Image URI" value={form.image_uri} onChange={(v) => setForm({ ...form, image_uri: v })} />
          <InputField label="Allowed Domains (comma-separated)" value={form.allowed_domains} onChange={(v) => setForm({ ...form, allowed_domains: v })} />
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowForm(false)} className="px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors">Cancel</button>
            <button onClick={handleInstall} className="px-4 py-2 text-sm font-medium text-white bg-orange-500 hover:bg-orange-600 rounded-lg transition-colors">Install</button>
          </div>
        </div>
      </Modal>

      <ConfirmDialog open={!!deleteId} onClose={() => setDeleteId(null)} onConfirm={handleUninstall} title="Uninstall Skill" message="Are you sure you want to uninstall this skill?" confirmLabel="Uninstall" variant="danger" />
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
