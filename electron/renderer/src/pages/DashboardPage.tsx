import { useNavigate } from 'react-router'
import { CheckCircle, Zap, Clock, XCircle, Shield } from 'lucide-react'
import MetricCard from '../components/dashboard/MetricCard'
import SuccessGauge from '../components/dashboard/SuccessGauge'
import { useDashboardStore } from '../stores/dashboardStore'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const quickLinks = [
  { to: '/admin/mcp-servers', emoji: '🔌', label: 'MCP Servers' },
  { to: '/admin/skills', emoji: '🧩', label: 'Skills' },
  { to: '/admin/devices', emoji: '📱', label: 'Devices' },
  { to: '/admin/harness', emoji: '🛡️', label: 'Harness' },
  { to: '/admin/audit', emoji: '📋', label: 'Audit Logs' },
]

export default function DashboardPage() {
  const metrics = useDashboardStore((s) => s.metrics)
  const fetchMetrics = useDashboardStore((s) => s.fetchMetrics)
  const isLoading = useDashboardStore((s) => s.isLoading)
  const navigate = useNavigate()

  useAutoRefresh(fetchMetrics, 30_000)

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Metric Cards */}
      <div className="grid grid-cols-4 gap-4">
        <MetricCard
          label="Completed"
          value={metrics?.completed ?? '—'}
          trend={metrics ? '+12% vs last week' : undefined}
          color="green"
          icon={<CheckCircle size={18} className="text-green-600 dark:text-green-400" />}
        />
        <MetricCard
          label="Running"
          value={metrics?.running ?? '—'}
          color="blue"
          icon={<Zap size={18} className="text-blue-600 dark:text-blue-400" />}
        />
        <MetricCard
          label="Pending"
          value={metrics?.pending ?? '—'}
          color="yellow"
          icon={<Clock size={18} className="text-amber-600 dark:text-amber-400" />}
        />
        <MetricCard
          label="Failed"
          value={metrics?.failed ?? '—'}
          color="red"
          icon={<XCircle size={18} className="text-red-600 dark:text-red-400" />}
        />
      </div>

      {/* Success Gauge + Quick Links */}
      <div className="grid grid-cols-3 gap-4">
        <SuccessGauge percentage={metrics?.success_rate_24h ?? 0} />
        <div className="bg-white dark:bg-gray-900 rounded-xl border border-gray-200 dark:border-gray-800 p-5 col-span-2">
          <div className="text-[10px] font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide mb-3">
            Quick Access
          </div>
          <div className="grid grid-cols-3 gap-3">
            {quickLinks.map((link) => (
              <button
                key={link.to}
                onClick={() => navigate(link.to)}
                className="p-3 rounded-lg border border-gray-200 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800 transition-colors text-center cursor-pointer"
              >
                <div className="text-xl mb-1">{link.emoji}</div>
                <div className="text-xs font-medium text-gray-700 dark:text-gray-300">
                  {link.label}
                </div>
              </button>
            ))}
          </div>

          {/* Harness Blocks */}
          {metrics && (
            <div className="mt-4 pt-4 border-t border-gray-100 dark:border-gray-800 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              <Shield size={14} className="text-gray-400" />
              Harness Blocks (24h):{' '}
              <span className="font-semibold text-gray-900 dark:text-gray-100">
                {metrics.harness_blocks_24h}
              </span>
            </div>
          )}
        </div>
      </div>

      {isLoading && (
        <div className="text-center text-xs text-gray-400 py-4 animate-pulse">
          Loading metrics...
        </div>
      )}
    </div>
  )
}
