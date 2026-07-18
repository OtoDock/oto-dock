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
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue>({
  user: null,
  loading: true,
  authConfig: null,
  login: () => {},
  logout: () => {},
  setUser: () => {},
  refreshUser: async () => {},
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

  // Re-fetch /auth/me and swap the snapshot in place. The user object is
  // otherwise loaded ONCE at app mount, so any mutation that changes the
  // CURRENT user's server-side profile must call this or the change is
  // invisible until a page reload — e.g. creating an agent assigns the
  // creator as its manager, and `user.agent_roles` drives the
  // Remote Machines settings tab and the per-agent role gates.
  // Keeps the existing snapshot on failure: never null out a logged-in
  // user over a transient refetch error.
  const refreshUser = useCallback(async () => {
    const u = await fetchCurrentUser()
    if (u) setUser(u)
  }, [])

  const logout = useCallback(() => {
    // Clear persisted drafts + sticky prefs so the next user on a shared
    // machine doesn't inherit them. Theme stays (display preference).
    try { useChatStore.persist.clearStorage() } catch { /* ignore */ }
    try { useAgentPrefsStore.persist.clearStorage() } catch { /* ignore */ }
    doLogout()
  }, [])

  return (
    <AuthContext.Provider value={{ user, loading, authConfig, login, logout, setUser, refreshUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
