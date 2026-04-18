import { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from 'react'
import { type User, type AuthConfig, fetchCurrentUser, fetchAuthConfig, startLogin, logout as doLogout } from '../api/auth'
import { useChatStore } from '../store/chatStore'
import { useAgentPrefsStore, migrateAgentPrefsToUser } from '../store/agentPrefsStore'
import { migrateAudioPrefsToUser } from '../store/audioPrefsStore'

interface AuthContextValue {
  user: User | null
  loading: boolean
  authConfig: AuthConfig | null
  login: () => void
  logout: () => void
  setUser: (u: User) => void
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  authConfig: null,
  login: () => {},
  logout: () => {},
  setUser: () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    // Load auth config and current user in parallel
    Promise.all([fetchAuthConfig(), fetchCurrentUser()])
      .then(([cfg, u]) => {
        setAuthConfig(cfg)
        setUser(u)
        // Per-user isolation for persisted localStorage stores: wipe drafts
        // + sticky prefs when the booting user differs from what was last
        // persisted on this machine (shared/family device case). agentPrefs
        // self-checks via its __user_sub field; chatStore's draft layer
        // has no user binding — we wipe on mismatch using the same signal.
        if (u?.sub) {
          const prevAgentSub = useAgentPrefsStore.getState().__user_sub
          if (prevAgentSub && prevAgentSub !== u.sub) {
            useChatStore.persist.clearStorage()
          }
          migrateAgentPrefsToUser(u.sub)
          migrateAudioPrefsToUser(u.sub)
        }
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  const login = useCallback(() => {
    startLogin().catch(console.error)
  }, [])

  const logout = useCallback(() => {
    // Clear persisted drafts + sticky prefs so the next user on a shared
    // machine doesn't inherit them. Theme stays (display preference).
    try { useChatStore.persist.clearStorage() } catch { /* ignore */ }
    try { useAgentPrefsStore.persist.clearStorage() } catch { /* ignore */ }
    doLogout()
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, authConfig, login, logout, setUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
