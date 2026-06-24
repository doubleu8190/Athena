import { useEffect, useCallback } from 'react'
import { useDashboardStore } from '../stores/dashboardStore'
import { useAuthStore } from '../stores/authStore'
import MetricCard from '../components/dashboard/MetricCard'
import SuccessGauge from '../components/dashboard/SuccessGauge'
import { useAutoRefresh } from '../hooks/useAutoRefresh'
import { CheckCircle, RefreshCw, Hourglass, XCircle, BarChart3, Shield, Server, Wrench, Smartphone, ClipboardList, MessageSquare } from 'lucide-react'

export default function DashboardPage() {
  const { metrics, isLoading, error, fetchMetrics } = useDashboardStore()
  const { checkAuth } = useAuthStore()

  useEffect(() => { checkAuth() }, [checkAuth])

  const fetch = useCallback(() => { fetchMetrics() }, [fetchMetrics])
  useAutoRefresh(fetch, 30000)

  const getTaskCount = (status: string) => metrics?.tasks?.[status] ?? 0

  return (
    <div className="p-8 max-w-6xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-text-primary">Dashboard</h1>
        <p className="text-sm text-text-secondary mt-1">System overview and real-time metrics</p>
      </div>

      {error && (
        <div className="mb-6 px-4 py-3 bg-error/5 border border-error/20 rounded-xl text-sm text-error">
          Failed to load metrics: {error}
        </div>
      )}

      {/* Task metrics */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <MetricCard
          title="Completed Tasks"
          value={getTaskCount('completed')}
          icon={<CheckCircle size={24} />}
          color="success"
        />
        <MetricCard
          title="Running Tasks"
          value={getTaskCount('running')}
          icon={<RefreshCw size={24} />}
          color="accent"
        />
        <MetricCard
          title="Pending Tasks"
          value={getTaskCount('pending')}
          icon={<Hourglass size={24} />}
          color="warning"
        />
        <MetricCard
          title="Failed Tasks"
          value={getTaskCount('failed')}
          icon={<XCircle size={24} />}
          color="error"
        />
      </div>

      {/* Subtask metrics */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
        <div className="bg-bg-surface shadow-soft rounded-2xl p-6 flex items-center justify-center">
          <SuccessGauge percentage={metrics?.subtask_success_rate ?? 0} />
        </div>
        <div className="space-y-4">
          <MetricCard
            title="Total Subtasks (24h)"
            value={metrics?.total_subtasks_24h ?? 0}
            subtitle="Last 24 hours"
            icon={<BarChart3 size={24} />}
          />
          <MetricCard
            title="Harness Blocks (24h)"
            value={metrics?.harness_blocks_24h ?? 0}
            subtitle="Security rule triggers"
            icon={<Shield size={24} />}
            color={metrics?.harness_blocks_24h ? 'warning' : 'default'}
          />
        </div>
      </div>

      {/* Quick links */}
      <div className="shadow-soft rounded-2xl bg-bg-surface p-6">
        <h2 className="text-sm font-semibold text-text-primary mb-4">Quick Actions</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          {[
            { label: 'MCP Servers', href: '/admin/mcp-servers', icon: Server },
            { label: 'Skills', href: '/admin/skills', icon: Wrench },
            { label: 'Devices', href: '/admin/devices', icon: Smartphone },
            { label: 'Harness Rules', href: '/admin/harness', icon: Shield },
            { label: 'Audit Logs', href: '/admin/audit', icon: ClipboardList },
            { label: 'Chat Console', href: '/console', icon: MessageSquare },
          ].map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="flex flex-col items-center gap-2 p-4 bg-bg shadow-soft rounded-xl hover:shadow-card-hover hover:-translate-y-0.5 transition-all text-center"
            >
              <link.icon size={28} className="text-text-secondary" />
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
