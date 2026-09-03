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
import { apiFetch } from '../api/auth'

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

      setLastInteractive: (agent, mode) => {
        set((s) => ({ lastInteractive: { ...s.lastInteractive, [agent]: mode } }))
        // Roam the pick: debounced write-through to the server ui-prefs bag,
        // so a fresh device picks it up too. localStorage stays the offline
        // fallback (the PUT failing is fine).
        scheduleUiPrefsWrite()
      },

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

// ---------------------------------------------------------------------------
// Server roaming of the interactive pick (ui-prefs `last_execution_mode`)
// ---------------------------------------------------------------------------
// The sticky map above is zustand-persist localStorage — PER DEVICE. That was
// the 2026-08-06 incident: a phone with empty localStorage started a new chat
// headless despite the user's interactive preference. The server ui-prefs bag
// roams the map: hydrated over the local one ONCE per session load (below),
// written through (debounced) by setLastInteractive. Hydration writes state
// directly — never through setLastInteractive — so it can never loop into a
// PUT of the value it just pulled.

const UI_PREFS_PUT_DEBOUNCE_MS = 1000
let uiPrefsPutTimer: ReturnType<typeof setTimeout> | null = null
let interactiveHydrated = false

function scheduleUiPrefsWrite() {
  if (uiPrefsPutTimer) clearTimeout(uiPrefsPutTimer)
  uiPrefsPutTimer = setTimeout(() => {
    uiPrefsPutTimer = null
    // Send the FULL map, not just the changed key: the server PUT shallow-
    // merges TOP-LEVEL keys (last_execution_mode replaces wholesale), so a
    // single-agent payload would wipe the other agents' roamed modes.
    const map = useAgentPrefsStore.getState().lastInteractive
    apiFetch('/v1/users/me/ui-prefs', {
      method: 'PUT',
      body: JSON.stringify({ last_execution_mode: map }),
    }).catch(() => { /* offline — localStorage still holds the pick */ })
  }, UI_PREFS_PUT_DEBOUNCE_MS)
}

// Merge the server map into the local one (server wins per agent key;
// local-only keys are kept). ONCE per session load: a later refetch must not
// clobber toggles the user made after boot. Only explicit modes are taken —
// the bag is free-form JSON and other values would poison seedExecMode.
export function hydrateLastInteractiveFromServer(serverMap: Record<string, unknown> | undefined) {
  if (interactiveHydrated) return
  interactiveHydrated = true
  const entries = Object.entries(serverMap ?? {}).filter(
    ([, m]) => m === 'interactive' || m === '-p',
  ) as Array<[string, string]>
  if (entries.length === 0) return
  useAgentPrefsStore.setState((s) => ({
    lastInteractive: { ...s.lastInteractive, ...Object.fromEntries(entries) },
  }))
}

// Test-only: re-arm the once-per-session hydration + drop a pending PUT.
export function __resetUiPrefsRoamingForTests() {
  interactiveHydrated = false
  if (uiPrefsPutTimer) clearTimeout(uiPrefsPutTimer)
  uiPrefsPutTimer = null
}
