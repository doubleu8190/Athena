import { create } from 'zustand'

interface AuthState {
  apiKey: string | null
  isDevMode: boolean | null
  _hydrated: boolean
  setApiKey: (key: string) => void
  clearApiKey: () => void
  checkAuth: () => Promise<void>
}

export const useAuthStore = create<AuthState>((set, get) => ({
  apiKey: localStorage.getItem('athena_api_key'),
  isDevMode: null,
  _hydrated: false,

  setApiKey: (key: string) => {
    localStorage.setItem('athena_api_key', key)
    set({ apiKey: key, isDevMode: false })
  },

  clearApiKey: () => {
    localStorage.removeItem('athena_api_key')
    set({ apiKey: null })
  },

  checkAuth: async () => {
    const { _hydrated } = get()
    if (_hydrated) return

    const storedKey = localStorage.getItem('athena_api_key')
    if (storedKey) {
      set({ apiKey: storedKey, isDevMode: false, _hydrated: true })
      return
    }

    // Check if server is in dev mode (no API key required)
    try {
      const res = await fetch('/api/v1/health')
      if (res.ok) {
        // Server is accessible without API key — dev mode
        set({ isDevMode: true, _hydrated: true })
      } else {
        set({ _hydrated: true })
      }
    } catch {
      // Can't reach server — assume key required
      set({ _hydrated: true })
    }
  },
}))
