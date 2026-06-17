import { useEffect, useCallback } from 'react'
import { useDashboardStore } from '../stores/dashboardStore'
import { useAuthStore } from '../stores/authStore'
import MetricCard from '../components/dashboard/MetricCard'
import SuccessGauge from '../components/dashboard/SuccessGauge'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

export default function DashboardPage() {
  const { metrics, isLoading, error, fetchMetrics } = useDashboardStore()
  const { checkAuth } = useAuthStore()

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetch = useCallback(() => { fetchMetrics() }, [fetchMetrics])
  useAutoRefresh(fetch, 30000)

  const getTaskCount = (status: string) => metrics?.tasks?.[status] ?? 0

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-text-primary">Dashboard</h1>
        <p className="text-sm text-text-secondary mt-1">System overview and real-time metrics</p>
      </div>

      {error && (
        <div className="mb-6 px-4 py-3 bg-error/10 border border-error/30 rounded-lg text-sm text-error">
          Failed to load metrics: {error}
        </div>
      )}

      {/* Task metrics */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <MetricCard
          title="Completed Tasks"
          value={getTaskCount('completed')}
          icon="✅"
          color="success"
        />
        <MetricCard
          title="Running Tasks"
          value={getTaskCount('running')}
          icon="🔄"
          color="accent"
        />
        <MetricCard
          title="Pending Tasks"
          value={getTaskCount('pending')}
          icon="⏳"
          color="warning"
        />
        <MetricCard
          title="Failed Tasks"
          value={getTaskCount('failed')}
          icon="❌"
          color="error"
        />
      </div>

      {/* Subtask metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        <div className="bg-bg-surface border border-border rounded-xl p-6 flex items-center justify-center">
          <SuccessGauge percentage={metrics?.subtask_success_rate ?? 0} />
        </div>
        <div className="space-y-4">
          <MetricCard
            title="Total Subtasks (24h)"
            value={metrics?.total_subtasks_24h ?? 0}
            subtitle="Last 24 hours"
            icon="📊"
          />
          <MetricCard
            title="Harness Blocks (24h)"
            value={metrics?.harness_blocks_24h ?? 0}
            subtitle="Security rule triggers"
            icon="🛡️"
            color={metrics?.harness_blocks_24h ? 'warning' : 'default'}
          />
        </div>
      </div>

      {/* Quick links */}
      <div className="border border-border rounded-xl bg-bg-surface p-5">
        <h2 className="text-sm font-semibold text-text-primary mb-4">Quick Actions</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          {[
            { label: 'MCP Servers', href: '/admin/mcp-servers', icon: '🔌' },
            { label: 'Skills', href: '/admin/skills', icon: '🛠️' },
            { label: 'Devices', href: '/admin/devices', icon: '📱' },
            { label: 'Harness Rules', href: '/admin/harness', icon: '🛡️' },
            { label: 'Audit Logs', href: '/admin/audit', icon: '📋' },
            { label: 'Chat Console', href: '/console', icon: '💬' },
          ].map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="flex flex-col items-center gap-2 p-4 bg-bg rounded-lg border border-border-subtle hover:border-accent/30 hover:bg-bg-elevated transition-all text-center"
            >
              <span className="text-2xl">{link.icon}</span>
              <span className="text-xs text-text-secondary">{link.label}</span>
            </a>
          ))}
        </div>
      </div>

      {isLoading && !metrics && (
        <div className="text-center text-sm text-text-muted py-8">Loading metrics...</div>
      )}
    </div>
  )
}
