import { create } from 'zustand'

/**
 * Placeholder auth store. Currently Athena runs in LAN mode without
 * real authentication. This store provides the shape for future use.
 */
interface AuthState {
  isAuthenticated: boolean
  user: { name: string; email: string } | null
  token: string | null
}

export const useAuthStore = create<AuthState>(() => ({
  isAuthenticated: true, // LAN mode: always authenticated
  user: { name: 'User', email: '' },
  token: null,
}))
