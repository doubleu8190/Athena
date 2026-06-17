import { useEffect, useState, useCallback } from 'react'
import { useAuthStore } from '../stores/authStore'
import { getSkills, installSkill, uninstallSkill, type Skill } from '../api/endpoints/admin'
import Modal from '../components/shared/Modal'
import StatusBadge from '../components/shared/StatusBadge'
import EmptyState from '../components/shared/EmptyState'
import { showToast } from '../components/shared/Toast'

export default function SkillsPage() {
  const { checkAuth } = useAuthStore()
  const [skills, setSkills] = useState<Skill[]>([])
  const [loading, setLoading] = useState(true)
  const [showModal, setShowModal] = useState(false)
  const [form, setForm] = useState({ name: '', version: 'latest', image_uri: '', allowed_domains: '' })
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetchSkills = useCallback(async () => {
    try {
      setLoading(true)
      const res = await getSkills()
      setSkills(res.data?.items ?? [])
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to load skills', 'error')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchSkills() }, [fetchSkills])

  const handleInstall = async () => {
    if (!form.name || !form.image_uri) {
      showToast('Name and Image URI are required', 'warning')
      return
    }
    setSubmitting(true)
    try {
      await installSkill({
        name: form.name,
        version: form.version,
        image_uri: form.image_uri,
        allowed_domains: form.allowed_domains || undefined,
      })
      showToast('Skill installation started', 'success')
      setShowModal(false)
      setForm({ name: '', version: 'latest', image_uri: '', allowed_domains: '' })
      await fetchSkills()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to install skill', 'error')
    } finally {
      setSubmitting(false)
    }
  }

  const handleUninstall = async (skillId: string) => {
    if (!window.confirm(`Uninstall skill "${skillId}"?`)) return
    try {
      await uninstallSkill(skillId)
      showToast('Skill uninstalled', 'success')
      await fetchSkills()
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to uninstall skill', 'error')
    }
  }

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-text-primary">Skills</h1>
          <p className="text-sm text-text-secondary mt-1">Manage installed Docker-based skills</p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors"
        >
          + Install Skill
        </button>
      </div>

      {skills.length === 0 && !loading ? (
        <EmptyState
          icon="🛠️"
          title="No Skills Installed"
          description="Install skills to add new capabilities to Athena."
          action={{ label: 'Install Skill', onClick: () => setShowModal(true) }}
        />
      ) : loading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[...Array(3)].map((_, i) => (
            <div key={i} className="animate-pulse bg-bg-surface border border-border rounded-xl p-5 h-40" />
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {skills.map((skill) => (
            <div key={skill.skill_id} className="bg-bg-surface border border-border rounded-xl p-5 hover:border-text-muted/30 transition-colors">
              <div className="flex items-start justify-between mb-3">
                <h3 className="font-semibold text-text-primary">{skill.name}</h3>
                <StatusBadge status={skill.status} />
              </div>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-text-muted">Version</span>
                  <span className="text-text-secondary">{skill.version}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-text-muted">Image</span>
                  <span className="text-text-secondary truncate max-w-[180px]" title={skill.image_uri}>
                    {skill.image_uri}
                  </span>
                </div>
                {skill.allowed_domains && (
                  <div className="flex justify-between">
                    <span className="text-text-muted">Domains</span>
                    <span className="text-text-secondary">{skill.allowed_domains}</span>
                  </div>
                )}
              </div>
              <button
                onClick={() => handleUninstall(skill.skill_id)}
                className="mt-4 w-full px-3 py-1.5 text-xs text-error border border-error/30 rounded-lg hover:bg-error/10 transition-colors"
              >
                Uninstall
              </button>
            </div>
          ))}
        </div>
      )}

      <Modal open={showModal} title="Install Skill" onClose={() => setShowModal(false)} size="md">
        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Name *</label>
            <input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
              placeholder="e.g. web-search"
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Version</label>
              <input
                value={form.version}
                onChange={(e) => setForm({ ...form, version: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-text-primary mb-1">Image URI *</label>
              <input
                value={form.image_uri}
                onChange={(e) => setForm({ ...form, image_uri: e.target.value })}
                className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
                placeholder="e.g. athena/skill-web-search:latest"
              />
            </div>
          </div>
          <div>
            <label className="block text-sm font-medium text-text-primary mb-1">Allowed Domains</label>
            <input
              value={form.allowed_domains}
              onChange={(e) => setForm({ ...form, allowed_domains: e.target.value })}
              className="w-full px-3 py-2 bg-bg border border-border rounded-lg text-sm text-text-primary focus:outline-none focus:border-accent"
              placeholder="e.g. api.example.com,*.internal.net"
            />
          </div>
          <div className="flex justify-end gap-3 pt-2">
            <button onClick={() => setShowModal(false)} className="px-4 py-2 bg-bg-elevated text-text-secondary rounded-lg text-sm hover:bg-border transition-colors">
              Cancel
            </button>
            <button
              onClick={handleInstall}
              disabled={submitting}
              className="px-4 py-2 bg-accent text-white rounded-lg text-sm font-medium hover:bg-accent-hover transition-colors disabled:opacity-50"
            >
              {submitting ? 'Installing...' : 'Install'}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  )
}
