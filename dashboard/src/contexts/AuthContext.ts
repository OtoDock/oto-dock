// The signed-in person as the dashboard knows them: the context object and
// `useAuth`. The provider that loads and refreshes it is AuthProvider.tsx
// (the tests mock this module's `useAuth` alone).

import { createContext, useContext } from 'react'
import type { User, AuthConfig } from '../api/auth'

interface AuthContextValue {
  user: User | null
  loading: boolean
  authConfig: AuthConfig | null
  /** Why the bypass-mode sign-in start was refused (the server's reason,
   * a 429's wait included); null while none was. */
  loginError: string | null
  login: () => void
  logout: () => void
  setUser: (u: User) => void
  refreshUser: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  authConfig: null,
  loginError: null,
  login: () => {},
  logout: () => {},
  setUser: () => {},
  refreshUser: async () => {},
})

export function useAuth() {
  return useContext(AuthContext)
}
