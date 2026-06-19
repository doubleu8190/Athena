import { create } from 'zustand'

interface AuthState {
  _hydrated: boolean
  checkAuth: () => void
}

export const useAuthStore = create<AuthState>((set, get) => ({
  _hydrated: false,

  checkAuth: () => {
    if (get()._hydrated) return
    set({ _hydrated: true })
  },
}))
