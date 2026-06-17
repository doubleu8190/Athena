import { create } from 'zustand'
import { getDashboard, type DashboardMetrics } from '../api/endpoints/admin'

interface DashboardState {
  metrics: DashboardMetrics | null
  isLoading: boolean
  error: string | null
  fetchMetrics: () => Promise<void>
}

export const useDashboardStore = create<DashboardState>((set) => ({
  metrics: null,
  isLoading: false,
  error: null,

  fetchMetrics: async () => {
    set({ isLoading: true, error: null })
    try {
      const res = await getDashboard()
      set({ metrics: res.data, isLoading: false })
    } catch (err) {
      set({ error: err instanceof Error ? err.message : 'Failed to load metrics', isLoading: false })
    }
  },
}))
