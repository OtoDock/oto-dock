// Per-agent sticky preferences (model + permission mode) — survives across
// chats. When the user picks a model/mode in chat A and then opens a NEW
// chat for the same agent, the dropdowns default to the sticky values.
// Existing chats keep their own DB-stored values; sticky only seeds new
// chats.
//
// Persisted to localStorage. Per-user isolation: an embedded __user_sub
// field is checked at boot; if it changes, the store is wiped so drafts
// from a previous user on the same machine don't leak.

import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'

interface AgentPrefsState {
  lastModel: Record<string, string>      // agent slug → model
  lastMode: Record<string, string>       // agent slug → permission mode
  // agent slug → explicit interactive override ('interactive' | '-p'); empty/
  // absent = follow the agent default. Seeds the interactive toggle on a NEW
  // chat the same way lastModel/lastMode seed the model/mode dropdowns.
  lastInteractive: Record<string, string>
  // Tracks which user's prefs are persisted; cleared on user mismatch at
  // boot. Empty until first authenticated load.
  __user_sub: string

  setLastModel: (agent: string, model: string) => void
  setLastMode: (agent: string, mode: string) => void
  setLastInteractive: (agent: string, mode: string) => void
  setUserSub: (userSub: string) => void
  reset: () => void
}

export const useAgentPrefsStore = create<AgentPrefsState>()(
  persist(
    (set) => ({
      lastModel: {},
      lastMode: {},
      lastInteractive: {},
      __user_sub: '',

      setLastModel: (agent, model) =>
        set((s) => ({ lastModel: { ...s.lastModel, [agent]: model } })),

      setLastMode: (agent, mode) =>
        set((s) => ({ lastMode: { ...s.lastMode, [agent]: mode } })),

      setLastInteractive: (agent, mode) =>
        set((s) => ({ lastInteractive: { ...s.lastInteractive, [agent]: mode } })),

      setUserSub: (userSub) => set({ __user_sub: userSub }),

      reset: () => set({ lastModel: {}, lastMode: {}, lastInteractive: {}, __user_sub: '' }),
    }),
    {
      name: 'oto-dock-agent-prefs',
      storage: createJSONStorage(() => localStorage),
    },
  ),
)

// Wipe persisted prefs if the user_sub at boot differs from what's stored.
// Call once after the user is resolved (AuthContext-level). Keeps a
// new user from inheriting the previous user's sticky model/mode on a
// shared machine.
export function migrateAgentPrefsToUser(userSub: string) {
  if (!userSub) return
  const state = useAgentPrefsStore.getState()
  if (state.__user_sub && state.__user_sub !== userSub) {
    useAgentPrefsStore.getState().reset()
  }
  useAgentPrefsStore.getState().setUserSub(userSub)
}
